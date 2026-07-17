# Plan: viz post-deploy fixes train (v5.147.0 smoke-check → 12 bugs)

Date: 2026-07-17 · Status: SHIPPED (v5.148.0, feat/viz-post-deploy-fixes) · Branch feat/viz-post-deploy-fixes → one PR (ADR-0088).
Source: user smoke-check of the deployed galaxy (v5.147.0). 12 bugs reported one-by-one.
RCA by 4 parallel agents (2 opus, 2 sonnet). Mockups = visual source of truth (ADR-0135).
NO browser harness → render fixes are user smoke-check; pure fns get vitest.

## Version / scope decision (READ FIRST) — REVISED post-audit (fable, 2026-07-17)
**CORE-ONLY. Bump core 5.147.0 → 5.148.0. Backend STAYS 5.55.0.** The audit proved Bug 9 is a
CORE endpoint bug (not backend recording) → NO backend change anywhere in this train. Version sync =
3 core sites only (pyproject.toml:7, server.json:10+16) + CHANGELOG; `yadgar/__init__.py:21
BACKEND_VERSION` and `server.json:11 backend_version` STAY 5.55.0.
- **Car 0 (#50) KEPT** — still pays off THIS train: ci-release.yaml fires `backend_changed=true` on ANY
  pyproject.toml change, so even a CORE version bump wastefully rebuilds backend. Landing #50 in this
  PR (workflow read from master at release time) makes this train's core bump NOT rebuild backend.

## Decision points — RESOLVED (2026-07-17)
- **D1 (#4 popup): CONFIRMED.** Build `docs/plans/viz-node-popup.mockup.html` (Memory/Wiki/Entity).
  Defaults: **anchor-to-node** position · **pulsing-halo-only** (no backdrop dim; click-away closes) ·
  **wiki auto-widen** 340→500. Replaces `#right` sidebar in galaxy mode; reuse `showDetail()`.
- **D2 (#10 search + nav): DEDICATED SEARCH TAB.** New `#tab-search` — global semantic search, results
  list with type-aware routing; REMOVE search from the graph toolbar. **Debug → moves under System.**
- **D3 (#11 debug panel): APPROVED, in this train.** 7 sections: DB-query console (POST
  /api/debug/read_query, exists) + health + stats + config + logs + SSE tail + DLQ (new ~10-line
  `GET /api/debug/dlq` wrapping dlq_inspect).
- **D4 (#7 traces): CORE HARDENING = YES.** Surface the Tempo 500 reason in the mesh payload +
  fallback-build mesh from `/api/search` spanSet when by-id fails. (Tempo infra fix = separate user
  hand-off; see MIGRATION_NOTES — Tempo research agent producing the tempo.nix fix. blocklist=56044,
  backend_worker not draining.)
- **D5 (#9 archived): IN SCOPE — backend.** Bump backend 5.55.0 → 5.56.0. **Land #50 as Car 0** so the
  backend rebuild isn't wasted.

---

## Car A — Galaxy render/interaction fixes (core-only) — RE-SPEC per audit
Root causes CORRECTED by fable audit (DB/code-verified). Bugs 2+6 collapse into ONE normalization fix.

- **Bug 2 + Bug 6 — ONE ROOT CAUSE: `normalizeHeat` compresses [0,1]→[0,0.5].** Heat is hard-capped
  `[0,1]` system-wide (thermodynamics.py:89,168,287; heat_decay.py:151; DB `maxh=1.0`). But
  `galaxy-view.js:86-90` `normalizeHeat = h/(h+1)` with `HEAT_H0=1.0` (GV:24, FALSE premise "heat is
  [0,∞)") maps [0,1]→[0,0.5]: (a) **Bug 2** — upper half of the color ramp (PHOS→AMBER→REDH) is
  unreachable, hot memories collapse to exactly 0.5 = ONE color; wiki→0, entity→~0 = one cold color.
  (b) **Bug 6** — `drive=1-nd.heat` (radmode 'heat') with heat≤0.5 → drive≥0.5 → arm additive term
  `drive^0.8*R_MAX*0.45` ≥ ~11.9 → arm roots CANNOT come inside r≈12 → the gap. The mockup
  (viz-galaxy.mockup.html:330-343) feeds raw heat [0,1] to `heatColor` — NO normalizeHeat. The
  normalization IS the mockup→ship regression.
  FIX: remove `normalizeHeat`'s `h/(h+1)` — feed the raw bounded heat to the ramp (mockup parity), OR
  percentile-normalize the corpus. This restores both distinct colors AND `drive∈[0,1]` (closes the arm
  gap for free). Do NOT do the `aCol` rename (the THREE-seam theory is WRONG — points render, so the
  shader compiled + attribute is bound; `vertexColors→USE_COLOR` is stable r125+). Only tune the arm-root
  blend AFTER, if a residual gap remains.
  TEST: on the REAL pipeline — a corpus with heat ∈ {0.2, 0.6, 1.0} must yield normalized-heat spanning
  >0.5 of the ramp (the OLD plan's "≥2 distinct heatColorRGB" PASSES today and can't catch this — do NOT
  use it). Render = smoke-check.

- **Bug 3 — node-type filters don't toggle (gl_PointSize clamp).** Both plan suspects CRACKED: the
  checkbox handlers (IDX:3912) already call the correctly-guarded `applyFilters`→`setVisible`, and
  `_baseSz` is set by the constructor `relayout()` before any external setVisible. REAL cause (confirm in
  browser): hiding via per-vertex `size=0` (GV:649-651) — WebGL clamps `gl_PointSize` to
  `ALIASED_POINT_SIZE_RANGE` (spec min ≥1) → "hidden" nodes render as 1px full-alpha additive dots →
  filters look dead. FIX: in the fragment shader, `discard` when the incoming size varying ≤ 0 (or
  displace hidden vertices outside the clip volume). TEST: extract the mask logic pure + vitest; render =
  smoke-check.

- **Bug 1 — controls don't persist (VERIFIED + ordering trap).** No localStorage in galaxy-view.js;
  siblings persist (IDX:1940,2080). FIX: `GALAXY_P_KEY`; pure `loadSavedP()`/`saveP(P)` (clamp to
  control min/max, guard private-mode throw, drop unknown keys); constructor `P = loadSavedP()`; `saveP`
  after every `_wireControls` mutation + reset. **TRAP (audit):** `_wireControls`' `bindSlider` calls
  `apply()` at bind time (GV:713-722) writing static-HTML defaults BACK into P → clobbers restored P.
  MUST sync DOM←P (`_syncControlsToP`) BEFORE binding, or drop the bind-time P←DOM apply. TEST: pure-fn
  round-trip + clamp + malformed-JSON fallback; the ordering = smoke-check.

- **Bug 12 — rotation wrong way (VERIFIED).** FIX: one-char `-this.P.spin` at the DRIVE site GV:804
  only (drag path GV:962-969 stays). TEST: smoke-check.

Sequencing: Bug 2+6 is one edit to `normalizeHeat`/layout; Bug 3 is a shader edit; both touch the render
path — same car, land 2+6 first (it also feeds the Bug-6 verification). Re-verify galaxy-view.test.js
determinism after the normalization change.

## Car B — Traces / toolbar / consolidation
- **Bug 8 — graph toolbar on every tab (core-only).** `#topbar-graph-controls` (IDX:1319) never
  hidden on tab switch. FIX: in `_switchTab` (IDX:4267, the sole funnel — hashchange:4426 + boot:4522
  route through it) toggle `display = tabName==='home' ? '' : 'none'`. TEST: vitest DOM
  (`_switchTab('traces')`→hidden, `('home')`→visible).
- **Bug 7 — trace replay empty (TEMPO INFRA — user hand-off).** `GET /api/traces/{id}` → HTTP 500
  "queue doesn't have room for ~1150 jobs" (Tempo querier/blocklist exhaustion). yadgar degrades
  correctly. USER FIX (MIGRATION_NOTES): diagnose `curl localhost:3200/metrics | grep blocklist_length`;
  fix compaction/retention or raise `querier.max_concurrent_queries` /
  `query_frontend.max_outstanding_per_tenant`. OPTIONAL core-only hardening (D4): surface the 500 reason
  in the mesh payload (`trace_mesh_handler` traces.py:303) + fallback-build mesh from `/api/search`
  spanSet when by-id fails.
- **Bug 9 — consolidation chart flat-zero (CORE one-liner, RE-SPEC per audit).** The counter WORKS
  (DB: `sum(memories_archived)=4000, max=163/cycle`) — the plan's "backend defect / gutted counter" story
  is WRONG. REAL cause is CORE: `/api/metrics/consolidation-log` (core/server/http.py:2232) does
  `ORDER BY timestamp ASC LIMIT 30` → returns the OLDEST 30 rows, which are all-NULL legacy rows (nulls
  sort first ASC) → chart plots 30 nulls coerced to 0 → PERMANENTLY flat-zero (never self-heals).
  FIX (core one-liner): `ORDER BY timestamp DESC LIMIT 30` then reverse for display + filter
  null-timestamp rows. Do NOT build the backend delta metric (goes negative on recall-boost/purge, needs
  cross-cycle state, AND still leaves the chart flat). Optional cosmetic: align the flow counter's
  threshold (heat_decay <0.02 vs ops.py:168 <0.05) via a stateless crossing count
  `old>=0.05 and new<0.05` — labeling only, not the defect. TEST: unit on the endpoint — seed rows incl
  null-timestamp legacy + recent nonzero, assert the returned window is the 30 NEWEST non-null rows.
  **CORE-ONLY — no backend change.**

## Car C — Global theme unification (#5, core-only, BIG)
Two coexisting systems: old phosphor-green hardcoded (`#161b22`/`#30363d`/`#58a6ff`) on topbar/tab-bar/
stats/health/info/help/debug/right-sidebar/bookmarks/cfgref vs new `--viz-*` (viz-theme.css) on
traces/galaxy + `--nc-*` on #tab-control. FIX: unify all tabs EXCEPT #tab-control onto `--viz-*`
(add a shared `viz-chrome.css` or extend viz-theme.css): topbar/#tab-bar, `.tab-card`/`.tab-grid`/
`.tab-section-title`, `.cfgref-*`, `.help-*`, `#tab-debug`, floating overlays, alias bookmarks-tab.css
`--bg-base`→`--viz-bg-0`. #tab-control stays on `--nc-*` by design. LANDMINE (audit — plan was
under-scoped): `test_viz_tab_pane_display.py:24` regex covers only
`home|stats|health|bookmarks|info|control|debug` — MUST extend to `traces`, `config-ref`, `help` (AND
`search` from Car D). `_CSS_SOURCES` (:15-18) scans only index.html + bookmarks-tab.css — if this car
adds `viz-chrome.css`, ADD it to `_CSS_SOURCES` or the guard is blind to it. Every new `#tab-*` display
rule `.active`-scoped. TEST: the display-guard test (widened regex + source list) + smoke-check.

## Car D — Search tab + node popup + debug panel + nav reorg (net-new surfaces; audit landmines)
- **#10 nav + search (D2 = DEDICATED tab):** (a) move Debug `<a>` from Help group to System group
  (IDX:1307-1315; nav display-only, no JS change). (b) NEW `#tab-search` — global semantic search,
  results list with type-aware routing; REMOVE search from the graph toolbar. **AUDIT LANDMINE:** a new
  tab needs registration in BOTH `_VALID` sets (index.html:4262 + the tabs.js fallback:4273) AND the
  test_viz_tab_pane_display regex AND the nav tree — plan must list all. Wire galaxy search-highlight
  (deferred v1 — `_applySearchHighlight` bails when graph null; route to `_galaxyView` highlight).
- **#4 popup (D1 = anchor-to-node · halo-only · wiki-widen):** build `viz-node-popup.mockup.html` as a
  floating click-away popup replacing `#right` in galaxy mode. Reuse `graph-detail.js` `showDetail()`
  (injectable factory :48-57; galaxy already routes `_galaxyOnPick→showDetail` IDX:2095) — DOM target
  changes to the floating div; suppress `#right` via `body.galaxy-active` (IDX:2139). **AUDIT LANDMINE:**
  the mockup's pulsing halo is a CSS `::after` on faux DOM nodes — the galaxy is a WebGL Points cloud with
  NO per-node DOM. The halo must be a projected screen-overlay (project node world→screen coords) or a
  THREE sprite added to the scene — spec this, don't assume CSS carries over.
- **#11 debug panel (D3):** rebuild the Debug view — Section 1 DB-query console (POST
  /api/debug/read_query, exists, YADGAR_DEBUG_APIS_ENABLED-gated) + health/stats/config/logs/SSE
  (existing endpoints) + DLQ (new ~10-line `GET /api/debug/dlq` wrapping dlq_inspect — the only new
  endpoint, core-side, debug-gated).

## Car 0 — #50 backend-rebuild CI fix (KEPT — pays off this core-only train)
`ci-release.yaml` fires `backend_changed=true` on ANY pyproject.toml change → this train's CORE version
bump would wastefully rebuild backend. FIX: ci-release.yaml (~L110-124) diff pyproject
`[project.dependencies]`/`[optional-dependencies.ml]` sections, NOT the `version=` line, to set
`backend_changed`; + add `uv.lock` to `.dockerignore` (Dockerfile.backend:5 `COPY . /app` + pip; uv.lock
unused in image → safe). Landing it in THIS PR makes the release run (workflow read from master) skip the
backend rebuild. Do NOT add pyproject/uv.lock to check_backend_bump.py's inputs.

## Build order (all CORE-ONLY — no backend change)
Cars A/B/C/D all edit index.html → SERIALIZE on the branch. Car 0 (ci-release + .dockerignore) is
independent → can land first/parallel.
Order: Car 0 → Car A (Bug 2+6 normalization FIRST, then 3/1/12) → Car B → Car C → Car D → version bump
(core 5.148.0; backend STAYS 5.55.0) + CHANGELOG + I32 (galaxy popup + search tab + debug panel + dlq
route) + archive plan (ADR-0082 archive-first commit).

## Risks (post-audit)
Bug 2 normalization fix is the linchpin — verify colors AND arm-gap in browser (one fix, two symptoms);
Bug 3 gl_PointSize discard needs browser confirm (WebGL driver behavior); layoutPositions determinism
(re-verify galaxy-view.test.js after normalization change); theme blast radius (Car C — widen the
display-guard regex + _CSS_SOURCES or it's blind); Car D net-new surfaces (search tab dual _VALID sets,
WebGL halo overlay, popup); Bug 7 is Tempo infra — NOT ours (fixed separately in nix). Backend untouched
→ no backend-bump drift trap this train.
