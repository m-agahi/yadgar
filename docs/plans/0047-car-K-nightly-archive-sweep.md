# Car K — nightly archive sweep, policy-dispatched

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: E, G, I
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Ship the nightly archive sweep for the engine-#2 ledger: a backend
consolidation phase that, once per nightly cycle, archives ledger rows whose
lifecycle is finished and excludes their body pages from recall. The §7 row is
"nightly archive sweep, policy-dispatched"; build-order step 9 (§6.1). The
sweep is **dispatched by the mutability policy** (Car J's `mutability` field +
page-type policy resolver): only rows whose body page is `mutable` are
eligible; `immutable` pages are skipped. The sweep keys on `project_id`
(stamped on every `task`/`adr` row by Car A, derived by Car A0) to scope
archival **per project** — it never archives project B's rows while sweeping
project A.

Three corpora, one sweep (per D3/D7/D38):

- **tasks** — `status='completed'` with `completed_at < now() - retention`
  → `status='archived'` + body page `page_type` retyped to the
  recall-excluded archived variant (D38). Aged off `completed_at`, NEVER
  `updated_at` (§3.4 / §14.2 — editing a completed task must not reset its
  90-day clock).
- **ADRs** — `status IN ('superseded','rejected','deprecated')` past
  retention → `status='archived'` + body retype. Car G already retypes the 12
  pre-existing superseded pages at seed; K handles only **future** superseded
  pages produced after G lands.
- **agent_pattern / agent_discipline** — `uses=0` AND `status='deprecated'`
  → archived. This is task #0015 ("agent-prompt prune becomes *archive* under
  D3/D7"). The `always_applied` discipline (the contract singleton) is
  **NEVER** archived — it is a singleton, not a prunable artifact.

`agent_pattern`/`agent_discipline` are reach-global (D3 — no `project_id`), so
their sweep is NOT per-project; it runs once over the whole table. `task`/`adr`
sweep IS per-project (iterates distinct `project_id` values).

