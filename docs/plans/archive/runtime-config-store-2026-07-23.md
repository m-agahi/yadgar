> ARCHIVED 2026-07-23 — executed on feat/code-graph, ships with this PR

# Plan — DB-backed runtime config store (folded into feat/code-graph)

**Date:** 2026-07-23
**Status:** DESIGN — building on feat/code-graph (PR #227 stays open until this lands)
**ADR:** ADR-0163
**Task:** #34 (store) · #35 (flag survey, follow-up)

## Goal

Replace code_graph's env-only `CODE_GRAPH_ENABLED` + `.code-graph-disable` repo file with a
proper DB-backed, directory-aware, cached runtime-config store. General (typed values, not just
bool). Fixes four gaps: persistence, repo-pollution, container-blind session-suggest, repo-blind
stop-hook opt-out.

## Verified seams (investigation 2026-07-23)

- **Core cache** `yadgar/core/cache/cache.py` — `Cache(name, max_bytes, invalidation, deep_copy, obs_tier)`; `get/put/invalidate/clear`; namespaces in `_NAMESPACE_WEIGHTS` (cache.py:115); policies `KeyFn|TTL|Manual`.
- **Closest precedent:** `_dir_branch_context` cache (ADR-0140) = DB-backed, dir-keyed, read-through core Cache with Manual+TTL invalidation and a `_forward_admin` miss-path. **Copy this shape.**
- **Dir-scoping template:** `_BlocksMixin` (`_shared/storage/blocks.py`) — `_canonical_dir(scope,directory)` (None=global, str=project), `directory IS NONE` vs `= $directory`.
- **New table:** migration (append-only `_MIGRATIONS`, `storage/migrations.py:1051`), `migration_012_memory_block_table` is the template; `_<Name>Mixin` mixed into `StorageEngine`.
- **Backend admin op:** add to `_ADMIN_OPS` (`backend/admin_exec/__init__.py:88`); core writes via `_forward_admin(op, payload)` (`tools/_forward.py:31`).
- **Warmup hook:** `core_init_engines` (`core/bootstrap/bootstrap.py`) after engines+storage live, before daemon threads.
- **Tool reg:** `@_tool(power=False)` (`core/server/_app.py:349`) in a new `tools/runtime_config.py`, imported from `core/server/__init__.py`. NOT `always_load` (ADR-0047 — reads aren't session-critical).
- **Host-side read:** stdlib `urllib` + `except Exception: pass` fail-open (`session-start-context.py:118`); daemon-down → hardcoded default.
- **Invalidation bust point:** colocate `cache.clear()` with `clear_config_caches()` (`control.py:464`).
- I25 escape: store VALUES are DB rows (not Settings) → no three-way-sync; store policy knobs = module constants (like `_DIR_BRANCH_CACHE_TTL`).

## Data model

Table `runtime_config` (SCHEMALESS): `{key: str, directory: str|None (None=global), value: <JSON: bool|int|str|list|dict>, updated_at}`. Index `(key, directory)`; uniqueness app-side (like blocks).

**Resolution** — `config_get(key, directory=None, default=None)`:
1. if `directory`: row `(key, directory)` → return its value if present;
2. else / fallback: row `(key, global)` → its value if present;
3. else: `default`.
Per-dir overrides global. → code_graph: `code_graph.enabled=true` global + `code_graph.enabled=false` at repo dir = that repo opted out. ONE key, no separate disabled marker.

## Cache (simple PTC)

- Namespace `runtime_config`, `invalidation=Manual()`, `deep_copy=True`, `obs_tier="cold"`; weight in `_NAMESPACE_WEIGHTS`.
- Cache key = `f"{key}\x00{directory or ''}"` (the REQUESTED (key,dir); resolution done in the getter).
- **PTC read:** cache.get → hit return; miss → storage resolve → cache.put → return.
- **Write:** storage set → `cache.clear()` (whole-flush; writes are rare + the set is small).
- **Warmup:** at daemon start, bulk-read all rows and cache.put each stored `(key,dir)`; per-dir fallbacks populate lazily on first miss.

## Tools + route + host client

- `config_get(key, directory=None, default=None)` → resolved value (PTC).
- `config_set(key, value, scope="global"|"project", directory=None)` → validate type, write (forward), invalidate.
- `config_list(directory=None)` → effective config rows (optional/debug).
- `config_delete(key, scope, directory)` → remove (revert to fallback/default).
- Core HTTP `GET /api/runtime-config/{key}?directory=…` → resolved JSON value.
- `yadgar/core/config_client.py` (or `_shared`) — stdlib-urllib fail-open host client: `get(key, directory, default)`; daemon down / any error → `default`.

## Cars (on feat/code-graph)

- **Car G1 — storage** (opus, TDD). Migration + `runtime_config` table + `_RuntimeConfigMixin` (get/set/list/delete rows, dir-scoped) + backend `_ADMIN_OPS` ops. Mirror blocks. Tests: global vs per-dir CRUD, resolution fallback, typed values.
- **Car G2 — cache + resolver + warmup + invalidation** (opus, TDD). `runtime_config` Cache namespace; resolver getter (per-dir→global→default); PTC; warmup in bootstrap; `cache.clear()` at the `clear_config_caches()` bust point. Tests: PTC hit/miss, per-dir override, whole-flush on write, warmup populates, fail-safe on storage error.
- **Car G3 — tools + HTTP route + host client** (opus, TDD). config_get/set/list/delete tools; `/api/runtime-config/{key}` route; stdlib fail-open host client. Tests: tool typed round-trip, route resolution, client fail-open (daemon-down → default) via mocked urlopen.
- **Car G4 — migrate code_graph to the store** (opus, TDD). `code_graph/config.py` `is_enabled(dir)`/opt-out → `config_client.get("code_graph.enabled", dir, default=False)` (per-dir override); DROP `CODE_GRAPH_ENABLED` env + `.code-graph-disable` file + `OPT_OUT_MARKER`. `setup` interactive prompt + on-enable `config_set("code_graph.enabled", true, scope=global)`. Stop-hook `_code_graph_enabled` → dir-aware host client (pass cwd into is_due; fixes the repo-blind opt-out). Session-suggest reads the store daemon-side (fixes container-blindness). Update Car A/D tests + docs (BC/CAP/CHANGELOG); note ADR-0162's flag mechanism superseded by ADR-0163.

## Dependencies

G1 → G2 → G3 → G4. Then #35 flag survey (separate). Merge PR #227 after G4.

## Risks

1. Host-side daemon round-trip latency in the stop-hook → add a short host TTL cache if measured (revisit).
2. Fail-open default MUST be safe (code_graph → false/off on daemon-down) — never crash the hook.
3. Warmup must not block daemon readiness meaningfully (small set; bounded).
4. setup interactive prompt must be non-interactive-safe (`--code-graph`/`--no-code-graph`/env → skip prompt) for CI/headless installs.
