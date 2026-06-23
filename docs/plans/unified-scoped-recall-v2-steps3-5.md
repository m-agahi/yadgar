# PLAN — Unified scoped recall v2: Steps 3–5 (redo)

Status: **PLANNED 2026-06-21.** Redo of the FAILED first attempt at yadgar
task #30 / T6 Steps 3–5. The first attempt is parked (broken) at branch
`feat/v6-t6-recall-345` (commits `7b4fa554` Step3, `56ecd393` Step4-5) in
worktree `.claude/worktrees/agent-aa3f01e785f26af58`.

theme: retrieval (architecture)
priority: high (usefulness centerpiece, blocked on a clean redo)

Parent design: [`docs/plans/unified-scoped-recall.md`](unified-scoped-recall.md).
Steps 0–2 already shipped on master @ v5.78 (providers, `_fanout_recall`,
`UNIFIED_RECALL_ENABLED` dormant flag, extended eval harness + golden set).
This plan covers ONLY Steps 3–5 plus a Step 0 prerequisite.

---

## 0. Post-mortem — what actually broke (empirically confirmed 2026-06-21)

The task brief framed two regressions: (1) a broken DB DirectoryFilter clause,
and (2) a "subtle dormant-path regression — Steps 4–5 regress legacy recall even
with the flag OFF." **The second framing did not survive reproduction.** What I
confirmed by running the suites against a live SurrealDB on the parked tip:

### Finding A — the canary does NOT reproduce a flag-OFF regression
`test_directory_scoping_v562.py::TestDirectoryScopingIntegration::test_directory_arg_changes_results`
**PASSES on the parked tip `56ecd393`** in every configuration tried:

| config | result |
|---|---|
| master, flag off (baseline) | 39/39 pass |
| parked tip, flag off, isolated | pass |
| parked tip, flag off, full file (xdist + random order) | 39/39 pass |
| parked tip, flag ON, isolated | pass |
| parked tip, flag ON, full file (real harness) | 39/39 pass |
| parked tip, flag off, canary + step4 + step5 combined, real harness | 69/69 pass |
| parked tip, flag off, **canary + `test_fanout_step2` (the flag-toggling test) combined, real harness** | canary passes (51 passed; only errors are unrelated collection of the mislabeled-e2e mocks of Finding C) |

The run with `test_fanout_step2` is the decisive control for the one plausible
"fails only in a combined run" mechanism: `settings = get_settings()` is a
module-level singleton in `recall.py`, so a sibling test that flips
`UNIFIED_RECALL_ENABLED` on that singleton and lands in the same xdist worker
before the canary would route it through fan-out. **It does not reproduce** —
because `test_fanout_step2` flips the flag via `monkeypatch.setattr`, which
auto-restores at teardown; the flag never leaks across tests, and no production
code mutates the singleton at runtime. (Note: a full-`yadgar/tests/` run was
attempted but the solo invocation timed out at 1800s before completing, and two
*parallel* full runs collided on the shared test-surreal reaper — the repo's own
Makefile warns against concurrent surreal-spawning runs; neither is evidence of a
canary regression. The targeted polluter run above settles the mechanism.) The
only legacy-path (flag-off) source change in the parked diff is the
SR-transition reorg in `recall.py` (moving `_bounded_set` inside an
`if _top_is_memory:` guard). That guard is always True when every result is a
memory — which is always the case on the legacy path — so it is **behaviorally
inert flag-off**. There is no flag-off byte-divergence that empties results.
**Conclusion: lesson #3 as originally stated ("flag-off path got changed") is
not supported. The real second regression is flag-ON and test-shaped (Finding C).**

### Finding B — the DB DirectoryFilter clause has a latent `IS NONE` field-absent bug
`storage/directory.py::_build_directory_clause` emits:

```sql
(directory_context IS NONE OR directory_context = '' OR directory_context = 'global'
 OR directory_context = $df_caller)
```

The `$df_caller` param **does** bind correctly against the test corpus — that is
why the canary passes flag-on. BUT: SurrealDB `IS NONE` matches **explicit-NULL
only, NOT field-absent rows** (rows written before the `DEFINE FIELD` that have
no `directory_context` key at all). This is a known SurrealDB footgun already
documented in this repo at `storage/migrations.py:555` and `:656` (migration 016
was rewritten in v5.42.6 to use a Python-side filter for exactly this reason).

