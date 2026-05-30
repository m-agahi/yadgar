# PLAN — v5.10.8: 3D/2D viz physics hang + mesh leak fix

**Status:** drafted 2026-05-30. Plan-first per I27. Patch on v5.10 minor train. Hotfix follow-up to v5.10.7's S2.1-S2.4 viz changes (v5.10.7 / .7.1 / .7.2 / .7.3 attempted feature work + reverts, but the underlying physics/mesh-leak bugs predated all of them).

**Master at draft time:** v5.10.7.3 LIVE; user verified BOTH 2D + 3D viz broken — nodes clumped at origin, force layout not running, 2297 meshes accumulated in 3D scene for 700 nodes.

**Sequencing:** v5.10.8 patch. Pure hotfix. Independent of all drafted plans (v5.11.0 secret-gate onward).

## Versioning note

Per `docs/VERSIONING.md` (set 2026-05-30): hotfix to v5.10.7 = patch v5.10.8. The historical v5.10.7.1 / .7.2 / .7.3 used the now-deprecated 4-digit scheme and are grandfathered. Going forward, hotfixes use single-digit patch bump.

---

## Root causes (verified live via browser DevTools console)

Console snapshot from v5.10.7.3 deployment showed:
- 700 graph nodes loaded
- 2297 Mesh children in scene Group (3.3× node count → mesh leak)
- All velocities `vx/vy/vz = 0` (force simulation never iterated)
- First nodes at position `(0, 0, 0)` or near-origin (memory at `(0, 7.94, 0)`, wiki at `(8.46, 78.94, 0.13)`)
- `cooldownTicks` reported `null`, `warmupTicks: 0`
- Backend `/api/graph` does NOT send `x/y/z/fx` coords (confirmed correct — frontend should compute)

### Bug A: `onEngineStop` auto-pin fires before physics runs

`yadgar/static/index.html:900-905`:

```javascript
.onEngineStop(() => {
  // After layout settles, pin everything so forces can't push nodes around
  for (const n of graph.graphData().nodes) {
    if (n.fx == null) { n.fx = n.x; n.fy = n.y; }
  }
})
```

If `onEngineStop` fires BEFORE the simulation has iterated (`cooldownTicks=null` or 0), each node's `n.x` is still `0` (initial). The callback then pins `fx = 0, fy = 0` permanently. Future restarts of the simulation can't move the nodes because they're pinned. Result: all 700 nodes clumped at near-origin forever.

The pinning intent is reasonable (lock layout once it stabilises), but the trigger condition is wrong — it pins on the FIRST stop event, regardless of whether the engine ever actually ran.

### Bug B: mesh accumulation in `applyFilters` reset hack

`yadgar/static/index.html:1546-1547` (inside `applyFilters`):

```javascript
graph.graphData({ nodes: [], links: [] });
setTimeout(() => graph.graphData(d), 50);
```

Empty-then-restore pattern is meant to force ForceGraph3D to re-init internal state. But the library doesn't dispose the previous Three.js Mesh objects on the empty step — they accumulate as orphan children in the scene Group. Across multiple filter applications, mesh count grows monotonically. 700 nodes → 2297 meshes observed after a few re-renders.

Symptom: many orphan Mesh objects rendered at origin (positions kept from before disposal) → overlapping spheres with Lambert material + `transparent: true + opacity: 0.75` → fragmented shard appearance (the same visual signature user has been chasing across 4 attempts).

## Goals

1. **Fix Bug A:** physics simulation actually runs; nodes spread per force layout.
2. **Fix Bug B:** mesh count == node count after any reset/re-filter cycle.
3. **2D and 3D viz both render visible, separated, force-laid-out nodes.**
4. **Preserve** existing user expectation: layout settles + stays put (no perpetual jitter); user-dragged nodes still pin.

## Non-goals

- Heat coloring on 3D (already attempted v5.10.7+; deferred until shapes work).
- Custom 3D mesh (octahedra for wiki, etc.) — three failed attempts; deferred.
- Backend layout state caching — out of scope.

## Approach

### Bug A: guard the auto-pin

Use an iteration counter. ForceGraph3D exposes `.d3AlphaTarget()` and similar; simplest robust signal: count `onEngineTick` invocations and only auto-pin if `_engineTickCount > 50` (or similar threshold). Alternative: check `d3Force('center')` alpha state.

Implementation sketch:

```javascript
let _engineTickCount = 0;
graph
  .onEngineTick(() => { _engineTickCount++; })
  .onEngineStop(() => {
    if (_engineTickCount < 50) {
      return;  // engine stopped before settling — don't pin
    }
    for (const n of graph.graphData().nodes) {
      if (n.fx == null) { n.fx = n.x; n.fy = n.y; }
    }
  });
```

50 ticks = arbitrary but well above the 0-or-few-tick scenarios. Library default cooldown is 15000; real settle takes 200-1000 ticks typically. 50 is a safe lower bound.

### Bug B: drop the empty-then-restore hack

The `graph.graphData({})` followed by `setTimeout(...graphData(d), 50)` pattern was likely added to force re-init when filters changed. ForceGraph3D supports direct re-data with `graph.graphData(newData)` — library handles diff internally + reuses meshes for surviving nodes.

Replace lines 1546-1547 with direct re-data:

```javascript
graph.graphData(d);
```

If the original reason for the hack was that filters didn't apply, that's a separate issue — investigate via test. If filters DO apply correctly with direct re-data → keep the simpler fix.

Optional belt-and-suspenders: after major filter changes, traverse the scene Group + dispose orphan meshes whose node IDs aren't in `allNodes` anymore. ForceGraph3D should handle this internally; only add if observed leaking post-Bug-B fix.

## Tests (red-first per TDD)

JS unit tests not feasible (browser-only context). Use static-asset assertions in `yadgar/tests/test_viz_static_assets.py`:

1. `test_onEngineStop_has_tick_count_guard` — assert `_engineTickCount` variable present AND `onEngineStop` body references it (or equivalent guard pattern).
2. `test_no_empty_then_restore_pattern` — assert NO `graph.graphData({ nodes: [], links: [] })` literal in code. Regression gate.
3. `test_onEngineTick_handler_present` — assert `.onEngineTick(` call exists in 3D init.

Plus manual smoke procedure documented in MIGRATION_NOTES:
1. Open `http://localhost:42069/` (hard-refresh `Ctrl+Shift+R` to bust browser cache).
2. 3D mode: nodes spread across visible volume, not clumped at origin. Scene mesh count ≈ node count (check via DevTools `graph.scene().children.find(c => c.type === 'Group').children.length`).
3. 2D mode: same — nodes laid out by force, hexagons (wiki) + circles (memory) visible with link lines.
4. Apply a filter (search, tag toggle): nodes update; mesh count stays ≈ node count (NOT 2× or 3×).

## Acceptance

- Static-asset tests green (+ regression gates)
- Manual smoke: post-deploy `Ctrl+Shift+R` shows separated nodes with visible links in both modes
- Mesh count == filtered-node count after filter cycle
- Pre-commit hooks pass
- CHANGELOG + MIGRATION_NOTES v5.10.8 entry

## Risks + rollback

| Risk | Mitigation |
|---|---|
| Threshold of 50 ticks wrong (too high → never pins; too low → still pins prematurely) | Default 50 is safe lower bound; observe in production; adjust if observed |
| Removing the empty-then-restore breaks an unintended dependency | Test all filter paths (search, tag toggle, type toggle) in manual smoke |
| Library version 3d-force-graph@1.73.0 doesn't expose `onEngineTick` | Verify via library docs; alternative: poll `graph._d3Sim.alpha()` |

Rollback: revert v5.10.8 commits → back to v5.10.7.3 (clumped origin state). Not desirable but recoverable.

## Files to modify

- `yadgar/static/index.html` — Bug A guard (~3 lines added, modify lines 900-905); Bug B hack removed (lines 1546-1547)
- `yadgar/tests/test_viz_static_assets.py` — 3 regression tests added
- `CHANGELOG.md` — v5.10.8 entry
- `MIGRATION_NOTES.md` — v5.10.8 section with manual smoke procedure
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.10.7.3 → 5.10.8

## Effort

~20 min code + tests + release artifacts. Trivial scope per fix; risk is in verifying the fix actually works visually (manual smoke required).

## Cross-references

- `docs/PLAN_V5_10_7_VIZ_FIXES.md` — original viz attempt
- `docs/PLAN_V5_10_7_3_VIZ_REVERT_TO_DEFAULTS.md` — most recent revert
- `docs/VERSIONING.md` — convention for hotfix patch numbering
- `docs/DECISIONS.md` — DECISIONS log if a deeper viz refactor is later chosen (e.g. switch off ForceGraph3D)
