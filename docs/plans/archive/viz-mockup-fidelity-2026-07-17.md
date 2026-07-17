# Plan: viz mockup-fidelity train (galaxy view + config panel + traces fix)

Date: 2026-07-17 · Status: SHIPPED (v5.147.0, feat/viz-mockup-fidelity) · ADR-0135 · Branch feat/viz-mockup-fidelity → one PR.
Full-auto build to a green ready-to-merge PR (no auto-merge/deploy). NO browser harness →
visual fidelity is the user's iterative smoke-check. Mockups = visual source of truth.

## Surface 1 — GALAXY VIEW (new raw-Three.js render mode) [BIG]
Port `docs/plans/viz-galaxy.mockup.html`'s ACTUAL scene as a third render mode. #209 only
ported positions into 3d-force-graph (no glow/halos/starfield/rotate/theme) — retire that.

- **NEW `yadgar/core/static/galaxy-view.js`** (ES module, traces-tab.js pattern):
  `initGalaxyView(container, deps)` / `destroyGalaxyView()`, exposed via
  `window._galaxyView = {mount, destroy, setVisible, patchHeat, relayout}`. Auto-served by
  viz_server.py (no server change). Import in the module block (index.html ~4395).
- **Reuse the already-loaded `window.THREE` r0.158** — do NOT load the mockup's r0.160 (2nd
  Three global clobbers WebGL). All mockup APIs exist in r0.158 (no EffectComposer/UnrealBloom;
  custom MiniOrbit; glow = AdditiveBlending radial-gradient CanvasTexture sprite + 2 billboard
  Sprite core-glows). Lift verbatim: `makeSprite`, `pointMat` ShaderMaterial, `heatColor` ramp,
  TYPE_TINT, size formula, dual core-glow, 900-star starfield, MiniOrbit auto-rotate, FogExp2,
  faint intra-arm LineSegments, `layout()`+`expRadius()`.
- **CLIENT-SIDE `layout()` recompute** (port verbatim) so the live controls drive the shape —
  the missing experience. Server x/y/z feed FG warm-seed only; galaxy scene ignores them.
- **Data map** (`/api/graph` → scene): NO cluster_id on nodes → derive from
  `clusters[].member_node_ids` (member_count≥2 = arm; else core/single). heat is [0,∞) →
  NORMALIZE to [0,1]; WIKI no heat (color by type); ENTITY no age (age fallback 0.5). Build one
  stable `nodes[]` + `idToIndex` backbone (picking + filter size=0 + heat-patch share it).
  Allocate buffers to actual N (~6800), dispose+rebuild on reload.
- **graph-null routing (TOP RISK)**: tearing down FG sets global `graph=null`; 46 `graph.*`
  sites in index.html. Add `_renderMode` guard; fan out `applyFilters` (node __visible →
  `_galaxyView.setVisible` per-vertex size=0), SSE `heat_updated` (galaxy → patchHeat or defer),
  `loadGraph` (galaxy → mount/relayout). Grep `\bgraph\.`, tag every site guarded/route/skip
  (put the table in the PR).
- **Teardown (2nd risk, ~16 WebGL-context ceiling)**: cancelAF, dispose all geo/mat/textures +
  both core-glow sprites + starfield + edges, `renderer.dispose()+forceContextLoss()`,
  removeEventListener (named bound handlers), container.innerHTML=''.