The canary cannot catch it because its seeder, `_insert_mem`, **always stamps
`directory_context` explicitly** — so no field-absent rows ever exist in the test
DB. Against a real production corpus with legacy field-absent rows, the DB clause
silently drops them. This is the "DB clause looked fine under unit test, broke on
real data" failure the brief described — generalized: **the regression is not in
the SQL string, it is in the gap between the test row shape and the production
row shape.**

### Finding C — Steps 4–5 "tests" are broken mocks that ran in NO gate
`test_type_param_step5.py` and `test_fusion_step4.py` mock the fan-out with:

```python
patch("yadgar.server.tools.recall.MemoryProvider")
patch("yadgar.server.tools.recall.WikiProvider")
patch("yadgar.server.tools.recall.fuse_candidates")
```

But `MemoryProvider`, `WikiProvider`, and `fuse_candidates` are imported
**function-locally inside `_fanout_recall`** (`from ... import` with `noqa:
PLC0415`), so they are NOT module attributes of `recall`. Three of these tests
**FAIL the moment they execute** — regardless of the flag, because they set
`mock_settings.UNIFIED_RECALL_ENABLED = True` themselves and call
`_fanout_recall` directly — with:

```
AttributeError: <module 'yadgar.server.tools.recall'> does not have the attribute 'MemoryProvider'
```

(`TestTypeMemoryFilter::test_type_memory_only_calls_memory_provider`,
`::test_type_memory_excludes_wiki_results`,
`TestTypeWikiFilter::test_type_wiki_only_calls_wiki_provider`.) I confirmed they
fail both flag-off and flag-on when forced to run (`-o addopts=""`).

**Why the commit's "all 81 T6 tests pass" claim was vacuous — these 3 ran in NO
gate.** The 3 failing classes are decorated `@pytest.mark.e2e`. The repo's
default `addopts` (pyproject.toml:196) is
`-n 4 --dist loadgroup -m 'not integration and not e2e'`, so:
- `make test` / `make test-ci` **deselect** them by marker → never collected.
- `make e2e` runs **`yadgar/tests/e2e/` only** (Makefile:292) — but these tests
  live in top-level `yadgar/tests/`, a directory `make e2e` does not scan.

So they were excluded from the fast suite *and* invisible to the e2e suite —
the worst of both worlds: green CI, and a broken mock test that the e2e gate
which should have caught it never even collected. They assert on `MockMem.called`
— they never exercise `_fanout_recall`'s real provider selection at all. **This
is the canonical "mock tests gave false confidence" failure, compounded: the
mocks don't bind to the code under test, they are mislabeled `e2e` while being
pure mocks, and they sit in a directory no gate runs.**

### Why the mock tests missed everything
- **Step 3 DB clause:** mock-based unit tests for `_build_directory_clause`
  assert the SQL **string**, not its **execution** against rows of varying shape.
  A string assertion can never surface the `IS NONE`/field-absent mismatch.
- **Steps 4–5 fan-out routing:** the mocks patch attributes that don't exist on
  the module, so they test nothing. The first green run was a false positive
  produced by the default-off flag hiding the broken patch target.

### The non-negotiable lesson the redo encodes
**Every step gets a real e2e against a live SurrealDB, with a corpus that
mirrors production row shape (including field-absent `directory_context` rows),
authored BEFORE the implementation. Mock unit tests are supplementary, never the
gate. The fan-out path is exercised flag-ON.**

**Plus a hard placement + marker rule** (the mechanical lesson from Finding C):
- Every new e2e test file lives under **`yadgar/tests/e2e/`** — that is the ONLY
  directory `make e2e` scans (Makefile:292). A `@pytest.mark.e2e` test outside it
  runs in no gate.
- `@pytest.mark.e2e` is reserved for tests that hit a live `surreal` binary. Do
  NOT label a mock test `e2e` — that hides it from `make test-ci` (deselected by
  `-m 'not e2e'`) without giving it any real-DB coverage.
- A test that patches a name imported function-locally must patch where it is
  defined, or the import must be hoisted to module top-level so the patch target
  binds. Prefer real providers + live DB over patching either way.
