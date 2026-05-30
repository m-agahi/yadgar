# PLAN — v5.23.0: Viz integration testing infrastructure (Playwright + API contract)

**Status:** drafted 2026-05-30 after v5.10.7→v5.10.9 viz chaos exposed a hard test-coverage gap. Plan-first per I27. Slotted v5.23.0 per `docs/VERSIONING.md` (next free minor after current pipeline through v5.22.0).

**Master at draft time:** v5.10.8 LIVE; v5.10.9 in flight (real root cause finally identified after 5 attempts).

**Sequencing:** v5.23.0. Independent of all currently-planned features. CANDIDATE for leapfrog to ship sooner — see "Why ship earlier" section.

---

## Why

Between v5.10.7 and v5.10.9, FIVE patches attempted to fix the viz rendering bug:
- v5.10.7 — introduced custom 3D mesh (Lambert material)
- v5.10.7.1 — Lambert → Basic material
- v5.10.7.2 — conditional `transparent` flag
- v5.10.7.3 — full revert to ForceGraph3D defaults
- v5.10.8 — tick-count guard + mesh-leak hack removal

ALL FIVE FAILED. Each was a careful hypothesis with a real test (regression gate on the code change). NONE caught the actual bug: the `/api/graph` payload returned 3 edges referencing `entity:*` IDs that weren't in the node list. `force-graph.min.js` crashes synchronously on those orphan refs → simulation engine never advances → everything visual cascades from one fatal error.

The user verbatim: *"proper tests would have caught this. disappointing."*

### What testing we had

| Layer | Coverage | Caught the bug? |
|---|---|---|
| Backend unit tests on graph builder logic | ~80% line coverage | No — tested isolated functions, not the combined `/api/graph` payload |
| Frontend static-asset string-match (`test_viz_static_assets.py`) | "MeshBasicMaterial present" etc. | No — string presence doesn't run JS |
| Manual smoke ("open viz + look at it") | 100% on user's screen | Yes, but post-deploy and required user time |

### What was missing

1. **API CONTRACT integrity** — a test that simply asserts `set(edge.source for edge in edges) ∪ set(edge.target for edge in edges) ⊆ set(node.id for node in nodes)` on the real, assembled payload would have caught the bug immediately. We had unit tests on the builder but no end-to-end shape check.

2. **HEADLESS BROWSER smoke** — Playwright (or Puppeteer) can load the actual viz, wait for graph to render, query DOM, capture screenshots, scrape `window` state, AND catch JS console errors. A single test that just checks "no `Uncaught Error` in console after page load" would have caught `node not found: entity:172` on the first deploy.

3. **JS LOGIC unit tests** — `_nodeColorFor`, `_makeNodeThreeObject` (back when it existed), filter logic — all browser-context JS that could be tested via Node + jsdom or Vitest. v5.10.7 plan said "JS unit tests not feasible (browser-only context)" — that was a cop-out.

## Goals

1. **API contract integrity test** for `/api/graph` (and any other graph endpoints). Fails CI if backend returns orphan edges, dangling node IDs, malformed types, etc.
2. **Headless browser smoke** for viz — load page, wait for graph render, assert NO uncaught JS errors, assert mesh count ≈ node count in 3D, screenshots saved as artifacts on failure.
3. **JS unit tests** for the small set of pure JS helpers in `index.html` (extract to a `.js` file to enable Vitest, or test via Node + jsdom).
4. **CI integration** — these tests run on every PR / push to master, gating deploys.
5. **Local-dev ergonomics** — `make viz-test` or `uv run pytest yadgar/tests/integration/viz/` should work without manual setup.

## Non-goals

- Replacing manual smoke entirely (still useful for visual nuance like "do shapes look right").
- Browser-compatibility matrix (only target Chrome/Chromium via Playwright; user uses Chrome/Brave per session evidence).
- Pixel-perfect visual regression (too brittle for force-laid-out graphs with random initial positions).

## Approach

### Layer 1: API contract integrity (Python)

New file `yadgar/tests/test_graph_api_contract.py`. Tests that fetch from a TEST-FIXTURE backend (or directly invoke the builder against a seeded test database), then assert structural invariants:

```python
def test_graph_api_no_orphan_edges():
    payload = build_graph_payload(test_storage)
    node_ids = {n["id"] for n in payload["nodes"]}
    edge_endpoints = set()
    for e in payload["edges"]:
        edge_endpoints.add(e["source"])
        edge_endpoints.add(e["target"])
    orphans = edge_endpoints - node_ids
    assert not orphans, f"Orphan edge endpoints: {orphans}"

def test_graph_api_node_required_fields():
    payload = build_graph_payload(test_storage)
    for n in payload["nodes"]:
        assert "id" in n
        assert "type" in n
        assert n["type"] in ("memory", "wiki", "entity")

def test_graph_api_edge_required_fields():
    # source, target, type
    ...
```

Fixture data: seed test SurrealDB with known nodes + edges, including INTENTIONALLY orphan edges (to confirm filtering works). Then run builder + verify clean payload.

Effort: ~½ day. Fast tests (no browser); run on every commit.

### Layer 2: Headless browser smoke (Playwright)

New dir `yadgar/tests/integration/viz/`:

```
yadgar/tests/integration/viz/
├── conftest.py              # spawn yadgar daemon, start viz server, configure Playwright
├── test_viz_smoke.py        # load page, no console errors, mesh count check
├── test_viz_search.py       # type query, assert nodes highlighted
└── test_viz_modes.py        # toggle 2D ↔ 3D
```

