# PLAN — v5.50.0: Viz UI Restructure — Tabs, Floating Overlays, Branding (CORE only)

**Status:** drafted 2026-05-31. REVISED 2026-06-02 post-opus-review (MAJOR split). Plan-first per I27.

**Revision notes (opus reviewer):**
- SPLIT into THREE plans — current scope (557 lines + Bookmarks addendum) is phase-commit-infeasible:
  - **v5.50.0** (this plan) — tab router + overlays + Home/Stats/Health/Info tabs + zoom regression bisect + logos + branding. Drop Bookmarks tab + Control tab.
  - **v5.50.1** (NEW plan needed) — Bookmarks tab refactor (full Addendum 2026-06-02 content — search/preview/versions rail/diff view). Composes v5.41 + v5.39 + v5.23. Separate dispatch.
  - **v5.50.2** (NEW plan needed) — Control tab + restart endpoints (depends on v5.47 API + sidecar restart unit). Holds until v5.47 lands.
- Hold restart-endpoint design until v5.47 lands + v5.45-v5.46 daemon mechanism stabilizes.
- `YADGAR_DEBUG_APIS_ENABLED` introduced HERE — I25 three-way registration explicit in acceptance. v5.52 must NOT re-register.
- Tighten test coverage in v5.50.1: minimum 10 tests per JS file in Bookmarks addendum.
- v5.50.0 acceptance: Control tab debug panel placeholder container exists (v5.52 fills it; v5.50.0 ships empty container).
- Bookmark URL preservation (Open Q5 — `/#bookmarks/<id>`) addressed in v5.50.1, not here.

**Renumbered:** v5.41.0 → v5.50.0 on 2026-05-31. Reason: user explicitly bumped the viz train forward so the setup-refactor (v5.45-v5.47) ships first. Numbering is locked at v5.50 / v5.51 / v5.52 across this train. Do NOT revert to v5.41-v5.43 anywhere in file names, content, CHANGELOG, or git messages.

**Audit lineage:** continues the viz-knob system shipped in v5.11.0 (35 visual knobs in `config.yaml`), the edge-tuning slot from v5.10.11, the bookmarks subsystem shipped in v5.23/v5.24, and the marked-rendering fix in v5.24.2.

**Depends on:** v5.25.0 shipped (benchmark infra not on the critical path; this is purely viz/frontend). **Soft-coupled to v5.47:** the Control tab's `[⬆ update]` button calls `/api/control/update`, which the setup-refactor train introduces. If v5.47 has not shipped at viz cut-time, the button must be greyed out with the tooltip "requires v5.47 update endpoint" — DO NOT delete it from the layout.

**Downstream:** v5.51.0 (CPU spike profile + Stats throttle), v5.52.0 (debug viz interaction APIs + console capture) both extend the Control / Info debug surfaces shipped here.

**Effort estimate:** 4-6 calendar days. Largest item is the Control tab config editor (knob hot-reload vs restart flag, validation, save round-trip).

---

## Goal — single-canvas SPA with floating chrome, no chrome-eats-canvas

Replace the v5.10-v5.24 layout (footer + right side panel + standalone `bookmarks.html`) with a hash-routed single-page app where the **3D graph is the entire viewport** on the default Home tab. All chrome floats. All chrome is collapsible. All chrome is drag-repositionable. All chrome is click-through-to-canvas when the user is interacting with the graph.

Add a **Control tab** that exposes the v5.11.0 35-knob config system as an inline editor, plus three action triggers (consolidate / vacuum / re-embed), an update button (cross-cut with v5.47), and two restart buttons with confirmation-by-typed-name.

Ship **three logo variants** (Synapse, Knot, Letterform-Y) committed as SVG so docs, favicon, and OG image pick from the same family. Recommendation: V3 for favicon (legibility at 16px), V2 for OG image (storytelling), V1 for documentation header.

The aesthetic target is **"terminal cartography"** — dark chart-at-night, hairline strokes, monospace precision, cyan-blue accent. No gradients, no glow, no AI-softness. Every pixel grid-aligned. Reads like a topographic map in `tmux`.

---

## Non-goals (explicit)

- **No new graph physics.** v5.11.0 35-knob system remains the source of truth. The Control tab edits the existing knobs; it does not introduce new ones except the three documented below.
- **No backend rewrite.** Tabs are pure frontend hash-routing on `index.html`. The backend exposes the same `/api/*` surface plus three new endpoints (config read/write, action triggers, restart) — that's it.
- **No mobile responsiveness target.** Yadgar viz is a desktop developer tool. The layout assumes ≥1280×720. Touch interactions out of scope.
- **No light theme.** Dark-only. The color palette below is fixed.
- **No read-only introspection panels** (state, logs) in Control. Those land in v5.52.0.
- **No browser console capture.** v5.52.0.
- **No CPU spike investigation.** v5.51.0.
- **No author-bio asset.** Info tab carries a placeholder card with TODO markers for photo / belts / whistles. User supplies the actual asset after ship.
- **No version bump strategy change.** v5.50.0 is a normal minor.
- **No removal of `bookmarks.html`.** Keep the file as a 302-redirect to `#bookmarks` for one minor cycle (deprecation grace). Remove in v5.52.0 or later.

---

## Current state (verified from code, 2026-05-31)

