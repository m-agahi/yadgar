> ARCHIVED 2026-07-16 — shipped in PR #208.

# Plan: read-only DB inspection surface (`db_inspect` / `/api/debug/read_query`)

Date: 2026-07-16 · Status: SHIPPED · Rides in PR #208 (with viz caps→0) per user (combine PRs)

## Problem
No sanctioned way to run an ad-hoc read query against SurrealDB. Core has zero DB
access (ADR-0078 end-state), and there is no read-query endpoint anywhere. Tricky
debugging (e.g. "what edges does `entity:4539` actually have", "row for memory N")
today needs `docker exec` into SurrealDB — which violates anchor #33. This adds a
compliant, safe-by-construction surface.

## Architecture (ADR-0078-compliant)
Backend executes the query; core forwards. Core NEVER touches the DB.
Safety = the query runs on a **read-only VIEWER-authed DB connection** — the DB
rejects writes regardless of query text. Parse-guard is defense-in-depth only
(SurrealQL is multi-statement: `SELECT 1; DELETE memory` defeats "starts with SELECT").

```
MCP db_inspect(query, params, limit)                    [core/server/tools]
  → POST /api/debug/read_query (core, bearer + debug-flag gated)   [core, thin forward]
    → POST /read_query (backend embed_service)                     [backend, executes]
      → _q_ro(surql, params)  via RO VIEWER httpx client           [backend storage]
        → SurrealDB (VIEWER role — writes rejected at the DB)
```

## Files to change

### Backend — RO client + executor
- `yadgar/_shared/storage/__init__.py` (~164, `_resolve_db_credentials` returns OWNER creds):
  add a SECOND credential resolver `_resolve_ro_db_credentials()` reading
  `YADGAR_RO_USER`/`YADGAR_RO_PASS` (already provisioned — see below), and build a
  lazily-initialised module-level RO httpx client (mirror the existing writer client
  construction). Do NOT reuse the writer `_q()` — it authenticates as OWNER (no RO safety).
- `yadgar/_shared/storage/client.py` (~569, generic `_q(surql, params)`):
  add `_q_ro(surql, params, *, timeout_ms, row_cap)` running on the RO client, with a
  hard row cap (default 500) applied post-fetch + the httpx timeout from `timeout_ms`.
  `@observe(tier="stage")` (I33).
- `yadgar/backend/embed_service/embed_service_routes.py` (~308, `/admin` + `/viz`
  dispatch twins): add a `POST /read_query` route. Request
  `ReadQueryRequest{query: str, params: dict = {}, timeout_ms: int = 5000}`,
  response `ReadQueryResponse{rows: list[dict], row_count: int, truncated: bool}`.
  `@observe(tier="boundary", metric="backend.read_query")`. Defense-in-depth parse-guard:
  reject if the statement text contains a write keyword (INSERT/UPDATE/DELETE/CREATE/
  DEFINE/REMOVE/RELATE/UPSERT) — 400, with a comment that the RO connection is the real guard.

### Core — thin forward
- `yadgar/core/server/routes/` NEW `debug_query.py` (model on `routes/logs.py`):
  `POST /api/debug/read_query` → forward to backend `/read_query` via the existing
  `_forward_*` admin-forward helper. No DB touch. Register via side-effect import in
  `core/server/__init__.py`.
- `yadgar/core/auth_middleware/auth_middleware.py` (~38, `_DEBUG_API_PREFIXES`):
  add `/api/debug/read_query` so it is bearer + `YADGAR_DEBUG_APIS_ENABLED` gated
  (dev-introspection, off in prod — sits with `/api/logs/*` per ADR-0013). NOT auth-only.

### MCP tool
- `yadgar/core/server/tools/` NEW tool (model on an existing read tool):
  `db_inspect(query: str, params: dict = {}, limit: int = 500) -> {rows, row_count, truncated}`
  → calls `POST /api/debug/read_query`. Register in the tool registry. Docstring must
  state: read-only (VIEWER role), debug-flag-gated, row-capped; for DB introspection only.
  I32 capability registration.