Key checks per `test_viz_smoke.py`:
- Page loads in <5s
- After 3s wait (let force layout settle):
  - `await page.evaluate("graph.scene().children.find(c => c.type === 'Group').children.length")` > 0
  - Mesh count == `allNodes.length` (no leak)
  - No "Uncaught Error" entries in captured console events
  - `_engineTickCount` > 0 (physics ran)
- Take screenshot of viz pane; save as CI artifact

Playwright dependency: add `playwright` + `pytest-playwright` to `[project.optional-dependencies] test`. CI: install browsers via `playwright install chromium`.

Effort: ~1.5 days. Mid-speed tests (browser launch ~2-3s); run on PR + nightly.

### Layer 3: JS unit tests (Vitest or jsdom)

For pure JS helpers (`_nodeColorFor`, link-filtering helper, etc.):

Option A: extract pure functions from `index.html` into `yadgar/static/viz_helpers.js`, import in `index.html` via `<script>`. Test with Vitest (Node + jsdom):
```
yadgar/static/viz_helpers.test.js
```
Tests run as part of CI; ~1s per test.

Option B: keep helpers inline; test via Python + Node subprocess (`node -e "..."`). Slower + more brittle. Not recommended.

Lean Option A. Migration cost: 1-2 days to extract + wire up Vitest.

### Layer 4: CI integration

Update CI workflow (Codeberg / GitHub Actions / wherever):
- New job: `viz-tests` (runs Layers 1–3)
- Gate deploys on this job passing
- Artifact upload on failure: console logs + screenshots

Effort: ~half day.

## Tests for the tests (meta)

- `test_layer1_catches_orphan_edges` — inject orphan edges into test fixture, assert Layer 1 fails
- `test_layer2_catches_js_error` — inject a `<script>throw new Error('test')</script>` into a viz fixture, assert Layer 2 fails
- `test_layer3_catches_helper_bug` — modify a helper to return wrong value, assert Layer 3 fails

## Acceptance

- All 3 layers green on master + on a known-good payload
- Each layer demonstrably catches the failure mode it's designed for
- CI gates pre-merge
- Documentation: `docs/VIZ_TESTING.md` explains how to run locally + interpret failures
- CHANGELOG + MIGRATION_NOTES v5.23.0 entries

## Open questions

1. **CI provider** — Codeberg vs GitHub Actions vs both? yadgar uses Codeberg as primary remote. Codeberg supports Actions (Forgejo CI). Check if Playwright works there. If not, fall back to local pre-commit + manual CI trigger.
2. **Browser install bloat** — Playwright pulls ~200 MB of Chromium. Acceptable on CI runners; not on dev laptops. Lean: `--with-deps` only when needed; skip browser install for local dev unless user opts in.
3. **Test isolation** — tests need their own yadgar daemon + storage. Use Docker compose stack OR mock storage layer. Lean mock (faster).
4. **Visual regression** — should we add `pytest-playwright`'s `expect(page).to_have_screenshot()` assertion? Force layouts are non-deterministic; pixel diff brittle. Skip for v5.23.0.

## Risks + rollback

| Risk | Mitigation |
|---|---|
| Playwright flaky in CI | Retry with exponential backoff; mark as `@pytest.mark.flaky(reruns=2)` |
| Test maintenance burden grows | Keep tests minimal; only assert on contracts + smoke checks, not pixel-perfect |
| New deps (playwright, vitest) bloat install | All in `[optional-dependencies]`; default install untouched |

Rollback: drop the new test directories. Pre-v5.23.0 manual-smoke regime returns. No production impact.

## Files to add

- `yadgar/tests/test_graph_api_contract.py` — Layer 1
- `yadgar/tests/integration/viz/conftest.py` + `test_viz_smoke.py` + `test_viz_search.py` + `test_viz_modes.py` — Layer 2
- `yadgar/static/viz_helpers.js` + `yadgar/static/viz_helpers.test.js` — Layer 3 (if extraction approach)
- `docs/VIZ_TESTING.md` — how-to doc
- CI workflow updates
- `pyproject.toml` — playwright + pytest-playwright in `[optional-dependencies.test]`
- `package.json` (new, for vitest) — if Layer 3 Option A chosen

## Effort

| Layer | Days |
|---|---|
| Layer 1 API contract | 0.5 |
| Layer 2 Playwright smoke | 1.5 |
| Layer 3 JS unit tests + extraction | 1.5 |
| Layer 4 CI integration | 0.5 |
| Documentation + smoke procedure | 0.5 |
| **Total** | **~4.5 days** |

## Why ship earlier (LEAPFROG CANDIDATE)

Current pipeline puts this at v5.23.0 — after v5.16.0 Wiki Bookmarks (which is another viz feature). That's BACKWARDS. If Wiki Bookmarks introduces new bugs, we'll repeat the v5.10.7-v5.10.9 saga.

**Recommend leapfrog to v5.16.0 slot** (push Wiki Bookmarks to v5.17.0). Test infra BEFORE more viz features. User's call.

If leapfrogged: renumber per `docs/VERSIONING.md` cascade. Wiki Bookmarks v5.16.0 → v5.17.0, Adopt-1 benchmarks v5.17.0 → v5.18.0, etc. — one-step shift.

## Cross-references

- `docs/PLAN_V5_10_7_VIZ_FIXES.md` — what we were trying to fix when this exposed the gap
- `docs/PLAN_V5_10_9_VIZ_ORPHAN_EDGE_FILTER.md` — the actual root cause this would have caught immediately
- `docs/VERSIONING.md` — slot numbering rule
- `docs/DECISIONS.md` — DECISIONS log entry candidate: "v5.10.7-v5.10.9 saga taught us viz needs integration testing. Acted on via v5.23.0 (or earlier per leapfrog)."