**Why nightly needs `project_id` (user question, answered).** Archival is
scoped per-project: `SELECT ... WHERE project_id = ? AND status = ... AND
completed_at < ?`. A sweep that crossed projects would archive project B's
completed tasks while running for A. `project_id` is the scoping key. It is
already stamped on every `task`/`adr` row at write time (Car A), so the sweep
reads it back from the row — no env var, no per-row derivation, no new systemd
unit (the §16.11 Car L note: "Nightly derives `project_id` per-row from the
source row's `directory_context`" applies to the **memory** backfill, not the
ledger sweep; the ledger rows already carry `project_id`). For the
reach-global prompt tables there is no `project_id`, so that half of the sweep
is unscoped — by design.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/backend/consolidation/archive_sweep.py` | NEW. The sweep implementation. Prior PR #32 put it here (`archive_sweep.py:86` read `row["number"]` — the bug §13.2 flagged); K re-creates it greenfield with `row["id"]` only (`number` retired, §14.1/ADR-0197). | file absent in master (confirmed); prior attempt lived only on the rejected `feat/spine-knob-mariadb` branch |
| `yadgar/backend/consolidation/__init__.py` | ADD sweep invocation to `run_nightly_consolidation()` (`:176`) alongside the existing retention phase. The nightly cron path is the existing entry — see §6. | `__init__.py:176` (`run_nightly_consolidation`), `:189` (`_maybe_sleep_cycle`) |
| `yadgar/backend/consolidation/cleanup.py` | PATTERN to follow, NOT necessarily edited: `_run_retention_tasks()` (`:229`) is the existing single-engine (SurrealDB memory/wiki) retention sweep; the memory archive purge at `:275` (`MEMORY_ARCHIVE_RETENTION_DAYS`) + circuit-breaker at `:278` is the shape K mirrors for tasks/ADRs. K's sweep is cross-engine (MariaDB row + SurrealDB page) and lives in `archive_sweep.py`, NOT here. | `cleanup.py:229,275,278,280` |
| `yadgar/backend/consolidation/orchestrator.py` | NO change expected. `_run_retention_tasks()` is called at `:368` inside the consolidation cycle; K's sweep is invoked from `run_nightly_consolidation` (`__init__.py:176`), which this orchestrator forwards to. | `orchestrator.py:368` |
| `yadgar/_shared/wiki/policy.py` | READ `get_policy(page_type)` (`:147`) for `recall_disposition` and the `mutability` axis Car J adds. K does NOT modify this file. | `policy.py:147,186` (`is_recall_visible`) |
| `yadgar/_shared/config/config.py` | ADD knobs `TASK_ARCHIVE_RETENTION_DAYS`, `ADR_ARCHIVE_RETENTION_DAYS` (+ circuit-breaker) alongside `MEMORY_ARCHIVE_RETENTION_DAYS: int = 90` (`:482`) and `MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER: int = 500` (`:484`). | `config.py:482,484` |
| `yadgar/_shared/storage/sql/mariadb.py` | CALL ledger methods (built by Car A/D/F/I): `list_task_rows`, `list_adr_rows`, `list_agent_pattern_rows`, `list_agent_discipline_rows`, `update_task_row`/`set_task_status`, `set_adr_status`. NOT in master; [VERIFY exact names at build time]. | `mariadb.py` exists (Car A doc cites `:85`); ledger methods absent pre-Car-A |
| `yadgar/backend/admin_exec/invariants_cross_engine.py` | K's cross-engine writes (row flip + page retype) MUST satisfy the `content_hash` desync check this file owns (§14.3). NO edit expected — K's writes are check-conformant by construction. | file exists |
| `yadgar/backend/admin_exec/ledger.py` | [VERIFY: file exists after Car B]. IF the sweep is dispatched as a backend admin-op (the wiring option in §9), the `archive_sweep` op lives here, mirroring `archive_purge` (`admin_exec/memory.py:304`). | `admin_exec/memory.py:304` (`archive_purge`) — sibling pattern; `ledger.py` not in master pre-Car-B |
| `yadgar/tests/backend/test_archive_sweep_car_k.py` | NEW. Stale `.pyc` of this name lingers in `tests/backend/__pycache__/` from the rejected PR #32 branch; the `.py` source is ABSENT in master (confirmed) — K writes it fresh. | `.pyc` present, `.py` absent |
| `yadgar/core/server.json` | bump `backend_version` (K touches `yadgar/backend` consolidation; `BACKEND_BUILD_DIRS=("backend",)` per `scripts/check_backend_bump.py`). | [VERIFY exact bump field — Car A doc cites `core/server.json` `backend_version`] |

Files **NOT** touched (called out to pre-empt scope creep):

| file | why not |
|------|---------|
| `yadgar/core/scripts/nightly_cycle.py` (`:584` `main`, step 3 at `:637`) | The sweep runs INSIDE the existing step-3 consolidation (`run_nightly_consolidation`), so no new nightly step and no core bump — UNLESS §9 wiring option (c) is chosen. |
| `pyproject.toml:79` (`yadgar-nightly-cycle` console_script), `flake.nix:659`/`:681` (systemd service+timer) | Scheduling already runs step 3; the sweep rides on it. No new unit (§16.11 Car L note verbatim). |
| `yadgar/backend/admin_exec/memory.py:304` (`archive_purge`) | That is the **memory** store archive purge; K is the **ledger** sweep. Adjacent, not the same. |

## 3. Functions / symbols

New backend function (home: `yadgar/backend/consolidation/archive_sweep.py`).
Final names [VERIFY at build time against the ledger methods Car A/D/F/I ship]:

```python
async def run_archive_sweep(
    *,
    mariadb,            # MariaStorageEngine — row flip (Car A). [VERIFY handle source, §9]
    storage,            # StorageEngine — SurrealDB wiki page retype (existing)
    settings,           # Settings — TASK/ADR_ARCHIVE_RETENTION_DAYS + circuit-breaker
    project_id: str | None = None,   # None = sweep all projects (task/adr); prompts ignore this
    dry_run: bool = False,
) -> dict:
    """One nightly archive sweep. Idempotent (re-running is a no-op on
    already-archived rows). Returns stats:
      {swept_projects, archived_tasks, archived_adrs, archived_patterns,
       archived_disciplines, skipped_immutable, skipped_contract,
       circuit_breaker_hit, errors, dry_run}
    """
