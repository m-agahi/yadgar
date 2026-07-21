# Plan: make GALAXY the ONLY graph render mode + draggable node popup

Date: 2026-07-17 · Status: PROPOSED · yadgar-core (`yadgar/core/static/`, viz frontend only).

Rides **PR #212** (`feat/task-restore-forcing-nudge`), core already bumped to **5.149.0**
(commit `2dd8e129`). **NO second version bump** — this train folds into #212 (or a sibling
PR off the same branch; user's call). Backend untouched (5.55.0).

## User ask (verbatim)
> "kill the 2d and force for 3d. only graph i want is galaxy mode (so we may drop the
> galaxy button as well). also the popups are really nice when i click on nodes but would
> be nice if i could drag them across the screen as well if i want."

Task #52. Three changes:
1. Remove the 2D + force-directed (3d-force-graph) render modes entirely — galaxy always
   mounts, the FG `graph` instance is never created.
2. Drop the mode-toggle UI (Galaxy button + 2D/3D `mode-btn`) and the mode persistence.
3. Make the `#galaxy-node-popup` draggable by its header; keep click-away / × / ESC close.

## Ground truth (investigated 2026-07-17, cite index.html unless noted; index.html = 5538 lines)
Current state: `_layoutModePref` **already defaults to `'galaxy'`** (`index.html:2409`), so galaxy
is the default today, but the FG path is still fully wired and reachable via the toggle. Galaxy
and FG are **mutually exclusive** — only one owns `#canvas-wrap` + one WebGL context. `_mountGalaxy`
tears the FG down first (`2616-2630`); `toggleLayoutMode` (`3254-3281`) swaps between them.

- Render-mode state: `_graphMode` (2D/3D, `2269`), `_layoutModePref` (galaxy/force, `2409`),
  `_serverLayoutMode` (`2410`, tracks payload `layout_mode` — **not** a mode toggle, KEEP),
  `_renderMode()` (`2420`), `_isGalaxy()` (`2421`).
- FG construction: `initGraph()` (`3044-3218`) — builds `ForceGraph3D()(wrap)` (3D, `3064`) or
  `ForceGraph()(wrap)` (2D, `3129`); global `let graph = null` (`2261`).
- `graph.*` FG API: 13 distinct methods, all reached only on the non-galaxy branch
  (`graphData`, `zoomToFit`, `nodeColor`, `centerAt`, `zoom`, `cameraPosition`,
  `pause/resumeAnimation`, `nodeCanvasObject`, `cooldownTicks`, `enableZoomInteraction`,
  `d3Force`, `_destructor`, `width`).
- Galaxy API (`galaxy-view.js`, `window._galaxyView`, exports at `1319-1387`): `mount` `destroy`
  `setVisible` `patchHeat` `relayout` `pause` `resume` `resize` `isMounted` `showHalo` `hideHalo`
  `nodeScreenPos` `highlight`. **No zoomToFit / camera-fly / reset-layout** — this gap drives the
  Fit/Reset/cluster-fly decisions below. Galaxy renders its own intra-arm edges as THREE
  `LineSegments` (`_buildEdges`, `galaxy-view.js:699`, gated `P.edges` off/on) — **not** FG links.

## DELETE
FG renderer + mode toggle. All line refs `index.html` unless noted.

