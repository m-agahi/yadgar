# Branch-scoping removal — train plan

> Plan doc. Created 2026-08-07. Binding: **ADR-0215** (remove branch scoping entirely) and **ADR-0216** (gitness survives, loses its branch half).
> Amends: ADR-0123, ADR-0126, ADR-0158. Enumeration was done separately and is treated as given; spot-checks and corrections are marked **[CORRECTION]**.

## 0. Reading this doc

Every car is a checklist. `- [ ] path:line — symbol — action`. Line numbers were captured 2026-08-07 and **will rot**; where a car is a mechanical sweep, the entry gives the *regenerating command* instead of a line number. Run the command, tick off the count.

Standing per-car requirements (not repeated in each car):
- version bump (`scripts/check_version_bump.py`) + a `CHANGELOG.md` unreleased entry (`check_changelog_unreleased_versions.py`)
- if the car touches `yadgar/backend/**`, a backend version bump (`check_backend_bump.py`, ADR-0080)
- `pre-commit run --all-files` green at the car boundary
- ADR-0081/0082: the **first commit of the car that completes this plan** is `git mv docs/plans/branch-scoping-removal-2026-08-07.md docs/plans/archive/`

### Corrections to the enumeration (verified this session)

1. **Do not edit migrations 004 or 015.** Migration 026's own docstring establishes the convention: historical migrations are kept for history immutability; removal is a *new forward* migration. Column drop = **migration 029** (next free — 028 is the highest in the registry, 017 is RESERVED).
2. **`wiki_draft.branch` needs nothing.** Migration 026 does `REMOVE TABLE IF EXISTS wiki_draft` — the table is gone, so the column from migration 015 is orphaned with it. Confirmed, not assumed. No code references it. **Zero work.**
3. **`yadgar/tests/e2e/test_wiki_set_metadata_allrows.py` is NOT trivial** — it was not in DIES/MIXED, so the classification rule would send it to TRIVIAL. It is MIXED-shaped: it seeds rows with explicit `branch=` values (`branch="master"`, `branch="feat/x"`) and monkeypatches `_detect_branch`/`_get_default_branch` at lines 66-67, 123-124, 161-162. It is also in the `check_test_weakening` e2e scan set. Handle it explicitly in Car 6.
4. **`check_test_weakening.py` blocks three car boundaries.** Its scan set is `yadgar/tests/(e2e/*.py | **e2e*.py)`, netted **per file**. Deleting `test_v5_42_1_gate_verification_e2e.py`, `test_v5_42_2_branch_default_e2e.py`, `test_scope_filter_e2e.py` registers as a per-file net assert removal → the `invariant-checks` CI job (which carries `fetch-depth: 0` and runs `--ci --base origin/master`) goes red. Handled per-car below.
5. **BEHAVIOR_CONTRACT ✅ count is safe.** BC-G3 (§25 resolution) and BC-G8 / BC-T57 (`wiki_cleanup_merged_branches`) are `⏳`, not `✅` — removing their rows does not decrease the green count. **BC-G10 is `✅`** and must be *reworded, not deleted* (see Car 6).
6. **`skip_inventory.json` entry 11** (`v5-46-9-branch-fallback-ci-env`) points at `test_v5_46_9_branch_fallback_conditional.py`, a DIES file. Must be removed in the same car or `check_skip_inventory.py --validate-inventory` fails.
7. **LIMIT-1 non-determinism after the migration.** `get_wiki_page_by_slug_directory_branch` resolves each step with `LIMIT 1`. Once branch is dropped, two rows with the same `(slug, directory_context)` become indistinguishable and the reader picks arbitrarily. Measured live: exactly **one** such pair exists today — `aws-org-migration-terraform-automation` in `/home/max/aws-work` (n=2). Cheap to fix, but it is a real correctness step, not hygiene. Car 8.
8. **import-linter does not name `branch.py`.** The four contracts in `pyproject.toml` are module-layer contracts (`_shared` / `core` / `backend`). Deleting `yadgar/_shared/storage/branch.py` needs no contract edit.

---

## 1. Car list (dependency order)

| # | Car | Blocks / blocked by |
|---|---|---|
| 0 | Preflight & guard baseline (read-only) | blocks all |
| 1 | Read path — retire all 5 filters | blocks 8, 9 |
| 2 | Write path — hard-reject removal | blocks 9 |
| 3 | gitness seam (ADR-0216) — **one car, end-to-end** | blocks 5 |
| 4 | Senders stop sending `branch_hint` | **must precede 5** |
| 5 | MCP signatures + SDK-JS regen | needs 1,2,3,4 |
| 6 | Detection helpers, dead tooling, test corpus | needs 5 |
| 7 | Knobs + capability registry (I25 + I32 atomic) | needs 1,2,3 |
| 8 | Data migration — **user-gated, two-step** | needs 1,2,3 |
| 9 | Schema drop (migration 029) | needs 8 |
| 10 | Docs, ADR amendments, **residue proof** | last |

Ordering rationale, per ADR-0215's stated hazard: **readers retire first (1,2,3), then values are nulled (8), then the column is dropped (9).** Reversed, reads break mid-train.

Car 4 before Car 5 is the *second* ordering constraint and it is not the one ADR-0215 names: callers must stop **sending** `branch_hint` before signatures stop **accepting** it. If signatures drop first, a model following stale rules gets `InputValidationError`. The reverse (sending an ignored optional param) is harmless.

---

## Car 0 — Preflight & guard baseline

**Scope:** No production code. Capture the "before" numbers that later exit criteria compare against, and prove which guards will fire.

- [ ] Record baseline residue counts (the completion proof in Car 10 compares against these):
  ```
  for id in branch_hint BranchFilter _build_branch_clause _detect_branch \
            _get_current_branch _get_default_branch _default_branch_for_root \
            BRANCH_ENFORCEMENT BRANCH_BOOST_WEIGHT missing_branch \
            wiki_cleanup_merged_branches YADGAR_CI_BRANCH \
            read_by_branch read_by_directory_branch get_wiki_page_by_slug_and_branch; do
    n=$(git grep -c "$id" -- yadgar/ sdk-js/src/ scripts/ .github/ install_assets/ \
        | awk -F: '{s+=$NF} END{print s+0}'); echo "$id: $n"; done
  ```
  Captured 2026-08-07: `branch_hint 582`, `BranchFilter 43`, `_build_branch_clause 15`, `_detect_branch 288`, `_get_current_branch 14`, `_get_default_branch 143`, `_default_branch_for_root 4`, `BRANCH_ENFORCEMENT 46`, `BRANCH_BOOST_WEIGHT 21`, `missing_branch 146`, `wiki_cleanup_merged_branches 34`, `YADGAR_CI_BRANCH 106`, `read_by_branch 19`, `read_by_directory_branch 10`, `get_wiki_page_by_slug_and_branch 6`.
- [ ] Run `python scripts/check_test_weakening.py --ci --base origin/master` on a clean tree; record it green. Any pre-existing redness is not this train's.
- [ ] Read-only data inventory (feeds Car 8; **no writes**):
  ```
  db_inspect: SELECT branch, count() FROM wiki_page GROUP BY branch
  db_inspect: SELECT branch, count() FROM memory GROUP BY branch
  db_inspect: SELECT slug, directory_context, count() AS n FROM wiki_page
              GROUP BY slug, directory_context ORDER BY n DESC LIMIT 50
  ```
- [ ] Confirm pre-migration backup convention from `docs/plans/0115-pre-migration-backup-2026-08-01.md` applies and is executable before Car 8.

**Exit criterion (positive evidence):** a committed baseline table in the CHANGELOG unreleased section listing all 15 identifier counts and the three DB aggregates, with the date. Car 10's proof is a *diff against these numbers*, not an unanchored "grep returns zero."

**Could this car pass while doing nothing?** Yes — it is inventory. That is why its output is a committed artifact with concrete numbers, not a claim.

**Rollback:** nothing to roll back.

---

## Car 1 — Read path: retire all 5 filtering implementations

**Scope:** The crux. All five distinct filters die together, plus the caller-side `current_branch`/`default_branch` plumbing that feeds them. Core↔backend contract change → **both version bumps**.

### 1a. `_build_branch_clause` / `BranchFilter` (implementation #1)

- [ ] `yadgar/_shared/storage/branch.py` — **delete the whole module** (`BranchFilter`, `_build_branch_clause`, `bf_default`, `bf_current`). No import-linter contract names it.
- [ ] `yadgar/_shared/storage/__init__.py` — drop the `BranchFilter` + `_build_branch_clause` re-exports (2 sites)
- [ ] `yadgar/_shared/storage/scope.py:63` and the class body — `ScopeFilter.branch` field, `from_scope` branch arm, `build_clause` branch composition. `ScopeFilter` **survives** with the directory half only. (9 `BranchFilter` + 3 `_build_branch_clause` + 2 `current_branch` refs — regenerate: `grep -n 'branch' yadgar/_shared/storage/scope.py`)
- [ ] `yadgar/_shared/storage/directory.py` — 1 `BranchFilter` + 1 `_build_branch_clause` ref
- [ ] `yadgar/_shared/storage/memory.py:766,812,873` — 6 `_build_branch_clause` + 4 `current_branch` sites. Regenerate: `grep -n '_build_branch_clause\|current_branch\|default_branch' yadgar/_shared/storage/memory.py`
- [ ] `yadgar/_shared/storage/vector.py:64` — 2 `_build_branch_clause` + 2 `current_branch` + 1 `default_branch`
- [ ] `yadgar/backend/retrieval/core.py:574` — 3 `BranchFilter` + 8 `current_branch` + 9 `default_branch`
- [ ] `yadgar/backend/retrieval/scoring.py` — 4 `BranchFilter` refs
- [ ] `yadgar/backend/retrieval/stages/knn.py`, `stages/fts.py`, `stages/temporal.py` — 2 `BranchFilter` + 1 `current_branch` + 2 `default_branch` each