### Config
- `YADGAR_DEBUG_APIS_ENABLED` already exists (gates `/api/logs/*`) — reuse, no new gate.
- Row cap (500) + default timeout (5000ms): module constants, NOT knobs (avoid I25
  surface churn unless tuning is genuinely needed).

### Version + docs
- Backend change → bump `backend_version` 5.53.0 → **5.54.0** in BOTH `server.json` AND
  `docker-compose.yml` (the `check-backend-bump` HARD gate), **AND** `yadgar/__init__.py`
  `BACKEND_VERSION = "5.54.0"` (the drift-guard `test_v5_46_12`/`test_v5_49_2` — the exact
  trap that reddened viz-train CI; the pre-commit gate does NOT cover the Python constant).
  Core stays 5.145.1 (already bumped by the caps commit).
- `docs/CHANGELOG.md` (NOT repo root) `## [Unreleased]`: add a db_inspect entry.

### ADR (mandatory — ADR-0078 forbids silent additions)
- `adr_add` a new ADR: "Sanctioned read-only DB inspection surface (`/api/debug/read_query`
  + `db_inspect`) — backend-executes via RO VIEWER client, core-forwards, debug-flag-gated."
  Reference ADR-0078 (names this as the sanctioned debug read path its revisit_trigger
  requires) + ADR-0013 (gating). supersedes=none.

## Tests (TDD — write-rejection FIRST, it is the go/no-go)
1. **`test_read_query_viewer_rejects_writes`** (RED→GREEN, THE safety gate): over the RO
   connection, issue `UPDATE`/`DELETE`/`CREATE` → assert the DB rejects (error, no mutation).
   The entire safety claim rests on "VIEWER rejects writes" — currently only INFERRED from
   the role name. If this fails, STOP: the RO role isn't actually read-only, ship nothing.
   (Needs the backend harness with a live SurrealDB — integration-tier, not a mock.)
2. `test_read_query_returns_rows` — a SELECT returns rows; params bind; row cap truncates
   at 500 with `truncated: true`; timeout respected.
3. `test_read_query_parse_guard_rejects_write_keyword` — defense-in-depth 400 (note in the
   test it is NOT the primary guard).
4. Core forward test — `/api/debug/read_query` forwards + is debug-flag-gated (403/404 when
   `YADGAR_DEBUG_APIS_ENABLED` off; bearer required).
5. MCP `db_inspect` tool test — maps to the endpoint, row cap honored.

## Verify (env traps that burned viz-train — do NOT repeat)
- NEVER `OTEL_SDK_DISABLED=true` (→ NoOp tracer, false span fails). Use `YADGAR_OTLP_ENDPOINT=''`.
- Set `HF_HOME=/home/max/.cache/huggingface` + `HF_HUB_OFFLINE=1` (else phantom recall fails).
- Invoke pytest via a SCRIPT FILE (inline "python -m pytest" self-matches pgrep guards).
- If any new `except (A, B):` is added, append `# fmt: skip` (py314/ruff strips the parens → CI red).
- The write-rejection + read tests need a live backend/SurrealDB → run the backend-harness
  suites (or the yadgar-ci container) — a pure mock cannot prove RO safety.

## Sequencing
One commit stack on `feat/viz-caps-unlimited-default` (already has caps→0 @ 2b65ed0b):
(1) backend RO client + _q_ro + /read_query + parse-guard, (2) core forward + gating,
(3) MCP tool, (4) tests incl. write-rejection, (5) backend bump (3 places) + CHANGELOG,
(6) ADR. Push → PR #208 auto-updates. Then update #208 title/body to cover both changes.

## Risks
- **RO role might not actually reject writes** (unverified) → test #1 is the gate; if red, do not ship.
- MCP tool lets the model pull large/sensitive rows into context → row cap + timeout + debug-flag-off-in-prod bound it.
- Backend bump drift (the __init__.py BACKEND_VERSION trap) → explicitly bump all 3 places.