| Asset | Path | Status |
|---|---|---|
| Viz entrypoint | `yadgar/server/static/index.html` | Single-page, monolithic. Footer (CPU/RSS/threads/FDs/daemon). Right side panel (heat slider + graph stats + node types). No tab routing. |
| Bookmarks | `yadgar/server/static/bookmarks.html` | Standalone page. v5.23 functionality, v5.24.2 marked-fix shipped. Linked from footer. |
| Viz config | `config.yaml` `viz:` section | 35 knobs (v5.11.0). Three-way registered (yaml + Settings + registry). |
| Stats poll | `index.html` `pollStats()` | 5s interval, no visibility-awareness, no throttle. Suspected CPU contributor (see v5.51.0). |
| Edge defaults | `viz.edge.width_3d_multiplier=1.5`, `arrow_len=4`, `opacity=0.85` | v5.10.11 ship — locked variant A. **v5.50.0 changes to variant C: 1.8 / 5 / 0.9 (both brightness + thickness).** |
| Wiki shape | `viz.wiki.shape="sphere"` | Default. **v5.50.0 changes to `"octahedron"`** (`THREE.OctahedronGeometry`). |
| Edge repulsion | `viz.physics.charge_strength=-12.0` | **v5.50.0 bumps to `-18.0`** (+50% absolute). |
| Zoom regression | unconfirmed, suspected v5.10.4-v5.11.0 function-breakdown | Loads zoomed-in then zooms-out automatically. Bisect required (Step 1 below). |
| Debug API gate | `YADGAR_DEBUG_APIS_ENABLED` | Does NOT exist yet. Introduced here as part of Control tab auth. |
| Logo / favicon | `static/favicon.ico` (16×16 placeholder) | No SVG. No OG image. No documentation header. |

---

## Scope — concrete file changes

### Frontend

| File | Change |
|---|---|
| `yadgar/server/static/index.html` | Restructure: hash router (`#home`, `#stats`, `#health`, `#bookmarks`, `#info`, `#control`). Default `#home`. History API. Tab bar above viewport. |
| `yadgar/server/static/css/yadgar.css` | New stylesheet. Replaces inline styles. Implements the 15-token palette + typography stack below. |
| `yadgar/server/static/js/tabs.js` | New. Hash router, tab switch animation (250ms), back/forward integration. |
| `yadgar/server/static/js/overlays.js` | New. Drag-to-reposition (per-overlay `localStorage` key `viz.overlay.<name>.position`), collapse state (`viz.overlay.<name>.collapsed`), auto-fade on drag/zoom of graph. `pointer-events: none` on overlay body, `auto` on title-bar + controls. |
| `yadgar/server/static/js/home.js` | Extract Home tab logic from `index.html`. Hosts the 3D ForceGraph canvas + 5 floating overlays (heat slider, graph stats, node types, edge legend, optional CPU mini). |
| `yadgar/server/static/js/stats.js` | Detail panels (existing stats content). 30s fixed poll on this tab only. Visibility-aware deferred to v5.51. |
| `yadgar/server/static/js/health.js` | New tab. CPU / RSS / threads / FDs / daemon stats. Replaces footer + side-panel health surface. |
| `yadgar/server/static/js/bookmarks-tab.js` | NEW — orchestrator for the refactored Bookmarks tab (see Addendum 2026-06-02). Replaces standalone `bookmarks.js`. Preserves v5.24.2 marked-fix verbatim inside the new `preview-pane.js` component. |
| `yadgar/server/static/css/bookmarks-tab.css` | NEW — layout, palette extension, diff colors, typography registers (Mono/Sans/Serif). |
| `yadgar/server/static/js/components/search-bar.js` | NEW — sticky semantic-first search w/ inline mode toggle (semantic / keyword / slug). |
| `yadgar/server/static/js/components/preview-pane.js` | NEW — markdown render via marked.js v15 + star button header (toggles bookmark). Used for current + historical versions. |
| `yadgar/server/static/js/components/versions-rail.js` | NEW — vertical timeline of `VersionLozenge` entries; click-to-preview historical; shift-click multi-select for compare. Composes v5.41 `wiki_history` / `wiki_read_version`. |
| `yadgar/server/static/js/components/diff-view.js` | NEW — split-pane synced scroll, unified-diff color tokens. Composes v5.41 `wiki_diff`. |
| `yadgar/server/static/js/components/bookmark-spine.js` | NEW — bookmark-shelf entry on empty-search landing. Drag-reorder, j/k nav. |
| `yadgar/server/static/js/info.js` | New tab. Version + commit SHA + build date / license + 3rd-party libs + SRI hashes / MCP tool catalogue (live `/api/tools`) / keyboard shortcuts / repo links / debug panel entrypoint / **author bio + photo + belts/whistles placeholder card**. |
| `yadgar/server/static/js/control.js` | New tab. Action triggers + config editor + update button + restart buttons. All gated on `YADGAR_DEBUG_APIS_ENABLED=on`. |
| `yadgar/server/static/bookmarks.html` | Replace body with a JS 302 to `/#bookmarks`. Mark deprecated. Remove in a later minor. |
| `yadgar/server/static/img/logo-synapse.svg` | NEW. Inline SVG from this plan. |
| `yadgar/server/static/img/logo-knot.svg` | NEW. Inline SVG from this plan. |
| `yadgar/server/static/img/logo-y.svg` | NEW. Inline SVG from this plan. |
| `yadgar/server/static/img/favicon.svg` | NEW. Mirrors `logo-y.svg`. Browsers that don't render SVG favicons fall back to existing `favicon.ico`. |
| `yadgar/server/static/img/og-image.png` | NEW. 1200×630 export of `logo-knot.svg` on `--bg-base`. Generated at build, not committed. Build script in `scripts/build-og-image.sh`. |