**HTML / buttons**
- `1552` — `<button id="mode-btn" onclick="toggleMode()">2D</button>` (2D/3D toggle) → delete.
- `1556` — `<button id="galaxy-btn" onclick="toggleLayoutMode()">✦ Galaxy</button>` → delete
  (one mode, no toggle; user explicitly OK'd dropping it).
- `1550` Fit / `1551` Reset buttons → **delete** (see "Fit/Reset/cluster-fly" below; UX-loss called out).

**Mode state + handlers**
- `_graphMode` (`2269`) + all reads (`3045/3062/3127` renderer select; `3523/3814/3929/3954`
  branches) → delete; localStorage key `'yadgar-graph-mode'` retired.
- `_renderMode()` (`2420`), `toggleMode()` (`3221-3235`), `toggleLayoutMode()` (`3254-3281`),
  `_syncGalaxyBtn` (`3242`), `_syncModeBtnDisabled` (`2638-2650`) → delete.
- `_layoutModePref` (`2409`) + localStorage `'yadgar-layout-mode'` → **delete** (was the toggle
  persistence; with one mode it is dead). NOTE: keep `_serverLayoutMode` (`2410`) — it is the
  server payload's layout, unrelated to the client toggle.

**FG construction + call sites**
- `initGraph()` (`3044-3218`) → delete entirely (both 3D and 2D branches).
- global `graph` (`2261`) + all 13 `graph.*` call sites → delete. Every site is already behind an
  `_isGalaxy()` false-branch, so removal is mechanical once `initGraph` is gone.
- FG teardown `graph._destructor` calls (`2620`, `3053`) → delete (nothing to tear down).
- `_mountGalaxy` (`2616-2630`) simplifies: drop the "tear FG down first" preamble (`2618-2622`);
  galaxy just mounts. Keep the mount body.
- `posMap` warm-start position cache (`3402`, `3414`, serialize/restore) → delete — FG-only
  (galaxy layout is deterministic server-seed + client `relayout`, no d3-force to persist).
- orphan-edge filter (`3428-3440`, "before passing to force-graph library") → delete; galaxy
  `relayout` takes `{nodes, clusters}` and derives edges itself, never consumes `allLinks`.
- `_flyToCluster` (`3514-3533`) → **delete** (FG camera API only; galaxy has no camera-fly).
  Its caller (cluster-click, `2754`) drops the fly call — cluster click still selects/highlights.

**Search-highlight FG paths**
- `_applySearchHighlight` (`3939-3966`) collapses to just the galaxy branch
  (`3943-3945`: `_galaxyView.highlight(...)`). Delete the FG 3D zoomToFit path (`3954-3955`) and
  FG 2D centerAt/zoom path (`3956-3965`).
- `_recomputeDim` / `_repaintDimState` (called `3948`) → delete (FG dim-repaint; galaxy dims
  natively inside `_galaxyView.highlight`).

**`_isGalaxy()` guards → unconditional**
- `_isGalaxy()` (`2421`) → delete the helper; every guard site becomes the galaxy branch
  unconditionally (`2944/2955` pause-resume, `3450/3461` loadGraph routing, `3549` node-click,
  `3627/3649` filter-apply, `4168` color re-apply). `loadGraph` keeps ONLY its galaxy path
  (fetch `/api/graph` → set `allNodes` → `_galaxyView.relayout`/`_mountGalaxy`).

## KEEP (unchanged or trivially de-branched)
- **galaxy-view.js** whole module + `galaxy-view.css` + the `#atmos`/starfield/core-glow scene.
- **`allNodes`** global (`2260`) — galaxy consumes it (`2610`, `2625`, `3403`, `3431`,
  visibility map). STAYS.
- `#canvas-wrap` mount target (galaxy mounts there; confirmed).
- `#galaxy-node-popup` + all popup JS (`_showGalaxyPopup` `2535-2577`, `_renderGalaxyPopupBody`
  `2489-2514`, `_fetchGalaxyPopupWiki` `2517-2532`, `_closeGalaxyPopup` `2579-2587`,
  field model `2447-2476`) — plus the drag addition below.
- `node-popup.js` (field model + `clampPopupPosition`).
- Filters / edge-toggle / node-type toggles, heat SSE (`patchHeat` route), search input.
- Traces tab (`traces-tab.js`) — independent, does NOT touch the renderer (verified: grep for
  `graph|_galaxyView|cameraPosition|flyTo|highlight|nodeColor|zoomToFit` in traces-tab.js → none).
- `heatColor` / `esc` / `_fmtBytes` / `_fmtUptime` pure helpers.
- `body.galaxy-active` class (`2641`, CSS `1426` hides `#right`) — now set unconditionally at boot
  instead of on mode-enter.

### Fit / Reset / cluster-fly — the real design decision (Hole 1)
Galaxy exposes NO `zoomToFit`, `cameraPosition`, or reset-layout. The three FG camera affordances
have no galaxy target:
- **Fit button** (`1550` → `fitGraph` `4192`) and **Reset button** (`1551` → `resetLayout`
  `4193-4205`): **DELETE both** (button + fn). Galaxy has MiniOrbit auto-rotate + its own
  interaction; a "fit" is meaningless for the fixed galactic layout. **UX loss: none material** —
  galaxy auto-frames on mount. Call it out to the user as a smoke-check item.
- **Cluster-click fly** (`_flyToCluster`, caller `2754`): DELETE the fly, keep the select/highlight.
  Adding a galaxy camera-fly would be a NEW car (out of scope for "kill FG"); default is drop.
  If the user wants cluster-fly back, that is a follow-up `_galaxyView.flyTo(id)` car.

This is the one place "make the guard unconditional" does NOT apply — there is no galaxy branch to
collapse into. Decision: delete, don't port. Flag for user sign-off in the PR.

## Draggable popup design (core deliverable)
Target: `#galaxy-node-popup` (`position: fixed`, `index.html:1428`). Drag handle = `.np-header`
(`1441-1470`), which contains `.np-type-badge` + `.np-title` + `.np-close` (× button). Popup
position is set ONCE per show (`2575-2576` `popup.style.left/top`); drag mutates the same two props.

**Mechanics** (wire ONCE, alongside the × handler at `2601-2604` — not per-show):
1. `header.addEventListener('mousedown', onDragStart)`.
2. `onDragStart(e)`: **bail if `e.target.closest('#gnp-close')`** (don't drag from ×).
   `e.preventDefault()` (kills text-selection drag-ghost). Record grab offset
   `(e.clientX - rect.left, e.clientY - rect.top)`. Add `document` `mousemove` + `mouseup`.
3. `onDragMove(e)`: compute new top-left `(e.clientX - offX, e.clientY - offY)`, clamp to viewport,
   write `popup.style.left/top`.
4. `onDragEnd()`: remove BOTH document listeners (self-cleaning — satisfies "teardown removes
   listeners"; no leak across drags). Optional `cursor: grabbing` toggle on `<body>`/header.
5. CSS: `.np-header { cursor: move; }` (`1441-1470`); `.np-close { cursor: pointer; }` stays so the
   × reads as a button not a handle.

**Click-away coexistence (state explicitly — the reviewer will ask):** the existing click-away
(`2590-2596`, `document mousedown` → if target outside popup, close) fires on mousedown. A drag-start
mousedown lands on `.np-header`, which is INSIDE the popup, so the outside-check is false → click-away
does NOT close on drag-start. No ordering hazard; no change needed to the close handler. ESC (`2597`)
and × (`2601`) unaffected.

**Clamp helper (vitest):** `clampPopupPosition(anchor, popupSize, viewport)` already exists
(`node-popup.js:152-160`) but it is an *anchor + 16px offset* clamp (for show-at-node). Drag needs a
*raw top-left* clamp. Add a sibling pure export **`clampToViewport(pos, size, viewport, margin=8)`**
in `node-popup.js` → returns `{left, top}` clamped so the popup stays fully on-screen (never
top-left off the edge, never bottom-right past `viewport - size - margin`). `_showGalaxyPopup` can
optionally reuse it, but its main consumer is `onDragMove`. Pure, no DOM → unit-testable.

## Build cars
Each car is a commit on `feat/task-restore-forcing-nudge` (no new bump). Suggest sequential —
Car A removes the machinery Car B/C's tests assert gone.

- **Car A — rip out FG renderer + mode toggle** (sonnet, mechanical delete). Everything in DELETE
  above except the popup drag. Result: galaxy mounts unconditionally at boot; `initGraph`/`graph`/
  `_isGalaxy`/`toggleMode`/`toggleLayoutMode`/`_graphMode`/`_layoutModePref`/`_flyToCluster`/
  `fitGraph`/`resetLayout`/orphan-filter/posMap gone; buttons removed; `_applySearchHighlight`
  collapsed. Boot IIFE (`~5534`) mounts galaxy directly (no `if (!_isGalaxy()) initGraph(...)`).
- **Car B — draggable popup** (sonnet). Add `clampToViewport` to `node-popup.js` + vitest;
  wire header drag in `index.html` next to the × handler; `.np-header { cursor: move }` CSS.
- **Car C — test flips** (sonnet). Update/delete per the test-change list below.

TDD note: this is frontend viz with **no browser harness** → render + drag are USER smoke-checks,
not automated. The ONE automated unit is `clampToViewport` (pure math, vitest, Car B) — write the
failing `node-popup.test.js` (or `clamp-math.test.js`) first. Static-asset grep tests (Car C) are
byte-presence assertions, not behavior; flip them to match the new source.

## Test changes (which pins flip)
Harness: `viz-tests/vitest.config.js` auto-discovers `../yadgar/core/static/**/*.test.js`
(vitest 3.2.7 + jsdom). New `node-popup.test.js` drops in directly.

**MUST-SURVIVE (galaxy pins — do not touch):**
- `yadgar/tests/server/test_viz_static_assets.py` `TestADR0135GalaxyRenderMode` subset:
  `test_galaxy_view_module_file_exists` (`538`), `test_galaxy_view_exposes_public_surface` (`545`),
  `test_galaxy_reuses_loaded_three_not_a_second_global` (`554`), `test_index_imports_galaxy_view_module`
  (`568`), `test_teardown_disposes_and_forces_context_loss` (`628`).
- `yadgar/core/static/galaxy-view.test.js` (whole file — pure galaxy math).
- `viz_helpers.test.js`: `_fmtBytes` `_fmtUptime` `esc` describes.
- `test_viz_smoke.py`: `test_heatColor_function_exists` (`156`). `test_graph_container_present`
  (`91`) survives IF galaxy keeps `#canvas-wrap` (it does — verify post-Car-A).

**MUST-FLIP (assertions pin the removed FG machinery):**
- `test_viz_static_assets.py`: the whole `TestV5506OctahedronForWiki` (`29-185`),
  `TestV5108PhysicsAndMeshLeakFix` (`252-292`), `TestV5109OrphanEdgeFilter` (`308-331`),
  `TestV51010VizPolish` (`358-398`), `TestV51011VizEdgeThicknessAndRepulsion` (`434-452`) — all pin
  ForceGraph3D-only APIs (`nodeThreeObject`, `onEngineTick`, `nodeRelSize`, `d3Force("link")`,
  `graph.graphData`, orphan filter). Delete these classes (FG is gone) OR, where a galaxy analogue
  exists, re-point (galaxy octahedra/anchor-shape live in `galaxy-view.js` `buildNodeModel`, already
  covered by `galaxy-view.test.js` — prefer DELETE the FG classes, don't duplicate).
- `TestS23SearchModeDetection` (`194-236`): asserts `_applySearchHighlight` has a `_graphMode`/
  `nodeCanvasObject` 3D branch. FLIP — the fn now has only the galaxy branch; assert it calls
  `_galaxyView.highlight` and has NO `_graphMode`.
- `TestADR0135GalaxyRenderMode` subset that pins the *mode machinery*:
  `test_render_mode_single_source_of_truth` (`577`, asserts `_renderMode`/`_isGalaxy` present),
  `test_applyFilters_does_not_bail_in_galaxy` (`599`, asserts `_isGalaxy` branch),
  `test_sse_heat_updated_guards_null_graph` (`615`), `test_boot_skips_fg_init_in_galaxy` (`622`,
  asserts `if (!_isGalaxy()) initGraph`). FLIP to the unconditional-galaxy world: no `_isGalaxy`,
  no `initGraph`, `applyFilters` routes straight to `_galaxyView.setVisible`, heat SSE →
  `_galaxyView.patchHeat`, boot mounts galaxy unconditionally.
- `test_viz_smoke.py`: `test_allNodes_global_exists` (`138`) / `test_allNodes_array_is_defined`
  (`150`) — `allNodes` SURVIVES, so these likely just survive; re-verify after Car A. Keep unless
  the global moves.

**MUST-DELETE (files pin FG-only surfaces with no galaxy analogue):**
- `yadgar/core/static/graph-node-factory.test.js` — `makeNodeThreeObject` is the FG `.nodeThreeObject`
  callback; galaxy builds meshes itself. Delete file.
- `yadgar/core/static/viz_positions.test.js` — `serializeNodePositions`/warm-start posMap; FG-only.
  Delete file.
- `viz_helpers.test.js`: `_linkWidth`, `particleCount`, `shouldFitOnStop`, `findOrphanEdgeEndpoints`,
  `convexHull` describes — all FG edge/physics/hull helpers (galaxy edges are native LineSegments,
  no convex hull, no d3 fit-on-stop). Delete these describes (or the file if nothing else remains).

**UNRELATED (leave):** `test_v5_54_3_graph_viz_fidelity.py` (edge schema), `test_viz_tab_pane_display.py`.

## Risks
- **Missed `graph.*` / `_isGalaxy` site** → a stray `graph.foo()` on the now-undefined global throws
  on an SSE tick or filter. Mitigation: after Car A, `grep -n "graph\.\|_isGalaxy\|_graphMode\|
  initGraph\|_layoutModePref\|_flyToCluster" index.html` must return only comments/none. The
  investigation enumerated all sites; the grep is the backstop.
- **Search-highlight** was already galaxy-routed in Car D and DEFERRED the FG branch — confirm the
  galaxy `highlight` still fires after the FG branch is deleted (it is the surviving branch). Smoke.
- **Trace-replay**: verified independent (traces-tab.js touches no renderer symbol) → one-line
  dismissal, no car. If a future trace-replay wants to pulse/fly a node on the galaxy it needs a
  galaxy method — out of scope here.
- **Fit/Reset/cluster-fly UX loss**: deliberate (galaxy has no camera-fly). Flag for user sign-off;
  cheap to add back later as a `_galaxyView.flyTo` follow-up car.
- **No browser harness**: render + drag correctness = user smoke-check. Only `clampToViewport` is
  automated. Accept.

## Smoke-check script (hand to user post-merge)
1. Open viz → galaxy renders immediately, no 2D/Force/Galaxy buttons, no Fit/Reset buttons.
2. Click a node → popup appears near it. Drag by the header → moves with cursor, clamps at edges.
3. Drag toward a corner → popup stays fully on-screen.
4. Click × / click away / ESC → popup closes (drag didn't break close).
5. Search a term → matching galaxy nodes brighten, rest dim.
6. Filters + edge/node-type toggles + heat still update the galaxy.
7. Traces tab still works.

## Yadgar findings
- **Confirmed observed-state vs recall drift, none material:** ADR-0135 (memory 7047) + memory
  532523 said galaxy is a THIRD render mode reached via toggle with ~46 `graph.*` null-guard sites;
  observed code has galaxy as the DEFAULT (`_layoutModePref='galaxy'` at index.html:2409) with 13
  distinct `graph.*` methods behind `_isGalaxy()` guards. This plan makes it the ONLY mode →
  guards become unconditional, `graph` global deleted. Update ADR-0135's "graph-null routing (46
  sites) is the top risk" once merged: the risk is removed, not routed.
- **`clampPopupPosition` already exists** (node-popup.js:152-160) but is an anchor+offset clamp, not
  a raw top-left clamp — drag needs a new sibling `clampToViewport`. Worth a wiki note if a future
  agent looks for "the popup clamp helper".
- **Galaxy renders its own edges** as THREE `LineSegments` (`_buildEdges`, galaxy-view.js:699,
  `P.edges` off/on) — NOT FG links. So FG `_linkWidth`/`particleCount`/`convexHull` helpers +
  their vitest are safe to delete; galaxy edges survive independently.
- **traces-tab.js is renderer-agnostic** (no `graph`/`_galaxyView`/camera refs) — trace-replay does
  not drive the graph; safe across the FG removal.
- **Version:** core 5.149.0 already bumped on `feat/task-restore-forcing-nudge` (#212); this work
  rides that PR, no second bump.
