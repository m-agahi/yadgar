# Car B — backend ops + cache (backend PTC, ledger/config read ops, /health reachability)

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §15 + §16)
> Status: shipped (in train `feat/spine-0047-train`, car B)
> Depends on: A (Car A ships `_LedgerMixin` + `runtime_config` MariaDB table + the Alembic chain; Car B wraps the read methods Car A exposes and builds the cache that serves them)
> Lifecycle: ADR-0081/0082 — archived on `car/B-backend-ops-cache` as the first commit of the completing branch.

## 1. Scope

§7 row B is terse: "backend ops + cache". §15 and §16.9 item 6 expand it. Car B is the **backend PTC (pass-through cache) build** that ADR-0200 calls a BUILD, not a re-route. It has four limbs:

1. **Backend PTC** — a new cache surface in `yadgar/backend/cache/` for **config + ledger reads** (NOT the existing data caches ce/embed/memory_doc/engram_slot/graph — those serve the recall pipeline and are untouched). Keyed by `(logical_key, scope_version)` so a write makes prior keys unreachable. Reuses the existing `Cache` class (`yadgar/backend/cache/cache.py:154`) and `ScopeVersions` (`yadgar/backend/cache/scope_versions.py:22`) extended with two new scope kinds.
2. **Backend read ops** — ledger/config READ op bodies added to the `_ADMIN_OPS` dispatch (`yadgar/backend/admin_exec/__init__.py:59`) so core can forward reads over HTTP via `_forward_admin` (`yadgar/core/forward.py:52`). Today `_ADMIN_OPS` carries only write ops; reads happen in-process in core via `_get_storage()` — an ADR-0078/ADR-0200 violation that Car B closes for the config surface that exists today.
3. **Piggyback envelope** — every backend response carries the current scope versions (header or envelope field, §15.2). Core compares against what it holds; a moved version makes its cached entries unreachable. Zero extra round-trips in steady state. Generalises the `(pattern, wiki epoch)` precedent at `dispatch_helper.py:67` (`_current_wiki_epoch`).
4. **`/health` ledger reachability** — the `/health` route (`yadgar/backend/embed_service/embed_service.py:775`) currently probes SurrealDB + the embedding model only. It must also surface MariaDB/ledger reachability, or a backend that cannot reach MariaDB while `/health` reports `{"status":"ok"}` is the same invisibility problem as the maintenance gate (§15.4).

Car B does NOT move `task.py`/`agent_prompts_ledger.py` onto HTTP forwards — those core tool files do not exist at master tip (Car D builds task tools, Car I builds agent_prompt tools). Car B ships the **infrastructure** (backend PTC, scope kinds, piggyback, read ops, /health) that Cars D/F/I will forward through. The ONE existing core read surface that Car B does re-route is `_runtime_config.py` (config reads via `_get_storage()` today — ADR-0200 violation).