### Backend

| File | Change |
|---|---|
| `yadgar/server/api/control.py` | NEW. Endpoints: `GET /api/control/config` (full knob table with current/default/reload metadata), `POST /api/control/config` (set single knob; validates type; returns 400 on type mismatch or out-of-range), `POST /api/control/action/{consolidate\|vacuum\|reembed}` (internally calls existing MCP tools), `POST /api/control/restart/{yadgar\|backend}` (body `{"confirm": "yadgar"}` must match service name verbatim). All gated on `YADGAR_DEBUG_APIS_ENABLED`. Returns 403 with `{"error": "debug APIs disabled"}` when off. |
| `yadgar/server/api/info.py` | NEW. `GET /api/info` → `{version, commit_sha, build_date, license, third_party: [{name, version, license, sri}], shortcuts: [...]}`. |
| `yadgar/server/api/tools.py` | NEW (if not present). `GET /api/tools` → list MCP tools with 1-line descriptions, read from the tool registry. |
| `yadgar/server/api/wiki_query.py` | EXTEND — add `?mode=semantic\|keyword\|slug` query param. Semantic default. Returns score per hit. Reuses existing wiki embedding index. |
| `yadgar/server/api/wiki_versions.py` | NEW (or extend existing wiki route). HTTP wrappers for v5.41 MCP tools: `GET /api/wiki_history?slug=…`, `GET /api/wiki_read_version?slug=…&version=N`, `GET /api/wiki_diff?slug=…&v1=A&v2=B`, `POST /api/wiki_restore` (confirmation-gated). |
| `yadgar/server/auth.py` | Extend bearer-token middleware to ALSO require `YADGAR_DEBUG_APIS_ENABLED=on` for the `/api/control/*` paths. Bearer-token alone is insufficient. |
| `yadgar/settings.py` | Register `YADGAR_DEBUG_APIS_ENABLED` (bool, default `False`), `viz.edge.variant` (str enum, default `"C"`), `viz.wiki.shape` (str enum, default `"octahedron"`). Three-way per I25 (yaml + Settings + registry). |
| `config.yaml` | Update defaults: `viz.edge.width_3d_multiplier=1.8`, `viz.edge.arrow_len=5`, `viz.edge.opacity=0.9`, `viz.wiki.shape="octahedron"`, `viz.physics.charge_strength=-18.0`. Add `viz.edge.variant="C"` (informational, not behavioral). Add `debug.apis_enabled=false`. |

### Tests

| File | Change |
|---|---|
| `yadgar/tests/test_viz_routes.py` | NEW. Hash routes are static — assert each route renders the correct tab container (Playwright or jsdom-lite). 6 tests. |
| `yadgar/tests/test_control_api.py` | NEW. `/api/control/*` returns 403 when `YADGAR_DEBUG_APIS_ENABLED=off`. Returns 200 when on. Restart confirmation mismatch returns 400. Config GET/POST round-trip. Type mismatch returns 400. |
| `yadgar/tests/test_overlays_persist.py` | NEW. `localStorage` round-trip for position + collapse state. Jsdom. |
| `yadgar/tests/test_bookmarks_migration.py` | NEW. `bookmarks.html` redirects to `#bookmarks`. Existing bookmark functionality (mark rendering, list, add, remove, reorder) intact under tab. |
| `yadgar/tests/test_bookmarks_search.py` | NEW. Semantic / keyword / slug mode toggle. Empty-search shows bookmark shelf. Search returns scored hits. Click result loads preview. Star toggle round-trips with `/api/bookmark_add` + `/api/bookmark_remove`. |
| `yadgar/tests/test_bookmarks_versions.py` | NEW. Preview pane shows VersionsRail. Click historical version → preview switches. Shift-click two versions → compare button → DiffView renders. Restore button → confirmation → new version created. Composes v5.41 endpoints. |

---

## Mockups

### Tab + overlay master layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ YADGAR  knowledge graph                          v5.50.0  ●live  127.0.0.1   │
├─[ Home ]─[ Stats ]─[ Health ]─[ Bookmarks ]─[ Info ]─[ Control ]─────────────┤
│                                                                              │
│            ┌──────────────────────────────────────┐ ┌──────────────────────┐ │
│            │ ⋮ HEAT ≥ 0.00 ─────────●──────  [─] │ │ ⋮ GRAPH STATS    [─] │ │
│            └──────────────────────────────────────┘ │   memories     1,247 │ │
│                                                     │   wiki           182 │ │
│                                                     │   edges        4,901 │ │
│                                                     └──────────────────────┘ │
│                                                                              │
│                                                              ┌─────────────┐ │
│                  ●                                           │ ⋮ NODES [─] │ │
│            ●─────●─────●                                     │  ● memory   │ │
│                                                              │  ◆ wiki     │ │
│                              [3D ForceGraph canvas]          │  ▲ entity   │ │
│                                                              │  ★ episode  │ │
│                                                              │  ⚓ anchor   │ │
│                                                              └─────────────┘ │
│ ┌──────────────────────────────────────┐                                     │
│ │ ⋮ EDGES                          [─] │                                     │
│ │  ━━ semantic   ─── temporal          │                                     │
│ │  ▰▰▰ transition  ◆◆◆ wiki-xref       │                                     │
│ └──────────────────────────────────────┘                                     │
└──────────────────────────────────────────────────────────────────────────────┘
   ⋮ = drag-grip   [−] = collapse   all overlays click-through to canvas