### 1b. §25 slug ladder (implementation #2)

- [ ] `yadgar/_shared/storage/wiki.py:410-499` — delete `get_wiki_page_by_slug_and_branch`; **rewrite** `get_wiki_page_by_slug_directory_branch` to the 2-step ladder (`directory=$caller_dir` → `directory='global'`) and rename to `get_wiki_page_by_slug_directory`. Keep `LIMIT 1` **but see Car 8** — a `(slug, directory)` collision is now user-visible.
- [ ] `yadgar/_shared/wiki/store.py:812-844` — delete `read_by_branch`; rewrite `read_by_directory_branch` → `read_by_directory` (drop `current_branch` param)
- [ ] `yadgar/core/server/tools/wiki.py` — `wiki_read` call site; also `wiki_history`, `wiki_diff`, `wiki_read_version`, `wiki_restore`, `wiki_set_metadata`, `wiki_append_section`, `wiki_replace_text` and the other edit primitives all resolve through this ladder. Regenerate the full list: `grep -n 'read_by_branch\|read_by_directory_branch\|_get_default_branch\|_detect_branch' yadgar/core/server/tools/wiki.py`

### 1c. `wiki_query` Python post-filter + 1.5× boost (implementation #3)

- [ ] `yadgar/core/server/tools/wiki.py:704-729` — delete the post-filter loop and the flat `* 1.5` branch boost entirely

### 1d. C4 convex fanout boost (implementation #4)

- [ ] `yadgar/backend/retrieval/recall_pipeline.py:227-294` — delete `_apply_fanout_boosts`' branch arm (the `settings.BRANCH_BOOST_WEIGHT` read at :269). The **postmortem** boost in the same function survives. 9 `current_branch` + 3 `default_branch` refs in this file.
- [ ] `yadgar/backend/retrieval/recall_pipeline.py:333` `_fanout_recall` — drop `current_branch`/`default_branch` params (this is `.complexity-allowlist.json` entry ~line 402; see Car 5)

### 1e. Similarity gate (implementation #5)