The dead `backend/cache/ledger_cache.py` from PR #32 (§15.5) is already gone at master tip (source deleted; only `__pycache__/ledger_cache.cpython-314.pyc` remains). Car B confirms the deletion and ships the `ScopeVersions` replacement rather than repairing it.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/backend/cache/scope_versions.py:22` | Extend `ScopeVersions` with `scope_kind="config"` (scope_id = config key or sentinel `"global"`) and `scope_kind="ledger"` (scope_id = `project_id` or sentinel `"global"` for reach-global tables). No new class — the `(scope_kind, scope_id) -> int` map already admits arbitrary kinds. | `scope_versions.py:22-50` — `version(scope_kind, scope_id)`, `bump(scope_kind, scope_id)` |
| `yadgar/backend/cache/cache.py:154` | No change to the `Cache` class itself — reused as-is. New PTC namespace instances constructed via the existing `Cache(name, max_bytes, invalidation=Manual(), key_fn=..., deep_copy=True)` pattern. `code_graph` cites `Cache.get` fan_in=222. | `cache.py:154-226` — `Cache.__init__` / `get` / `put` / `invalidate` |
| `yadgar/backend/cache/cache_budgets.py` | Add `_make_config_ptc_cache` / `_make_ledger_ptc_cache` namespace factories (mirror the existing `_make_*_cache` factories) + register in `_NAMESPACE_WEIGHTS`. RAM-% byte-budget machinery reused. | [VERIFY: exact factory names + `_NAMESPACE_WEIGHTS` keys — grep `cache_budgets.py` for existing factory pattern before implementing] |
| `yadgar/backend/cache/__init__.py` | Re-export the new PTC cache accessors (getters) alongside the existing `get_ce_cache` etc. | `__init__.py` — `_EXPORTS` re-export table |
| `yadgar/backend/admin_exec/__init__.py:59` | Add READ ops to `_ADMIN_OPS`: `list_task_rows`, `get_task_row`, `list_task_rows_all_projects`, `list_adr_rows`, `get_adr_row`, `list_agent_prompt_rows` (wrapping `_LedgerMixin` methods from Car A), and `get_config_row` / `list_config_rows` (wrapping `_RuntimeConfigMixin` methods). These are the "backend ops" half of §7 row B. | `__init__.py:59-134` — `_ADMIN_OPS` dispatch table; `run_admin_op:180`, `run_admin_op_async:241` |
| `yadgar/backend/admin_exec/ledger.py` (NEW) | Backend execution bodies for the ledger read ops — undecorated `(payload: dict) -> dict` impls that call `_get_storage().list_task_rows(...)` etc. Mirrors the existing `admin_exec/runtime_config.py` shape. | [VERIFY: `_LedgerMixin` method signatures — not at master; see Car A on `feat/spine-knob-mariadb:yadgar/_shared/storage/ledger.py:286,318,361,442,476,585`] |
| `yadgar/backend/admin_exec/runtime_config.py:28` | Add `get_config_row` / `list_config_rows` read op bodies alongside the existing `runtime_config_set` (`:28`) / `runtime_config_delete` (`:46`). | `runtime_config.py:28,46` — existing write ops; `_RuntimeConfigMixin.get_config_row`/`list_config_rows` at `_shared/storage/runtime_config.py:150,171` |
| `yadgar/backend/embed_service/embed_service_routes.py:292` | `/admin` route: admit the new read ops (they go through the same `run_admin_op_async` dispatch). Optionally add the piggyback scope-versions header to the response. | `:292-318` — `admin_route` returns `AdminResponse(result=result)` |
| `yadgar/backend/embed_service/embed_service_models.py:147` | If the piggyback is an envelope field (not a header): add `scope_versions: dict = {}` to `AdminResponse` (and to the other response models — `RecallResponse`, `ReadQueryResponse`, `VizResponse`). If the piggyback is an HTTP header: no model change. §15.2 permits either. | `:147-151` — `AdminResponse(result: dict)`; `:183` — `ReadQueryResponse` |
| `yadgar/backend/embed_service/embed_service.py:775` | `/health` route: add a MariaDB/ledger reachability probe. The plan says `_ledger_healthcheck` exists at `ledger.py:96` — but `_ledger_healthcheck` is NOT at master tip (grep returns nothing; it lives on the spine branch). Wire a reachability check or delete the dead helper per §15.4. | `:775-801` — `health()` checks SurrealDB (`YADGAR_DB_URL`) + model; [VERIFY: `_ledger_healthcheck` — not confirmable at master tip] |
| `yadgar/core/server/tools/_runtime_config.py:43` | Re-route config reads from in-process `_get_storage()` onto HTTP forward via `_forward_admin("get_config_row", {...})` + core PTC keyed by `(key, scope_version)`. Fallback on backend-down-cold-PTC: `Settings` code default (§15.4). | `:43` — `from yadgar._shared.runtime.lifecycle import _get_storage`; `:22-24` docstring admits the in-process read |
| `yadgar/core/forward.py:52` | Reuse `_forward_admin` for the new read ops (same helper, same `/admin` route). No new forwarder. | `:52` — `def _forward_admin(op, payload, timeout_s=30.0)` |
| `yadgar/backend/cache/__pycache__/ledger_cache.cpython-314.pyc` | Delete the stale bytecode orphan (source already gone). | `find yadgar/backend/cache -name "ledger_cache*"` → `.pyc` only |

## 3. Functions / symbols

**ScopeVersions extension** (`yadgar/backend/cache/scope_versions.py:22`) — no signature change; the existing `version(scope_kind, scope_id)` / `bump(scope_kind, scope_id)` already admit new kinds. Car B adds two conventions:
- `scope_kind="config"`, `scope_id` = config key string (or `"__global__"` sentinel for list reads).
- `scope_kind="ledger"`, `scope_id` = `project_id` string (or `"__global__"` for reach-global tables / `list_*_all_projects`).