```

### Control tab

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ⚠ CONTROL — requires YADGAR_DEBUG_APIS_ENABLED=on                            │
│                                                                              │
│  ┌─ ACTIONS ─────────────────────────────────────────────────────────────┐  │
│  │  [↻ consolidate]  [⚒ vacuum]  [⟳ re-embed]  [⬆ update]                │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ CONFIG EDITOR ───────────────────────────────────────────────────────┐  │
│  │  filter ▶ [          ]   group: [viz ▾]                                │  │
│  │  KNOB                       TYPE   CURRENT    DEFAULT  RELOAD   EDIT  │  │
│  │  YADGAR_VIZ_NODE_SIZE_3D    float  10.0       8.0      ●hot      ✎    │  │
│  │  YADGAR_VIZ_EDGE_WIDTH      float  1.8        1.5      ●hot      ✎    │  │
│  │  YADGAR_VIZ_PHYSICS_CHARGE  float  -18.0      -12.0    ⟳restart  ✎    │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ RESTART ─────────────────────────────────────────────────────────────┐  │
│  │  [⟲ restart yadgar]  [⟲ restart yadgar-backend]                       │  │
│  │  (confirmation: type service name)                                    │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Bookmarks tab (Addendum 2026-06-02)

**Aesthetic direction:** forensic library dashboard. Knowledge as strata you sift through. Three modes: shelf (landing), microfiche reader (preview), forensic compare (diff). Mono caret + serif body + sans chrome — three typographic registers reinforce the three roles of text (input / chrome / payload).

**Typography (extends locked palette):**
- Chrome (UI labels, titles, tabs): IBM Plex Sans 400/500
- Mono (slug, version timestamps, search caret, code): IBM Plex Mono 400
- Body (markdown-rendered wiki content in preview): IBM Plex Serif 400 — gives "library" feel
- Self-hosted via `@font-face` (static-file site)

**New design tokens (extend v5.50's locked 15):**
```css
--star: #e6b800;              /* filled bookmark — non-blue emotional pop */
--scope-current: #58a6ffb3;   /* current version highlight (accent + alpha) */
--diff-add: #1a4d2eaa;
--diff-del: #4d1a1aaa;
--diff-ctx: var(--text-mute);
--surface-2: <surface + 6/6/6>; /* preview bg one step lighter than chrome */
```

**Landing state — bookmark shelf:**
```
┌─────────────────────────────────────────────────────────┐
│ ╱   search wiki…              semantic │ keyword │ slug │  ← sticky
├─────────────────────────────────────────────────────────┤
│  BOOKMARKED                                    ⋮⋮⋮      │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐          │
│  │ road │ │comp- │ │arch- │ │bench │ │decis-│  ★ filled│
│  │ map  │ │etitor│ │inv   │ │marks │ │ions  │  per spine
│  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘          │
└─────────────────────────────────────────────────────────┘
```

**Search active, no preview:**
```
┌─────────────────────────────────────────────────────────┐
│ ╱   benchmark│                semantic │ keyword │ slug │
├─────────────────────────────────────────────────────────┤
│ ▸ benchmarks-current                          0.94      │ ← cosine score
│   yadgar/competitors  yadgar/longmemeval                │
│   v5.26.0 Sonnet 4.6 full 500q result. 69.4%...         │
│                                                         │
│ ▸ yadgar-competitor-catalog                   0.81      │
└─────────────────────────────────────────────────────────┘
```

**Preview open (3-col: results | preview | versions rail):**
```
┌──────────┬────────────────────────────┬──────────────┐
│ RESULTS  │ benchmarks-current      ★  │ HISTORY      │
│ ━━━━━━━  │                            │ ━━━━━━━━━━━━ │
│ ▸ bench  │ # Benchmarks (current)     │ ◉ v4 · now   │ ← current
│ ▸ comp   │                            │ ◯ v3 · 2h    │
│ ▸ arch   │ ## v5.26.0 results         │   ▁▃▂  +12   │ ← size sparkline
│          │                            │ ◯ v2 · 5h    │
│          │ Sonnet 4.6 hit 69.4%...    │ ◯ v1 · 1d    │
│          │                            │              │
│          │                            │ [⇄ compare]  │
│          │                            │ [↶ restore]  │ ← non-current selected
└──────────┴────────────────────────────┴──────────────┘
```

**Diff mode (two versions selected, versions rail collapses):**
```
┌──┬────────────────────┬────────────────────┐
│R │ v2 · 5h ago        │ v4 · now           │
│E │ # Benchmarks       │ # Benchmarks (curr)│
│S │ - Phase 1 deferred │ + 69.4% on 500q    │  ← --diff-add / --diff-del per line
└──┴────────────────────┴────────────────────┘
```

**Components (vanilla, web-component-style):**

| Component | Behavior |
|---|---|
| `SearchBar` | Sticky top. Mono caret (`╱`). 200ms debounce. `/` focuses. Live result count. |
| `ModeToggle` | 3 inline chips: semantic (default) / keyword / slug. Tab cycles. State persists in localStorage. |
| `BookmarksShelf` | Empty-search landing. Spine grid (2-col blocks). HTML5 DnD reorder. Each spine: slug-abbrev + star-filled. Click → preview. |
| `ResultCard` | Title (Plex Sans 16) + slug (Plex Mono 12 dim) + tag chips (`--accent-dim` bg) + 2-line snippet (`-webkit-line-clamp:2`) + score chip right. |
| `PreviewPane` | Renders via marked.js v15 (PRESERVE v5.24.2 fix verbatim). Body Plex Serif. Star button top-right (24px, fills `--star` on toggle). Close top-left. |
| `VersionsRail` | Vertical timeline. Each `VersionLozenge` = ◉/◯ marker + `v{N} · {relative-time}` (Plex Mono) + size-delta sparkline (mini `<canvas>` 40×8) + change_summary truncated. Click selects, shift-click multi-selects. Current = `--scope-current` border. |
| `DiffView` | Split-pane synced scroll. Headers show v1/v2 + timestamp. Unified diff via `--diff-add`/`--diff-del`. Toggle from VersionsRail compare button. |
| `ConfirmModal` | Restore action: "Restore v{N} as new v{current+1}?" cancel/confirm. |

**Keyboard:**
`/` focus search · `j/k` nav results · `Enter` open preview · `Esc` close preview/diff · `⌘B`/`Ctrl+B` toggle star · `[`/`]` cycle versions · `Shift+Click` multi-select for compare · `Tab` cycle search modes

**API surface (composes v5.41 + v5.39 + v5.23):**
- `GET /api/wiki_query?q=…&mode=semantic|keyword|slug` — search (semantic default)
- `GET /api/wiki_read?slug=…` — current version
- `GET /api/wiki_history?slug=…` — versions list (v5.41)
- `GET /api/wiki_read_version?slug=…&version=N` — historical content (v5.41)
- `GET /api/wiki_diff?slug=…&v1=A&v2=B` — diff (v5.41)
- `POST /api/wiki_restore` — restore (v5.41, confirmation-gated)
- `GET /api/bookmarks` · `POST /api/bookmark_add` · `POST /api/bookmark_remove` · `POST /api/bookmark_reorder` (v5.23)

**Differentiating moves:**
1. Three typographic registers (Mono/Sans/Serif) make text role visually unambiguous — chrome vs query vs payload.
2. Vertical versions rail with size-delta sparklines turns history into a glanceable shape, not a list of dates.
3. Bookmark shelf as spines on empty search — feels like a library, not a hamburger menu.
4. Mode toggle inline in search bar — no hidden settings, no separate filter row. State always visible.
5. `--star` gold accent breaks the blue monochrome at the one emotional decision point (save/unsave) — single non-blue token in the palette.

**Wiki node detail in 3D graph (composes):** clicking a wiki node in the Home tab graph opens the same `PreviewPane` + `VersionsRail` overlay (drag-positionable, click-through-to-canvas). Reuses the same components — single source of truth for wiki rendering across tabs.

---

### Logo V1 — Synapse

Inline SVG, ships at `yadgar/server/static/img/logo-synapse.svg`:

```svg
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="yadgar synapse mark">
  <g fill="none" stroke="#58a6ff" stroke-width="2" stroke-linecap="square" stroke-linejoin="miter">
    <path d="M6 32 H22"/>
    <path d="M22 32 L28 24 M22 32 L28 32 M22 32 L28 40"/>
    <path d="M30 24 L36 24 M30 32 L36 32 M30 40 L36 40"/>
    <path d="M36 24 L42 32 L36 40 Z"/>
    <path d="M42 32 H58"/>
  </g>
  <circle cx="33" cy="32" r="1.5" fill="#58a6ff"/>
