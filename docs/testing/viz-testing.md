# Viz Integration Testing

v5.37.0 added three-layer integration testing for the viz dashboard to catch the class of bugs
exposed by v5.10.7–v5.10.9 (orphan edge endpoints crashing force-graph.min.js).

---

## Layers

| Layer | What it tests | Speed | Command |
|-------|--------------|-------|---------|
| 1 — API contract | `/api/graph` wire format: shape, orphan edges, required fields | ~5 s | `pytest yadgar/tests/test_graph_api_contract.py` |
| 2 — Playwright smoke | Full headless browser: page loads, no JS errors, DOM elements present | ~25 s | `pytest yadgar/tests/integration/viz/ -m integration` |
| 3 — JS unit tests | Pure helper functions (\_fmtBytes, \_fmtUptime, esc, \_linkWidth, findOrphanEdgeEndpoints) | <2 s | `cd viz-tests && npx vitest run` |

---

## Running locally

### Prerequisites

```bash
# Python deps (includes playwright + pytest-playwright):
pip install -e ".[test,ml]"

# Playwright — either install bundled Chromium:
playwright install chromium

# Or point at system Chromium (NixOS / Debian with chromium package):
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)

# Layer 3 — Node.js deps:
cd viz-tests && npm ci && cd ..
```

### Layer 1 — API contract (Python, fast)

```bash
python -m pytest yadgar/tests/test_graph_api_contract.py -v --tb=short
```

18 tests. No browser, no SurrealDB process (uses in-process engine). Runs in ~5 s.

### Layer 2 — Playwright headless smoke

```bash
# With bundled Chromium:
python -m pytest yadgar/tests/integration/viz/ -m integration -v --tb=short

# With system Chromium:
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium \
  python -m pytest yadgar/tests/integration/viz/ -m integration -v --tb=short
```

10 tests. Spawns uvicorn daemon + viz HTTP server on ephemeral ports. Launches Chromium headless.
Runs in ~25 s including browser launch + SurrealDB startup.

**Skip if Chromium unavailable:** tests auto-skip when `playwright` is not installed or no
Chromium binary is found.

### Layer 3 — JS unit tests (Vitest)

```bash
cd viz-tests
npm ci          # only needed once
npx vitest run
```

28 tests on `yadgar/static/viz_helpers.js`. No browser, no Python. Runs in <2 s.

---

## Architecture

### Layer 2 topology

```
Playwright → viz HTTP server (port B)
                ↓  /api/* proxy
             yadgar MCP daemon (uvicorn, port A)
                ↓
             SurrealDB (in-process via test tmp_path)
```

- Daemon auth disabled (`YADGAR_REQUIRE_AUTH=0`) so proxy works without token injection.
- `conftest.py` seeds 4 memories into storage after a 2 s startup wait for SurrealDB.
- `wait_until="load"` (not `"networkidle"`) — SSE connections + CDN (Three.js, force-graph) keep
  network perpetually busy, making networkidle never settle.

### Layer 3 module structure

`yadgar/static/viz_helpers.js` is an ES module imported by `index.html` via
`<script type="module" src="viz_helpers.js">`. The same file is imported by Vitest tests in
`yadgar/static/viz_helpers.test.js`. `viz-tests/vitest.config.js` points Vitest at
`../yadgar/static/**/*.test.js`.

---

## Interpreting failures

### Layer 1 failure: "Orphan edge endpoints found"

```
AssertionError: Orphan edge endpoints: {'entity:172', 'entity:39'}
```

Graph builder returned edges whose `source`/`target` IDs are not in the node list.
Root cause of v5.10.9 crash. Check `yadgar/graph_api.py` — specifically the entity node
inclusion logic (entities referenced only by edges must be added to the node list too, or
the edge must be dropped).

### Layer 1 failure: "stats key missing"

`/api/graph/stats` shape changed. Update callers and the contract test together.

### Layer 2 failure: "console errors found"

```
FAIL: no console errors found — found: ["Error: node not found: entity:172"]
```

A JS runtime error occurred during graph rendering. Layer 1 should have caught the root cause
(orphan edge). If Layer 1 passes but Layer 2 fails, the bug is in JS itself (not the API).

### Layer 2 failure: skip (playwright not installed)

```
SKIPPED: playwright not installed — skip Layer 2 smoke tests
```

Install playwright or set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`. Tests are non-blocking skips,
not failures, when browser infra is absent.

### Layer 3 failure

Standard Vitest output — function name + expected vs received. All helpers are pure functions;
failures point directly to `viz_helpers.js`.

---

## CI

The `viz-tests` job in `.forgejo/workflows/ci.yaml` runs all three layers on every PR
and version tag. It installs system Chromium (`/usr/bin/chromium`) from the Debian package
and sets `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium` to avoid the 200 MB
Playwright-bundled download.

Layer 2 uses `--reruns 1` for Playwright flakiness (browser startup race conditions).

---

## Adding new tests

- **Layer 1** — add tests to `yadgar/tests/test_graph_api_contract.py`. Keep the `_engines`
  autouse fixture; it handles init/shutdown automatically.
- **Layer 2** — add test functions to `yadgar/tests/integration/viz/test_viz_smoke.py` or
  create new files in `yadgar/tests/integration/viz/`. Use the `page_with_console` function-
  scoped fixture (captures console errors automatically).
- **Layer 3** — add helper functions to `yadgar/static/viz_helpers.js` and corresponding
  tests to `yadgar/static/viz_helpers.test.js`. Pure functions only — no DOM, no globals.
