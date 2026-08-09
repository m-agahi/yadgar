# Car A — ledger tables + alembic chain + chokepoint guard

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: A0
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Ship the engine-#2 ledger schema and the chokepoint that confines every row
access to it. Concretely, Car A writes the `002_ledger_tables` Alembic revision
(descending from the existing `0001_config` chain head), creates the seven
ledger/join tables (`task`, `adr`, `agent_pattern`, `agent_discipline`,
`task_blocked_by`, `adr_supersedes`, `agent_pattern_composes`) with `project_id`
shipped as a plain column on `task`/`adr` and NO foreign key (the FK is added in
A0's `003_project_registry`), implements the ledger CRUD surface as methods on
the concrete `MariaStorageEngine`, establishes least-privilege MariaDB grants
in place of SurrealDB `PERMISSIONS` (D19), adds a no-op scope-filter hook (D17),
and adds the new AST guard `scripts/check_ledger_chokepoint.py` with an
allowlist for pre-existing violations (D20).

`runtime_config` is the first revision in this chain (task #0119). Its schema
ALREADY EXISTS as `0001_config_table.py` (verified, schema-only, zero rows —
see §2). Car A's runtime_config work is therefore the chain ordering — `002`
descends from `0001_config` — NOT re-creating `0001`. The knob-store MOVE
proper (repoint config reads from SurrealDB `_RuntimeConfigMixin` to MariaDB,
boot re-sync of `default_value`, float acceptance, deprecate
`_RuntimeConfigMixin`) is the knob train's job (§16.9 item 2) and is OUT of
scope here (§8) — schema exists, the move does not.

**Architecture note (load-bearing).** The master plan §7 row and D20 name
`_LedgerMixin` as the chokepoint surface. Observed repo state supersedes that
name: the engine-#2 bootstrap (ADR-0195, shipped in the strict-typing train)
deliberately REMOVED the mixin approach. `yadgar/_shared/storage/sql/mariadb.py:11-15`
states verbatim why: PR #32 died because a MariaDB `_LedgerMixin` sat behind
SurrealDB's `_RuntimeConfigMixin` in the `StorageEngine` MRO, SurrealDB silently
won every `set_config_row` call, and the MariaDB half was dead code with green
tests. The two engines are now CONCRETE classes with no shared base class or
mixin list. D20's INTENT survives (one chokepoint, every row access through a
sanctioned surface, mechanically enforced) but the VEHICLE is `MariaStorageEngine`'s
ledger methods, NOT a mixin. The AST guard enforces that all row access goes
through those methods. This doc reflects the shipped architecture; re-introducing
a `_LedgerMixin` into the MRO would re-introduce the exact bug PR #32 died from.
See §7 and §9.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/storage/sql/migrations/versions/0001_config_table.py` | EXISTING chain head — NO change; cite as `down_revision` of `002`. revision=`"0001_config"`, `down_revision=None`, creates `config` table (key PK, value, default_value, updated_at), schema-only zero rows. | yes — `0001_config_table.py:49-50` |
| `yadgar/_shared/storage/sql/migrations/versions/002_ledger_tables.py` | NEW. Creates 7 tables (see §3). `revision="002_ledger_tables"`, `down_revision="0001_config"`. SCHEMA ONLY — no `INSERT` (the zero-rows gate, see §5). | dir verified `yadgar/_shared/storage/sql/migrations/versions/` exists with `0001_config_table.py` |
| `yadgar/_shared/storage/sql/mariadb.py` | ADD ledger CRUD methods to `MariaStorageEngine` (class at `mariadb.py:85`). Today has `verify`, `list_tables` (`:174`), `count_rows` (`:193`), `dispose` (`:214`), NO ledger methods. | `mariadb.py:85,174,193,214` |
| `yadgar/_shared/storage/sql/migrate.py` | NO change expected. `upgrade_to_head` (`:126`), `current_revision` (`:145`), `render_sql` (`:87`), `heads` (`:81`) already drive the chain programmatically. Car A's `002` is picked up automatically. | `migrate.py:87,126,145` |
| `yadgar/_shared/storage/sql/migrations/env.py` | NO change. `target_metadata` is None (no autogen); revisions hand-written. | `env.py:1-30` (docstring) |
| `scripts/check_ledger_chokepoint.py` | NEW. AST guard enforcing D20 — all row access to `task`/`adr`/`agent_pattern`/`agent_discipline` goes through `MariaStorageEngine` ledger methods. Allowlist for pre-existing violations. | `scripts/` dir exists (30+ `check_*.py` siblings); file confirmed absent (greenfield) |
| `yadgar/tests/scripts/test_check_ledger_chokepoint.py` | NEW. Self-test for the guard (mirrors `test_check_dynamic_span_names.py` pattern). | `yadgar/tests/scripts/` exists |
| `yadgar/tests/_shared/test_mariadb_migrations.py` | EXTEND — assert `002` renders, descends from `0001_config`, and the chain renders ZERO `INSERT` (the existing zero-rows gate must cover `002` too). | file exists |
| least-privilege grants (D19) | [VERIFY: exact grant script location]. Account bootstrap lives in `entrypoint-backend.sh` `_bootstrap_mariadb_accounts` (cited at `yadgar/_shared/storage/sql/config.py:17-20`); the app user is `yadgar_app` (`MARIADB_APP_USER`, verified at `yadgar/tests/_shared/test_mariadb_option_file.py:31`). Car A scopes this user's GRANTs to the ledger schema rather than omnipotent. | `config.py:17-20`; grant command shape at `yadgar/tests/integration/test_mariadb_restore_arm.py:72-73` |
| no-op scope-filter hook (D17) | NEW. [VERIFY: module home — likely `yadgar/_shared/storage/sql/mariadb.py` as a method on `MariaStorageEngine`, or a small `yadgar/_shared/storage/sql/scope_filter.py`]. A no-op hook today; tenancy columns are deferred (D17 retired the columns in §14.1). | [VERIFY] |
| [VERIFY: `yadgar/core/server.json` — path not found at HEAD; the real file is `server.json` at repo root (synced from `pyproject.toml` via `scripts/sync_version.py`)] | bump `backend_version` (Car A touches `yadgar/backend`-adjacent storage + grants; `BACKEND_BUILD_DIRS=("backend",)` per `scripts/check_backend_bump.py:44`). | `check_backend_bump.py:44,51`; real file `server.json` (repo root) |

## 3. Functions / symbols

Ledger CRUD methods to add to `MariaStorageEngine`
(`yadgar/_shared/storage/sql/mariadb.py:85`). These are `async def` (the engine
is async — `create_async_engine`, `mariadb.py:113`; car B made admin-op dispatch
async-capable). Signatures follow the per-entity-tool principle (D1) — NOT a
generic `record_query`/`record_write`. Final names [VERIFY at build time against
the consuming cars B/D/F/I, which own the ops that call these]:

```python
# task
async def create_task_row(self, *, project_id: str, title: str, status: str = "pending",
                          state: str | None = "open", active_form: str | None = None,
                          plan_path: str | None = None, body_slug: str | None = None) -> dict
async def list_task_rows(self, *, project_id: str, status: str | None = None) -> list[dict]
async def list_task_rows_all_projects(self, *, status: str | None = None) -> list[dict]
async def get_task_row(self, task_id: int) -> dict | None
async def update_task_row(self, task_id: int, **fields) -> None
async def set_task_body_slug(self, task_id: int, body_slug: str) -> None
# task_blocked_by
async def add_task_blocked_by(self, task_id: int, blocked_by_id: int) -> None
async def list_task_blocked_by(self, task_id: int) -> list[int]
# adr
async def create_adr_row(self, *, project_id: str, title: str, status: str = "open",
                         decided_on: str | None = None, subsystem: str | None = None,
                         tier: str | None = None, body_slug: str | None = None) -> dict
async def list_adr_rows(self, *, project_id: str, status: str | None = None) -> list[dict]
async def get_adr_row(self, adr_id: int) -> dict | None
async def set_adr_body_slug(self, adr_id: int, body_slug: str) -> None
# adr_supersedes
async def add_adr_supersedes(self, adr_id: int, supersedes_id: int) -> None
# agent_pattern
async def save_agent_prompt(self, *, name: str, body_slug: str, purpose: str | None = None,
                            status: str = "active", baseline_hash: str | None = None,
                            content_hash: str) -> dict  # upsert by name (PR #32 Fix 8)
async def list_agent_prompt_rows(self) -> list[dict]
async def get_agent_prompt_row(self, name: str) -> dict | None
async def increment_agent_prompt_uses(self, name: str) -> None  # atomic SET uses=uses+1 (D40)
# agent_discipline
async def save_agent_discipline(self, *, name: str, body_slug: str, purpose: str | None = None,
                                always_applied: bool = False, position: int = 0,
                                status: str = "active", baseline_hash: str | None = None,
                                content_hash: str) -> dict
async def list_agent_discipline_rows(self) -> list[dict]
# agent_pattern_composes
async def set_pattern_composes(self, pattern_name: str,
                              composes: list[tuple[str, int]]) -> None  # (discipline_name, position)
# scope-filter hook (D17) — no-op today
def apply_scope_filter(self, query, *, project_id: str | None) -> object: ...  # returns query unchanged
```

`scripts/check_ledger_chokepoint.py` — stdlib `ast` (mirrors
`scripts/check_dynamic_span_names.py` and `scripts/check_trace_spans.py`):

```python
def check(tree: ast.Module, allowlist: set[str]) -> list[Finding]: ...
# Rejects: any call/module-attr access that touches task/adr/agent_pattern/
# agent_discipline rows OUTSIDE MariaStorageEngine's ledger methods.
# ALLOWS: access inside MariaStorageEngine methods; the allowlist entries.
```

## 4. Build steps (TDD)

1. **RED** — extend `yadgar/tests/_shared/test_mariadb_migrations.py`: assert
   `heads()` returns exactly one head `002_ledger_tables`; assert
   `render_sql("base:head")` contains `CREATE TABLE ... task`, `... adr`,
   `... agent_pattern`, `... agent_discipline`, `... task_blocked_by`,
   `... adr_supersedes`, `... agent_pattern_composes`; assert the rendered SQL
   contains NO `INSERT` (zero-rows gate — `0001`'s docstring at
   `0001_config_table.py:6-9` makes this load-bearing for the chain). Assert
   `002`'s `down_revision == "0001_config"`. Tests fail (no `002` file).
2. **GREEN (schema)** — write `002_ledger_tables.py` with the seven
   `op.create_table` calls per §3. `project_id` on `task`/`adr` is
   `VARCHAR(255) NOT NULL` with an index, NO FK. `agent_pattern`/`agent_discipline`
   carry `content_hash CHAR(64) NOT NULL` and `baseline_hash CHAR(64) NULL`
   (§14.3 — the cross-engine desync check is a permanent stub until these
   exist). `id` columns are `BIGINT UNSIGNED AUTO_INCREMENT` PK (is the number,
   ADR-0197). Join tables get composite PKs. `mysql_engine="InnoDB"`,
   `mysql_charset="utf8mb4"`. Migration tests pass.
3. **RED** — `test_check_ledger_chokepoint.py`: plant a module that does
   `session.query(Task)` outside `MariaStorageEngine`; assert guard exits 1.
   Plant the same access inside a `MariaStorageEngine` method; assert exit 0.
   Plant an allowlisted site; assert exit 0.
4. **GREEN (guard)** — write `scripts/check_ledger_chokepoint.py` (stdlib AST,
   `node.decorator_list`/`ast.Call` scope per the `check_dynamic_span_names`
   pattern). Re-derive the pre-existing-violation allowlist by GREP against the
   CURRENT tree (the plan's D20 list — `cli/stats.py:719`,
   `hooks/prompt-recall.py:83,98`, `project.py:1381` — is STALE: those paths do
   not exist at those locations after the reorg; verified absent). Candidate
   sites to audit: `yadgar/backend/admin_exec/audit.py`,
   `yadgar/core/server/tools/audit.py`, `yadgar/backend/admin_exec/project.py`.
   [VERIFY: full allowlist re-derived at build time]
5. **RED** — ledger CRUD tests (use `render_sql` / a fixture engine or the
   `yadgar/tests/_shared/test_mariadb_engine.py` pattern). Assert
   `create_task_row` returns a dict with `id`, `list_task_rows` is keyed on
   `id` (NOT `number` — PR #32 Fix 1 / §13.2 blocker 2), upsert of an existing
   `agent_pattern.name` updates rather than violating UNIQUE (Fix 8), and
   `increment_agent_prompt_uses` is `SET uses = uses + 1` (not read-modify-write).
6. **GREEN (CRUD)** — implement the methods on `MariaStorageEngine`. Alembic
   invocation is already wired (`migrate.py:126 upgrade_to_head`); confirm the
   boot path calls it (or note if a later car wires `_init_ledger` — there is
   no `_init_ledger` in the tree today; verified absent).
7. **RED** — scope-filter hook test: `apply_scope_filter(query, project_id="x")`
   returns the query unchanged (no-op).
8. **GREEN** — add the no-op `apply_scope_filter` method.
9. **RED** — least-privilege grant test: assert the app user `yadgar_app` has
   grants scoped to the ledger tables and NOT omnipotent. [VERIFY: test shape
   against `yadgar/tests/integration/test_mariadb_restore_arm.py:72-73` grant
   pattern; may be integration-gated]
10. **GREEN** — scope grants in the account bootstrap. [VERIFY: exact file —
    `entrypoint-backend.sh` `_bootstrap_mariadb_accounts` per `config.py:17-20`]
11. **REFACTOR** — run `scripts/check_ledger_chokepoint.py` clean on the tree;
    wire into `.pre-commit-config.yaml` as `language: system` (mirror
    `check-dynamic-span-names` hook). Bump `backend_version` in
    `yadgar/core/server.json`.

## 5. Acceptance gates

- [ ] `002_ledger_tables.py` exists; `heads()` returns exactly `("002_ledger_tables",)`.
- [ ] `002` `down_revision == "0001_config"` (chain head preserved).
- [ ] `render_sql("base:head")` creates all 7 tables and ZERO `INSERT` rows.
- [ ] `task`/`adr` carry `project_id VARCHAR(255) NOT NULL` with index, NO FK (FK is A0's `003`).
- [ ] `agent_pattern`/`agent_discipline` carry `content_hash NOT NULL`, `baseline_hash NULL`.
- [ ] `id` columns are `BIGINT UNSIGNED AUTO_INCREMENT` PK (no separate `number` column — ADR-0197/§14.1).
- [ ] ledger CRUD methods live on `MariaStorageEngine`; return dicts keyed on `id` not `number`.
- [ ] `save_agent_prompt` upserts on `name` (no UNIQUE violation on second call).
- [ ] `increment_agent_prompt_uses` is atomic `SET uses = uses + 1`.
- [ ] `scripts/check_ledger_chokepoint.py` exit 0 on tree, exit 1 on planted violation.
- [ ] no-op scope-filter hook present and a no-op.
- [ ] least-privilege grants scoped to ledger schema; app user not omnipotent.
- [ ] `backend_version` bumped in `yadgar/core/server.json` (Car A touches backend-adjacent storage + grants).
- [ ] core/backend version bumped per WORKFLOW RULE — `backend_version` in `server.json` (mechanism: `scripts/check_backend_bump.py:44,51`, `BACKEND_BUILD_DIRS=("backend",)`). Core `pyproject.toml` version (5.181.0 today) bump only if core files touched; Car A scope is `backend`/`sql`/`scripts` → [VERIFY whether a core bump is triggered].
- [ ] pre-commit green (incl. new `check-ledger-chokepoint` hook).
- [ ] tests pass (`yadgar/tests/_shared/test_mariadb_migrations.py`, `test_check_ledger_chokepoint.py`, ledger CRUD tests).

## 6. Sequencing

Car A depends on **A0** (identity derivation `yadgar/core/identity.py` + the
`project` registry's alembic revision `003_project_registry`). §16.5 fixes the
alembic order: `002_ledger_tables` (Car A) ships the `project_id` COLUMN;
`003_project_registry` (A0) creates the `project` table and ADDS the FKs. "A0
precedes A" is a code-availability statement (derivation must exist when Car A
stamps `project_id`), NOT an alembic-order statement — `002` < `003` in the
chain either way.

Cars that wait on Car A: **B** (backend ops + cache — consumes ledger CRUD),
**J** (mutability, depends only on A, lands early), and transitively D, F, G,
H, I, K, E (the whole read/write train). Car A is the schema foundation.

## 7. ADRs / decisions

- **D34** — engine-#2 schema changes are Alembic revisions, NOT entries in
  `migrations.py`'s list. Two systems stay separate; `0001_config` (exists) is
  the MariaDB chain head, `002_ledger_tables` descends from it. The SurrealDB
  chain (`_MigrationsMixin._run_migrations`, `migrations.py`) is untouched.
- **D19** — SurrealDB `PERMISSIONS` does not port; the equivalent is a MariaDB
  dedicated least-privilege user (`yadgar_app`) with GRANTs scoped to the
  ledger schema. Intent (connection not omnipotent by default) survives.
- **D17** — `owner_kind`/`owner_id`/`reach` columns RETIRED (§14.1). Car A ships
  a no-op scope-filter HOOK instead. The hook is the guard against a
  single-tenant query shape baking in; the columns are deferred until a
  consumer exists.
- **D20** — one chokepoint, every row access through a sanctioned surface,
  AST-enforced. NOTE: the plan names `_LedgerMixin` as the surface; observed
  repo state (`mariadb.py:11-15`) deliberately killed the mixin (PR #32 MRO
  bug). The surface is `MariaStorageEngine`'s ledger methods; the guard
  (`check_ledger_chokepoint.py`) enforces that. Re-introducing a mixin would
  re-introduce the PR #32 bug — do not.
- **D1** — per-entity tools over a generic record interface. Ledger methods
  are per-entity (`create_task_row`, `create_adr_row`, `save_agent_prompt`).
- **ADR-0197** — `id` (AUTO_INCREMENT PK) IS the number; no separate `number`
  column. `number`/`MAX+1 FOR UPDATE`/`UNIQUE(project_id,origin,number)` all
  RETIRED (§14.1). Return shapes keyed on `id`, not `number` (PR #32 §13.2
  blocker 2).
- **D40** — `uses` is a plain SQL integer; `increment_*_uses` is atomic
  `SET uses = uses + 1`; `SELECT ... ORDER BY uses DESC` is the reader.
- **ADR-0209 / §14.3** — `content_hash NOT NULL` + `baseline_hash NULL` ship on
  `agent_pattern`/`agent_discipline` in `002` (not deferred). Without them
  `check_page_row_desync` is a permanent stub and the cross-engine invariant
  check turns into a vacuous pass — the exact failure the arm exists to prevent.

## 8. Out of scope

- **The knob-store MOVE (task #0119 proper)** — repointing config reads from
  SurrealDB `_RuntimeConfigMixin` to MariaDB, boot re-sync of `default_value`,
  float acceptance, deprecating `_RuntimeConfigMixin`. Schema (`0001`) exists;
  the move is the knob train (§16.9 item 2). [VERIFY: which car in this train
  owns the move — §7 row's parenthetical is ambiguous]
- **`agent_pattern_model` + `client` table (task 0094)** — §16.9 item 5, a
  separate train item. Not in `002`; its FK target `client.name` is created in
  its own revision.
- **`project` registry table + FKs** — Car A0 (`003_project_registry`). `002`
  ships the `project_id` column only.
- **Seeding any rows** — D35a: the seed is a separate one-shot admin op, NOT a
  migration step. `002` is schema-only, zero rows (same rule as `0001`).
- **Backend PTC / version piggyback / core moved to HTTP forwards** — §15,
  Car B/s later. Car A only provides the schema + CRUD surface.
- **Tools (task_list/adr_add/etc.) repointing** — cars D, F, I. Car A is the
  storage layer they consume.
- **`check_invariants` cross-engine desync assertion going live** — task 0136
  owns the assertion; Car A ships the `content_hash` columns it needs.

## 9. Risks / open questions

- **[VERIFY] `_LedgerMixin` vs `MariaStorageEngine`.** The master plan §7 row
  and D20 name `_LedgerMixin`; the shipped engine-#2 architecture
  (`mariadb.py:11-15`, CHANGELOG) deliberately removed mixins and makes that
  failure unrepresentable. This doc follows observed state (methods on
  `MariaStorageEngine`). If the plan is later re-amended to restore a mixin, it
  MUST NOT re-enter the `StorageEngine` MRO behind `_RuntimeConfigMixin` (the
  PR #32 bug). Resolution needed from the user: confirm the no-mixin vehicle is
  authoritative for Car A, or amend D20 to name the mixin explicitly with an
  MRO-safe placement.
- **[VERIFY] D20 allowlist.** The plan's pre-existing-violation list
  (`cli/stats.py:719`, `hooks/prompt-recall.py:83,98`, `project.py:1381`) is
  STALE — those paths do not exist in the current tree (verified absent). The
  allowlist must be re-derived by grep at build time. Candidate sites:
  `yadgar/backend/admin_exec/audit.py`, `yadgar/core/server/tools/audit.py`,
  `yadgar/backend/admin_exec/project.py`.
- **[VERIFY] scope-filter hook home.** Whether `apply_scope_filter` is a method
  on `MariaStorageEngine` or a standalone module (`yadgar/_shared/storage/sql/scope_filter.py`)
  is not fixed by the plan. Decide at build time; a method keeps the chokepoint
  in one class.
- **[VERIFY] least-privilege grant mechanism.** The app user `yadgar_app` is
  bootstrapped in `entrypoint-backend.sh` `_bootstrap_mariadb_accounts`
  (`config.py:17-20`). Scoping its GRANTs to the ledger tables (vs the current
  broader grant) must not break the `config` table reads/writes the knob train
  depends on. Integration-test shape follows `test_mariadb_restore_arm.py:72-73`.
- **[VERIFY] core version bump.** `check_version_bump.py` requires a core
  (`pyproject.toml`) bump when yadgar files change and the version matches the
  latest tag. Car A's files are under `yadgar/_shared/storage/sql/` and
  `scripts/` — [VERIFY whether `check_version_bump.py`'s `yadgar_files` glob
  covers `_shared`, triggering a core bump alongside the backend bump].
- **[VERIFY] `_init_ledger` wiring.** No `_init_ledger` exists in the tree today
  (verified). The boot path must invoke `migrate.upgrade_to_head(engine)`
  (`migrate.py:126`) for `002` to apply. If the engine-#2 bootstrap already
  calls this on boot, Car A needs no wiring change; if not, a boot-path call is
  required (and may belong to Car B). Confirm against the boot sequence.
- **ORM query detection.** `env.py` sets `target_metadata=None` (no ORM
  models). PR #32 Fix 7 added ORM-query detection to a `check_ledger_chokepoint`
  that no longer exists. Car A's guard must decide whether to detect
  `session.query(...)` calls or only raw-SQL/access patterns given there are no
  ORM models yet. [VERIFY at build time — likely text/AST pattern matching on
  table names, mirroring `check_dynamic_span_names.py`'s decorator-only scope.]
- **`status`+`state` nonsense combinations on `task`.** §14.3 "still open":
  `completed` + `state=planned` is not rejected. Tolerate or add a CHECK —
  decided when Car D (task tools) lands, not a Car A blocker. Car A ships the
  columns per §3.4/§16.10 (`state` NULLABLE, cleared on completion).