</svg>
```

Tagline: *Three signals in, one decision out.*

### Logo V2 — Knot

Inline SVG, ships at `yadgar/server/static/img/logo-knot.svg`:

```svg
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="yadgar knot mark">
  <g fill="none" stroke="#58a6ff" stroke-width="2.5" stroke-linecap="square">
    <path d="M32 8 C 14 8, 8 26, 16 38"/>
    <path d="M16 38 C 22 50, 42 50, 48 38"/>
    <path d="M48 38 C 56 26, 50 8, 32 8"/>
    <path d="M22 22 L42 42"/>
    <path d="M42 22 L34 30"/>
    <path d="M30 34 L22 42"/>
  </g>
  <rect x="30" y="30" width="4" height="4" fill="#58a6ff"/>
</svg>
```

Tagline: *Edges cross, the node persists.*

### Logo V3 — Letterform Y

Inline SVG, ships at `yadgar/server/static/img/logo-y.svg`:

```svg
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="yadgar Y mark">
  <g fill="none" stroke="#c9d1d9" stroke-width="3" stroke-linecap="square">
    <line x1="14" y1="14" x2="32" y2="32"/>
    <line x1="50" y1="14" x2="32" y2="32"/>
    <line x1="32" y1="32" x2="32" y2="54"/>
  </g>
  <line x1="46" y1="14" x2="50" y2="14" stroke="#58a6ff" stroke-width="3" stroke-linecap="square"/>
  <line x1="50" y1="14" x2="50" y2="18" stroke="#58a6ff" stroke-width="3" stroke-linecap="square"/>