- **Gate-reachability check:** before declaring any e2e written, run it via
  `make e2e` (not just `pytest -o addopts=""`) and confirm it is collected and
  executes. A test that no `make` target collects is not a gate.

---

## 1. Step 0 (prerequisite) — make the measurement real before measuring

The brief requires "measurement points on `make eval`" per step. **Today those
gates are vacuous.** Confirmed:

1. `benchmarks/run_eval.py::evaluate_pair` calls `retriever.recall(...)` — the
   `Retriever` object's method — **NOT** the MCP `recall` tool. It therefore
   never enters `_fanout_recall`, never touches fusion, never reads `type=`.
   `make eval` measures the legacy memory-only path **regardless of the flag.**
2. `run_eval.py` has no `--unified` / `--type` CLI flag; the flag is only
   reachable via the `YADGAR_UNIFIED_RECALL_ENABLED` env var, and even when set
   it has no effect on the harness because of (1).

**Step 0 deliverable (do FIRST, before Step 3 implementation):**

- **File:** `benchmarks/run_eval.py`.
- Route `evaluate_pair` through the fan-out path when measuring unified recall:
  call `yadgar.server.tools.recall.recall(query, directory=<eval_dir>, type=<...>, ...)`
  (the MCP tool), OR call `_fanout_recall` directly, so fusion + `type=` are
  exercised. Keep the legacy `retriever.recall` call selectable for the baseline.
- Add `--unified {on,off}` CLI flag; `--unified on` sets the flag for the run.
  (The `--type {all,memory,wiki}` flag arrives in **Step 5** — `recall(type=)`
  and `_fanout_recall(type_filter=)` do not exist until then. Steps 0/3/4 measure
  `--unified on` only, i.e. implicit `type=all`.)
- **e2e test FIRST:** `yadgar/tests/e2e/test_eval_routing_e2e.py::test_eval_routes_through_fanout_when_unified`
  — assert that with `--unified on`, a wiki-only golden pair (gold key
  `wiki:<slug>`) is retrievable (recall@10 > 0), which is impossible on the
  legacy `retriever.recall` path (it returns memories only). This test fails on
  master (proves the gap) and passes after Step 0.
  **Placement (apply the Finding-C gate-reachability rule to Step 0 itself):**
  the test lives in `yadgar/tests/e2e/`, NOT `benchmarks/` — no `make` target
  scans `benchmarks/`, so a test there runs in no gate. It needs a live DB to
  exercise the real fan-out, so `@pytest.mark.e2e` + the `e2e/` dir is correct;
  confirm `make e2e` collects it.
- **Measurement baseline — re-baselined, do not compare to any pre-Step-0
  figure.** Switching `evaluate_pair` from `retriever.recall()` to the MCP
  `recall` tool adds the directory post-filter, quality floor, content dedup,
  branch boost, and wiki blend that `retriever.recall()` skips. The new
  **flag-off, MCP-routed** `make eval` number is THE baseline; every later step
  compares against it. Old `retriever.recall()` numbers are not comparable.
- **Golden-set check:** confirm `benchmarks/golden/golden_set.jsonl` actually
  contains at least one pair with `relevant_wiki_slugs` (a wiki/mixed pair). The
  harness *supports* the field; verify the *data* has one. If none exists, Step 0
  must seed a wiki/mixed golden pair, else the Step-0 e2e (wiki retrievable) and
  all wiki-bucket measurements are vacuous.

Rationale: without Step 0 the brief's "measure on `make eval`" gates cannot
detect a fusion regression. Fix the instrument before trusting it.

---

## 2. The ScopeFilter refactor decision (#6) — LAND IT in Step 3

The brief asks whether to bundle `branch_filter` + `directory_filter` into one
`ScopeFilter` dataclass here or defer. **Decision: land it in Step 3.** Evidence:

- The parked branch added **3 new I30 complexity-allowlist entries** that exist
  **solely** because directory_filter is a separate param:
  `recall_via_pipeline` (params=9), `core.recall` (fn_loc=151),
  `_collect_vector_scores` (params=9). Bundling deletes all three entries
  instead of allowlisting more.