- **Toggle**: reuse `_layoutModePref`; `galaxy-btn` → mount/teardown; grey `mode-btn` (2D/3D) in
  galaxy. **Picking**: THREE.Raycaster(Points) → idToIndex → existing `showDetail(node)`.
  **Filters**: Memory/Entity/Wiki → setVisible; edge-type toggles = no-op on decorative edges.
  **Controls**: port the mockup right-panel as galaxy-only (arms/pitch/thickness/core-density/
  bulge/rotate LIVE → mutate P + layout(), DEBOUNCE slider input; radmode/loose/z-layer/edges
  seg-toggles; reset). Legend+HUD galaxy-specific. **RAF into existing idle-pause** (cancelAF on
  tab-away — the "high CPU idle" complaint). Panel + #atmos gated to galaxy mode; FG overlays
  hidden in galaxy. **Defer v1** (state, don't drop): galaxy SSE live-heat-patch + search-highlight.
- **Tests**: NEW `galaxy-view.test.js` vitest — pure fns (heatColor boundaries + heat
  normalization, payload→node-model incl single/loose derivation, cluster→arm, layout() ranges +
  determinism, idToIndex). Add to `tests/backend/test_viz_static_assets.py` allowlist. Render/
  picking/teardown/controls = user smoke-check (toggle Galaxy↔Force↔2D repeatedly for leaks).

## Surface 2 — CONFIG PANEL (restyle to mockup) [MEDIUM]
`viz-config-control-panel.mockup.html` = neural-console 3-column. Panel is ALREADY a 2-col
Chrome-style redesign (v5.89) — RESTYLE, don't rewrite; PRESERVE all P1-P4 + actions/restart
(decided: do NOT drop function to match the mockup's editor-only view). No backend change —
`GET /api/control/config` already returns category/destructive/enum_choices/source/reload.

- **index.html** config CSS block (~724-994): replace with neural-console tokens (amber→coral
  heat, teal, red; scope under `#tab-control` so it doesn't leak to the phosphor-green siblings)
  + 3-col grid (rail | content | tray) + card/toggle/slider/badge/pill/tray styles + grid/grain
  atmosphere scoped to the tab.
- **control.js**: restructure `_buildShell()` 2-col→3-col; pending-BAR → commit-TRAY
  (`renderTray()`); per-category pending badges; header status line; **arm UX** typed-name →
  button + "expires in Ns" countdown (POST still carries `armed:true`). Reuse ALL handlers
  (applyOne/handleApply/handleDiscard/handleRestart/_fireAction/_renderRestartSection/_buildControl).
- **control_helpers.js**: NEW pure helpers `categoryPendingCounts`, `pendingDiffs`,
  `armCountdown`, extend `formatConfigStatus`. TDD (vitest first).
- **Fonts**: vendor Fraunces + IBM Plex Mono WOFF2 to `core/static/lib/` (no CDN/SRI — matches
  viz-theme.css). **Tests**: new helpers → vitest; update `control.test.js` DOM-wiring block
  (bar→tray selectors, arm-UX rewrite — assert POST still `armed:true`); render → smoke-check.

## Surface 3 — TRACES FIX (1-line) [TRIVIAL]
`yadgar/core/server/routes/traces.py:164` TraceQL `{ name =~ "tool\..*" }` → Tempo rejects `\.`
(HTTP 400) → empty tab. Fix → `r'{ name =~ "tool\\..*" }'`. Verified live (bad→400, good→200,
17 hits; `tool.*` spans exist as children of POST /mcp). Add a `test_traces_api_contract.py`
guard: capture the `q` param (assert `tool\\..*` present, `tool\..*` absent) + a 400→[] degrade.

## Version / housekeeping
Core change (frontend) → bump core; traces.py is core too. Backend UNCHANGED → NO backend bump.
Bump core 5.146.0 → **5.147.0** (pyproject + server.json; sync-version cascades flake/compose/
uv.lock; do NOT re-bump per gate). `pre-commit run --all-files` exit 0 before push (Validate).
I32 CAPABILITY_REGISTRY for galaxy-view + galaxy controls; CHANGELOG [Unreleased] entry. No new
`except (A,B):` without `# fmt: skip`.

## Build order + sequencing
Galaxy + Config both edit index.html → serialize on the branch. Traces (traces.py) is
independent → parallel.
1. Traces fix (parallel, quick). 2. Galaxy build. 3. Config build (after galaxy's index.html).
4. Version bump + CHANGELOG + I32 + plan archive. 5. Push → PR → `pre-commit --all-files` + CI
green (single controlled runs on the co-hosted runner — no thrash; if all-11-fail see the
wedged-dind memory: restart forgejo-runners).

## Risks
graph-null routing (miss one site → viz dies on SSE/filter); WebGL teardown leaks; ragged
per-type payload (heat/age gaps); no browser harness → the whole visual layer is user smoke-check
(build faithfully, mark reasoned-not-seen). Config arm-UX rewrite touches behavior (keep armed:true).