</svg>
```

Tagline: *The shape of "yes, I remember."*

**Locked use:** V3 favicon + apple-touch (16px legibility wins). V2 OG image (storytelling, knot-as-graph metaphor). V1 documentation header (concept piece — the three-into-one is the literal Yadgar consolidation story).

---

## Locked design decisions

### Edge variant C (both brightness + thickness)

| Param | Variant A (current) | Variant B (brightness only) | Variant C (LOCKED) |
|---|---:|---:|---:|
| `viz.edge.width_3d_multiplier` | 1.5 | 1.5 | **1.8** |
| `viz.edge.arrow_len` | 4 | 4 | **5** |
| `viz.edge.opacity` | 0.85 | 0.95 | **0.9** |

Rationale: variant A reads as faded on dense subgraphs; variant B over-glares on sparse views. C is the legibility sweet-spot across both regimes.

### Wiki node shape — Octahedron

`new THREE.OctahedronGeometry(radius)` with `MeshBasicMaterial({ flatShading: true })`. Eight faces, six vertices. Sphere-adjacent silhouette but with clear flat-face highlight on hover, distinguishing wiki from memory (still sphere) without breaking the "round = noun-like entity" visual grammar.

### Physics charge — bump to -18.0

Existing `-12.0` produces clumpy clusters on graphs >500 nodes. +50% absolute (`-18.0`) gives the layout room without flying nodes off-canvas. Tuned at v5.10.11 with the edge work; promoted to default here.

---

## Color palette (15 tokens, locked)

| Token | Hex | Role |
|---|---|---|
| `--bg-base` | `#0d1117` | App background, viewport |
| `--bg-panel` | `#161b22` | Overlay surface, panels |
| `--bg-sunken` | `#010409` | Inputs, code blocks |
| `--bg-hover` | `#21262d` | Hover state |
| `--bg-active` | `#1f3a5c` | Active tab |
| `--border` | `#30363d` | 1px hairlines |
| `--border-muted` | `#21262d` | Secondary dividers |
| `--text` | `#c9d1d9` | Primary text |
| `--text-muted` | `#8b949e` | Labels, captions |
| `--text-faint` | `#484f58` | Disabled |
| `--accent` | `#58a6ff` | Brand, focus, link |
| `--accent-strong` | `#79c0ff` | Hover on accent |
| `--success` | `#3fb950` | Live indicator, OK |
| `--warn` | `#d29922` | Throttling, degraded |
| `--crit` | `#f85149` | Errors, circuit-breaker open |
| `--brand-violet` | `#a371f7` | Memory→Wiki edges (existing convention) |

## Typography

- **Display:** `"Berkeley Mono", "JetBrains Mono", "Cascadia Code", ui-monospace, monospace` 14-16px / 600
- **Body:** same stack 12px / 400
- **Caption:** same stack 9-10px / 400 / 1px letter-spacing / UPPERCASE
- **Numeric:** same stack with `font-variant-numeric: tabular-nums` 14px / 700
- **Spacing scale (4px base):** 4 · 6 · 8 · 10 · 14 · 20 · 32 · 48
- **Borders:** always `1px solid var(--border)`
- **Motion:** 150ms ease-out hover, 250ms ease-out tab swap, 800ms ease-out camera fly-to. Respect `prefers-reduced-motion`.

---

## Open questions (must resolve during implementation)

1. **Zoom regression bisect window.** Suspected v5.10.4-v5.11.0 function-breakdown. Step 1 below pins the exact commit. If the regression predates v5.10.4, expand the bisect.
2. **Hot-reload vs restart-required classification per knob.** First pass: anything in `viz.*` is hot-reload (frontend re-reads). Anything in `physics.*`, `consolidation.*`, `embedding.*`, `storage.*` is restart-required. Edge cases: `viz.physics.charge_strength` is hot-reloadable in the frontend force graph engine, but `viz.physics.cooldown_ticks` requires re-init. Audit all 35 knobs during Step 4.
3. **Restart endpoint mechanism.** Two options: (a) `os.execv` to re-exec yadgar daemon in place; (b) write a sentinel file picked up by a systemd unit / launchd job. Lean (b) — safer, no in-process state corruption. Decide during Step 5 TDD.
4. **`yadgar-backend` restart from a sibling service.** Backend is a separate process. yadgar daemon does NOT have privilege to restart it on most setups. Either (a) the backend exposes its OWN restart endpoint and the Control tab calls both, or (b) yadgar shells out to `systemctl restart yadgar-backend` which requires polkit setup. Lean (a). Decide during Step 5.
5. **Bookmark migration UX.** Should the migrated `#bookmarks` tab preserve the standalone-page URL structure (`/#bookmarks/<id>`) for shareable links? Lean yes. Adds ~20 lines to the router.
6. **OG image build pipeline.** `scripts/build-og-image.sh` needs a headless renderer (rsvg-convert, Inkscape, or Playwright screenshot). Lean `rsvg-convert` — already in nixpkgs, no JS runtime needed. Confirm in Step 6.
7. **Author bio asset format.** User supplies later. Plan ships placeholder card with TODO markers (`<!-- TODO: photo -->`, `<!-- TODO: belts -->`, `<!-- TODO: whistles -->`). No design constraint until the asset arrives.

---

## Step plan (TDD per HARD RULE)

### Step 0 — Pre-flight (≤ 0.25 day)
- Confirm v5.25.0 is shipped on master.
- Confirm `bookmarks.html` v5.24.2 marked-fix still applies (grep for the fix marker comment).
- Snapshot `index.html` size + LOC pre-refactor for regression comparison.

### Step 1 — Zoom regression bisect (≤ 0.5 day)
- `git bisect` between v5.10.3 (last known good) and v5.11.0 inclusive, using a manual repro script: load viz, wait 2s, assert `camera.position.z` is within 5% of the initial value.
- Document the offending commit in `MIGRATION_NOTES.md`. Patch surgically — do NOT revert the function-breakdown commit; restore the intended camera-init order.