- `branch_filter` and `directory_filter` are the same kind of thing (DB-level
  WHERE-clause scope predicates), threaded through the identical call chain
  (`FTSParams` → `storage/*` → stages → providers). They are cohesive by
  definition — the I30 rationale's own words ("both filter params are parallel
  DB-level scoping concerns; bundling into a single filter dataclass is the right
  long-term refactor").
- Deferring means Step 3 ships 3 allowlist entries that Step 6 immediately
  reverts — churn for no benefit, and it normalizes the param-count creep.

### ScopeFilter design
- **New file:** `yadgar/storage/scope.py`.
  ```python
  @dataclass(frozen=True)
  class ScopeFilter:
      branch: BranchFilter | None = None
      directory: DirectoryFilter | None = None

      def build_clause(self) -> tuple[str, dict]:
          """Return (sql_fragment, params) combining both predicates with AND."""
  ```
  `build_clause()` composes `_build_branch_clause` + `_build_directory_clause`,
  ANDs the non-empty fragments, merges param dicts. Empty when both are None →
  `('', {})` (legacy no-op preserved exactly).
- Storage methods (`search_memories_fts_scored`, `search_vectors`,
  `get_memories_by_heat`, `search_by_content_date/month`, `search_wiki_fts_scored`,
  `search_wiki_vectors`) take **one** `scope: ScopeFilter | None = None` param
  instead of `branch_filter=` + `directory_filter=`. Net param count goes DOWN
  vs master (two existing branch_filter params collapse to one).
- `FTSParams` carries `scope: ScopeFilter | None` (replacing `branch_filter`).
- Backward-compat: keep `BranchFilter` / `DirectoryFilter` as the components;
  only the **threading** changes. `is_directory_eligible` (the legacy Python
  post-filter) is untouched — it stays the live mechanism on the flag-off path.

**Net I30 effect:** removes the 3 parked-branch allowlist entries; should add
zero new ones (verify with the complexity gate before commit). This is the
test-first unit Step 3 is built around.

---

## 3. Step 3 — DB-level scoping (ScopeFilter), live-DB tested

**Goal:** single DB-level enforcement point for directory+branch scope in the
fan-out path; flag-off path byte-identical to master.

### Files / functions
- New: `yadgar/storage/scope.py` (`ScopeFilter`, `build_clause`).
- `yadgar/storage/memory.py`, `storage/vector.py`, `storage/wiki.py`: methods
  take `scope: ScopeFilter | None`.
- `yadgar/wiki.py`: `WikiStore.query(..., scope=None)`, `_collect_wiki_*` pass
  scope through.
- `yadgar/retrieval/scoring.py` (`FTSParams`, `_collect_*_scores`),
  `retrieval/core.py` (`recall`, `recall_via_pipeline` gain `scope=`),
  `retrieval/state.py` (`scope` field), `retrieval/stages/{fts,knn,temporal}.py`
  (build `ScopeFilter` from `state`).
- `yadgar/retrieval/providers/memory.py`, `providers/wiki.py`: build `ScopeFilter`
  from `Scope` and pass it down.
- `storage/directory.py::_build_directory_clause`: **fix the `IS NONE`
  field-absent bug.** Two acceptable fixes — pick the one the e2e proves:
  - (a) widen the clause to also catch field-absent rows if SurrealQL allows
    (`directory_context = NONE OR type::is::none(directory_context) ...`), or
  - (b) accept that post-v5.65 production has no field-absent rows (migration 016
    backfilled them) and **assert that invariant with an e2e**, documenting that
    the clause is correct only because the corpus is backfilled. If the e2e finds
    field-absent rows survive, fall to (a).

### e2e tests — WRITE FIRST (all `@pytest.mark.e2e`, live `surreal` binary)
File: `yadgar/tests/e2e/test_scope_filter_e2e.py`

1. `test_db_clause_excludes_other_dir` — seed memory rows in YADGAR_DIR and
   AWS_DIR via raw `insert_memory` (real DB). Run a fan-out recall flag-ON,
   `directory=YADGAR_DIR`. Assert: AWS row absent, YADGAR row present, sentinel
   (`global`) row present. **Asserts the `$df_caller` param binds against real
   rows** — the thing the mock string-assertion could not.
