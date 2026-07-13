# Dead-code sweep — repo-wide (task #19)

**Status: DRAFT — awaiting audit.** No code changed. This is a design plan only. Written
2026-07-13 against HEAD `3c70ed88` (#194, post-Reorg Rounds 1–2, core **5.132.0** / backend 5.41.0).
Read-only survey ran `ruff` (F401/F811/F841), `uvx vulture`, targeted greps, and a TS-export
probe — **collect-only, no `--fix`, no edits**. Recall-first: prior audits on `recall_via_pipeline`
(ADR-0046 forward-only) and the blast-radius lesson (memory 531809) were consulted; observed state
at HEAD wins and is re-verified in the inventory below.

---

## BLUF

Post-reorg the repo carries three *distinct* classes of removable-looking code, and they must not be
swept together:

1. **Genuinely dead** — symbols with exactly one repo-wide reference (their own def), not exported, not
   an entrypoint. The confirmed set is **8 storage/update-layer methods** (Car B). This is the only
   class that gets a "remove" verdict, and it stays in one small capped car.
2. **Test-only-reachable** — the flagship case is `recall_via_pipeline`
   (`yadgar/backend/retrieval/core.py:383`): a definition plus ~9 test call-sites and **zero
   production callers** at HEAD (re-verified below), which ADR-0046 (forward-only recall) already
   flagged for retirement. This is NOT a mechanical delete — removing the code means removing the
   tests that are its only user, and it intersects an in-flight architecture decision. Handled in its
   own gated car with an explicit user decision point.
3. **Intentional back-compat shims** (PEP-562 module `__getattr__` forwarders created by Reorg #167)
   and **public-API / MCP-tool / plugin entrypoints** — these look dead (no *internal* callers) but
   are load-bearing for external callers or deliberate deprecation windows. **Default verdict: KEEP /
   DEFER.** Proposing their removal is exactly the false-positive that broke CI last time.

**The plan's #1 job is scope discipline.** Memory 531809 records a codebase-wide decorator sweep
(`@observe` rollout) that surfaced ~11 contract bugs caught serially over hours and wedged CI for 2h
because changes were pushed before a completing full-suite local run. A dead-code sweep has the same
blast-radius shape (many files, subtle import/collection breakage). The antidote here is **small,
capped, independently-revertable cars gated on `pytest --collect-only` + affected-module tests**, with
the low-confidence and shim/public-API candidates **deferred, not swept**.

Headline: ruff already enforces all `F` codes in CI (F401/F811/F841 = zero repo-wide) → unused-imports
are a *solved* problem in-tree. The uncovered surface is **unused functions/methods** — surveyed via
`vulture --min-confidence 60` (NOT 80: vulture rates functions ~60%, so an 80 threshold is blind to
them — the trap this task's first pass hit). Of 198 function/method candidates, **~87% are
false-positives** (routes, hooks, ABI methods, `storage.`-dispatch, shims); the **firm dead set is just
8 storage/update methods** (Car B), plus `recall_via_pipeline` (test-only, gated Car R) and ~21
low-confidence storage methods deferred to a separate investigation. So the actual deletions are small;
the **CI-prevention allowlist gate** (§CI-prevention) is arguably the highest-value output — but the
sweep is NOT empty, and the 8-symbol set is real.

---

## Candidate inventory

Populated from the read-only survey (ruff F401/F811/F841, `uvx vulture`, per-symbol grep, TS-export
probe). **Two-pass vulture caveat (methodology correction — keep this):** the first pass used
`--min-confidence 80`, which mathematically cannot flag unused functions/methods (vulture rates those
~60%) — it only surfaced unused *parameters* and gave a false "clean of dead functions" impression.
The corrected `--min-confidence 60 --exclude '*/tests/*,*/benchmarks/*'` pass surfaced **198 function/
method/class candidates** (after removing PEP-562 `__getattr__`/`__dir__` shim pairs and
`contracts/models.py` pydantic classes; 224/97/20 raw fn/method/class before that filter). Every one
was classified per-symbol by grep. **Result: ~87% are false-positives** — 68 framework/entrypoint
(Starlette `@custom_route` handlers, `hook_*` endpoints, FastAPI routes, pydantic validators,
SpanProcessor/watchdog/`BaseHTTPRequestHandler` ABI methods), ~45 dynamic/protocol with confirmed
production or `storage.<name>()`-dispatch callers, 6 test-isolation hooks, 10 PEP-562 re-exports.
**Firm dead set: 8 storage/update-layer methods with exactly ONE repo-wide reference (their own def)**
— independently re-verified this session — plus ~21 low-confidence deferred storage methods. Firm
facts: ruff `F401/F811/F841` is **zero repo-wide**; `recall_via_pipeline` is dead-in-production;
`stage_overrides` is a functional bug, not dead code.

**Truly-dead shortlist (the only "remove" verdicts — each re-verified `grep -rn '\bNAME\b'
--include='*.py' .` = 1 hit, the def):**

| file:line | symbol | evidence | confidence | verdict |
|---|---|---|---|---|
| `yadgar/_shared/storage/memory.py:78` | `_get_consts` | 1 repo-wide ref (def only) | high | **remove** (Car B) |
| `yadgar/_shared/storage/memory.py:1049` | `get_total_reconsolidation_count` | 1 ref (def) | high | **remove** (Car B) |
| `yadgar/_shared/storage/memory.py:1061` | `count_memories_by_compression_level` | 1 ref (def) | high | **remove** (Car B) |
| `yadgar/_shared/storage/rules.py:210` | `update_memory_sr_coords` | 1 ref (def) | high | **remove** (Car B) |
| `yadgar/_shared/storage/rules.py:216` | `get_memories_with_sr_coords` | 1 ref (def) | high | **remove** (Car B) |
| `yadgar/_shared/storage/episode.py:57` | `get_all_episodes` | 1 ref (def) | high | **remove** (Car B) |
| `yadgar/_shared/storage/wiki.py:686` | `search_wiki_fts` (plain) | 1 ref (def); live path uses `search_wiki_fts_scored` (`wiki/store.py:794`) | high | **remove** (Car B) |
| `yadgar/core/update/snapshot.py:115` | `Snapshot.read_target_version` | 1 ref (def) — zero test/prod/string refs | high | **remove** (Car B, `core/update` sub-scope) |

**Evidence provenance + trust division (audit-critical):** the 198-candidate list is regenerable —
`uvx vulture yadgar --min-confidence 60 --exclude '*/tests/*,*/benchmarks/*'` (do NOT use a higher
threshold; see Risks). The **8 REMOVE verdicts above were independently re-grepped this session**
(`grep -rn '\bNAME\b' --include='*.py' .` → 1 hit each) and are **authoritative.** The KEEP/DEFER
bucketing of the other ~190 came from a single advisory classification pass — treated as **advisory,
conservative-safe**: a wrong KEEP merely declines a removal (safe direction), never causes a bad
delete, so it was not exhaustively re-verified. **One known advisory-pass error:** it misclassified
`recall_via_pipeline` as a live "main retrieval entry point" (KEEP) — independent grep this session
confirms it is **def-only, zero production callers**, which is why this plan treats it as test-only /
gated-retirement (Car R). Where the advisory pass and direct grep disagree, **direct grep wins**
(observed-state-wins). Below: confirmed-KEEP anchors + flagship items.

| file:line | symbol | deadness evidence | confidence | verdict |
|---|---|---|---|---|
| `yadgar/backend/retrieval/core.py:383` | `Retriever.recall_via_pipeline` (method) | Only non-test reference is its own def (line 383 + `@observe` line 382) — **0 production callers at HEAD** (re-verified this session); ~9 test call-sites (`tests/_shared/test_retrieval_pipeline.py`, `tests/server/test_fanout_step2.py`, `test_mcp_recall_pipeline_kwargs.py`); not in any `__all__` / retrieval `__init__.py`; ADR-0046 forward-only flagged it for retirement | **high** (dead in prod) | **test-only-decide** — gated Car R; removing it removes its ~9 tests too |
| `yadgar/core/server/tools/recall.py:134` | `stage_overrides` (MCP `recall()` param) | Accepted in the public MCP tool signature + docstring (line 162) but never forwarded to `_forward_to_backend()` (call at 254–264 omits it) → silent no-op for external callers | high | **OUT of sweep — functional bug** (route to bug tracker, see §Scope OUT); NOT dead code |
| `yadgar/backend/ml_client/ml_client.py:22` | `attributes` (param, `_rpc_span`) | Accepted but not forwarded into the OTel span; `_rpc_span` itself is live (called :748/:755/:762) | med | **defer** — param cleanup, low value, not dead code |
| `yadgar/_shared/runtime/offload.py:202` | `join_timeout` (param, `shutdown_pool`) | Accepted but unused in body; `shutdown_pool` is prod-called (`_shared/runtime/lifecycle.py:478`) | med | **defer** — possibly incomplete impl, not dead code |
| `yadgar/backend/graph/graph_api.py:544` | `hops` (param, `get_neighborhood`) | Vulture flag, but active callers pass it (`viz_exec/__init__.py:96`, `core/server/http.py:1939`) | low | **KEEP** — false positive |
| `yadgar/core/lifecycle/lifecycle.py:185` | `frame` (signal handler) | OS `(signum, frame)` ABI — required even if unused | low | **KEEP** — OS ABI |
| `yadgar/_shared/observability/tracing.py:397,498` | `parent_context` (`on_start` ×2) | OTel `SpanProcessor` interface contract | low | **KEEP** — interface ABI |
| `yadgar/_shared/storage/__init__.py:388` | `exc_val`, `exc_tb` (`__exit__`) | Context-manager protocol signature | low | **KEEP** — protocol ABI |
| `yadgar/_shared/runtime/lifecycle.py:383–384` | `start_daemons`, `watch_directory` | Inline comment (line 420): "params remain for signature stability — backend slim bootstrap calls this directly" | low | **KEEP** — self-documented intentional |
| `yadgar/_shared/contracts/protocols.py:89,106,109,135` | `idle_seconds`, `scope_id`, `scope_kind` | Protocol/ABC method signatures | low | **KEEP** — protocol ABI |
| `yadgar/_shared/knowledge_graph/knowledge_graph.py:417` | `now_iso` | Active internal caller (:97,:170 pass `now`) | low | **KEEP** — false positive |
| `yadgar/_shared/observability/metrics.py:1178` | `request` (`metrics_handler`) | Starlette route handler ABI | low | **KEEP** — framework ABI |
| 67 files (`def __getattr__`) | PEP-562 back-compat shims | Reorg #167 forwarders; 9+ carry explicit back-compat annotations (`backend/{embed_service,prospective,ml_client,conflict_resolver,narrative,safe_start,cache,predictive_coding}/__init__.py`, `_shared/{log_config,config_registry,cognitive_map,tracing,secrets}.py`, …); zero internal callers **by design** | high (that they're shims) | **KEEP/DEFER** — surface retire-question to user, never sweep |
| `sdk-js/src/*.ts` (7 files, ~1495 LOC) | TS exports | `ts-prune` unavailable offline — dead-export detection not performed | n/a | **DEFER** — needs `ts-prune`/`knip` with network |
| `yadgar/_shared/retrieval/{providers,stages}/` | empty dirs | gitignored `__pycache__`-only husks, no `.py` sources; post-reorg leftovers | high | **Car 0 (disk hygiene)** — not source dead-code |

**Deadness-evidence legend:** *zero-callers* = no reference in any `.py` outside its own def, incl.
tests. *test-only* = referenced only under `yadgar/tests/`. *shim* = module-level `__getattr__`
back-compat forwarder. *exported* = present in `__all__` or a public `__init__.py` re-export.
*entrypoint* = MCP tool registration, CLI command, plugin hook, or FastAPI/HTTP route. *ABI* = the
symbol's signature is fixed by an OS/framework/protocol calling convention (unused ≠ removable).

---

## Classification method

Every candidate is bucketed by a **conservative decision tree** (false-positive removal is the failure
mode we are explicitly avoiding — see Risks):

1. **Is it exported / an entrypoint?** In `__all__`, a public-package `__init__.py` re-export, an MCP
   tool, a CLI command, a FastAPI route, or a registered plugin/hook →
   **KEEP** (may have external callers; grep cannot see them). Do not remove even with zero internal
   callers.
2. **Is it a PEP-562 back-compat shim?** Module-level `__getattr__` forwarder from Reorg #167 →
   **KEEP/DEFER**. Surface a separate "when can these shims retire?" question to the user; do not
   propose removal in this sweep.
3. **Is it referenced only by tests?** → **test-only-decide.** Sub-question: *is the test the code's
   only reason to exist?* If yes and the feature is retired (e.g. `recall_via_pipeline` under
   ADR-0046), removing code+tests together is coherent but needs a user/architecture decision — it is
   NOT a mechanical dead-code delete. If the test guards behaviour still reachable another way, KEEP.
4. **Is it referenced nowhere (incl. tests), not exported, not an entrypoint?** → **remove**, high
   confidence — but still inside a capped subsystem car with the import/collect gate.
5. **Otherwise / any doubt** → **defer**, low confidence. Deferral is a valid, encouraged outcome.

**Tooling honesty (coverage limits):**
- `ruff --select F401,F811,F841` finds unused *imports* and locally-unused *vars* only — it does NOT
  find repo-wide unused *functions/methods/classes*. And ruff already runs `F` in CI, so most F401 is
  pre-caught (remaining hits are in `benchmarks/`, which ruff excludes, or genuinely new).
- `vulture` is the unused-*function* tool; it is **not installed** — this plan runs it ephemerally via
  `uvx vulture` (read-only; prints only). vulture false-positives heavily on dynamic dispatch, pydantic
  validators, fixtures, plugin entrypoints, and `__getattr__` shims → every vulture hit is treated as
  **candidate, not verdict**, and re-checked by grep before any car includes it.
- `ts-prune` (TS dead exports in `sdk-js/`) is **not installed**; if `npx` can't fetch it offline,
  TS dead-export detection is **deferred** and noted as an out-of-scope gap, not silently claimed.
- Net: **function-level deadness leans on grep + manual caller analysis → lower confidence → more
  deferral.** The plan does not imply coverage it lacks.

---

## Scoped-car design (small, capped, revertable)

Principle (directly answering 531809): **no single big sweep.** Each car touches one subsystem or one
confidence tier, is independently revertable (own commit/PR), and is capped by symbol count so a bad
call blasts a small radius. Cars are ordered lowest-risk-first; each is a gate the next depends on.

**Reality check from the survey (settled):** ruff is already clean (no unused-import car needed). The
corrected min-conf-60 vulture pass + full per-symbol classification resolved the unused-function
picture: **~87% false-positives, a firm dead set of 8 storage/update methods, and ~21 low-confidence
storage methods needing separate investigation.** So the sweep is genuinely small but NOT empty. Four
cars total: **Car 0** (husk hygiene, firm), **Car R** (`recall_via_pipeline`, gated on ADR-0046),
**Car B** (the 8 confirmed dead storage/update methods, capped), and a **deferred** low-confidence
storage set (no car — targeted investigation first). Do not expand beyond these; do not bulk-sweep the
deferred set — either error reintroduces the 531809 false-positive risk.

- **Car 0 — husk/empty-dir hygiene (disk only, NOT source).** Remove post-reorg empty dirs / ignored
  `__pycache__` husks (`yadgar/_shared/retrieval/{providers,stages}/` + 88 standard pycache dirs) via
  the documented cure (mem 532111):
  `git clean -fdX yadgar/ && find yadgar -type d -empty -delete`. **No `.py` deleted, no imports
  touched → zero test risk.** Ships independently, or hand to the user as a local hygiene command
  (each checkout accumulates its own husks). **Cap: directories only.**
- **Car R — `recall_via_pipeline` retirement (GATED, own car, own decision).** The ONE real dead-code
  item. Remove `Retriever.recall_via_pipeline` (`yadgar/backend/retrieval/core.py:383`) **and** its ~9
  test sites (`tests/_shared/test_retrieval_pipeline.py`, `tests/server/test_fanout_step2.py`,
  `test_mcp_recall_pipeline_kwargs.py`). **Preconditions (all must hold before this car ships):**
  (1) user/arch confirms ADR-0046 forward-only cutover is complete and this path has no revival plan;
  (2) `grep -rn recall_via_pipeline yadgar` shows only the def + the tests being deleted (no other
  importer sneaked in). Removing it means removing its tests, so this is a *coordinated code+test*
  delete, not a mechanical strip. **Do NOT fold into any other car.** **Cap: 1 method + its dedicated
  tests, single PR.** Gate: `--collect-only` + retrieval characterization tests isolated (`-n0`,
  mem 529001) + CI.
- **Car B — dead storage/update methods (the confirmed dead set — 8 symbols, all in `_shared/storage`
  + one `core/update`).** Remove exactly the 8 truly-dead-shortlist symbols (§inventory): `_get_consts`,
  `get_total_reconsolidation_count`, `count_memories_by_compression_level` (`memory.py`),
  `update_memory_sr_coords`, `get_memories_with_sr_coords` (`rules.py`), `get_all_episodes`
  (`episode.py`), `search_wiki_fts` plain variant (`wiki.py:686`), and `Snapshot.read_target_version`
  (`core/update/snapshot.py:115`). Each has ONE repo-wide reference (its def) — re-verified this
  session. **Cap: these 8, single small PR.** Optional split: `core/update` symbol into its own commit
  if reviewers want subsystem-pure diffs. Gate: `python -c "import yadgar"` + `pytest --collect-only`
  on affected shards + `test_storage*` / `test_upgrade_snapshot*` module tests + CI. **Before merge,
  re-run each symbol's grep** — if any now shows >1 ref (a caller landed since 2026-07-13), drop it
  from the car. If a removal reddens a test, the symbol was NOT dead → revert, re-classify.
- **DEFERRED — the ~21 low-confidence storage methods (NO car yet).** The classification flagged ~21
  further storage methods (`cluster.py`, `entity.py`, more `memory.py`/`rules.py`/`narrative.py`/
  `user.py`/`vector.py`/`wiki.py`) with zero *production* callers but test callers or docstring
  references, several looking like a coherent never-fully-wired storage feature-set (bitemporal
  beliefs, similarity-link cluster ops, implicit vectors). **These need a separate targeted
  investigation** — "is this a dormant feature to keep, or genuinely abandoned?" — before any removal.
  Do NOT bulk-remove them in this sweep; that is exactly the blast-radius move 531809 warns against.
- **DEFERRED (no car — the plan recommending *against* action, on purpose):**
  - **PEP-562 shims (67 files).** Reorg #167 back-compat forwarders. Surface a *separate*, later
    question to the user — "which shims can retire, and on what deprecation-window schedule?" — never
    sweep them here.
  - **`stage_overrides` MCP param.** NOT dead code — a *functional bug* (accepted then silently
    dropped). Route to the bug tracker / a behaviour-fix PR, not this cleanup sweep.
  - **`attributes` / `join_timeout` unused params.** Medium-value param cleanups on live functions;
    defer (touching a live signature risks a caller no grep sees). Bundle into a future param-hygiene
    task if ever worth it.
  - **All low-confidence vulture hits** (ABI/protocol/OS-convention params) — KEEP, never touch.
  - **TS dead exports in `sdk-js/`** — `ts-prune` DID run (via `npx --yes`): every hit is in
    `src/index.ts`, the public barrel re-export (`YadgarClient`, `*Args`/`*Result` types, etc.) → these
    are the SDK's public API surface, **KEEP**. No dead TS exports actionable in this sweep. (A future
    `ts-prune --ignore index.ts` or `knip` gate could catch genuinely-internal dead exports.)

Each car is one commit / one small PR, branch-first off latest master, revertable in isolation. If the
user's appetite is "just the safe hygiene," ship **Car 0 only**; Car R waits on the ADR-0046 gate; Car B
is ready (8-symbol set confirmed) but re-grep each symbol immediately before merge in case a caller
landed after 2026-07-13.

---

## Acceptance criteria [unit]

Per car (the plan *specifies*; the implementation PR *runs* — full-suite execution is out of scope for
this doc, and the box cannot run the full ~5600-test suite fast per mem 529001, so CI is the
authoritative full gate):

- `ruff check .` clean (no new F-code violations introduced by the edit).
- **`pytest --collect-only` on affected shards succeeds** — removing a symbol something still imports
  breaks collection, not just a test; this is the cheap early tripwire.
- Import-smoke: `python -c "import yadgar"` (and the touched submodules) succeeds — no `ImportError`
  from a removed name.
- Affected-module unit tests green (the tests co-located with the touched subsystem).
- No public-API / MCP-tool / entrypoint symbol removed (grep-verified against `__all__` + tool
  registry + route table before merge).
- CI green on the PR (authoritative full-suite gate).

Global done: all shipped cars merged, suite green on master, zero import/collection breaks, deferred
candidates documented (not deleted).

---

## Test plan

- **No new tests required for pure removals** — the acceptance gate is "existing suite still green +
  collection intact." Removing dead code should be behaviour-preserving by definition; if a removal
  turns a test red, the symbol was NOT dead → revert that car, re-classify.
- **Car R exception:** removing `recall_via_pipeline` deletes its ~9 test sites; verify no *other*
  test imports the symbol first (`grep -rn recall_via_pipeline yadgar/tests`), and confirm the
  forward-only path's own e2e/characterization tests still cover the behaviour (per ADR-0046).
- **Characterization guard (mem 529001):** if any car touches retrieval/recall internals, run the
  `test_*characterization*.py` oracles **isolated (`-n0`) per file** — mixed `-n auto` runs produce
  false `[]` failures from cross-test corpus wipes. Do not conclude "clean" from a partial/killed run.
- **Per-car locality:** run only the affected subsystem's tests locally; rely on CI for the full
  cross-shard gate. Do not attempt the full suite locally (contention + can't finish fast).

---

## Risks

- **PRIMARY — false-positive removal (cite memory 531809).** The `@observe` codebase-wide sweep
  surfaced ~11 decorator-contract bugs caught *serially* over hours and **wedged the CI runner for 2h**
  because edits were pushed before a completing full-suite local run; a partial/killed run was
  falsely read as "clean." A dead-code sweep is the same blast-radius shape: many files, subtle
  `ImportError`/collection breakage, and symbols that *look* dead but are reached dynamically. **Direct
  mitigations baked in above:** small capped cars (not one sweep), per-car `--collect-only` + import
  gate, conservative KEEP/DEFER default for shims + public API, and "if a removal reddens a test, the
  symbol wasn't dead — revert, don't force."
- **Dynamic-dispatch / reflection blind spot.** vulture and grep both miss `getattr`-dispatch, string
  registries, `__init_subclass__`, plugin loaders. Mitigation: KEEP any symbol reachable via a
  registry/entrypoint regardless of static caller count.
- **PEP-562 shim mis-removal.** Reorg #167 shims have zero internal callers *by design*. Removing one
  breaks external importers silently (no test in *this* repo covers external clients). Mitigation:
  shims are DEFERRED, never swept.
- **`recall_via_pipeline` intersects an in-flight decision.** ADR-0046 forward-only recall may or may
  not have fully cut over at HEAD. Removing the path prematurely could delete a still-referenced code
  route. Mitigation: Car R is user-gated on cutover confirmation.
- **Test-corpus-wipe false failures (mem 529001).** Retrieval characterization tests give false `[]`
  under parallel xdist. Mitigation: isolated `-n0` runs for any recall-touching car.
- **Vulture-threshold blind spot (discovered this task).** `--min-confidence 80` (or 90) cannot flag
  unused functions/methods — vulture rates those ~60%, so a high threshold silently reports "no dead
  functions" while seeing none of them. This task's first survey pass hit exactly this and nearly
  shipped a false "repo is clean" conclusion. Mitigation: the function sweep MUST run at
  `--min-confidence 60`; the CI-prevention gate spec below encodes this.
- **Deferred-storage-set premature removal.** The ~21 low-confidence storage methods look like a
  coherent never-wired feature-set (bitemporal beliefs, cluster similarity-links, implicit vectors) —
  removing them as "dead" could delete a dormant-but-intended API. Mitigation: they get a separate
  investigation, not a car in this sweep.

---

## Scope IN / OUT

**IN:**
- The 8 confirmed-dead storage/update methods — Car B (capped, re-grep before merge).
- `recall_via_pipeline` retirement — gated Car R, pending ADR-0046 confirmation.
- Empty-dir / husk hygiene — Car 0 (disk, not source).
- A CI-prevention allowlist recommendation (§below) — arguably the highest-value output of this task.
- *(No unused-import car: ruff already clean. No broad unused-function cars: ~87% of vulture hits are
  framework/dynamic false-positives — only the 8-symbol set survives the zero-reference bar.)*

**OUT / DEFERRED:**
- **`stage_overrides` MCP param** (`core/server/tools/recall.py:134`) — NOT dead code; a *functional
  bug* (accepted then silently dropped, never forwarded to backend). **Route to a bug-fix PR / tracker,
  not this sweep.** Included here only because the survey surfaced it; do not delete the param (that
  would be an API-breaking non-fix) — wire it through or explicitly document it as reserved.
- **The ~21 low-confidence storage methods** (cluster/entity/bitemporal/implicit-vector ops, some
  `memory.py`/`rules.py`/`wiki.py` methods with test-only or docstring refs) — deferred to a **separate
  targeted investigation** ("dormant feature to keep, or abandoned?"), NOT this sweep.
- **PEP-562 back-compat shims (67 files)** — kept; separate "when to retire, on what deprecation
  window" question surfaced to user later.
- **`attributes` / `join_timeout` unused params on live functions** — deferred param hygiene; touching
  a live signature risks callers grep can't see.
- **Public-API surfaces, MCP tools, CLI commands, FastAPI routes, plugin entrypoints** — kept even
  with zero internal callers (external callers invisible to grep).
- **All low-confidence vulture hits** (ABI/protocol/OS-convention params) — kept; not worth the blast
  radius.
- **TS dead exports in `sdk-js/`** — deferred until `ts-prune`/`knip` can run read-only.
- **Any refactor beyond deletion** (renames, consolidation, "while we're here" cleanup) — forbidden;
  surgical-edit discipline.
- **Full-suite local execution** — out of scope for a plan doc; CI is the authoritative gate.

---

## CI-prevention recommendation

Cleanup without prevention just re-accumulates. Concrete recommendations (ranked by value/cost):

1. **Add `vulture` to CI as an allowlisted gate (highest value — this is the actual coverage gap).**
   ruff already catches unused imports/vars via `F`; nothing catches unused *functions*. **Use
   `--min-confidence 60`, NOT 80/90** — vulture rates unused functions/methods ~60%, so a higher
   threshold silently blinds the gate to exactly the category it's meant to catch (the trap this task
   fell into on its first pass). Pair the low threshold with a checked-in **`.vulture_allowlist`**
   (generated by baselining the current ~190 confirmed-FP symbols: routes, `hook_*`, validators, ABI
   methods, PEP-562 shims, `storage.`-dispatch methods) so the gate fails only on *new* dead functions.
   Low threshold + a real allowlist is the only combination that both sees functions and stays quiet on
   the repo's legitimate dynamic surface. This is the prevention counterpart to the cleanup — and given
   how small the actual cleanup is, it is arguably the highest-value deliverable of task #19.
2. **Add `ts-prune` to the `sdk-js/` CI job** (or `knip`, which is more accurate for TS) with an
   allowlist, to gate dead TS exports the same way. Only if the TS surface justifies it (survey reports
   `sdk-js/src` size).
3. **Do NOT tighten ruff further for this purpose** — ruff's static F-rules already run; adding more
   ruff rules won't catch function-deadness. Don't imply otherwise.
4. Optional: a lightweight `--collect-only` smoke job (probably already implied by the test job) as a
   fast import-integrity tripwire.

The allowlist approach is essential: a bare `vulture`/`ts-prune` gate with no allowlist would fail
immediately on the repo's many legitimate dynamic/entrypoint symbols and get disabled — the allowlist
is what makes prevention sustainable.

---

## Version impact

- **Car 0 (husk hygiene):** disk-only, no source/version change. If done as a repo commit at all,
  **PATCH** (5.132.x); more likely handed to the user as a local command → no bump.
- **Car R (`recall_via_pipeline` retirement):** removes a method on `Retriever` in the backend package.
  Not re-exported and no production caller, but it *is* a public method surface → treat as an API
  removal: **backend MINOR** at minimum, with a CHANGELOG `[Removed]` note; confirm against the
  forward-only cutover (ADR-0046) before assigning. Core version likely PATCH (test-only churn) unless
  a core symbol is touched.
- **Car B (8 dead storage/update methods):** removes internal-ish storage methods, none re-exported,
  none with production callers → **core PATCH** (5.132.x) with a CHANGELOG `[Removed]` note. `_get_consts`
  is a private helper (safe); the public-looking storage methods have zero callers but conservatively
  note the removal in case a future migration seeder wanted them.
- **CI-prevention (vulture/ts-prune gate):** CI/infra only → no runtime version bump; note in CHANGELOG
  under tooling.
- **Net:** the version footprint of this whole task is small — one backend MINOR (Car R, if it ships) +
  one core PATCH (Car B) + a tooling note. The task is mostly a *near-clean-bill-of-health* (ruff clean,
  ~87% of vulture hits are FP) with a genuinely-small 8-symbol removal and a prevention gate.

---

## Yadgar findings

- **VULTURE METHODOLOGY TRAP (important):** `vulture --min-confidence 80` CANNOT flag unused
  functions/methods — vulture rates those ~60%, so an 80-threshold run only sees unused imports/params
  and gives a false "no dead functions" reading. Always run **`--min-confidence 60`** for the
  function/method/class sweep. The corrected pass here surfaced 198 candidates (vs "0" at 80).
- **ruff `F401/F811/F841` is zero repo-wide** at HEAD 3c70ed88 — unused imports/vars are a solved
  problem in-tree (ruff already runs `F` in CI). The real coverage gap is unused *functions* (vulture,
  not installed) and internal TS dead exports (`ts-prune`, not installed).
- **Of the 198 min-conf-60 candidates, ~87% are false-positives** of predictable classes: Starlette
  `@custom_route` handlers, `hook_*` endpoints, standalone hook scripts (`prompt-recall.py`), pydantic
  validators, SpanProcessor/watchdog/`BaseHTTPRequestHandler` ABI methods, PEP-562 re-exports, and
  storage/KG/embedding methods reached via `storage.<name>()` attribute dispatch (vulture is blind to
  attribute dispatch). **Firm dead set = 8 storage/update methods** (`_get_consts`,
  `get_total_reconsolidation_count`, `count_memories_by_compression_level`, `update_memory_sr_coords`,
  `get_memories_with_sr_coords`, `get_all_episodes`, plain `search_wiki_fts`, `Snapshot.read_target_version`)
  — each re-verified at exactly 1 repo-wide ref. Plus **~21 low-confidence storage methods** (cluster/
  entity/bitemporal/implicit-vector ops) that look like a never-fully-wired feature-set → separate
  investigation, NOT a bulk sweep.
- **`recall_via_pipeline` (`yadgar/backend/retrieval/core.py:383`) confirmed dead in production** — only
  non-test reference at HEAD is its own definition; ~9 test call-sites; not in any `__all__`/
  `__init__.py`; matches ADR-0046 forward-only retirement premise. Test-only-reachable → gated removal.
- **New functional-gap finding (not dead code):** `stage_overrides` param on the public MCP `recall()`
  tool (`core/server/tools/recall.py:134`) is accepted + documented but never forwarded to
  `_forward_to_backend()` → **silent no-op for external callers.** Belongs in a bug-fix PR, not this
  sweep. Worth a memory/tracker entry.
- **67 PEP-562 `__getattr__` shim files** from Reorg #167 — all intentional back-compat, zero internal
  callers by design; a bare vulture/ts-prune CI gate would false-positive on these → an **allowlist is
  mandatory** for any prevention gate.
- **ruff already enforces all `F` codes in CI**, so the actual prevention gap is unused *functions*
  (vulture, not installed) and TS dead exports (`ts-prune`, not installed). CI-prevention recommendation
  is arguably the highest-value output of task #19, above the near-empty cleanup itself.
- Post-reorg husk dirs `yadgar/_shared/retrieval/{providers,stages}/` are gitignored pycache-only
  leftovers (mem 532111 pattern) — disk hygiene, not source dead-code.