- [ ] `yadgar/_shared/wiki/store.py:1059-1170` — `find_similar_wiki_pages` / `_collect_similar_candidates`: drop the branch axis. **directory_context scoping survives** (ADR-0158 — the gate being directory-scoped is that ADR's fix and is untouched).
- [ ] `yadgar/core/server/tools/wiki.py` — `wiki_check_duplicate` loses its `branch` param/axis

### 1f. Dataclass / model fields on the read path

- [ ] `yadgar/backend/retrieval/providers/base.py` — `Scope.branch` (+2 `default_branch`)
- [ ] `yadgar/backend/retrieval/providers/base.py` — `Candidate.branch`
- [ ] `yadgar/backend/retrieval/state.py` — `RetrievalState.current_branch` / `.default_branch`
- [ ] `yadgar/backend/retrieval/providers/memory.py`, `providers/wiki.py`, `providers/fusion.py`, `providers/__init__.py` — normalization sites
- [ ] `yadgar/backend/embed_service/embed_service_models.py` + `embed_service_routes.py` — `current_branch`/`default_branch` model fields
- [ ] `yadgar/backend/retrieval/compare.py` (3+3), `fusion.py`, `query_analysis.py`, `reranking.py` — regenerate: `git grep -n 'current_branch\|default_branch\|BranchFilter' -- yadgar/backend/retrieval/`
- [ ] `yadgar/core/server/tools/recall.py` — `_forward_to_backend` drops `current_branch`/`default_branch` positional context (allowlist entry ~line 413; see Car 5)

### Tests in this car

- [ ] DELETE: `test_branch_retrieval_filter`, `test_retrieval_branch_polish`, `test_scope_filter_unit`, `test_wiki_read_resolution`, `test_fanout_boost_scope`, `test_storage_read_span`
- [ ] DELETE (e2e — **`ALLOW_TEST_WEAKEN=1` required**, see below): `test_scope_filter_e2e`
- [ ] MIXED surgery — delete only the named functions:
  - `test_providers_step1` → `TestScope::test_scope_fields`, `test_scope_optional_defaults`, `TestCandidate::test_candidate_fields`, `TestMemoryProvider::test_candidates_normalizes_fields`, `TestWikiProvider::test_candidates_normalizes_fields`
  - `test_phase1_recall_backend_widen` → `TestBranchBoostInFanout::test_current_branch_memory_gets_boosted`, `test_no_branch_no_boost`
  - `test_fts_scores_params` → `TestCollectFtsScores::test_branch_filter_forwarded`
  - `test_retrieval_pipeline` → `TestRetrievalStateDataclass::test_state_current_branch_none_by_default`
  - `test_wiki_similarity_gate` → `TestFindSimilarWikiPages::test_branch_scope_isolation`
  - `test_wiki_versioning` → `TestCorruptionPrevention::test_branch_resolution_keys_versioning_on_page_id`
- [ ] **ADD the positive-reachability test** (this is the exit criterion, not an extra):
  `yadgar/tests/e2e/test_branch_agnostic_reachability.py` — insert rows **directly via storage**, bypassing the tool layer, with `branch='feat/does-not-exist'` on both `wiki_page` and `memory`; then assert:
  - `wiki_read(slug, directory=D)` returns the row from a `master` caller context
  - `recall(query, directory=D)` returns the memory row
  - `find_similar_wiki_pages` sees the row as a duplicate candidate
  This test **must be written to survive Car 9** (once the column is gone it inserts without `branch=` and still asserts reachability). Note it as such in the file docstring.

**`check_test_weakening` handling:** this car deletes one e2e file (`test_scope_filter_e2e`) and **adds** one (`test_branch_agnostic_reachability`). The guard nets **per file**, so the addition does not offset the deletion. Expect a fail on `test_scope_filter_e2e.py` → commit with `ALLOW_TEST_WEAKEN=1` and state the reason in the commit body: *"e2e file deleted because the behaviour it asserted (branch+directory composition) no longer exists per ADR-0215; replacement positive-reachability e2e added in the same car."*

**[Q4 — ANSWERED 2026-08-07: CI has NO mechanism. The workflow edit IS part of this car.]**
`check_test_weakening.py:205` honours `ALLOW_TEST_WEAKEN` only when it equals exactly `"1"` (post-`.strip()`; `"true"` does not work). Verified: grepping that name across `.github/workflows/*.yml` and `.forgejo/workflows/*.yaml` returns **nothing** — no step `env:`, no job `env:`, no label gate, no repo var. It works locally and in pre-commit only, so `invariant-checks` **will** go red without an edit.

- [ ] `.github/workflows/ci-pr.yml` (~:471) — add a label-gated `env:` to the Layer-4 step:
  ```yaml
  env:
    ALLOW_TEST_WEAKEN: ${{ contains(github.event.pull_request.labels.*.name, 'allow-test-weaken') && '1' || '' }}
  ```
- [ ] `.forgejo/workflows/ci-pr.yaml` (~:460) — **the same edit.** This repo carries TWO workflow sets that can diverge; editing only one is a half-fix that passes on GitHub and fails on Forgejo. Confirm Forgejo Actions supports `contains()` / `labels.*.name` first — if not, use whatever equivalent it offers rather than leaving the step unguarded.
- [ ] Apply the `allow-test-weaken` label to the PR.
- [ ] **Car 10 reverts both workflow edits.** The bypass must not outlive the train.

**Exit criterion (positive evidence):**
`test_branch_agnostic_reachability.py` passes: a row physically stamped `branch='feat/does-not-exist'` is **returned** by `wiki_read`, by `recall`, and by the similarity gate, from a caller whose context is `master`. Plus `git grep -c BranchFilter -- yadgar/ | awk -F: '{s+=$NF} END{print s}'` returns the count of *test-only* survivors (expected 0 in `yadgar/_shared` and `yadgar/backend`).

**Could this car pass while doing nothing?** Yes, and this is the sharpest instance in the train: the car deletes the filters *and* the tests that exercised them, so a stub that deletes nothing and deletes the tests anyway would be green. The reachability test is the counter — it asserts a **new positive behaviour** that is impossible while any of the five filters is alive.

**Rollback:** single revert; the branch column still exists and still carries values, so reverting restores prior behaviour exactly.

---

## Car 2 — Write path: hard-reject removal

**Scope:** The `missing_branch` v5.42.3 guard, everywhere except the wiki_add boundary (that lives in Car 3, because it is inside `_check_wiki_add_context`).

- [ ] `yadgar/core/server/tools/memorize.py` — delete `_resolve_memorize_branch`; remove the `missing_branch` return (1 site) and the 2 `YADGAR_CI_BRANCH` reads
- [ ] `yadgar/core/server/tools/misc.py` — delete `_resolve_checkpoint_branch` and `_resolve_anchor_branch`; 2 `missing_branch` sites, 8 `YADGAR_CI_BRANCH` sites
- [ ] `yadgar/core/server/tools/project.py` — `update_active_work` inline reject (1 `missing_branch`, 3 `YADGAR_CI_BRANCH`)
- [ ] `yadgar/backend/write_exec/_memorize_phases/_phase_resolve_branch.py` — **delete the module**; remove from `_memorize_phases/__init__.py` phase list
- [ ] `yadgar/backend/write_exec/_memorize_phases/_phase_store.py` — drop the branch write
- [ ] `yadgar/backend/write_exec/memorize_impl.py` — 2 `branch_hint` refs; `MemorizeContext.branch` field
- [ ] `yadgar/backend/write_exec/anchor_impl.py`, `checkpoint_impl.py` — branch args
- [ ] `yadgar/_shared/write_exec/context.py` — `MemorizeContext.branch`
- [ ] `yadgar/backend/queue_drainer/dlq.py:99-207` — delete the defence-in-depth branch reject (4 `BRANCH_ENFORCEMENT` at :142,:149,:184,:193; 5 `missing_branch`; 2 `branch_hint`). **The directory reject in the same function survives.**
- [ ] `yadgar/backend/queue_drainer/__init__.py:340-390` — `_REJECTION_TAXONOMY`: remove the `missing_branch` member (5 refs)
- [ ] `yadgar/core/server/tools/admin_dlq.py:16-30,121-172` — remove `missing_branch` from the taxonomy mirror and the `dlq_requeue` special-case (8 refs). **`dlq_requeue` itself survives**; only its branch arm dies, and its `force=True` docstring loses the missing_branch paragraph.
- [ ] `yadgar/backend/queue_drainer/apply.py` — branch field on apply
- [ ] `yadgar/core/server/routes/control.py` — 1 `BRANCH_ENFORCEMENT` ref (enforcement status surface)
- [ ] `yadgar/_shared/observability/metrics.py:241` — the comment naming `YADGAR_BRANCH_ENFORCEMENT` on the `writes_with_enforcement_relaxed` metric; the metric itself survives for `directory`
- [ ] DELETE snapshot fixtures: `yadgar/tests/snapshots/memorize_reject_missing_branch_v5_49_4.json` and `yadgar/tests/scripts/snapshots/memorize_reject_missing_branch_v5_49_4.json`

### Tests in this car

- [ ] DELETE: `test_v5_42_3_drainer_branch_enforcement`, `test_v5_42_6_enforcement_knobs`, `test_v5_46_6_branch_hint_required`, `test_v5_46_9_branch_fallback_conditional`, `test_v5_46_7_yadgar_ci_branch_env_fallback`, `test_v5_46_3_ci_branch_env_var`
- [ ] DELETE (e2e — `ALLOW_TEST_WEAKEN=1`): `test_v5_42_1_gate_verification_e2e`
- [ ] `yadgar/tests/skip_inventory.json` — remove `entries[11]` (`v5-46-9-branch-fallback-ci-env`), otherwise `check_skip_inventory.py --validate-inventory` fails
- [ ] MIXED surgery: `test_dlq_rejection_taxonomy` (branch half), `test_queue_drainer_validation` lines 115-153, `test_v5_42_5_directory_contract` (branch half only — **the directory half is the point of that file and survives**), `test_v5_49_5_memorize_phases` → `test_phase_resolve_branch_uses_branch_hint_when_cwd_fails` + `test_phase_resolve_branch_prefers_branch_hint_over_ci_env`, `test_v5_49_5_memorize_snapshots` → `test_memorize_reject_missing_branch_returns_expected_dict`
- [ ] **ADD:** a test asserting `memorize(..., no branch context available)` **succeeds and stores a row** — with `_detect_branch` monkeypatched to raise, `YADGAR_CI_BRANCH` deleted from env, and no `branch_hint`. Same shape for `anchor`, `checkpoint`, `update_active_work`.

**Exit criterion (positive evidence):** four writes that previously returned `{"error": "missing_branch"}` now return a stored row ID, under an environment with *no* git, *no* `YADGAR_CI_BRANCH`, and *no* `branch_hint`. Assert on the returned ID and a subsequent read-back, not on the absence of an error key. Plus: `git grep -c missing_branch -- yadgar/backend/ yadgar/core/server/tools/admin_dlq.py` returns 0.

**Could this car pass while doing nothing?** Yes — deleting the reject tests makes a no-op car green. The added success-path tests cannot pass while the reject is alive, because they construct exactly the condition the reject fires on.

**Rollback:** revert. Rows written during the window carry `branch=None`, which every surviving reader (Car 1 already landed) treats as visible — no orphaning.

---

## Car 3 — gitness seam (ADR-0216) — **ONE car, end-to-end**

**Scope:** ADR-0216 is explicit that the hook → endpoint → persist → cache → read chain must not be split. A partial edit leaves the endpoint sending a field the persist layer no longer stores, or a cache keyed on a shape the reader no longer expects.

**Invariant for this car: every `default_branch` reference in the chain is deleted; every `gitness` reference is preserved.**

### The chain, in order

- [ ] `yadgar/core/hooks/session-start-context.py:28` `_compute_git_facts` — return `gitness` only; stop computing `default_branch` (11 `default_branch`, 8 `gitness` refs — regenerate: `grep -n 'gitness\|default_branch\|branch' yadgar/core/hooks/session-start-context.py`)
- [ ] `yadgar/core/hooks/session-start-context.py:103` — unpack site (`_gitness, _default_branch = ...`)
- [ ] `yadgar/core/hooks/session-start-context.py:87-121` — stop sending `branch` / `default_branch` on the `/hooks/session-context` request; keep `gitness`
- [ ] `yadgar/core/server/http.py:1010-1096` — `/hooks/session-context` handler: drop the `branch` and `default_branch` query params, keep `gitness` (10 `default_branch`, 9 `gitness`, 11 `dir_branch`, 15 `branch_hint`, 5 `_detect_branch`, 4 `current_branch` refs — regenerate: `grep -n 'branch\|gitness' yadgar/core/server/http.py`)
- [ ] `yadgar/core/server/http.py:180-228,290-309,865-902,1700-1717` — the remaining branch plumbing sites
- [ ] `yadgar/core/server/http.py::_persist_dir_branch_context` — drop `default_branch` from the persisted blob
- [ ] `yadgar/backend/admin_exec/project.py` — `upsert_dir_branch_context` / `get_dir_branch_context`: drop `default_branch` from the stored dict (7 `default_branch`, 7 `gitness`, 6 `dir_branch` refs)
- [ ] `yadgar/backend/admin_exec/__init__.py` — 2 `dir_branch` op registrations (keep the ops, they now carry gitness only)
- [ ] `yadgar/_shared/storage/wiki.py` — the `_dir_branch_context`-tagged memory-row read/write (8 `gitness`, 6 `dir_branch`, 10 `default_branch` refs)
- [ ] `yadgar/core/cache/cache.py` — the `dir_branch_context` namespace weight entry: **keep the namespace**, it now caches gitness alone (1 `gitness`, 1 `default_branch`, 2 `dir_branch`)
- [ ] `yadgar/core/server/tools/_dir_branch.py` — **the module survives**. Delete `default_branch` from the docstring contract and from `get_context`'s returned shape (6 `default_branch`, 8 `gitness`, 14 `dir_branch`, 2 `branch_hint`). Keep `get_context`, `invalidate`, the cache singleton, and the fail-safe semantics.
- [ ] `yadgar/core/server/tools/_runtime_config.py` — 2 `dir_branch` refs
- [ ] **Reader-tolerance check (ADR-0216 explicitly says confirm, don't assume):** verify `get_context` ignores unknown keys, so pre-existing `_dir_branch_context` rows still carrying `default_branch` are harmless until the next SessionStart re-upserts. If it does **not** ignore unknown keys, this car gains a one-line strip on read. Record which of the two it was in the CHANGELOG entry.

### `_check_wiki_add_context` and the canonical-write helper

- [ ] `yadgar/core/server/tools/wiki.py:34-49` — delete `_missing_branch_error`. **Keep `_missing_directory_error`.**
- [ ] `yadgar/core/server/tools/wiki.py:60-135` `_check_wiki_add_context` — collapse to directory enforcement only. The whole ADR-0126 §0.4 four-flow table goes; what remains is: empty directory + `YADGAR_DIRECTORY_ENFORCEMENT` on → `missing_directory`; otherwise proceed. `gitness` is no longer consulted **by this function** — flag this explicitly, because it is the one place where "gitness survives" could be misread as "gitness stays wired here." It stays wired for directory enforcement's *trusted-fact* property; if after this edit `_dir_branch.get_context` has **no remaining consumer**, say so in the car's commit body rather than deleting it silently — that is the ADR-0216 revisit trigger firing early and needs a user decision, not a build-time judgement.
- [ ] `yadgar/core/server/tools/wiki.py:31` — `CANONICAL_PAGE_TYPES` and `:140` `_wiki_write_canonical`: with branch gone, "canonical" is the only slot, so this helper collapses to an identity wrapper. **[Q1 — DECIDED: KEEP.]** Retain the function as a thin named passthrough so `adr_add` and `wiki_write_task_list` keep a stable server-side seam and the `_internal` token keeps its meaning for the drainer. Delete ONLY the `branch=None` assignment and the branch prose. Do NOT delete the seam — that widens the diff into ADR-0123/0158 territory for no gain.
- [ ] `yadgar/core/server/tools/adr.py`, `adr_render.py` — `branch_hint` (1 ref) and any default-branch pin
- [ ] `yadgar/core/server/tools/wiki.py::wiki_write_task_list` — drop the branch prose from its docstring (it currently explains at length *why* the page is canonical from any branch; that explanation becomes vacuous)

### Tests in this car

- [ ] DELETE: `test_car0_canonical_branch_model` (28 gitness / 25 dir_branch refs — its entire premise is the four-flow table), `test_car1_task_list_writer`, `test_session_start_context_hook`
- [ ] MIXED surgery in `test_session_context_endpoint` — **this file straddles the seam**; delete `test_task_list_nudge_absent_for_default_branch_pinned_row`, `TestCar0SetChannel::test_endpoint_populates_durable_store_and_fires_invalidate`, `test_endpoint_nongit_populates_canonical_facts`, `test_endpoint_without_gitness_param_does_not_clobber`. **Replace** the last two with gitness-only equivalents in the same commit — do not just delete them, or the gitness half loses its only coverage at the exact moment it is being rewired.
- [ ] MIXED: `test_wiki_add_branch_hint` is a DIES file but read it first — anything asserting *directory* behaviour moves to `test_v5_42_5_directory_contract`

**Exit criterion (positive evidence):** an end-to-end assertion across the whole chain in one test: fire the session-start hook against a **non-git** temp dir → assert the persisted `_dir_branch_context` memory row contains `gitness: false` and **does not contain a `default_branch` key** → assert `_dir_branch.get_context(dir)` returns `{found: True, gitness: False}` → assert `wiki_add(directory=<that dir>, no branch anything)` **stores**. Then repeat with a git dir and assert `gitness: true` and that `wiki_add` still stores. Both directions, one test, chain traversed for real.

**Could this car pass while doing nothing?** Yes, and dangerously — the five layers can each look locally sane while disagreeing. The end-to-end assertion is the only shape that catches a half-edit, which is precisely why ADR-0216 forbids splitting the car.

**Rollback:** revert. Existing `_dir_branch_context` rows are re-upserted by the next SessionStart either way, so the durable store self-heals in both directions.

---

## Car 4 — Senders stop sending `branch_hint`

**Scope:** Everything that *tells a caller* to pass branch context. Must land before Car 5.

- [ ] `yadgar/core/install_assets/rules/AGENTS.md.template:53` — delete the `Pass branch_hint on wiki_add (project-canonical → "master")` bullet
- [ ] `yadgar/core/hooks/templates/stop_checkpoint_prompt.md` — lines 5 (`{default_branch}` computation), 31, 68, 70, 99 (`branch_hint="{default_branch}"` on 4 calls), 59 (`adr_add ... branch-pins the entry`), 119-124 (step 5c branch prose). Line 39's *"git push, branch cleanup"* is git-workflow prose — **leave it**.
- [ ] `yadgar/core/hooks/templates/anchor_audit_prompt.md:5` — `{default_branch}` computation line
- [ ] `yadgar/core/install/clients/hooks_render.py` — the `{default_branch}` template substitution and its `git symbolic-ref` computation. Regenerate: `grep -n 'branch' yadgar/core/install/clients/hooks_render.py`
- [ ] `yadgar/core/install_assets/agents/general-purpose.md`, `agents/cavecrew-builder.md`, `agents/cavecrew-investigator.md` — `branch_hint` instructions. Regenerate: `git grep -n branch_hint -- yadgar/core/install_assets/agents/`
- [ ] `yadgar/core/server/tools/agent_prompts.py` — 18 `branch_hint` refs: the seeded starter-prompt bodies, the contract page, and the discipline pages. **These are seed *materials*; editing the source does NOT update the pages already stored in the live wiki.**
- [ ] **[Q3 — DECIDED: RE-SEED. This is a REQUIRED step of this car, not a follow-up.]** `seed_agent_prompts` is create-if-absent, so the ~17 already-stored pages (15 starters + contract + disciplines) will keep instructing agents to pass `branch_hint` after the source is clean. Force-reseed them, or push the new bodies through the versioning path (`agent_prompt_save` on the same slug versions rather than skipping).
  **This is the train's sharpest vacuous-pass trap:** every residue grep in Car 10 goes green while the live corpus is still wrong, because the greps read the repo and the corpus lives in the DB. Do not close the train on the source edit alone.
  Verification (positive, DB-side — not a grep):
  ```
  recall(type="wiki", tags=["agent-prompt"], directory="/home/max/git/yadgar")
  ```
  then assert **no returned page body contains `branch_hint`**. Repeat for the discipline pages and the contract page.
- [ ] `yadgar/core/server/tools/dispatch_helper.py` — 11 `branch_hint` refs in `agent_dispatch_prelude` (both the signature — deferred to Car 5 — and the prelude *text* it emits, which is a sender and dies here)
- [ ] `.github/workflows/ci-pr.yml:30,36`, `ci-release.yml:39`, `eval.yml:38`, `mutation-sweep.yml:29`, `perf.yml:36` — delete `YADGAR_CI_BRANCH: master` and the explanatory comment block at ci-pr.yml:30
- [ ] `yadgar/core/cli/hook.py` — the uncached `_detect_branch` at :54 is Car 6; here, only the 2 `branch_hint` *emission* sites
- [ ] `AGENTS.md` (repo root) — 3 branch refs; check whether any is the rules-block that `sync_instructions` regenerates

**Exit criterion (positive evidence):** `git grep -n 'branch_hint' -- yadgar/core/install_assets/ yadgar/core/hooks/templates/ .github/` returns **0**, AND a freshly rendered rules file + stop-checkpoint prompt (produced by actually running the renderer, not by reading the template) contains no `branch_hint` and no `{default_branch}` placeholder. The render step is what distinguishes this from "edited a template that isn't the one shipped."

**Could this car pass while doing nothing?** Partly — editing the template but not the renderer's substitution map leaves a dangling `{default_branch}` that either KeyErrors or renders literally. The "render it and grep the output" criterion catches exactly that.

**Rollback:** revert. Signatures still accept `branch_hint` (Car 5 hasn't landed), so old and new rules both work.

---

## Car 5 — MCP signatures (28 tools) + SDK-JS regen

**Scope:** Drop `branch_hint` / `branch` from the tool surface. **The SDK-JS regen is in this car, not after it** — `sdk-js/src/generated/{tools.ts,types.ts}` mirror the Python schemas and `npm run verify-tool-coverage` runs in `prepublishOnly`; splitting them leaves the intermediate car red.

Regenerate the exact per-tool list rather than trusting a count:
```
git grep -n 'branch_hint' -- yadgar/core/server/tools/
```
Expected file-level distribution (2026-08-07): `wiki.py 73`, `misc.py 26`, `agent_prompts.py 18`, `project.py 17`, `memorize.py 11`, `dispatch_helper.py 11`, `recall.py 6`, `adr.py 1` (plus `_dir_branch.py 2`, already handled in Car 3).

- [ ] `yadgar/core/server/tools/wiki.py` — `wiki_add`, `wiki_read`, `wiki_query`, `wiki_history`, `wiki_diff`, `wiki_read_version`, `wiki_restore`, `wiki_set_metadata`, `wiki_check_duplicate`, `wiki_append_section`, `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_before`, `wiki_insert_after`, `wiki_insert_at`, `wiki_replace_at`, `wiki_delete_at`, `wiki_replace_markdown_block`. Also `wiki_add`'s `branch` param (distinct from `branch_hint`) and `WikiAddOptions.branch`.
- [ ] `yadgar/core/server/tools/wiki.py` — `wiki_set_metadata`: remove `'branch'` from the allowed-`field` enum; **`'directory_context'` survives**, so the tool survives
- [ ] `yadgar/core/server/tools/misc.py` — `anchor`, `checkpoint`, `install_hooks`, `sync_instructions` and the rest (26 refs)
- [ ] `yadgar/core/server/tools/project.py` — `project_brief`, `update_active_work`, `bootstrap_project` (17 refs)
- [ ] `yadgar/core/server/tools/memorize.py` — `memorize` (11 refs)
- [ ] `yadgar/core/server/tools/recall.py` — `recall` (6 refs)
- [ ] `yadgar/core/server/tools/agent_prompts.py` — `agent_prompt_save`, `seed_agent_prompts` (signature half; the seed *text* was Car 4)
- [ ] `yadgar/core/server/tools/dispatch_helper.py` — `agent_dispatch_prelude` (signature half)
- [ ] `yadgar/core/server/tools/adr.py` — `adr_add` / `adr_get` / `adr_list`
- [ ] `sdk-js` — run `npm run generate`, then `npm run verify-tool-coverage` and `npm run typecheck`. Commit the regenerated `src/generated/tools.ts` + `types.ts` **in this car**.
- [ ] `.complexity-allowlist.json` — three entries cite branch params as param-count justification and will now be **over-justified**, which is a stale-entry failure for `check_complexity_allowlist.py`:
  - line ~159 `recall.py::recall` — params 10 → 9; rationale text names `branch_hint` and "branch detection with fallback logic". Either re-measure and shrink the entry, or delete it if the function now falls under the cap.
  - line ~402 `recall_pipeline.py::_fanout_recall` — params 10 → 8; the rationale enumerates `current_branch/default_branch` explicitly. Dropping 2 params likely brings it **to** the 8-param soft cap → the entry probably deletes entirely.
  - line ~413 `recall.py::_forward_to_backend` — params 10 → 8; same shape.
  - Also `wiki.py::wiki_add` (params 18) loses 2. Re-measure with `python scripts/check_complexity.py` and update rather than guess.

**Exit criterion (positive evidence):** `python scripts/check_complexity_allowlist.py` green with the re-measured entries **and** a live MCP call proving rejection: `wiki_add(title=..., content=..., directory=..., branch_hint="x")` returns an `InputValidationError` naming `branch_hint` as unexpected. That is positive evidence the schema actually changed — a signature edit that didn't propagate to the registered schema would still accept the kwarg silently. Plus `npm run verify-tool-coverage` green against the regenerated SDK.

**Could this car pass while doing nothing?** Yes — Python signatures can be edited without the MCP schema regenerating, and the SDK can be left stale. The unexpected-kwarg assertion and `verify-tool-coverage` are the two things that cannot both be green on a partial edit.

**Rollback:** revert both Python and `sdk-js/src/generated/` together. Because Car 4 already stopped the senders, a revert is safe in either direction.

---

## Car 6 — Detection helpers, dead tooling, test corpus

**Scope:** The now-unreferenced machinery, plus the bulk mechanical test sweep.

### Detection (two separate `_detect_branch` definitions — the enumeration is right, verify both)

- [ ] `yadgar/core/server/tools/project.py:99-125` — the **cached** `_detect_branch`; also `_get_current_branch` (4), `_get_default_branch` (6), and 14 `default_branch` refs. Regenerate: `grep -n '_detect_branch\|_get_current_branch\|_get_default_branch\|default_branch' yadgar/core/server/tools/project.py`
- [ ] `yadgar/core/cli/hook.py:54` — the **uncached** `_detect_branch` (3 refs)
- [ ] `yadgar/_shared/server_helpers/server_helpers.py` — `_default_branch_for_root` (3) + `_get_default_branch` (1); `yadgar/_shared/server_helpers/__init__.py` — 1 export
- [ ] `yadgar/core/server/__init__.py` — 3 `_detect_branch`, 1 `_get_current_branch`, 2 `_get_default_branch` re-exports
- [ ] `yadgar/core/server/tools/__init__.py` — 4 `_detect_branch`, 2 `_get_current_branch`, 4 `_get_default_branch`, 4 `default_branch` re-exports
- [ ] `yadgar/core/scripts/hook_runner.py` — 1 `_detect_branch`
- [ ] `yadgar/_shared/trace_mesh.py` — 2 `_detect_branch`, 1 `_get_default_branch`, 1 `default_branch`
- [ ] `yadgar/backend/admin_exec/wiki.py` — 1 `_detect_branch`, 9 `branch_hint`
- [ ] `yadgar/backend/restoration/checkpoint_restore.py` — 1 `_detect_branch`
- [ ] `yadgar/core/hooks/pretooluse-router.py` — 2 `default_branch` (**check first**: the G3 push guard in this file is a FALSE POSITIVE family; only the `default_branch` refs tied to session context die)

### Dead tooling — `wiki_cleanup_merged_branches` (all 6 sites)

- [ ] `yadgar/core/server/tools/project.py:2541` — the MCP tool (4 refs)
- [ ] `yadgar/backend/admin_exec/project.py:202` — the backend op (6 refs)
- [ ] `yadgar/backend/admin_exec/__init__.py` — op registration
- [ ] `yadgar/core/server/tools/__init__.py` — 2 registration/export refs
- [ ] `yadgar/core/server/__init__.py` — 1 ref
- [ ] `yadgar/__main__.py` — 1 ref
- [ ] `scripts/cleanup-merged-branches.sh` — **check before deleting**: if this is a git-branch housekeeping script unrelated to the wiki tool, it is a FALSE POSITIVE and stays.

### Other partly-dead surfaces

- [ ] `yadgar/backend/cls_store/promotion.py`, `yadgar/backend/consolidation/cleanup.py`, `yadgar/backend/graph/graph_layout.py` — branch refs; regenerate `git grep -n branch -- <file>` and classify each (several are likely programming-sense)

### Test corpus — the bulk sweep

- [ ] DELETE the remaining DIES files not consumed by Cars 1-3:
  `test_branch_auto_capture`, `test_branch_schema_migration`, `test_wiki_cleanup_merged_branches`, `test_v5_42_2_branch_default_e2e` (**e2e — `ALLOW_TEST_WEAKEN=1`**), `test_v5_42_4_master_fallback_cleanup`, `test_v5_42_6_resolution_hole`, `test_worktree_orphan_repair`, `test_adr`, `test_project_brief_adr_log`, `test_v5_46_4_fixture_directory_context`, `test_memorize_worktree_normalization`, `test_worktree_context_normalization`
- [ ] MIXED surgery, remaining files (delete only the named functions):
  - `test_v5_43_0_mcp_schema_discipline` — delete `q1,q2,q3,r1,r2,r3,r4,v1..v5,i1` (12 of 15). **KEEP** `q4_requires_directory_v565`, `b1_block_create`, `b2_agent_prompt_save`.
  - `test_project_brief` — `test_catalog_branch_field_is_string_or_none`, `test_branch_fallback_returns_string_for_git_repo`, `test_branch_fallback_non_git_stays_none`, `test_branch_hint_used_when_passed`, `test_branch_hint_overrides_get_current_branch`, `test_branch_hint_absent_falls_back_to_get_current_branch`
  - `test_wiki_edit_primitives` — `TestWikiSetMetadata::test_set_branch_non_null`, `test_idempotent_noop_branch`, `test_branch_empty_string_rejects`, `test_branch_null_clears_field`
  - `test_v5_44_0_subagent_mcp_wiring` — `TestAgentDispatchPreludeX1::test_include_context_uses_v5_43_0_signatures`
  - `test_agent_prompts` — `TestGlobalScopeBranchCanonicalization` (both)
  - `test_roadmap_update_signal` — **only** the `_insert_roadmap_wiki` `branch_hint` kwarg. `test_signal_uses_master_not_current_branch` is a **FALSE POSITIVE** (git checkout / lag-vs-master semantics) — **do not touch**.
- [ ] **[CORRECTION]** `yadgar/tests/e2e/test_wiki_set_metadata_allrows.py` — MIXED, not trivial. Remove the `branch=` param from `_insert_wiki_page_direct` (:28,:46), the three `_detect_branch`/`_get_default_branch` monkeypatch pairs (:66-67, :123-124, :161-162), and the seed rows at :75,:79,:131-133,:170-171 — collapsing the third row's `branch="feat/x"` into a distinct `directory_context`. The test's *point* (all-rows reach, not LIMIT-1) survives and gets **more** important after Car 8. e2e file → `ALLOW_TEST_WEAKEN=1` if asserts net down.
- [ ] TRIVIAL sweep — **80 files, ~167 kwarg deletion sites.** Do not transcribe; regenerate and count:
  ```
  git grep -c 'branch_hint=\|branch=' -- yadgar/tests/ \
    | grep -v -f <known-DIES-and-MIXED-and-FALSE-POSITIVE list>
  ```
  Target: every remaining hit is a `branch_hint=...` or `branch=...` **kwarg on a yadgar tool call**, deleted with no other change. If a hit requires thinking, it is misclassified — stop and reclassify rather than improvising.
- [ ] **DO NOT TOUCH** the 55 FALSE POSITIVE files. Families: `test_code_graph_*`, `test_cli_code_graph_install`, `test_check_backend_bump`, `test_v5_45_1_makefile_route`, `test_v5_46_8_workflow_triggers`, `test_systemd_generator_convergence`, `test_systemd_greenfield_units`, `test_hook_pretooluse_router_unit` (G3 push guard), `test_seed_disciplines`, `test_seed_materials`, `test_prelude_composition`, `test_tamper_guards`, `test_rules_render`, `_unit_render.py`, plus ~40 files whose only hits are programming-sense "code branch" / "else branch" / "404 branch" in comments (heavy in `test_vacuum*`, `test_cli_*`, `test_scripts_*`, `test_install_*`).

### BEHAVIOR_CONTRACT edits (must be in this car, with the test deletions)

- [ ] `docs/contracts/BEHAVIOR_CONTRACT.md:91` — **delete** BC-G3 (§25 branch resolution). `⏳[r]` → no ✅ loss.
- [ ] `:95` — **delete** BC-G8 (`wiki_cleanup_merged_branches`). `⏳[u]`.
- [ ] `:356` — **delete** BC-T57 (`=G8`).
- [ ] `:97` — **BC-G10 is `✅`. Reword, do not delete:** *"wiki_set_metadata reaches ALL rows of a slug across directory contexts"*. Keep the `✅` and the test pointer, so the green count is unchanged and `check_test_weakening`'s contract check stays green.
- [ ] `:138` — retitle the `DB-CONTRACT (directory/branch, v5.42–v5.65, PD-46..49)` section to drop `/branch`. BC-DC1 and BC-DC2 are directory-only and survive verbatim.

**Exit criterion (positive evidence):**
`git grep -c '_detect_branch\|_get_current_branch\|_get_default_branch\|_default_branch_for_root\|wiki_cleanup_merged_branches' -- yadgar/ scripts/` returns **0**, AND the full suite runs with the exact set of collected tests enumerated — record `pytest --collect-only -q | tail -1` before and after and assert the delta equals the number of functions this car intended to delete. **The repo's history of vacuous guards is exactly why the collected-count delta is the criterion**: "tests pass" would be green after deleting a test file *and* the feature it was supposed to cover.

**Could this car pass while doing nothing?** Very easily — it is 80% deletion. The collected-count delta is the guard: a car that deletes 40 tests when the plan says 62 is visibly wrong, and a car that deletes 62 tests but leaves `_detect_branch` alive fails the grep.

**Rollback:** revert. Signature and behaviour changes already landed in Cars 1-5, so a revert of Car 6 restores dead-but-harmless helpers.

---

## Car 7 — Knobs + capability registry (I25 + I32 atomic)

**Scope:** The tripwire car. `check_capability_coverage.py`'s ORPHAN/STALE classes make the code and its registry entry a **single atomic unit** — this is a genuine constraint on the car boundary, not just an exit check. And I25's three-way sync fails if a knob is dropped from only one of three files.

### `BRANCH_ENFORCEMENT` — all three sync files together

- [ ] `yadgar/_shared/config/config.py` — delete the `branch_enforcement` Settings field
- [ ] `yadgar/_shared/config/config_registry.py` — delete the registry entry
- [ ] `yadgar/_shared/config/config_yaml.py` — delete if present (grep first: `grep -n branch_enforcement yadgar/_shared/config/config_yaml.py`)

### `BRANCH_BOOST_WEIGHT` — all three

- [ ] `yadgar/_shared/config/config.py` (2 refs)
- [ ] `yadgar/_shared/config/config_registry.py` (1)
- [ ] `yadgar/_shared/config/config_yaml.py` (1)

### CAPABILITY_REGISTRY — four whole entries + prose

- [ ] `docs/contracts/CAPABILITY_REGISTRY.md:911-920` — **delete** `CAP-STOR-024 — Branch scoping enforcement (BRANCH_ENFORCEMENT)`
- [ ] `:1794` — **delete/rewrite** `CAP-WIKI-006` (§25 directory+branch scoping). The **directory** half survives → rewrite as directory-only rather than delete, or the directory-scoping capability becomes an ORPHAN.
- [ ] `:1955-1969` — **delete** the Car-0 wiring entries (`CAP-WIKI-021` and its sibling) whose `settings:` cite `BRANCH_ENFORCEMENT`. **Careful:** these entries also cover `DIRECTORY_ENFORCEMENT` and the gitness mechanism, which survive per ADR-0216 → **rewrite to the surviving gitness/directory scope**, don't delete wholesale, or `DIRECTORY_ENFORCEMENT` orphans.
- [ ] `:440,446` — the fanout-boost entry: remove `BRANCH_BOOST_WEIGHT` from `settings:` and the branch-boost sentence from `explanation:`. `POSTMORTEM_BOOST_*` and `FANOUT_BOOST_SCOPE` survive.
- [ ] `:963` — the `anchor()` entry's `wiring:` prose names `_resolve_anchor_branch` + `YADGAR_CI_BRANCH` — rewrite (this is exactly the `check_registry_prose_liveness.py` failure class: a cited identifier that stops existing)
- [ ] Full sweep — 68 branch hits in this file: `grep -n -i branch docs/contracts/CAPABILITY_REGISTRY.md`. Classify each; the code-graph default-branch entries are FALSE POSITIVES and stay.
- [ ] Any entry citing `wiki_cleanup_merged_branches` as an MCP tool → delete (the tool is gone, so the entry is STALE)

**Exit criterion (positive evidence):** `python scripts/check_capability_coverage.py` and `python scripts/check_registry_prose_liveness.py` **both green in the same commit** as the Settings-field deletions, plus `pytest yadgar/tests/server/test_config_three_way_sync.py` green. The prose-liveness gate is the sharpest instrument in the train for this car — it fires when a registry sentence names an identifier that no longer exists, which is precisely the failure mode of a half-done registry edit. Additionally: `python scripts/check_capability_coverage.py --list-orphans` must show **no new orphans**, proving the surviving `DIRECTORY_ENFORCEMENT` / gitness capabilities were rewritten rather than dropped.

**Could this car pass while doing nothing?** No, unusually — the I32 gate fails on *both* directions (orphan code without an entry, stale entry without code), so a partial edit in either direction is red. This is the one car whose guards are genuinely non-vacuous, and the car boundary was drawn to exploit that.

**Rollback:** revert; knob deletion is pure removal with no data effect.

---

## Car 8 — Data migration (user-gated, **two-step**, one-way)

**Scope:** The irreversible step. ADR-0215 is explicit: *"inspect those rows during migration and drop any that are pure noise rather than silently promoting them."* Split into a read-only step the user reviews, then a write step.

### Step 8a — read-only inventory (no writes; produces a review artifact)

- [ ] `db_inspect: SELECT id, slug, directory_context, branch, title, updated_at FROM wiki_page WHERE branch != NONE AND branch != 'master' AND branch != 'main' ORDER BY updated_at` — expect ~14 rows
- [ ] `db_inspect: SELECT id, branch, directory_context, string::slice(content,0,200) AS preview, tags, is_protected, created_at FROM memory WHERE branch != NONE AND branch != 'master' AND branch != 'main' ORDER BY created_at` — expect ~84 rows
- [ ] `db_inspect: SELECT slug, directory_context, count() AS n FROM wiki_page GROUP BY slug, directory_context ORDER BY n DESC LIMIT 50` — **the collision list**. Measured 2026-08-07: exactly one pair at n=2 (`aws-org-migration-terraform-automation` @ `/home/max/aws-work`). Re-measure; the number may have moved.
- [ ] Same collision query against `memory` keyed on whatever its natural identity is (there is no slug — collisions are not a correctness issue there, only volume)
- [ ] **Split the rows by `is_protected` (DECIDED — see Q1-Q5 section, Q2).** The keep/drop rule is settled and does NOT need re-litigating; what 8a still produces is the *current* row list, because counts move:
  ```
  db_inspect: SELECT count() AS n, is_protected, tier FROM memory
              WHERE branch != NONE AND branch != 'master' AND branch != 'main'
              GROUP BY is_protected, tier
  ```
  Measured 2026-08-07: 67 unprotected/no-tier, 6 unprotected/ephemeral, 18 protected/conditional, 3 protected/semantic_immortal.
- [ ] Take the pre-migration backup per `docs/plans/0115-pre-migration-backup-2026-08-01.md`

### Step 8b — write (only after 8a is signed off)

- [ ] **DELETE the UNPROTECTED branch-scoped rows only** — `is_protected = false`, ~73 memory rows, plus the ~14 branch-scoped `wiki_page` rows:
  ```
  DELETE memory    WHERE branch != NONE AND branch != 'master' AND branch != 'main'
                     AND is_protected = false;
  DELETE wiki_page WHERE branch != NONE AND branch != 'master' AND branch != 'main';
  ```
- [ ] **DO NOT DELETE the ~21 PROTECTED rows** (18 conditional + 3 semantic_immortal). They are anchored durable knowledge, not branch litter — they merely happened to be written on a feature branch. They fall through to the nulling step below and become globally reachable, which is the desired outcome. **Assert this explicitly before nulling:**
  ```
  SELECT count() FROM memory WHERE branch != NONE AND branch != 'master'
    AND branch != 'main' AND is_protected = true
  ```
  must still return **21** (or whatever 8a measured) immediately after the DELETE. If it returns 0, the DELETE was over-broad — STOP and restore from the backup.
- [ ] **Resolve `(slug, directory_context)` collisions before nulling** — for each pair, keep the newest by `updated_at` and delete the loser (or merge if the user says so). If this is skipped, `get_wiki_page_by_slug_directory` returns an arbitrary row for that slug forever, silently. This is a correctness step, not tidying.
- [ ] `UPDATE wiki_page SET branch = NONE WHERE branch != NONE`
- [ ] `UPDATE memory SET branch = NONE WHERE branch != NONE`
- [ ] Optional (ADR-0216, explicitly not required): if `_dir_branch_context` rows are being renamed, migrate the durable tag. Recommendation: **don't** — Car 3 already confirmed reader tolerance, and the rows self-heal on next SessionStart.

**Exit criterion (positive evidence):**
`SELECT branch, count() FROM wiki_page GROUP BY branch` and the same on `memory` each return exactly **one group**, `branch = NONE`, with counts matching (Car 0 baseline total − user-approved deletions). State both numbers explicitly. AND: the collision query returns **zero pairs with n > 1**. AND: `wiki_read` of the previously-colliding slug returns the row the user chose, asserted by ID.

**Could this car pass while doing nothing?** The nulling can't — the group-by proves it. The *collision* resolution absolutely can, because nothing else in the system notices; that is why the collision query is a named part of the exit criterion with an explicit zero.

**Rollback:** **Partial only.** Nulling is reversible from the backup. Row deletion is not, beyond restoring the backup wholesale. This is the train's genuine point of no return and is the reason for the user gate.

---

## Car 9 — Schema drop (migration 029)

**Scope:** Remove the column. Last structural step.

- [ ] `yadgar/_shared/storage/migrations.py` — add `_migration_029_drop_branch_column`:
  ```
  REMOVE FIELD IF EXISTS branch ON TABLE wiki_page;
  REMOVE FIELD IF EXISTS branch ON TABLE memory;
  ```
  and register it as `{"version": "029_drop_branch_column", ...}` at the end of the registry list.
- [ ] **Do not touch migrations 004 or 015.** Migration 026's docstring sets the precedent: historical migrations are kept for history immutability. Editing them would break replay on a fresh DB.
- [ ] **`wiki_draft.branch` needs nothing** — migration 026 dropped the whole table.
- [ ] `yadgar/core/export/schema.py:102,121` — delete the two `Column("branch", "branch", "VARCHAR")` entries (DuckDB export schema).
- [ ] **[Q5 — a real consumer exists; established 2026-08-07]** `yadgar/core/export/views.sql:159-169` — the `v_branch_distribution` view selects `memory.branch` **from the export**. DELETE it (a branch-distribution view is meaningless once there is one distribution). Dropping the schema columns without this leaves the view referencing a column that no longer exists.
- [ ] `yadgar/tests/core/test_export_duckdb.py` — remove `v_branch_distribution` from **all three** sites: `:491` (`TestViewsCreated.test_all_views_present`), `:519` (`TestViewsExecutable.test_view_executes`, parametrized — it runs `SELECT * FROM v_branch_distribution LIMIT 10`), and `:661`. All three go red otherwise.
- [ ] Everything else was checked and is clear — `yadgar/core/viz/`, `yadgar/backend/viz_exec/`, `viz-tests/`, `sdk-js/`, the CLI: zero reads of the export's branch column. No notebooks in the repo. (`yadgar/static/**` does not exist; the viz code is at `yadgar/core/viz/`.)
- [ ] `yadgar/_shared/storage/client.py` — any branch field in the row mapper
- [ ] `yadgar/_shared/wiki/contract.py`, `yadgar/_shared/wiki/policy.py` — branch refs in the wiki contract/policy shapes
- [ ] Verify `python scripts/check_capability_coverage.py` — every migration must be referenced by ≥1 registry entry, so **029 needs a CAPABILITY_REGISTRY entry in this car** or it orphans.

**Exit criterion (positive evidence):** on a **fresh** DB built by replaying all migrations 001→029, `INFO FOR TABLE wiki_page` and `INFO FOR TABLE memory` contain **no `branch` field**. And on the **live** DB after applying 029, the same. Both, because a forward migration that only works on a fresh DB is a known failure shape. Plus: `test_branch_agnostic_reachability.py` (added in Car 1) still passes — proving reads work with the column physically absent, which is the thing the ordering hazard was about.

**Could this car pass while doing nothing?** Yes — a migration function that is written but not registered runs never and fails nothing. The `INFO FOR TABLE` assertion on **both** a fresh and the live DB is what catches it.

**Rollback:** **Not clean.** Re-adding the field via a new migration restores the schema but not the values. The values are already NULL from Car 8, so the practical loss is zero — but state plainly that this car is the schema point of no return, and it is deliberately placed after the data car for that reason.

---

## Car 10 — Docs, ADR amendments, residue proof

### ADR amendments (amend, do not supersede)

- [ ] **ADR-0126** — amend: the §0.4 four-flow table is dissolved by ADR-0215. Record that the *trusted-facts / non-forgeability* principle **survives** via gitness (ADR-0216); only the branch flows die. Flows 2a/2b/3 collapse into "directory enforcement, then write."
- [ ] **ADR-0123** — amend: this ADR **dissolves rather than breaks**. Its intent ("ADRs must be readable from any branch and from non-git dirs") is now the universal default — with branch removed, everything is canonical. Record explicitly that the intent is *preserved by construction*, not abandoned.
- [ ] **ADR-0158** — amend: the similarity gate loses its branch axis; the **directory_context scoping that ADR-0158 introduced is the surviving mechanism and is untouched**. `page_type` routing, `wiki_policy`, `slug`/`upsert` are all unaffected.
- [ ] **Residual-risk flag:** the 214-ADR corpus was **not** fully body-searched. Before closing, run `adr_list` + grep the ADR wiki pages for `branch_hint|missing_branch|§25|branch scoping` and amend anything else that mandates branch behaviour. Record the search as done, with the hit list, so a future reader knows the coverage boundary.

### Doc rewrites

- [ ] `docs/reference/configuration.md` — **the largest single edit**: §25 at 708-751 and §26 at 839-894 both go. Also :109 (`branch_boost_weight` row) and :490 (`branch_enforcement` row). 30 branch hits total.
  **CAUTION:** §26's `wiki_refresh_stale` / `force_branch` references are **pre-existing drift from ADR-0157, NOT this train's scope.** Do not fix them here and do not let them block the car. Leave a one-line note pointing at ADR-0157.
- [ ] `docs/contracts/ARCHITECTURE_INVARIANTS.md:734` — the branch invariant (8 hits total)
- [ ] `docs/reference/architecture.md:49,238` — the MCP boundary contract paragraph and the enforcement-gates paragraph. Keep the `missing_directory` half of :49. 20 hits total.
- [ ] `docs/reference/retrieval.md` — 11 hits (branch filter / boost in the retrieval narrative)
- [ ] `docs/reference/claude-workflow.md` — 32 hits; **most are git-workflow prose (Branch-First rule) and are FALSE POSITIVES.** Only `branch_hint`-instruction lines die. Grep for `branch_hint` specifically.
- [ ] `docs/reference/decisions.md` — 8 hits
- [ ] `README.md` — 15 hits; grep for `branch_hint` and §25
- [ ] `AGENTS.md` (root) — 3 hits
- [ ] `docs/contracts/CAPABILITY_REGISTRY.md` — already done in Car 7; re-run the prose-liveness gate here as a cross-check
- [ ] **Wiki pages** (3, live corpus — not files): `yadgar-directory-branch-contract-v5-42-3-5-architecture` (delete or rewrite as directory-only) plus 2 others. Find with `recall(query="branch contract §25 resolution", directory="/home/max/git/yadgar", type="wiki")`.
- [ ] **Do NOT touch:** `docs/plans/archive/**`, `docs/CHANGELOG.md` history, `docs/reports/**`, `docs/roadmap/archive/**`. These are historical records; rewriting them destroys the archaeology this repo depends on.

### THE RESIDUE CHECK — completion proof for the whole train

This is the train's exit criterion. **Grep the dying identifiers, not the word `branch`** — a bare `grep -rn branch` will never go green and will tempt deletion of the false-positive set.

**Set A — must return exactly 0** (no exclusions needed; these names exist only for branch scoping):

```
for id in branch_hint BranchFilter _build_branch_clause _detect_branch \
          _get_current_branch _get_default_branch _default_branch_for_root \
          bf_default bf_current BRANCH_ENFORCEMENT BRANCH_BOOST_WEIGHT \
          missing_branch wiki_cleanup_merged_branches YADGAR_CI_BRANCH \
          read_by_branch read_by_directory_branch \
          get_wiki_page_by_slug_and_branch get_wiki_page_by_slug_directory_branch; do
  n=$(git grep -c "$id" -- yadgar/ sdk-js/src/ scripts/ .github/ install_assets/ \
      docs/reference/ docs/contracts/ README.md AGENTS.md 2>/dev/null \
      | awk -F: '{s+=$NF} END{print s+0}')
  printf '%-40s %s\n' "$id" "$n"
done
```
Baseline (Car 0, 2026-08-07) → target: `branch_hint 582→0`, `BranchFilter 43→0`, `_build_branch_clause 15→0`, `_detect_branch 288→0`, `_get_current_branch 14→0`, `_get_default_branch 143→0`, `_default_branch_for_root 4→0`, `BRANCH_ENFORCEMENT 46→0`, `BRANCH_BOOST_WEIGHT 21→0`, `missing_branch 146→0`, `wiki_cleanup_merged_branches 34→0`, `YADGAR_CI_BRANCH 106→0`, `read_by_branch 19→0`, `read_by_directory_branch 10→0`, `get_wiki_page_by_slug_and_branch 6→0`.

**Set B — needs scoped exclusions** (`default_branch` and `_compute_git_facts` are legitimately used by code-graph default-branch indexing):

```
git grep -n 'default_branch' -- yadgar/ \
  ':(exclude)yadgar/core/code_graph/*' \
  ':(exclude)yadgar/core/cli/code_graph.py' \
  ':(exclude)yadgar/core/install/code_graph_provision.py' \
  ':(exclude)yadgar/tests/core/test_code_graph_*' \
  ':(exclude)yadgar/tests/core/test_cli_code_graph_install*'
```
Expected: **0**. Carry the exclusions as literal pathspecs, not a regex — a cleverness regex will silently swallow a real hit.

**Set C — `current_branch`:** must be 0 outside the code-graph exclusion set. Note `_compute_git_facts` survives in `session-start-context.py` **name only**, computing gitness alone — assert it exists and that `grep -c default_branch yadgar/core/hooks/session-start-context.py` returns 0.

**Set D — schema:** `INFO FOR TABLE wiki_page` / `INFO FOR TABLE memory` contain no `branch` field, on both a fresh migration replay and the live DB.

**Set E — the false-positive floor.** After all the above are zero, `git grep -c -i branch -- yadgar/ | awk -F: '{s+=$NF} END{print s}'` will still be **non-zero**, and that is correct. Every remaining hit must be in the named false-positive families: code-graph default-branch indexing, the G3 push guard, git-workflow prose, and programming-sense "code branch" in comments. **Record the final number in the CHANGELOG so a future reader knows the expected floor** and does not mistake it for incomplete work.

**Set F — the LIVE CORPUS, not the repo (Q3).** Every grep above reads the repo; the seeded agent-prompt / discipline / contract pages live in the DB and are invisible to all of them. This is the train's sharpest vacuous-pass trap — Sets A–E can be perfectly green while every dispatched agent is still being told to pass `branch_hint`.
```
recall(type="wiki", tags=["agent-prompt"], directory="/home/max/git/yadgar")
```
Assert **no returned page body contains `branch_hint`**. Repeat for the discipline pages and the contract page. This must be checked DB-side; a repo grep cannot substitute.

### Revert the Car 1 CI bypass

- [ ] `.github/workflows/ci-pr.yml` (~:471) — remove the `ALLOW_TEST_WEAKEN` `env:` block added in Car 1
- [ ] `.forgejo/workflows/ci-pr.yaml` (~:460) — the same removal. Both, or the bypass survives on one platform.
- [ ] Remove the `allow-test-weaken` label from the PR
- [ ] Confirm `python scripts/check_test_weakening.py --ci --base origin/master` is green **without** the bypass at the train's tip

**Exit criterion for Car 10:** all of Sets A–D at their stated targets, Set E recorded with a number, **Set F green against the live DB**, both CI bypasses reverted and the guard green without them, all three ADRs amended, and the ADR-corpus sweep run with its hit list recorded.

**Could this car pass while doing nothing?** The greps can't be faked. The ADR amendments can — mitigate by requiring the amendment text to name the specific superseded clause (ADR-0126 §0.4, ADR-0123's default-pin reversal, ADR-0158's branch axis), not a generic "amended by ADR-0215."

---

## 2. NOT in scope

Explicitly out. Touching any of these widens the train and muddies the residue proof.

1. **The 55 FALSE POSITIVE test files.** Named in Car 6. The residue greps in Car 10 are constructed to leave them alone; if a grep starts flagging them, the grep is wrong, not the files.
2. **`wiki_refresh_stale` / `force_branch` drift in `docs/reference/configuration.md` §26 and `sdk-js`.** Pre-existing drift from **ADR-0157**, not ADR-0215. It lives in files this train edits, which makes it tempting. Leave a pointer, fix separately.
3. **Code-graph default-branch indexing** (`yadgar/core/code_graph/default_branch.py`, `digest.py`, `runner.py`, `__init__.py`, `cli/code_graph.py`, `install/code_graph_provision.py`, `BC-CODEGRAPH-4/7`). This is "index `origin/<default-branch>` in a temp worktree" — an entirely different concept that shares a word.
4. **The G3 push guard** in `yadgar/core/hooks/pretooluse-router.py` and `test_hook_pretooluse_router_unit`.
5. **`docs/plans/archive/**`, `docs/CHANGELOG.md` history, `docs/reports/**`, `docs/roadmap/archive/**`.** Historical record.
6. **Migrations 004 and 015.** History immutability.
7. **Project identity / owner-repo (task 0095).** ADR-0215 explicitly leaves open whether it absorbs any of branch's scoping intent, and says *"It should not be assumed to."* Do not fold it in.
8. **Renaming `dir_branch_context` → `dir_context`.** ADR-0216 permits it, does not require it, and warns it inflates the diff and adds a durable-tag migration. **Recommendation: don't.**
9. **`ScopeFilter` and `DirectoryFilter`.** Only `ScopeFilter`'s branch field dies. The bundle and the directory half survive.
10. **`gitness`.** Survives per ADR-0216. Any change to it beyond dropping `default_branch` is out of scope.

---

## 3. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **`LIMIT 1` non-determinism after the migration.** With branch gone, two rows sharing `(slug, directory_context)` are indistinguishable; `wiki_read` returns an arbitrary one, silently, forever. | **HIGH** — silent wrong-answer, the exact failure class ADR-0215 was written to kill | Car 8's collision query is a named exit criterion with an explicit zero. Currently exactly 1 pair. Re-measure at execution time. |
| R2 | **Cars 1, 2, 6 delete the tests that prove the behaviour they change.** Each is trivially green as a no-op. | **HIGH** — this repo has documented history of exactly this (mypy ratchet checked nothing for its whole life; skip-gate blind to deselected tests) | Every one of those cars carries a *positive* exit criterion: a new test asserting a behaviour that is impossible while the old code lives, plus a collected-count delta. |
| R3 | **The gitness chain half-edited.** Five layers, two facts travelling together. | **HIGH** | ADR-0216 mandates one car; the exit criterion is a single end-to-end test traversing hook→endpoint→persist→cache→read in both git and non-git directions. |
| R4 | **`check_test_weakening` red in CI** on three e2e deletions; the `invariant-checks` job carries `fetch-depth: 0` and cannot fail-open. | MEDIUM — blocks merge, doesn't corrupt | `ALLOW_TEST_WEAKEN=1` with a written reason per car (Cars 1, 2, 6). Confirm the CI job honours a repo-level override; if not, the workflow edit is part of Car 1. |
| R5 | **Signatures drop `branch_hint` before senders stop sending it** → live `InputValidationError` for anyone on stale rules. | MEDIUM | Car 4 strictly precedes Car 5. The reverse order is harmless. |
| R6 | **Seeded agent-prompt / discipline wiki pages do not refresh** when their source in `agent_prompts.py` is edited — `seed_agent_prompts` is create-if-absent. Live pages keep instructing `branch_hint` after the source is clean. | MEDIUM — the residue grep goes green while live behaviour is stale | Open question **Q3**. Do not close the train on the source edit alone. |
| R7 | **~98 feature-branch rows become globally visible.** One-way. | MEDIUM, accepted by ADR-0215 | Car 8 step 8a is a read-only user review; nothing is promoted without sign-off. |
| R8 | **I25 three-way sync fails if a knob is dropped from only 1-2 of 3 files.** | LOW — loud, immediate failure | Car 7 lists all three files per knob. The gate is the tripwire, working as designed. |
| R9 | **`.complexity-allowlist.json` entries become over-justified** (params drop below the stated count) → stale-entry failure. | LOW | Car 7/5 re-measures with `check_complexity.py` rather than guessing. Two entries probably delete entirely. |
| R10 | **Undiscovered ADRs mandating branch behaviour** — the 214-ADR corpus was not body-searched. | LOW-MEDIUM, honestly unknown | Car 10 requires the sweep to be *run* and its hit list *recorded*, so the coverage boundary is explicit rather than assumed. |
| R11 | **DuckDB export schema loses two columns** (Car 9); downstream viz/analytics may read them. | LOW | Car 9 checks consumers before deleting. If a consumer exists, it moves into the same car. |
| R12 | **`_dir_branch.get_context` may have zero remaining consumers** after Car 3 collapses `_check_wiki_add_context`. | LOW, but it is a design question not a build one | Car 3 requires this to be *reported in the commit body*, not resolved silently. It is ADR-0216's revisit trigger firing early. |

---

## 4. Open questions — ALL RESOLVED 2026-08-07

All five were answered by the user (Q1-Q3) or established factually (Q4-Q5). Recorded here as decisions; the car checklists above already reflect them.

**Q1 — `_wiki_write_canonical` / `CANONICAL_PAGE_TYPES` → KEEP.**
Retain the helper as a thin named passthrough. `adr_add` and `wiki_write_task_list` keep a stable server-side seam and the `_internal` token keeps its meaning for the drainer. Delete only the `branch=None` assignment and the branch prose. Deleting the seam outright would widen the diff into ADR-0123/0158 territory.

**Q2 — the ~98 feature-branch rows → SPLIT ON `is_protected`, not a blanket drop.**
Measured before deciding: of the ~94 branch-scoped `memory` rows, 67 are unprotected/no-tier, 6 unprotected/ephemeral, **18 protected/conditional, and 3 protected/semantic_immortal**. The protected 21 are not branch litter — they are deliberately anchored durable knowledge that merely happened to be written while a feature branch was checked out (the reusable haproxy-ingress adoption playbook, the eu-shared prometheus incident + recovery chain, quinyx/flux external-secrets topology marked *"SETTLED, do not re-derive"*, `ecs:StopTask` IAM behaviour; and among the semantic_immortal ones, the hook-bypass incident record and a `directory_context: "global"` cross-project lesson).

So:
- **DROP** the 73 unprotected `memory` rows + the 14 branch-scoped `wiki_page` rows (87 total).
- **KEEP** the 21 protected rows; null their branch like everything else. This makes them *more* reachable, not less.

Rationale for splitting on `is_protected` rather than agent judgement: anchoring is an explicit act and `semantic_immortal` requires a written reason, so the system's own signal already encodes "this was meant to last." User confirmed this recommendation over a blanket drop.

**Q3 — stale seeded wiki pages → RE-SEED.**
`seed_agent_prompts` is create-if-absent, so editing `agent_prompts.py` will NOT refresh the ~17 already-stored pages. The train must force-reseed them (or push the new bodies through the versioning path) so the live corpus stops instructing agents to pass `branch_hint`. **This is a required step, not a follow-up** — without it the residue grep goes green while live behaviour stays wrong. See Car 4.

**Q4 — `ALLOW_TEST_WEAKEN` in CI → the override exists, but CI CANNOT USE IT. Workflow edit required.**
`scripts/check_test_weakening.py:205` bypasses when the env var equals exactly `"1"` (post-`.strip()`; `"true"` does not work). Verified: grepping `ALLOW_TEST_WEAKEN` across `.github/workflows/*.yml` and `.forgejo/workflows/*.yaml` returns **nothing** — no step `env:`, no job `env:`, no label gate, no repo var. It works locally and in pre-commit only.
→ Car 1 adds a label-gated `env:` block to the Layer-4 step in **BOTH** `.github/workflows/ci-pr.yml` (~:471) and `.forgejo/workflows/ci-pr.yaml` (~:460) — this repo carries two workflow sets that can diverge, so editing one is a half-fix. Car 10 reverts both.
Note: confirm Forgejo Actions supports `contains()` / `labels.*.name` before relying on that expression form; unverified.

**Q5 — DuckDB export consumers → YES, one exists, and the plan originally missed it.**
`yadgar/core/export/views.sql:159-169` defines `v_branch_distribution`, which selects `memory.branch` **from the DuckDB export**. It is pinned by name in `yadgar/tests/core/test_export_duckdb.py` at **three** sites (:491 `test_all_views_present`, :519 `test_view_executes`, :661).
→ Car 9 must delete/rewrite the view alongside `schema.py:102,121` and update all three test sites. A branch-distribution view is meaningless once there is one distribution, so deletion is the expected outcome.
Everything else checked and clear: `yadgar/core/viz/`, `yadgar/backend/viz_exec/`, `viz-tests/`, `sdk-js/` and the CLI have zero reads of the export's branch column (sdk-js `branch` hits are MCP tool params against the live DB). No notebooks in the repo. Note `yadgar/static/**` does not exist — the viz code lives at `yadgar/core/viz/`.

---

## 5. Car summary (compact)

```
0  Preflight            baselines + read-only data inventory        no code
1  Read path            5 filters + branch.py deleted + plumbing    +reachability e2e
2  Write path           missing_branch reject, drainer, DLQ tax.    +success-path tests
3  gitness seam         hook→endpoint→persist→cache→read  ONE CAR   ADR-0216
4  Senders              rules, prompts, agents, CI env var          MUST precede 5
5  Signatures           28 MCP tools + sdk-js regen + allowlist     needs 1,2,3,4
6  Cleanup              detection, dead tooling, 130 test files, BC needs 5
7  Knobs + registry     I25 three-way + I32 atomic code↔registry    needs 1,2,3
8  Data migration       user-gated 2-step, collisions, ~98 rows     ONE-WAY
9  Schema drop          migration 029 + export schema               needs 8
10 Docs + ADRs          3 amendments, doc rewrites, RESIDUE PROOF   completion
```
