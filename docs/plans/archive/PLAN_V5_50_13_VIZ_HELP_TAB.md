# PLAN v5.50.13 — Viz Help tab (single-source legend + glossary)

Status: PLANNED 2026-06-12. Core release (frontend `index.html` Help tab + backend `/api/viz/config` extension). No backend image change.

## Goal

A **Help** nav tab in the viz that documents what the encodings mean — node types & shapes, wiki category colors, edge types, heat — so a viewer (or future-me) isn't guessing. Hard requirement: **zero duplicate documentation.** The legend is RENDERED from the same source that drives rendering; the only authored text is short per-item descriptions, living in exactly one place.

## Anti-duplication architecture (the whole point)

**Single source = `/api/viz/config`, server-built by iterating the canonical sets.** The frontend Help tab is a pure renderer over the response. Nothing is hardcoded twice.

Audited sources (2026-06-12):
- **Categories — canonical = 8, ONE source:** `CATEGORIES` frozenset `yadgar/wiki.py:207-218` (architecture, decision, pattern, debugging, reference, convention, fact, analysis), validated `:245`. The viz `category_colors` (`http.py:1646-1655`, `index.html:1204`) colors exactly these 8. → No hidden bigger enum. Grey cubes = legacy/auto-gen pages with **non-canonical** categories (outside `CATEGORIES`) → default `#8b949e`.
- **Edge types = 5**, currently STRING LITERALS scattered in `graph_api.py`: `temporal`:125, `transition`:147, `wiki_crossref`:202, `memory_wiki`:219, `semantic`:433. **Plus already duplicated** in the `edge-legend` overlay (`index.html:944-956`) and the config `edge.color` map (`:1212`). Three copies today — consolidate to one.
- **`/api/viz/config`** = `http.py:1624-1694` `api_viz_config()`, server-built from Settings, **extensible** (can add fields). This is the home for descriptions.
- **Type→shape:** `nodeType()` (`graph-detail.js:30`) → wiki/memory/entity; `_makeNodeThreeObject` (`index.html:1303-1317`) wiki+octahedron→OctahedronGeometry, else sphere.
- **Heat:** memory only — `heatColor()` gradient (`index.html:1269-1273`, `_nodeColorFor:1283`); wiki uses category color, NO heat (`graph_api.py:172-178` no heat field). `RECALL_BOOST=0.05` (`config.py:545`).

## Design

1. **Make the canonical sets the single source.**
   - `category_colors` in `/api/viz/config` must be **built by iterating `CATEGORIES`** (import the frozenset) — not an independent 8-key literal. Adding a category to `CATEGORIES` then auto-appears (with a fallback color) instead of silently going grey.
   - **Extract an `EDGE_TYPES` constant** (e.g. in `graph_api.py` or a small `viz_meta.py`): `{semantic, temporal, transition, wiki_crossref, memory_wiki} → {color, label, description}`. The edge-builder, the `/api/viz/config` `edge.color`, AND the legend all reference it. Kills the 3-copy duplication.

2. **Extend `/api/viz/config`** to return a glossary block — the ONLY new authored text:
   ```
   legend: {
     categories: [{key, color, label, description}],   # iterated from CATEGORIES
     edges:      [{key, color, label, description}],   # iterated from EDGE_TYPES
     node_types: [{key:'memory', shape:'sphere', color_rule:'heat gradient', description},
                  {key:'wiki',   shape:'octahedron', color_rule:'category color', description},
                  {key:'entity', shape:'sphere', color_rule:'…', description}],
     heat: {description:'memory only; +0.05 per recall, decays; wiki has none',
            gradient:'blue(cold)→red(hot)'}
   }
   ```
   Descriptions are short one-liners. Long-form stays in wiki/docs — Help links, doesn't copy.

3. **Help tab = pure renderer.** New `#tab-help` pane + nav entry. Fetch `/api/viz/config`, iterate `legend.*`, render swatch + label + description per item. **No hardcoded colors/labels/text.** Sections: Node types & shapes · Wiki categories · Edge types · Heat · (short "reading this graph" intro).

4. **Consolidate the existing `edge-legend` overlay** (`index.html:944-956`) to render from the same `EDGE_TYPES`/config source — so overlay + Help + renderer can never diverge. (Don't leave it as a 4th hardcoded copy.)

5. **Honest note in Help:** non-canonical categories (legacy pages outside `CATEGORIES`) render grey `#8b949e`.

## TDD

- Backend: `/api/viz/config` `legend.categories` has exactly the keys of `CATEGORIES`, each with a non-empty `description`; `legend.edges` has the keys of `EDGE_TYPES`, each with `color`+`description`; node_types includes memory/wiki/entity with shape.
- Drift-guard: every category in `CATEGORIES` appears in the legend (add a category → test fails until described); every edge type emitted by `graph_api` exists in `EDGE_TYPES` (no orphan edge type without a legend entry).
- Frontend (viz-tests/, jsdom): Help renderer given a config produces one row per `legend.categories`/`edges`/`node_types` entry with matching color + description; renders NOTHING hardcoded (drive it with a stub config missing a category → that category absent, proving render-from-source).
- (Reminder: viz JS tests run from `viz-tests/`, not repo root.)

## Out of scope
- Reworking categories/edge semantics themselves.
- Long-form per-category essays (link to wiki).
- Backend image change (core only).

## Effort
S–M (~0.5–1 day). Backend: import `CATEGORIES` + new `EDGE_TYPES` constant + `legend` block in `api_viz_config` + descriptions. Frontend: `#tab-help` pane + nav wiring + a small render function + refactor the edge-legend overlay to the shared source. ~6–8 tests.

## Ship
Bump core 5.50.12 → 5.50.13; CHANGELOG; tag; build+push core image; nix core bump; PyPI. Backend stays 5.5.0.

## Why this can't duplicate
The only authored strings (descriptions) live once, server-side, attached to the canonical `CATEGORIES`/`EDGE_TYPES`. Colors come from the existing config. Shapes from the existing render logic. The Help tab, the edge-legend overlay, and the renderer all read one source. Add/rename a category or edge type → it flows everywhere, and the drift-guard test forces a description.