2. `test_db_clause_includes_field_absent_or_proves_none_exist` — insert a row
   with `directory_context` **field-absent** (raw SurrealQL `CREATE memory SET
   content=..., heat=1.0` with no directory_context). Run fan-out recall. Assert
   the field-absent behavior the chosen fix (a/b) commits to. **This is the test
   the first attempt was missing — it mirrors production row shape.**
3. `test_branch_and_directory_compose` — seed rows differing on branch AND
   directory; assert `ScopeFilter(branch, directory)` ANDs correctly (a row
   matching dir but wrong branch is excluded, and vice-versa).
4. `test_scope_filter_none_is_legacy_noop` — `ScopeFilter()` (both None) →
   `build_clause() == ('', {})`; a fan-out recall with no scope returns the same
   set as the unscoped query.

Supplementary (mock/unit, NOT the gate):
`yadgar/tests/test_scope_filter_unit.py` — `build_clause` string + param-merge
assertions, empty-case, AND-composition. Fast feedback only.

### Parity gate (every commit)
`test_directory_scoping_v562.py` (full file, real DB, **real harness** — xdist +
random order, no `-o addopts=""`) MUST pass unchanged. This is the canary that
caught the original. Run it **flag-off AND flag-on**:
- flag-off: proves legacy byte-parity.
- flag-on: proves the fan-out DB path returns the same scoped set the legacy
  Python post-filter returns (the equivalence the first attempt never asserted
  flag-on against real rows).

### Measurement (`make eval`, via the Step-0-fixed harness)
- `make eval --unified off` == Step-0 baseline (no change; proves flag-off
  parity at the metric level).
- `make eval --unified on` (implicit type=all — `--type` lands in Step 5)
  recall@10 within noise of baseline (DB-level dir filter must not drop genuine
  in-scope memories).
- Record latency p50/p95 delta vs baseline (DB clause adds a WHERE term — must
  not regress p95 meaningfully).

### BC rows
- **New:** `BC-G2` "fan-out recall applies a single DB-level directory+branch
  scope filter; out-of-scope rows are excluded pre-fetch, not post-cropped."
  e2e: `test_scope_filter_e2e.py::test_db_clause_excludes_other_dir`. Mark
  `⏳[r]` → `✅` once e2e is green.

---

## 4. Step 4 — cross-type fusion, live-DB tested

**Goal:** memory + wiki candidates fused into one relevance-ranked list via the
cross-encoder; per-type quotas; provenance dedup. (Reuse parked
`providers/fusion.py::fuse_candidates` design — it is sound; the failure was the
tests, not the fusion logic.)

### Files / functions
- `yadgar/retrieval/providers/fusion.py`: `fuse_candidates`, `_score_candidates_ce`,
  `_apply_type_prior`, `_cross_type_dedup` (port from parked branch; review CE
  fallback path).
- `yadgar/server/tools/recall.py::_fanout_recall`: call `fuse_candidates`;
  **move the `MemoryProvider`/`WikiProvider`/`fuse_candidates` imports to module
  top-level** (or assert the patch target correctly) so e2e/mock targets bind —
  this directly fixes Finding C's AttributeError class of bug.
- Config (3-way sync, already on parked branch — port verbatim): `config.py`,
  `config_registry.py`, `config_yaml.py` for `RECALL_MEMORY_QUOTA`,
  `RECALL_WIKI_QUOTA`, `RECALL_MEMORY_PRIOR_WEIGHT`, `RECALL_WIKI_PRIOR_WEIGHT`.

### e2e tests — WRITE FIRST (`@pytest.mark.e2e`, live DB, flag-ON)
File: `yadgar/tests/e2e/test_fusion_e2e.py`

1. `test_fanout_returns_memory_and_wiki` — seed a memory AND a wiki page both
   answering the same query, in-scope. Flag-ON fan-out recall. Assert BOTH a
   `mem:<id>` and a `wiki:<slug>` appear in results. **Runs the real
   `_fanout_recall` + real providers + real DB** — the path the parked mocks
   never touched.
2. `test_relevant_wiki_outranks_irrelevant_hot_memory` — seed a high-heat memory
   irrelevant to the query and a low-heat-but-on-topic wiki page. Flag-ON.
   Assert the wiki ranks above the memory (CE relevance is the sort key, heat is
   only a prior). This is the fusion equalizer assertion.
