# PLAN — v5.11.0: Viz knobs configurable via config.yaml

**Status:** drafted 2026-05-30. Plan-first per I27. New minor on v5.11 train. **Pre-empts old v5.11.0 slot (secret-gate context-awareness)** which cascades to v5.13.0 per pipeline renumber accompanying this plan.

**Master at draft time:** v5.10.10 LIVE; v5.10.11 in draft (edge thickness + repulsion).

**Sequencing:** v5.11.0 ships after v5.10.11. Pre-empts ALL prior v5.11.0+ minor slots (renumber cascade documented separately).

---

## Why

User feedback after a session of 11 viz patches (v5.10.7 through v5.10.11) realised every tweak is a CODE change requiring redeploy + hard-refresh + sometimes wrestling with browser cache. **Every tweak should be a config knob.** Edge thickness, node size, colors, physics, search behavior — all tuneable without touching code.

Verbatim:
> *"i want all these knobs to be configurable in config.yaml. node size, node colors, physics tweaks. edge colors and so on."*

Yadgar already has a `config.yaml` system (`yadgar/config_yaml.py` + `YADGAR_CONFIG_FILE` env). v5.11.0 extends it to cover viz.

## Goals

1. **Knob coverage** — all currently-hardcoded viz values become config keys:
   - **Node:** size (3D `nodeRelSize`, 2D radius), category colors (`WIKI_CAT_COLOR`), heat color HSL params (`heatColor`)
   - **Edge:** width by type (`EDGE_WIDTH`), color by type (`EDGE_COLOR`), arrow length (`_arrowLen`), 3D thickness multiplier (v5.10.11 hardcoded 1.5)
   - **Physics:** charge strength (currently -12), link distance (30 in 2D, 36 in 3D per v5.10.11), repulsion multipliers
   - **Layout:** auto-zoom-fit tick threshold (currently 80 per v5.10.10), zoom-fit padding (50), zoom-fit transition duration (800ms)
   - **Search:** highlight color, dim opacity (currently 0.18)

2. **Config delivery to frontend** — backend serves a `/api/viz/config` endpoint that returns the active viz config as JSON. Frontend fetches it on page load, applies before graph init.

3. **Live-reload (NICE-TO-HAVE; not v5.11.0 scope)** — config changes auto-apply without page reload. Lean **out of scope** for v5.11.0; user reloads after editing config.yaml.

4. **Sane defaults** — if config.yaml omits a viz key, use the v5.10.11 hardcoded value as fallback. No-config-change deploys behave identically to v5.10.11.

5. **Type-safe config** — yadgar's existing config loader uses pydantic-ish typed schemas; viz config follows same pattern.

## Non-goals

- Live-reload (deferred)
- Per-user viz preferences (config.yaml is global per-deployment)
- UI control panel for knobs (could come later as separate minor)
- Multi-config-file support (single config.yaml + env overrides as today)

## Approach

### Backend

`yadgar/config.py` + `yadgar/config_registry.py`: add new `viz` section with typed fields. Per I25 invariant, each config knob registered three-way (Settings field + env name + config.yaml key + category).

Skeleton:

```yaml
# config.yaml
viz:
  node:
    size_3d: 8                # nodeRelSize default
    size_2d: 4                # radius base
    category_colors:          # WIKI_CAT_COLOR
      decision: "#3fb950"
      reference: "#58a6ff"
      pattern: "#d29922"
      # ... etc
    heat:
      hue_start: 240          # heatColor h=0 start (blue)
      hue_end: 0              # heatColor h=1 end (red)
      saturation_base: 60     # 60 + h*30
      saturation_gain: 30
      lightness_base: 40      # 40 + h*20
      lightness_gain: 20
  edge:
    color:
      semantic: "#484f58"
      temporal: "#d29922"
      causal: "#f85149"
      wiki_crossref: "#58a6ff"
      memory_transition: "#a371f7"
    width_base: 1.0
    width_3d_multiplier: 1.5
    arrow_length: 4
  physics:
    charge_strength: -12
    link_distance_2d: 30
    link_distance_3d: 36
    cooldown_ticks: 15000      # ForceGraph default
  layout:
    auto_zoom_fit_tick_threshold: 80
    zoom_fit_padding: 50
    zoom_fit_transition_ms: 800
  search:
    highlight_color: "#ffd700"
    dim_opacity: 0.18
```

### `/api/viz/config` endpoint

New route in `yadgar/server/http.py` (or wherever HTTP routes live):

