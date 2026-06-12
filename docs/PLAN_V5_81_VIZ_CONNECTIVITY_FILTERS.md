# PLAN v5.81.0 — Viz connectivity visibility + filters + physics reheat (SKELETON)

Status: **SKELETON — parked, low priority, post-5.80** (user-deferred 2026-06-12). Follow-up polish on v5.54.3 (viz fidelity / all-edges). Not urgent. Needs scoping before build.

## Origin

User feedback on the deployed v5.54.3 viz ("nicer now, but…"). v5.54.3 shipped all-edges-toggleable + role-styling + lazy + (claimed) physics-reheat-on-toggle, but live testing found gaps. Tracked here per I27 (user-visible issues land in a plan).

## Items (problem statements — scope later)

1. **Better connectivity visibility — "show what is actually connected."** The headline want. v5.54.3 renders the real entity graph, but it's still hard to see a given node's actual connections. Ideas to scope: hover/select → highlight + isolate the selected node's neighborhood (fade the rest); connection-count badge per node; a "focus mode" that pins a node + shows only its edges; directional emphasis.

2. **Edge toggle does NOT reheat the physics engine (BUG).** v5.54.3 wired `d3ReheatSimulation()` guarded by `linksChanged`, but live: ticking edge types on/off does NOT re-layout — nodes stay put, layout goes stale. The reheat isn't firing on toggle (guard too strict, or links removed from render but not from `graphData()`, or reheat called on the wrong graph instance). Verify + fix so edge toggles reheat.

3. **Node-type filter incomplete — can only toggle `wiki`, not `memory`/`entity`.** Currently the only node-type filter is wiki (legacy). Add per-node-type toggles: `memory`, `wiki`, `entity` (mirror the per-edge-type toggle pattern from v5.54.3). Turning off a node type hides those nodes (+ their edges) and reheats.

4. **Node selection on/off does NOT reheat physics.** Selecting/deselecting a node doesn't re-layout. If selection drives a focus/isolate behavior (item 1), it should reheat (or animate focus). Wire selection → reheat/focus.

5. **Hold-click dims floating panels but they snap back while still holding (BUG).** Holding a click (drag-start) on the graph briefly dims the floating overlay panels, then they immediately return to full opacity even though the click is still held. Intended: panels stay dimmed for the duration of the hold/drag (so the graph is readable while dragging). Fix the dim-during-interaction state to persist while the mouse button is down (mousedown→mouseup, not a transient flash).

6. **Cluster panel: needs max-height + scroll.** The cluster/connections panel renders an unbounded "loooong list." Cap its height (max-height) + make it scrollable (overflow-y:auto) so it doesn't run off-screen.

7. **Better Info tab.** The Info tab (author card / bio / logo, added v5.50.7) needs improvement. Vague — scope later: what content/layout is wanted (richer bio? stats? links? project overview? live daemon info?). Capture concrete wants before building.

## Common thread
Items 2 + 4 are the same root: **physics reheat isn't reliably triggered on viz state changes** (edge toggle, node-type filter, selection). v5.54.3's reheat-on-toggle likely has a guard/instance bug. Items 1 + 3 are missing filter/focus UX. Scope as one viz-polish pass.

## Notes for scoping
- Where: `yadgar/static/index.html` (toggles, selection, reheat) + `yadgar/static/viz_filters.js` (the v5.54.3 filter module) + the edge-legend overlay.
- Confirm `graphData()` is actually mutated (nodes/links removed) on toggle, not just visually hidden — d3-force only reheats meaningfully when the link/node set changes.
- Reuse v5.54.3's `EDGE_TYPES`/role infra; add a parallel node-type registry.
- JS tests run from `viz-tests/` (NOT repo root).
- Physics-reheat UX choice already decided in PLAN_V5_54 (gentle reheat vs freeze+button) — apply consistently.

## Effort (guess)
S–M. Mostly frontend. No backend/schema/migration. Bundle items 1-4 as one viz-polish release when picked up.