**Backend read op bodies** (NEW, `yadgar/backend/admin_exec/ledger.py`):
```python
def list_task_rows(payload: dict) -> dict:
    """payload: {project_id: str, status?: list[str]} -> {rows: list[dict]}"""
def get_task_row(payload: dict) -> dict:
    """payload: {id: int} -> {row: dict | None}"""
def list_task_rows_all_projects(payload: dict) -> dict:
    """payload: {status?: list[str]} -> {rows: list[dict]}"""
def list_adr_rows(payload: dict) -> dict:
    """payload: {project_id: str, status?: str} -> {rows: list[dict]}"""
def get_adr_row(payload: dict) -> dict:
    """payload: {id: int} -> {row: dict | None}"""
def list_agent_prompt_rows(payload: dict) -> dict:
    """payload: {status?: str} -> {rows: list[dict]}"""
```
[VERIFY: exact parameter shapes — `_LedgerMixin` methods are Car A scope, not yet at master; signatures derived from `feat/spine-knob-mariadb:yadgar/_shared/storage/ledger.py:286,318,361,442,476,585`]

**Config read op bodies** (`yadgar/backend/admin_exec/runtime_config.py`):
```python
def get_config_row(payload: dict) -> dict:
    """payload: {key: str, directory: str | None} -> {row: dict | None}"""
def list_config_rows(payload: dict) -> dict:
    """payload: {directory?: str | None} -> {rows: list[dict]}"""
```
`_RuntimeConfigMixin.get_config_row` (`_shared/storage/runtime_config.py:150`) and `list_config_rows` (`:171`) — verified at master.

**PTC cache factories** (`yadgar/backend/cache/cache_budgets.py`):
```python
def _make_config_ptc_cache() -> Cache: ...   # invalidation=Manual(), key_fn embeds config scope_version
def _make_ledger_ptc_cache() -> Cache: ...   # invalidation=Manual(), key_fn embeds ledger scope_version
```
[VERIFY: existing factory pattern in `cache_budgets.py` — mirror `_make_*_cache` shape before implementing]

**Piggyback** — either a response header `X-Yadgar-Scope-Versions: config=<v>,ledger=<v>` set on `/admin` (and `/recall`, `/read_query`, `/viz`) responses, OR an envelope field `scope_versions: dict[str, int]` added to the response models. §15.2 permits either; header avoids touching every response model.