```python
@_tool(http_route="/api/viz/config", auth_required=True)
def get_viz_config() -> dict:
    settings = get_settings()
    return {
        "node": {...},
        "edge": {...},
        "physics": {...},
        "layout": {...},
        "search": {...},
    }
```

Returns flat dict mirror of config.yaml `viz:` section. Frontend can `await fetch(...)` then apply.

### Frontend integration

`yadgar/static/index.html`:

1. On page load, BEFORE `loadGraph()`: fetch `/api/viz/config`, store in `window.YADGAR_VIZ_CONFIG`
2. Replace hardcoded constants with `YADGAR_VIZ_CONFIG.foo.bar` references:
   - `WIKI_CAT_COLOR = YADGAR_VIZ_CONFIG.node.category_colors`
   - `_linkColor(l) = (YADGAR_VIZ_CONFIG.edge.color[l.type] || default)`
   - `nodeRelSize(YADGAR_VIZ_CONFIG.node.size_3d)`
   - etc.

### Fallback if config fetch fails

Frontend keeps hardcoded constants as fallback values. If `/api/viz/config` returns error / unreachable, viz falls back to today's hardcoded behavior. No silent regression.

## Tests

Backend:
1. `test_viz_config_endpoint_returns_yaml_values` — fixture config.yaml with custom values → endpoint returns those values
2. `test_viz_config_endpoint_returns_defaults_when_unset` — minimal config.yaml → endpoint returns hardcoded defaults
3. `test_viz_config_endpoint_auth_required` — unauth request → 401
4. `test_viz_config_registry_complete` — all viz knobs registered three-way per I25

Frontend (static-asset):
5. `test_loadGraph_fetches_viz_config` — assert `fetch('/api/viz/config')` present in `loadGraph` body
6. `test_viz_constants_reference_config` — assert `YADGAR_VIZ_CONFIG` referenced in node-color / link-color / nodeRelSize call sites

## Acceptance

- All new + existing tests green
- Pre-commit hooks pass (incl. I25 three-way config sync)
- Manual smoke: edit config.yaml → restart yadgar → reload viz → see knob applied
- Manual smoke: omit any single viz key → still works with default
- CHANGELOG + MIGRATION_NOTES v5.11.0 entry
- New `docs/VIZ_CONFIG.md` reference doc listing every viz knob + default + range

## Open questions

1. **Hot reload** — re-affirm out of scope. Config changes require restart.
2. **`/api/viz/config` authentication** — match other `/api/*` (bearer token). Yes.
3. **Defaults snapshot** — should `docs/VIZ_CONFIG.md` doc auto-generate from config_registry? Lean YES — single source of truth.
4. **Color picker validation** — accept hex / hsl / rgb / named? Lean hex only for simplicity; document validation.

## Risks + rollback

| Risk | Mitigation |
|---|---|
| Config schema bug breaks viz | Frontend fallback to hardcoded defaults |
| Adding ~25 new knobs explodes config.yaml | Defaults stay implicit — config.yaml only needs entries for OVERRIDES |
| Per-deployment config doesn't suit per-user preferences | Future minor: per-user UI control panel |

Rollback: revert v5.11.0 commits. Hardcoded values return. No data migration needed.

## Effort

| Phase | Days |
|---|---|
| Backend config schema + registry | 0.5 |
| `/api/viz/config` endpoint + tests | 0.5 |
| Frontend wiring (fetch + apply) | 1.0 |
| Tests + docs | 0.5 |
| **Total** | **~2.5 days** |

## Files to add / modify

### New
- `docs/VIZ_CONFIG.md` — reference doc
- `yadgar/server/tools/viz_config.py` (or extend existing) — `/api/viz/config` route

### Modify
- `yadgar/config.py` — add `viz` Settings fields
- `yadgar/config_registry.py` — register all viz knobs (I25)
- `yadgar/config_yaml.py` — add `viz` section parsing
- `yadgar/static/index.html` — fetch + apply config; constants reference `YADGAR_VIZ_CONFIG`
- `CHANGELOG.md` + `MIGRATION_NOTES.md` — v5.11.0 entries
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.10.11 → 5.11.0

## Cross-references

- `docs/PLAN_V5_10_11_VIZ_EDGE_THICKNESS_AND_REPULSION.md` — last hardcoded values; becomes defaults
- `docs/VERSIONING.md` — convention update for skip-1 minors (see roadmap)
- v5.11.0 pre-empts old slot (secret-gate context-awareness) — cascades pipeline by 2 per skip-1 rule