3. `test_provenance_dedup_collapses_memory_into_wiki` — seed a memory and a wiki
   page whose `source_memory_ids` contains that memory id. Assert only one of
   them survives (the higher-CE one).
4. `test_quota_prevents_source_starvation` — seed 20 strong memories + 2 wikis;
   with `RECALL_WIKI_QUOTA=5` assert the 2 wikis are not starved out of the pool
   before rerank.
5. `test_ce_unavailable_falls_back_to_native_score` — force CE unavailable
   (retriever with no reranker / monkeypatch raise); assert fusion still returns
   a list ordered by native_score and does NOT crash. (Guards the fallback the
   parked code has but only mock-tested.)

Supplementary (unit, fast, NOT the gate): port `test_fusion_step4.py` BUT
**delete the broken module-attribute patches**; keep only the pure-function
tests of `_apply_type_prior` / `_cross_type_dedup` / quota slicing on synthetic
`Candidate` lists (no patching of import-local names).

### Parity gate
`test_directory_scoping_v562.py` full file, flag-off AND flag-on, real harness —
unchanged. (Fusion must not perturb the scoped set's membership, only ordering.)

### Measurement (`make eval`, Step-0 harness)
- `make eval --unified on` (type=all; `--type` lands in Step 5) on the
  (bootstrap) golden set: record recall@k / nDCG@k / MRR. Wiki gold keys should
  now be **non-zero** — impossible pre-Step-0/pre-fusion.
- On memory-only golden pairs, fusion must not degrade memory retrieval (adding
  wikis to the pool must not push the correct memory below k). Compare to the
  Step-0 baseline on the per-type "memory" bucket the harness aggregates.
- Latency p95 with CE rerank over the pooled set vs Step-3 baseline (rerank cost
  is the known risk — see parent plan's "consolidate light 5.7min" note).

### BC rows
- `BC-U1` recall(flag-on) returns both memory AND wiki, more-relevant first.
  e2e: `test_fusion_e2e.py::test_fanout_returns_memory_and_wiki`. `⏳[u]`→`✅`.
- `BC-U4` high-relevance wiki outranks high-heat-irrelevant memory.
  e2e: `test_fusion_e2e.py::test_relevant_wiki_outranks_irrelevant_hot_memory`.
- (Port BC-U1/U4 from parked branch but **re-point them at the e2e tests**, not
  the mock unit tests; flip `[u]`→`[r]`/`✅` only after e2e green.)

---

## 5. Step 5 — `type=` param + `wiki_query` deprecation, live-DB tested

**Goal:** `recall(type=all|memory|wiki)` selects the provider subset in the
fan-out path; `wiki_query` becomes a thin alias.

### Files / functions
- `yadgar/server/tools/recall.py`: `recall(..., type="all")`, early
  `ValueError` validation (port from parked — that part is correct), thread
  `type_filter` into `_fanout_recall`'s provider selection.
- `yadgar/server/tools/wiki.py::wiki_query`: deprecation INFO log (port);
  **decision: keep `wiki_query` fully functional this release** (one-cycle alias
  per parent plan) — do NOT migrate internal callers here (defer to #30 Step 6).
- SR-transition type-guard (T5): keep the `_top_is_memory = top.get("_source")
  != "wiki"` guard around `cognitive_map` recording — it is correct and
  type-safe. (Note: inert flag-off, so it cannot regress legacy; verified.)

### e2e tests — WRITE FIRST (`@pytest.mark.e2e`, live DB, flag-ON)
File: `yadgar/tests/e2e/test_type_param_e2e.py`

1. `test_type_memory_returns_only_memories` — seed memory + wiki both on topic,
   flag-ON, `type="memory"`. Assert results contain `mem:` keys and **zero**
   `wiki:` keys. **Real fan-out, no mocks** (replaces the broken
   `patch(...MemoryProvider)` test).
2. `test_type_wiki_returns_only_wiki` — same corpus, `type="wiki"`. Assert only
   `wiki:` keys, zero `mem:`.
3. `test_type_all_returns_both` — `type="all"`, assert both present.
4. `test_type_invalid_raises_before_retrieval` — `type="bogus"` raises
   `ValueError` (this one can stay a fast unit test — pure validation, no DB —
   but assert it raises *before* any provider is constructed).
5. `test_wiki_query_alias_equivalent_to_type_wiki` — flag-ON, assert
   `wiki_query(q, directory=d)` returns the same slug set as
   `recall(q, directory=d, type="wiki")` (alias equivalence) and that the
   deprecation INFO is logged once.

Supplementary unit: validation-only tests; **no module-attribute patching of
import-local provider names.**

### Parity gate
`test_directory_scoping_v562.py` full file, flag-off + flag-on, real harness —
unchanged. Plus: flag-off `recall(type=...)` is ignored (legacy path) — add an
assertion that `type=` has no effect when flag is off (documented behavior).

### Measurement (`make eval`, Step-0 harness)
- `make eval --unified on --type wiki` vs `--type memory` vs `--type all`:
  recall@k per type bucket (`run_eval.py` already aggregates "by query type" —
  confirmed at line 344). `type=wiki` recall on wiki-gold pairs must be > 0 and
  ≈ legacy `wiki_query` recall.
- Confirm `--type memory` MRR == Step-3 memory baseline (routing to memory-only
  must reproduce the legacy memory result set exactly through the fan-out path).

### BC rows
- `BC-U2` recall(type="memory") returns only memories. e2e:
  `test_type_param_e2e.py::test_type_memory_returns_only_memories`.
- `BC-U3` recall(type="wiki") returns only wiki. e2e:
  `test_type_param_e2e.py::test_type_wiki_returns_only_wiki`.
- `BC-U5` recall(type="invalid") raises ValueError before retrieval. unit:
  `test_type_param_e2e.py::test_type_invalid_raises_before_retrieval`.
- (Port BC-U2/U3/U5 from parked branch, re-point at e2e/real tests; flip to `✅`
  on green. Re-count BC surface in `BEHAVIOR_CONTRACT.md` header + run
  `scripts/check_contract_coverage.py`.)

---

## 6. Sequencing + flag strategy (master green + flag-off parity at every commit)

Branch off latest `master`: `feat/v6-t6-recall-345-v2` (fresh — do NOT reuse the
parked branch; per Check-Branch-State the parked branch is unmerged/abandoned).

Commit order (each commit independently green on `make test-ci` + parity gate):

1. **Step 0** — eval harness routes through fan-out + `--unified` flag + its e2e.
   (`--type` flag is added in Step 5 with the `type=` param. No production code
   path changes here; flag still off everywhere.)
2. **Step 3a** — `ScopeFilter` dataclass + unit + e2e (storage layer only,
   not yet threaded into recall). Removes 3 parked allowlist entries.
3. **Step 3b** — thread `ScopeFilter` through retrieval/providers; fix `IS NONE`
   bug per e2e finding. Parity gate flag-off + flag-on green.
4. **Step 4** — fusion + module-top-level provider imports + e2e. Parity green.
5. **Step 5** — `type=` param + `wiki_query` alias + e2e. Parity green.

**Flag invariant:** `UNIFIED_RECALL_ENABLED` stays **default False** through all
five commits. Every commit:
- runs `test_directory_scoping_v562.py` flag-OFF (byte-parity gate), and
- runs the new e2e flag-ON (fan-out path gate).

"flag-off = byte-parity with master" is a hard, tested invariant — the parity
gate is the mechanical proof, run flag-off on every commit.

**Default-flip is NOT in this plan.** Flipping `UNIFIED_RECALL_ENABLED` to True
is a separate, measured release gated on a **CURATED** golden set (the current
set is bootstrap/auto-drafted — `make eval` itself warns it is informational
only). Do not flip until: (a) golden set human-curated, (b) `make eval --unified
on` recall@k ≥ legacy baseline across all type buckets, (c) latency p95 within
budget. That is a future task, referenced here, out of scope.

---

## 7. Definition of done + pre-merge gate checklist

**Definition of done (Steps 0–5):**
- ScopeFilter landed; 3 parked I30 allowlist entries removed; no new I30 entries
  added (verify with the complexity gate).
- Fan-out path (flag-on) exercised end-to-end by live-DB e2e for scope, fusion,
  and `type=` — no mock stands in for the real path on any gate.
- `IS NONE` field-absent behavior committed to and proven by e2e against a corpus
  that includes a field-absent row.
- BC-G2, BC-U1..U5 all `✅` (e2e-backed), surface re-counted, coverage script green.
- `make eval` measures the fan-out path; baseline + per-step numbers recorded in
  the PR.
- `UNIFIED_RECALL_ENABLED` still default False; `test_directory_scoping_v562.py`
  passes flag-off (parity) AND flag-on (equivalence) on master HEAD.

**Pre-merge gate checklist:**
- [ ] `make test-ci` green (non-e2e suite, xdist + random order).
- [ ] `make e2e` green (all new `tests/e2e/*` + existing safety net).
- [ ] **Gate-reachability:** every new e2e is collected+run by `make e2e` (lives
      in `yadgar/tests/e2e/`; `@pytest.mark.e2e` only on real-DB tests). Confirm
      collection count rose by the number of e2e added — no test ships in a
      directory/marker combo that no gate runs (the Finding-C failure mode).
- [ ] `test_directory_scoping_v562.py` full file, **real harness**, flag-off AND
      flag-on — 39/39 unchanged.
- [ ] Lints/types/complexity gate green; `.complexity-allowlist.json` net-negative.
- [ ] `scripts/check_contract_coverage.py` green; BC surface re-counted in header.
- [ ] `make eval --unified on` produces non-zero wiki-bucket recall; numbers in PR body.
- [ ] Container verify: build the daemon image, start it, run a real
      `recall(directory=…, type=…)` against the containerized daemon flag-ON, and
      a `recall(directory=…)` flag-OFF; confirm both return scoped results
      (catches container-path issues the in-process tests can't — e.g. the
      `os.getcwd()` container mis-scope that motivated v5.65 Fix D).
- [ ] No `--no-verify`, no co-author trailer, branched off latest master.

---

## 8. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| **Recall hot path regression** (recall is the most-called tool) | High | flag-off byte-parity gate on every commit; `test_directory_scoping_v562.py` flag-off run is the mechanical proof; default stays False until a measured flip. |
| **`IS NONE` field-absent rows silently dropped on real corpus** (Finding B) | High | e2e seeds a field-absent row and asserts behavior; container verify against a real daemon; fix (a) clause-widen or (b) backfill-invariant chosen by the e2e, not by assumption. |
| **Mock tests give false confidence again** (Finding C) | High | live-DB e2e is the ONLY gate; mocks supplementary; module-top-level provider imports so patch targets bind; CI runs e2e flag-ON. |
| **CE rerank latency** over pooled memory+wiki set | Medium | per-type quotas bound the pool size; measure p95 on `make eval` each step; CE-unavailable fallback e2e-tested; reuse `profile=fast` skip path where hooks call recall. |
| **ScopeFilter refactor touches every storage query** (blast radius) | Medium | land as its own commit (Step 3a) with storage-level e2e before threading; net param count goes DOWN; `build_clause()` empty-case is the exact legacy no-op. |
| **Eval golden set is bootstrap, not curated** | Medium | per-step numbers are directional only; default-flip explicitly gated on a curated set; `make eval` already prints the bootstrap warning. |
| **Parked-branch behavioral drift** (porting good parts) | Low | port `fusion.py` logic + config 3-way sync verbatim (sound); rewrite ALL tests; do not reuse the broken mock tests or the parked branch itself. |

---

## 9. Deferred (NOT in this plan)

- Default-flip of `UNIFIED_RECALL_ENABLED` (separate measured release, curated
  golden set).
- Internal caller migration off `wiki_query` + alias removal (#30 Step 6).
- Consensus / landscape fusion mode, BC-AC3a, MMR diversity (#67 — `fusion.py`
  is designed pluggable for it but does not implement it here).

## 10. Related
- [`unified-scoped-recall.md`](unified-scoped-recall.md) — parent design.
- [`recall-scoping-restamp.md`](recall-scoping-restamp.md) — shipped the Python
  quick filter; this builds the DB-level filter it deferred.
- Parked (broken) attempt: branch `feat/v6-t6-recall-345`, commits `7b4fa554`,
  `56ecd393` — read for the fusion logic to port; do NOT reuse its tests.
- Code: `retrieval/providers/`, `retrieval/core.py`, `storage/{directory,branch}.py`,
  `server/tools/recall.py`, `benchmarks/run_eval.py`.
