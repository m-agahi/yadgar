# PLAN — v5.10.10: Viz polish — 2x 3D node size + auto-zoom-fit on load

> **STATUS: SHIPPED v5.10.10 (2026-05-30)**

**Status:** drafted 2026-05-30 after v5.10.9 LIVE + user-verified working. Plan-first per I27. Patch on v5.10 minor train per `docs/VERSIONING.md`.

**Master at draft time:** v5.10.9 LIVE; user verified viz works end-to-end — coloring functional, nodes spread by force layout, no console errors. Two small polish asks remain.

**Sequencing:** v5.10.10 patch.

---

## Why

User feedback post-v5.10.9 (verbatim):
> *"3d, make the nodes a bit bigger. 2x their current size. also when the page refreshes it zoomes in and not show all nodes and i have to zoom out to see them all. same zoom issue with 2d. coloring works very nicely in both 2d and 3d so that is fixed as well. dont touch :D"*

Two small bugs + explicit instruction NOT to touch coloring.

## Goals

1. **3D nodes 2x bigger.** ForceGraph3D library default `nodeRelSize` = 4. Set to 8.
2. **Auto-zoom-fit on initial load** in both 2D and 3D. After graph data lands + layout settles, call `graph.zoomToFit(...)` so all nodes are framed.
3. **Coloring untouched** — DO NOT modify `_nodeColorFor`, `_linkColor`, or any color/highlight logic.

## Non-goals

- 2D node size change. User didn't ask. Leave it.
- Custom 3D mesh / shape distinction (S2.2). Deferred per v5.10.7.3 plan.
- Entity-node assembly fix (causal edges currently filtered as orphans per v5.10.9 agent finding). Tracked separately for v5.10.11 or later.
- Cache-Control headers. Separate concern.

## Approach

### Fix 1: 3D node size 2x

`yadgar/static/index.html` 3D init block (after `ForceGraph3D()(wrap)`):

```javascript
graph = ForceGraph3D()(wrap)
  .backgroundColor('#0d1117')
  .nodeRelSize(8)            // v5.10.10: 2x default (4) for visibility
  .nodeId('id')
  ...
```

`.nodeRelSize(N)` scales the default sphere radius. Doubles render size without affecting layout coordinates.

### Fix 2: Auto-zoom-fit on initial load (both modes)

Use `graph.zoomToFit(duration_ms, padding_px)` after engine has run enough iterations to spread nodes.

Two approaches:
- **A**: call in `onEngineStop` AFTER pinning (relies on engine actually stopping post-spread)
- **B**: call after a fixed delay post-`graphData()` (simpler, doesn't depend on engine state)
- **C**: hook on `onEngineTick` once `_engineTickCount >= 50` (the same threshold from v5.10.8 — when layout is reasonably settled)

Lean **C** — uses existing v5.10.8 tick guard. Add a flag so we only fit once per data load:

```javascript
let _zoomFitDone = false;
// ... in initGraph, reset:
_zoomFitDone = false;
// ... in graph init chain, add to onEngineTick:
.onEngineTick(() => {
  _engineTickCount++;
  if (!_zoomFitDone && _engineTickCount === 80) {
    // Auto-fit once layout is mostly settled (slightly after the pin threshold of 50)
    _zoomFitDone = true;
    if (typeof graph.zoomToFit === 'function') {
      graph.zoomToFit(800, 50);   // 800ms transition, 50px padding
    }
  }
})
```

Reset `_zoomFitDone = false` in `initGraph` so 2D ↔ 3D toggle re-fits. Also reset in `loadGraph` (reload button).

ForceGraph (2D) and ForceGraph3D (3D) both expose `zoomToFit(transitionMs, padding)` per library docs. Same call signature works in both modes.

## Tests (red-first, static-asset regression gates)

`yadgar/tests/test_viz_static_assets.py`:

1. `test_nodeRelSize_set_to_8_in_3d_init` — assert `.nodeRelSize(8)` literal in 3D init block.
2. `test_zoomFitDone_flag_declared` — assert `_zoomFitDone` variable present at module scope.
3. `test_onEngineTick_calls_zoomToFit_at_threshold` — assert `onEngineTick` body references `_zoomFitDone` AND `zoomToFit`.

## Acceptance

- 3 new tests green + existing viz tests still pass
- Pre-commit hooks pass
- Manual smoke after deploy + hard-refresh:
  - 3D mode: nodes visibly larger than v5.10.9 (~2x area)
  - 3D mode: on initial load, after ~1s, view auto-fits to show all nodes (no manual zoom-out needed)
  - 2D mode: same auto-fit behavior on initial load
  - 2D ↔ 3D toggle: re-fits on each switch
  - Reload button: re-fits
- CHANGELOG + MIGRATION_NOTES v5.10.10 entry
- Coloring UNTOUCHED — `_nodeColorFor` unchanged

## Risks + rollback

| Risk | Mitigation |
|---|---|
| `zoomToFit` fires too early before layout actually spreads | Threshold 80 ticks (30 ticks past the pin threshold) — empirically enough; if not, bump to 150 in follow-up |
| `nodeRelSize(8)` makes large graphs visually crowded | User asked for 2x; if "too big" feedback, can drop to 6 in follow-up |
| Different library version expects different zoomToFit signature | Both `force-graph@1.51.4` and `3d-force-graph@1.73.0` expose `zoomToFit(ms, padding)` per docs |
| _zoomFitDone flag survives across initGraph calls (re-init pollution) | Reset in initGraph + loadGraph explicitly |

Rollback: revert v5.10.10 commits. Back to v5.10.9 (working but small + zoomed-in).

## Files to modify

- `yadgar/static/index.html` — add `.nodeRelSize(8)` in 3D init; add `_zoomFitDone` module var + reset in initGraph; extend onEngineTick to call zoomToFit at tick 80
- `yadgar/tests/test_viz_static_assets.py` — 3 regression tests
- `CHANGELOG.md` — v5.10.10 entry
- `MIGRATION_NOTES.md` — v5.10.10 section with manual smoke
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.10.9 → 5.10.10

## Effort

~20 min code + tests + release artifacts. Trivial. Same pattern as v5.10.8.

## Cross-references

- `docs/PLAN_V5_10_8_VIZ_PHYSICS_AND_MESH_LEAK_FIX.md` — introduced `_engineTickCount` we reuse
- `docs/PLAN_V5_10_9_VIZ_ORPHAN_EDGE_FILTER.md` — actual root-cause fix that made viz functional again
- `docs/PLAN_V5_23_0_VIZ_INTEGRATION_TESTING.md` — testing infra that would catch zoom regressions in future
- `docs/VERSIONING.md` — slot rule (hotfix patch on v5.10 minor train)