**`/health`** (`yadgar/backend/embed_service/embed_service.py:775`) — add `ledger: bool` to the payload + gate 503 on it:
```python
payload = {
    "status": "ok" if (db_ok and engine_loaded and ledger_ok) else "degraded",
    "db": db_ok,
    "model": engine_loaded,
    "ledger": ledger_ok,   # NEW — MariaDB/ledger reachability
    "drainer": ...,
}
```
[VERIFY: how to probe MariaDB reachability from the backend process — `_ledger_healthcheck` is the plan's named helper but is not confirmable at master tip; likely a `SELECT 1` on the ledger engine or a `_LedgerMixin`-level ping added in Car A]

## 4. Build steps (TDD)

1. **RED** — `tests/backend/test_scope_versions_config_ledger.py`: assert `ScopeVersions().bump("config", "seq_batch")` then `version("config", "seq_batch") == 1`; same for `scope_kind="ledger"`, `scope_id="m-agahi/yadgar"`. Assert the two kinds share the same map but never collide on equal `scope_id` across kinds.
2. **GREEN** — no code change needed (the mechanism already admits arbitrary kinds); the test pins the two new conventions as a contract for Cars D/F/I to rely on.
3. **RED** — `tests/backend/test_admin_ledger_read_ops.py`: register the six ledger read ops in `_ADMIN_OPS`, dispatch via `run_admin_op` against a fake `_LedgerMixin` that returns canned rows. Assert each op returns `{rows: [...]}` / `{row: ...}` and that an unknown op raises `KeyError` (existing dispatch behaviour).
4. **GREEN** — add `yadgar/backend/admin_exec/ledger.py` with the six read bodies; register them in `_ADMIN_OPS` (`admin_exec/__init__.py:59`). [VERIFY: `_LedgerMixin` method names against Car A's `ledger.py` before wiring]
5. **RED** — `tests/backend/test_admin_config_read_ops.py`: `get_config_row` / `list_config_rows` op bodies dispatch to `_RuntimeConfigMixin` and return rows.
6. **GREEN** — add the two config read bodies to `admin_exec/runtime_config.py`.
7. **RED** — `tests/backend/test_ptc_cache_namespaces.py`: `_make_config_ptc_cache()` / `_make_ledger_ptc_cache()` return `Cache` instances; `put` then `get` hits; after `ScopeVersions.bump("config", key)`, the old key misses (because `key_fn` embeds the version).
8. **GREEN** — add the two factories to `cache_budgets.py` + register in `_NAMESPACE_WEIGHTS`; re-export getters from `cache/__init__.py`.
9. **RED** — `tests/backend/test_piggyback_scope_versions.py`: `/admin` response carries the current scope versions (header or envelope field); a `bump` between two calls is reflected in the second response.
10. **GREEN** — add the piggyback to the `/admin` route (and the other read routes). Decide header vs envelope field in this step (§15.2).
11. **RED** — `tests/backend/test_health_ledger.py`: `/health` returns `ledger: true` when MariaDB is reachable, `ledger: false` + 503 when not. Mock the ledger reachability probe.
12. **GREEN** — add the MariaDB reachability probe to `embed_service.py:775` `/health`. [VERIFY: probe mechanism — `_ledger_healthcheck` or `SELECT 1`]
13. **RED** — `tests/core/test_runtime_config_forward.py`: `_runtime_config.py` config reads go through `_forward_admin("get_config_row", ...)` + core PTC; backend-down + cold PTC falls back to `Settings` code default; backend-up returns the cached value and a subsequent read hits the core PTC without an HTTP call.
14. **GREEN** — re-route `_runtime_config.py:43` reads onto `_forward_admin`; key the core PTC by `(key\x00{dir}, config_scope_version)`; implement the §15.4 fallback.
15. **refactor** — delete `yadgar/backend/cache/__pycache__/ledger_cache.cpython-314.pyc` (orphan bytecode). Confirm no `ledger_cache` importers remain (`grep -rn ledger_cache yadgar/`).

## 5. Acceptance gates

- [ ] `ScopeVersions` admits `scope_kind="config"` and `scope_kind="ledger"`; tests pin the two conventions.
- [ ] `_ADMIN_OPS` dispatches the six ledger read ops + two config read ops; unknown op → `KeyError` (400).
- [ ] Backend PTC config + ledger namespaces constructed via `Cache` + `Manual` invalidation + version-in-key `key_fn`; a `ScopeVersions.bump` makes prior keys unreachable (no explicit `invalidate` call).
- [ ] Piggyback: every `/admin` (and read-route) response carries current scope versions; core sees the bump with zero extra round-trips.
- [ ] `/health` reports `ledger: bool` and gates 503 on it — a backend that cannot reach MariaDB no longer reports `{"status":"ok"}`.
- [ ] Core `_runtime_config.py` reads forward over HTTP to backend; backend-down + cold core PTC falls back to `Settings` code default (never raises out of a config read); backend-up + warm core PTC hits without an HTTP call.
- [ ] Dead `ledger_cache` bytecode orphan gone; no `ledger_cache` importers.
- [ ] core/backend version bumped per WORKFLOW RULE (new core/backend version) — [VERIFY exact bump mechanism: `scripts/sync_version.py` updates `pyproject.toml` + `server.json` + `flake.nix` + `docker-compose.yml`; backend_version vs core version distinction per the core-cache train precedent]
- [ ] pre-commit green (ruff, import-linter, I32 capability registry, I33 coverage, `check_versions`, `check_ledger_chokepoint`).
- [ ] tests pass; ADR-0078/ADR-0200 invariant holds — core touches no DB, not even via `_get_storage()` for config reads.

## 6. Sequencing

**Must merge before B:** Car A (`_LedgerMixin` + `runtime_config` MariaDB table + Alembic chain + least-privilege grants + chokepoint guard). Car B wraps `_LedgerMixin` read methods that do not exist at master tip — they are on `feat/spine-knob-mariadb:yadgar/_shared/storage/ledger.py:37`. The §13.2 blocker #1 (MRO collision: `_RuntimeConfigMixin` config CRUD resolves to SurrealDB, not MariaDB `_LedgerMixin`) must be resolved in Car A so that the config read ops Car B exposes hit MariaDB, not SurrealDB.

**Waits on B:** Cars D, E, F, H, I all list B as a dependency (§7). Concretely:
- **D** (task tools) — forwards core task reads/writes through the backend PTC + read ops Car B ships.
- **F** (ADR tools re-pointed) — forwards `adr_list`/`adr_get` through `list_adr_rows`/`get_adr_row`.
- **I** (agent_prompt table + tools) — forwards through `list_agent_prompt_rows`.
- **E** (seed + SessionStart/stop-hook rewire) — the stop-hook checkpoint reads task rows via the path Car B builds.
- **H** (tier + subsystem + rollups) — rollup reads go through the ledger PTC.

J (mutability) depends only on A and can land in parallel with B.

## 7. ADRs / decisions

- **ADR-0200** (accepted) — read path is core PTC → backend PTC → DB with version-in-key invalidation piggybacked on existing responses, never a TTL. Car B IS the build ADR-0200 names: backend PTC, scope kinds, piggyback envelope, then move core onto HTTP forwards. REJECTS TTL (guarantees a staleness window). REJECTS repairing `ledger_cache.py` (dead + structurally unable to work — `invalidate(project_id)` can't invalidate global agent_prompt rows).
- **ADR-0078** (accepted) — only backend functions touch the DB; core is HTTP-forward only. Car B closes the `_runtime_config.py` in-process `_get_storage()` read violation.
- **ADR-0053** (accepted) — per-scope version-in-key invalidation via `ScopeVersions`. Car B extends the same mechanism from data caches (slot/entity) to config + ledger scope kinds.
- **D20** — every row access goes through `_LedgerMixin`, lint-enforced by `scripts/check_ledger_chokepoint.py` (Car A). Car B's read ops call `_LedgerMixin` methods — they do not bypass the chokepoint.
- **D19** — least-privilege grants on the ledger schema (Car A). Car B's read ops run within the same privilege boundary.
- **D33** — `runtime_config` is the first Alembic revision (Car A); Car B's config read ops read that table. The MRO collision (§13.2 #1) must be resolved in Car A so config CRUD resolves to MariaDB.
- **D34** — Alembic owns MariaDB schema; Car B adds no schema (it is a cache + ops layer).

## 8. Out of scope

- Task/ADR/agent_prompt **core tool files** — Car D builds `task.py`, Car F re-points `adr_*`, Car I builds agent_prompt tools. Car B ships the infra they forward through; it does not write those tools.
- The **data caches** (ce/embed/memory_doc/engram_slot/graph) — untouched. Car B's PTC is a separate cache surface for config + ledger reads.
- **Write path caching** — never cached (§15.3). Writes forward, backend validates + writes, bumps the scope version, returns the new version. Car B does not cache writes.
- **`_ledger_healthcheck` deletion vs wiring** — §15.4 says "wire it or delete it". If the helper does not exist at Car B's base (it is not at master tip), Car B implements a fresh reachability probe; it does not resurrect a dead helper.
- **Maintenance-mode state in the PTC** — must always be read live (§15.6); Car B does not cache it.
- **The seed** (Cars E, G, I) — one-shot admin ops, not Car B.

## 9. Risks / open questions

- [VERIFY: `_LedgerMixin` read method signatures] — not at master tip; derived from `feat/spine-knob-mariadb:yadgar/_shared/storage/ledger.py:286,318,361,442,476,585`. Car A must land first so Car B wraps real methods, not the PR-#32 mock shapes (§13.2 #2: mocks returned `{"number": ...}` while real returns `{"id": ...}`).
- [VERIFY: `_ledger_healthcheck`] — §15.4 / ADR-0200 name it at `ledger.py:96` with zero callers, but grep at master tip returns nothing. Either it lives on the spine branch and Car A delivers it, or Car B implements a fresh `SELECT 1`-style probe. Resolve at Car B branch time.
- [VERIFY: piggyback shape — header vs envelope field] — §15.2 permits either. Header avoids touching every response model (`AdminResponse`, `RecallResponse`, `ReadQueryResponse`, `VizResponse`); envelope field is more visible to core but widens the diff. Decide in step 10.
- [VERIFY: `_make_config_ptc_cache` / `_make_ledger_ptc_cache` factory names + `_NAMESPACE_WEIGHTS` keys] — mirror the existing `cache_budgets.py` factory pattern; grep for `_make_*_cache` + `_NAMESPACE_WEIGHTS` before implementing to match the naming convention exactly.
- [VERIFY: exact core/backend version bump mechanism] — the core-cache train precedent (`scripts/sync_version.py` updates `pyproject.toml` + `server.json` + `flake.nix` + `docker-compose.yml`; backend_version stayed constant when only core changed). Car B touches BOTH core (`_runtime_config.py`) and backend — both versions likely bump. Confirm against WORKFLOW RULE at branch time.
- **MRO collision dependency** — if Car A does not resolve §13.2 #1 (`_RuntimeConfigMixin` config CRUD resolving to SurrealDB over MariaDB `_LedgerMixin`), Car B's config read ops will read SurrealDB, not MariaDB, defeating the knob-store move. This is a Car A acceptance gate, not Car B scope, but Car B cannot ship its config-read forward without it.
- **Read-op route choice** — read ops go through the existing `/admin` route + `run_admin_op_async` (same dispatch as writes). §15.3 says writes are never cached; reads through `/admin` are fine because the PTC caches at the core layer, not the `/admin` route. If a cleaner separation is wanted (a dedicated `/ledger_read` route), raise it at Car B branch time — but the plan does not require it.