### Step 2 — Tab router + CSS scaffolding (≤ 0.5 day)
- TDD: `test_viz_routes.py` asserts each hash route renders the correct container.
- Implement `tabs.js`, `yadgar.css` with the 15-token palette + typography stack.
- Tabs render empty containers — content extraction follows in Step 3.

### Step 3 — Extract Home / Stats / Health / Bookmarks tabs (≤ 1 day)
- Move existing `index.html` 3D canvas + overlays into `home.js`.
- Move existing stats panel logic into `stats.js`. 30s fixed poll (visibility-aware deferred to v5.51).
- Move footer + side-panel health surface into `health.js`.
- Migrate `bookmarks.html` body into `bookmarks.js`. Preserve marked-fix verbatim. Add 302 redirect from old URL.
- TDD: each tab's test asserts its container renders the expected DOM under the route.

### Step 4 — Overlay drag + collapse + persistence (≤ 0.5 day)
- TDD: `test_overlays_persist.py` — `localStorage` round-trip.
- Implement `overlays.js`. Drag grip `⋮`. Collapse button `[−]`. `pointer-events: none` body / `auto` controls.
- Auto-fade overlay opacity to 0.3 during canvas drag/zoom, restore to 1.0 on idle (200ms debounce).

### Step 5 — Info tab + branding (≤ 0.5 day)
- Write all 3 SVGs to `static/img/`. Add `favicon.svg` (mirrors `logo-y.svg`).
- Generate OG image via `scripts/build-og-image.sh` (rsvg-convert). NOT committed.
- Implement `info.js` rendering version, license, 3rd-party libs (read from `pyproject.toml` + `package.json` via `GET /api/info`), MCP tool catalogue (`GET /api/tools`), keyboard shortcuts, repo links, author bio placeholder card.

### Step 6 — Control tab (≤ 1.5 day, largest single chunk)
- TDD: `test_control_api.py` — 403 gating, 200 unlock, restart confirmation typed-name match, config round-trip with type validation.
- Implement `/api/control/config` GET + POST.
- Implement `/api/control/action/*` (calls existing MCP tools internally).
- Implement `/api/control/restart/*` (mechanism per Open Question 3 — likely sentinel file).
- Implement `control.js` — filter + group dropdown + inline edit row with type-aware input (number / float / string / bool / enum) + reload-vs-restart pill + validation hint on save.
- Update button stub — if `/api/control/update` returns 404, the button greys out with "requires v5.47". If it returns 200, the button is live.

### Step 7 — Edge variant C + wiki octahedron + physics bump (≤ 0.25 day)
- Update `config.yaml` defaults.
- Update frontend renderer: `OctahedronGeometry` branch in the wiki node factory.
- Snapshot regression test: render a fixed seed graph, assert edge thickness pixel count within ±5% of expected for variant C.

### Step 8 — Acceptance + CHANGELOG (≤ 0.25 day)
- Run full pytest suite. Fix any breakage in `test_bookmarks_migration` from URL changes.
- Manual smoke: each tab, each overlay drag, each Control action with `YADGAR_DEBUG_APIS_ENABLED=on` and `off`.
- `CHANGELOG.md` v5.50.0 entry. `MIGRATION_NOTES.md` block for the zoom fix + Control auth env knob.

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 pre-flight | 0.25 |
| Step 1 zoom bisect + fix | 0.5 |
| Step 2 tab router + CSS | 0.5 |
| Step 3 extract 4 tabs | 1.0 |
| Step 4 overlays drag/persist | 0.5 |
| Step 5 Info tab + branding | 0.5 |
| Step 6 Control tab (largest) | 1.5 |
| Step 7 edge / wiki / physics defaults | 0.25 |
| Step 8 acceptance + CHANGELOG | 0.25 |
| **Total** | **4.75 – 6 calendar days** |

---

## Acceptance criteria

v5.50.0 ships when ALL are true:

- [ ] `index.html` renders all 6 tabs (`#home`, `#stats`, `#health`, `#bookmarks`, `#info`, `#control`) with correct containers under hash routing.
- [ ] Default route is `#home` with full-canvas 3D graph viewport.
- [ ] 5 floating overlays (heat, graph stats, node types, edge legend, optional CPU mini) drag-reposition, collapse, persist position + collapse state in `localStorage`.
- [ ] Overlays auto-fade during canvas drag/zoom; restore on idle.
- [ ] `pointer-events` rules verified: body click-through, controls clickable.
- [ ] Zoom regression bisected and fixed — viz loads at the intended camera position and stays there.
- [ ] Edge variant C live: `viz.edge.width_3d_multiplier=1.8`, `arrow_len=5`, `opacity=0.9`.
- [ ] Wiki nodes render as octahedrons.
- [ ] Physics charge bumped to `-18.0`.
- [ ] 3 logo SVGs committed at `static/img/logo-{synapse,knot,y}.svg`. `favicon.svg` mirrors V3.
- [ ] Info tab live with author-bio placeholder card carrying `<!-- TODO -->` markers.
- [ ] Control tab gated on `YADGAR_DEBUG_APIS_ENABLED=on`. Returns 403 with the documented error body when off.
- [ ] Config editor lists all 35+ knobs, supports filter + group, inline edit with type validation, hot-reload vs restart pill correctly classified per Open Question 2.
- [ ] Update button is live if `/api/control/update` returns 200, else greyed out with tooltip.
- [ ] Restart buttons require typed confirmation matching the service name.
- [ ] `bookmarks.html` redirects to `#bookmarks`. Existing bookmark functionality intact (mark rendering preserved per v5.24.2 fix).
- [ ] All 4 new test files green: `test_viz_routes.py`, `test_control_api.py`, `test_overlays_persist.py`, `test_bookmarks_migration.py`.
- [ ] `CHANGELOG.md` v5.50.0 entry. `MIGRATION_NOTES.md` zoom-fix block + Control auth env knob block.
- [ ] `python scripts/check_versions.py` exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Zoom bisect lands on a load-bearing function-breakdown commit; surgical fix is non-trivial | Document the offending commit; if surgical fix exceeds 0.5 day, defer to v5.50.1 hotfix and ship v5.50.0 with the regression flagged in MIGRATION_NOTES. Don't block the whole ship on it. |
| `pointer-events: none` body breaks accessibility (keyboard focus path) | Keep keyboard focus on overlay controls explicitly. Add `:focus-visible` outline. Tab navigation still works because `pointer-events` is mouse-only. |
| `localStorage` quota exhaustion if user has many overlays / projects | Per-overlay key with stable scheme. Drop-and-rewrite on collision. Total footprint <5KB even for 20 overlays. |
| Config editor lets user set a knob to a value that bricks the daemon | Type validation in the POST handler. Range checks for known-bounded knobs (e.g. `viz.node.size_3d > 0`). Final defence: hot-reload knobs roll back to default on render-time exception. |
| Restart endpoint mis-fires and the daemon dies without a supervisor | Document required systemd unit / launchd job in MIGRATION_NOTES. Without a supervisor, restart endpoint is a foot-gun — but it's gated on `YADGAR_DEBUG_APIS_ENABLED` which is off by default. |
| OG image build requires a JS runtime in the build container | Use `rsvg-convert` (pure C, in nixpkgs). Confirm in Step 6. If `rsvg-convert` can't render the stroke + fill correctly, fall back to Inkscape headless. |
| Author bio placeholder is forgotten and ships with TODO markers in production | Add a `grep -r "TODO: photo"` check to the pre-release sanity script. Fail the check if any TODO marker survives. |
| v5.47 doesn't ship before v5.50 cut; update button is grey on launch | Documented as known limitation. Button stays in place. No code change required when v5.47 lands — the button auto-activates when `/api/control/update` returns 200. |
| Edge variant C is subjectively worse on a user's particular graph | All three numerics are config knobs. User can revert via the Control tab itself, post-ship. No fix-forward required for taste disagreements. |

---

## TDD test list (write red, then implement green)

1. `test_viz_routes.py::test_route_home_renders` — hash `#home` shows the canvas container.
2. `test_viz_routes.py::test_route_stats_renders` — hash `#stats` shows the stats container.
3. `test_viz_routes.py::test_route_health_renders` — hash `#health` shows the health container.
4. `test_viz_routes.py::test_route_bookmarks_renders` — hash `#bookmarks` shows the bookmarks container.
5. `test_viz_routes.py::test_route_info_renders` — hash `#info` shows the info container.
6. `test_viz_routes.py::test_route_control_renders` — hash `#control` shows the control container.
7. `test_control_api.py::test_403_when_debug_apis_disabled` — all `/api/control/*` paths return 403 with `YADGAR_DEBUG_APIS_ENABLED=off`.
8. `test_control_api.py::test_200_when_debug_apis_enabled` — same paths return 200 when on.
9. `test_control_api.py::test_restart_confirmation_must_match_service_name` — POST `/api/control/restart/yadgar` with body `{"confirm": "wrong"}` returns 400.
10. `test_control_api.py::test_config_round_trip` — GET → POST → GET preserves the new value.
11. `test_control_api.py::test_config_type_mismatch_returns_400` — POSTing a string to a float knob returns 400.
12. `test_control_api.py::test_config_out_of_range_returns_400` — POSTing `viz.node.size_3d=-1` returns 400.
13. `test_overlays_persist.py::test_position_persists` — drag overlay, reload, position restored.
14. `test_overlays_persist.py::test_collapse_persists` — collapse overlay, reload, collapse state restored.
15. `test_overlays_persist.py::test_invalid_localStorage_reset_to_default` — corrupt JSON in localStorage falls back to default position.
16. `test_bookmarks_migration.py::test_bookmarks_html_redirects` — GET `/bookmarks.html` returns a 302 (or JS redirect on load) to `/#bookmarks`.
17. `test_bookmarks_migration.py::test_marked_rendering_intact` — bookmark with markdown body renders via marked (v5.24.2 fix verbatim).
18. `test_bookmarks_migration.py::test_add_remove_reorder` — bookmark CRUD intact under the tab.

---

## Dependencies + blockers

- **None blocking start.** v5.25.0 shipped 2026-05-30. Frontend changes are isolated to `static/`. Backend changes are additive (new endpoints, no schema migration).
- **Soft-coupled to v5.47.** Update button is greyed out if v5.47 hasn't shipped. Not a blocker.
- **v5.51.0 + v5.52.0 depend on this plan shipping** — both extend the Control / Info surfaces introduced here.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work requires a feature branch — `feat/v5.50.0-viz-tabs-control` is the obvious name. Branch from latest master after this plan commits.
- Related plans: `docs/PLAN_V5_51_0_*.md` (CPU spike + Stats throttle), `docs/PLAN_V5_52_0_*.md` (debug viz APIs + console capture).
- Setup-refactor train (v5.45-v5.47) ships in parallel — no file overlap with viz train, but `/api/control/update` is the cross-cut. Confirm v5.47 endpoint shape before Step 6.