```

Per-item ordering (§4.1, load-bearing — a crash must leave a retyped page with
a stale row, NOT a flipped row pointing at a recall-visible page):

1. **retype the body wiki page** (SurrealDB) — `page_type` → archived variant,
   `recall_disposition="exclude"` per D38. Car G's retype mutator is reused.
2. **flip the ledger row** (MariaDB) — `status='archived'`; for tasks also
   clear `state=NULL` (§16.10: `state` is NULL once `status` is
   `completed`/`archived`).

Candidate predicates (per project_id for task/adr; global for prompts):

```python
# task (per project_id)
status == "completed" AND completed_at < now() - TASK_ARCHIVE_RETENTION_DAYS
  AND mutability(page) == "mutable"
# adr (per project_id)
status IN ("superseded","rejected","deprecated")
  AND created_at < now() - ADR_ARCHIVE_RETENTION_DAYS   # ADR has no completed_at (§3.5)
  AND mutability(page) == "mutable"
# agent_pattern (global — reach per D3)
uses == 0 AND status == "deprecated" AND mutability(page) == "mutable"
# agent_discipline (global) — EXCLUDE always_applied (the contract singleton)
uses == 0 AND status == "deprecated" AND always_applied == FALSE
  AND mutability(page) == "mutable"
```

Settings knobs to add in `yadgar/_shared/config/config.py` (sibling to
`:482`/`:484`):

```python
TASK_ARCHIVE_RETENTION_DAYS: int = 90
ADR_ARCHIVE_RETENTION_DAYS: int = 90
LEDGER_ARCHIVE_CIRCUIT_BREAKER: int = 500   # mirror MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER (:484)
```

[VERIFY at build time: exact `mutability` read path — Car J stores the field on
the wiki page and exposes `wiki_set_mutability`; K reads it via
`get_policy(page_type)` augmented by the per-page override, NOT a new resolver.
Car J is not a §7 hard dep of K but lands before K (J depends only on A; K
depends on E,G,I which transitively include A) — see §6.]

## 4. Build steps (TDD)

1. **RED** — `test_archive_sweep_archives_completed_task_past_retention`: a
   task row `status='completed'`, `completed_at` 100 days ago, body page
   `mutability='mutable'`. After sweep: row `status='archived'`, `state=NULL`;
   body page `page_type` is the archived variant; `is_recall_visible(page)`
   (`policy.py:163`) returns False. A second sweep run is a no-op
   (idempotent).
2. **RED** — `test_archive_sweep_skips_immutable`: same task but body
   `mutability='immutable'` — row untouched, page untouched,
   `skipped_immutable=1`. This is the "policy-dispatched" gate.
3. **RED** — `test_archive_sweep_ages_off_completed_at_not_updated_at`: task
   `completed_at` 10 days ago but `updated_at` 100 days ago (edited after
   completion) — NOT archived (retention is 90d on `completed_at`, §3.4/§14.2).
4. **RED** — `test_archive_sweep_archives_superseded_adr`: ADR
   `status='superseded'`, `created_at` 100 days ago → archived + body
   excluded. An `accepted` ADR of the same age is NOT archived.
5. **RED** — `test_archive_sweep_per_project_scoping`: two projects, each with
   an eligible completed task; sweep `project_id="m-agahi/yadgar"` archives
   only that project's row. Answers the user's "why nightly needs project_id"
   question with a test.
6. **RED** — `test_archive_sweep_archives_deprecated_zero_use_pattern`: an
   `agent_pattern` `uses=0`, `status='deprecated'` → archived. An
   `agent_discipline` with `always_applied=TRUE` is NEVER archived
   (`skipped_contract=1`) — the contract singleton is not prunable.
7. **RED** — `test_archive_sweep_orders_page_first_row_last`: simulate a crash
   between the page retype and the row flip (mock the row-flip to raise). After
   crash: page IS retyped (excluded), row `status` is STILL the pre-archive
   value — recoverable/detectable via the `content_hash` desync check
   (`invariants_cross_engine.py`). Assert the inverse order is NOT used.
8. **RED** — `test_archive_sweep_circuit_breaker`: seed > circuit-breaker
   candidate count → sweep aborts with `circuit_breaker_hit=True`, archives
   nothing (mirrors `MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER` shape,
   `cleanup.py:278`).
9. **RED** — `test_archive_sweep_invoked_from_run_nightly`: patch
   `run_archive_sweep` into `ConsolidationScheduler.run_nightly_consolidation`
   (`__init__.py:176`); assert it is called once per nightly cycle, after the
   consolidation cycle, before/around `_maybe_sleep_cycle` (`:189`).
10. **GREEN** — implement `archive_sweep.py`, add knobs, wire into
    `run_nightly_consolidation`.
11. **REFACTOR** — extract the per-item page-first/row-last ordering into a
    helper shared with Car G's retype mutator (same ordering rule, §4.1).

## 5. Acceptance gates

- [ ] A completed task past retention is archived (`status='archived'`,
      `state=NULL`); its body page is recall-excluded (D38); no row or body is
      hard-deleted (D7).
- [ ] An `immutable` body page is skipped (policy-dispatched gate).
- [ ] Sweep ages off `completed_at`, NOT `updated_at` (§3.4 / §14.2).
- [ ] Per-project scoping via `project_id` — a `project_id=...`-scoped sweep
      touches only that project's rows (answers the user question).
- [ ] Page-retype-first, row-flip-last ordering (§4.1); a mid-sweep crash
      leaves a retyped page + stale row, detectable by
      `invariants_cross_engine.py`.
- [ ] The `always_applied` discipline (contract singleton) is never archived.
- [ ] Circuit-breaker aborts an over-large sweep.
- [ ] Idempotent — a second run archives nothing new.
- [ ] core/backend version bumped per WORKFLOW RULE (new core/backend version).
      [VERIFY exact bump mechanism — K touches `yadgar/backend` so a
      `backend_version` bump in `yadgar/core/server.json` is expected; core
      bump only if §9 option (c) forces a `nightly_cycle.py` edit]
- [ ] pre-commit green (ruff, import-linter, I32, I33, `check_versions`,
      `check_ledger_chokepoint`).
- [ ] tests pass (`tests/backend/test_archive_sweep_car_k.py` + existing).

## 6. Sequencing

**K waits on (§7 deps, hard):**

- **E** — task seed + SessionStart/stop-hook rewire. K sweeps task rows; they
  must exist (seeded) for the sweep to have a corpus and for `completed_at` to
  be populated.
- **G** — ADR seed (from pages) + retype of the 12 superseded pages +
  `adr_superseded` page type. K sweeps ADR rows; the seed and the retype
  mutator K reuses both come from G.
- **I** — `agent_pattern`/`agent_discipline` tables + `list`/`get`. K sweeps
  deprecated zero-use patterns/disciplines; the tables must exist.

All three transitively include **A** (ledger tables + `MariaStorageEngine`
methods) and **A0** (`project_id` derivation + registry). K does NOT depend on
A0/A directly in §7 — they reach it through E/G/I.

**Soft / expected dep — J (mutability).** J ("mutability policy field +
per-page override + `wiki_set_mutability`") is NOT in K's §7 dep list, but K
reads the `mutability` field as its dispatch policy. J lands early (§7: "J
depends only on A and can land early"), so J is expected to have merged before
K starts. If J has NOT landed, K cannot distinguish `mutable` from `immutable`
and must either (a) block on J, or (b) ship with a default-`mutable` fallback
and a [VERIFY] note. State the chosen posture in the completing PR.

**Nothing waits on K.** K is terminal in the build order (§6.1 step 9). The
D35c verification gate (step 10) follows K but does not depend on the sweep's
existence — it depends on the seed being correct. M (cross-project `project=`
param) composes with K's per-project scoping but does not wait on K.

## 7. ADRs / decisions

- **D7** (archive, never hard-delete) — K sets `status='archived'`; never
  `DELETE`. The body page is retyped, never removed (D4).
- **D38** (body retained + excluded from recall via `page_type` retype) — K
  performs the retype that makes the body recall-excluded. The row persists at
  zero read cost (D37 open-only default).
- **D37** (task_list defaults to open-only) — K's archival is what makes a
  row leave the default read; archived rows need an explicit filter.
- **§4.1 ordering** — page-retype-first, row-flip-last; non-atomic in embedded
  mode (the nightly consolidation cycle runs embedded in some modes), so
  idempotency + ordering make a crash recoverable.
- **§3.4 / §14.2** — `task.completed_at` is the sweep's age field, not
  `updated_at`. This column exists BECAUSE of the sweep (§14.2: "the archive
  sweep ages tasks off last-touched, so editing a completed task resets its
  90-day clock").
- **D35a shape** — the sweep is a re-runnable, idempotent nightly op, not a
  migration step; re-running converges (already-archived rows are no-ops).
- **D3** — `agent_pattern`/`agent_discipline` are reach-global (no
  `project_id`); their sweep is unscoped. The contract (`always_applied`
  discipline) is a singleton and is excluded from archival.

## 8. Out of scope

- The `mutability` field, `wiki_set_mutability`, and the policy-axis addition
  (Car J). K reads them; J builds them.
- The ledger tables, `MariaStorageEngine` CRUD methods, `completed_at` column
  (Car A), task tools (Car D), ADR tools (Car F), prompt tables/tools (Car I).
- The seed (Car E/G) and the retype of the 12 pre-existing superseded ADR
  pages (Car G, build-order step 6). K handles only superseded pages produced
  AFTER G.
- The memory store archive purge (`archive_purge` at
  `admin_exec/memory.py:304`, `MEMORY_ARCHIVE_RETENTION_DAYS`). That is a
  different store; K is the ledger sweep.
- Cross-engine quiesced backup (nightly step 5b), vacuum, snapshot prune —
  all separate nightly steps.
- Rollup regeneration trigger + `subsystem` vocabulary (Car H, §10 deferred).
- A new nightly systemd unit / console_script / `YADGAR_PROJECT_ID` env var —
  explicitly rejected (§16.11 Car L note); the sweep rides the existing
  `yadgar-nightly-cycle` step 3.

## 9. Risks / open questions

- **[VERIFY — load-bearing] MariaDB engine handle.** `ConsolidationScheduler.__init__`
  (`__init__.py:80`) receives `storage: StorageEngine` (SurrealDB) — it does
  NOT hold the MariaDB engine. K's sweep must flip MariaDB rows. Wiring
  options:
  (a) Car B injects the MariaDB engine into the scheduler (or a backend
      consolidation service that holds both engines);
  (b) dispatch `run_archive_sweep` as a backend admin-op over HTTP
      (`admin_exec/ledger.py`, mirroring `archive_purge` at
      `admin_exec/memory.py:304`), invoked from `run_nightly_consolidation`;
  (c) add a new step to `nightly_cycle.py:main` (`:584`) that calls the
      backend op (forces a core bump + a `nightly_cycle.py` edit — otherwise
      out of scope).
  Preferred: (a) or (b) — both keep `nightly_cycle.py` untouched (backend bump
  only). Decide at build time against Car B's backend-op layout, which is not
  in master.

- **[VERIFY] exact ledger method names** K calls (`list_task_rows`,
  `list_adr_rows`, `list_agent_pattern_rows`, `list_agent_discipline_rows`,
  row-status setters). Built by Car A/D/F/I; absent in master. The §13.2
  review flagged the prior PR #32 `archive_sweep.py:86` reading `row["number"]`
  → KeyError. K MUST use `row["id"]` only — the `number` column is retired
  (§14.1/ADR-0197; `id` IS the number).

- **[VERIFY] exact `mutability` field name + read path** (Car J not in
  master). K assumes `mutability` is readable via `get_policy(page_type)`
  augmented by a per-page override; confirm against Car J's shipped shape.

- **[VERIFY] the archived `page_type` string.** D38 says "page_type retype on
  the D23 model" but does not pin the exact archived-type literal (e.g.
  `adr_archived` vs reusing `adr_superseded`). Decide at build time; reuse
  Car G's retype mutator's target type if it defines one.

- **Embedded-mode non-atomicity (§4.1).** Where the nightly cycle runs
  embedded, the sweep's two writes (Surreal page + MariaDB row) are NOT
  atomic. Idempotency + page-first ordering make a crash recoverable, and the
  `content_hash` cross-engine check (`invariants_cross_engine.py`) detects the
  stranded state. State this in the completing PR; do NOT add a
  cross-engine transaction (none exists — §4.1).

- **Retention default.** 90 days is inherited from
  `MEMORY_ARCHIVE_RETENTION_DAYS` (`config.py:482`) for consistency, but the
  plan does not pin a task/ADR retention number. Knob-ify (§3) and default to
  90; operator-tunable.

## Yadgar findings

- Car K ("nightly archive sweep, policy-dispatched", §7 row K, deps E/G/I) is
  build-order step 9; it is a backend consolidation phase, NOT a new nightly
  systemd unit — it rides the existing `yadgar-nightly-cycle` step-3
  consolidation (`yadgar/core/scripts/nightly_cycle.py:584` `main` → step 3
  `run_nightly_consolidation` → backend `ConsolidationScheduler.run_nightly_consolidation`
  at `yadgar/backend/consolidation/__init__.py:176`).
- The sweep is cross-engine: flips MariaDB ledger rows to `status='archived'`
  and retypes SurrealDB body pages to a recall-excluded `page_type` (D38).
  Ordering is page-retype-first, row-flip-last (master plan §4.1) so a crash
  leaves a retyped page + stale row, detectable by
  `yadgar/backend/admin_exec/invariants_cross_engine.py`'s `content_hash`
  check — never the inverse (flipped row + recall-visible page).
- `task.completed_at` (master plan §3.4/§14.2) exists BECAUSE of this sweep:
  archival ages off `completed_at`, not `updated_at`, so editing a completed
  task does not reset its 90-day clock. K reads `completed_at` as the age
  field for tasks; ADRs have no `completed_at` so K uses `created_at` for ADRs.
- "Why nightly needs project_id": the sweep keys on `project_id` (stamped by
  Car A, derived by Car A0) to scope archival per-project
  (`WHERE project_id=? AND status=? AND completed_at<?`); it does NOT derive
  project_id per-row (that is Car L's memory backfill, which uses
  `directory_context` — `yadgar/backend/consolidation/cleanup.py:192`). The
  reach-global prompt tables (`agent_pattern`/`agent_discipline`, D3) carry no
  `project_id`, so that half of the sweep is unscoped by design.
- Existing nightly/retention infra K builds on (verified in master):
  `run_nightly_consolidation` (`__init__.py:176`), the retention sweep pattern
  `_run_retention_tasks` (`yadgar/backend/consolidation/cleanup.py:229`), the
  memory archive purge + circuit-breaker shape (`cleanup.py:275,278`,
  `MEMORY_ARCHIVE_RETENTION_DAYS`/`CIRCUIT_BREAKER` at
  `yadgar/_shared/config/config.py:482,484`), the policy resolver
  (`yadgar/_shared/wiki/policy.py:147` `get_policy`, `:163`
  `is_recall_visible`), and the nightly console_script + systemd timer
  (`pyproject.toml:79`, `flake.nix:659,681`).
- Open wiring question: `ConsolidatorScheduler.__init__`
  (`yadgar/backend/consolidation/__init__.py:80`) holds only the SurrealDB
  `StorageEngine`, not the MariaDB engine — K must obtain the MariaDB handle
  via Car B injection or a backend admin-op dispatch (`archive_purge` at
  `yadgar/backend/admin_exec/memory.py:304` is the sibling pattern).
- Prior PR #32 attempt (`feat/spine-knob-mariadb`, rejected per §13.2) had an
  `archive_sweep.py:86` reading `row["number"]` → KeyError; K must use
  `row["id"]` only (`number` retired, §14.1/ADR-0197). A stale
  `test_archive_sweep_car_k.cpython-*.pyc` lingers in
  `tests/backend/__pycache__/` from that branch; the `.py` source is absent in
  master — K writes it fresh.
