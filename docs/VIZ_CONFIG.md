# VIZ_CONFIG — Visualization Knobs Reference (v5.11.0)

All viz knobs are configurable via `config.yaml` (or matching `YADGAR_*` env vars). If a key is omitted, the v5.10.11 default is used unchanged — no behavioral change on no-config deploys.

Values are served to the frontend via `GET /api/viz/config` and applied before `loadGraph()` runs. A page reload is required after editing `config.yaml`.

---

## Node

| config.yaml key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `VIZ_NODE_SIZE_3D` | `YADGAR_VIZ_NODE_SIZE_3D` | float | `8.0` | 3D node sphere radius (`nodeRelSize`). ForceGraph3D default = 4; doubled to 8 in v5.10.10. |
| `VIZ_NODE_SIZE_2D` | `YADGAR_VIZ_NODE_SIZE_2D` | float | `4.0` | 2D canvas node base radius (pixels). |

### Heat colour HSL

Heat colour formula: `hsl((1-h)*hue_start + h*hue_end, sat_base+h*sat_gain%, light_base+h*light_gain%)`

| config.yaml key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `VIZ_HEAT_HUE_START` | `YADGAR_VIZ_HEAT_HUE_START` | int | `240` | Hue at h=0 (cool memories, blue). |
| `VIZ_HEAT_HUE_END` | `YADGAR_VIZ_HEAT_HUE_END` | int | `0` | Hue at h=1 (hot memories, red). |
| `VIZ_HEAT_SAT_BASE` | `YADGAR_VIZ_HEAT_SAT_BASE` | int | `60` | Saturation base %. |
| `VIZ_HEAT_SAT_GAIN` | `YADGAR_VIZ_HEAT_SAT_GAIN` | int | `30` | Saturation gain % (added at full heat). |
| `VIZ_HEAT_LIGHT_BASE` | `YADGAR_VIZ_HEAT_LIGHT_BASE` | int | `40` | Lightness base %. |
| `VIZ_HEAT_LIGHT_GAIN` | `YADGAR_VIZ_HEAT_LIGHT_GAIN` | int | `20` | Lightness gain % (added at full heat). |

### Wiki category colours

| config.yaml key | Env var | Type | Default |
|---|---|---|---|
| `VIZ_CAT_COLOR_ARCHITECTURE` | `YADGAR_VIZ_CAT_COLOR_ARCHITECTURE` | string (hex) | `#58a6ff` |
| `VIZ_CAT_COLOR_DECISION` | `YADGAR_VIZ_CAT_COLOR_DECISION` | string (hex) | `#ffa657` |
| `VIZ_CAT_COLOR_PATTERN` | `YADGAR_VIZ_CAT_COLOR_PATTERN` | string (hex) | `#3fb950` |
| `VIZ_CAT_COLOR_DEBUGGING` | `YADGAR_VIZ_CAT_COLOR_DEBUGGING` | string (hex) | `#f85149` |
| `VIZ_CAT_COLOR_REFERENCE` | `YADGAR_VIZ_CAT_COLOR_REFERENCE` | string (hex) | `#8b949e` |
| `VIZ_CAT_COLOR_CONVENTION` | `YADGAR_VIZ_CAT_COLOR_CONVENTION` | string (hex) | `#d2a8ff` |
| `VIZ_CAT_COLOR_FACT` | `YADGAR_VIZ_CAT_COLOR_FACT` | string (hex) | `#a5d6ff` |
| `VIZ_CAT_COLOR_ANALYSIS` | `YADGAR_VIZ_CAT_COLOR_ANALYSIS` | string (hex) | `#d29922` |

---

## Edge

| config.yaml key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `VIZ_EDGE_COLOR_SEMANTIC` | `YADGAR_VIZ_EDGE_COLOR_SEMANTIC` | string (hex) | `#1f6feb` | Semantic edge colour. |
| `VIZ_EDGE_COLOR_TEMPORAL` | `YADGAR_VIZ_EDGE_COLOR_TEMPORAL` | string (hex) | `#6e40c9` | Temporal edge colour. |
| `VIZ_EDGE_COLOR_TRANSITION` | `YADGAR_VIZ_EDGE_COLOR_TRANSITION` | string (hex) | `#3fb950` | Transition edge colour. |
| `VIZ_EDGE_COLOR_WIKI_CROSSREF` | `YADGAR_VIZ_EDGE_COLOR_WIKI_CROSSREF` | string (hex) | `#d2a8ff` | Wiki cross-reference edge colour. |
| `VIZ_EDGE_COLOR_MEMORY_WIKI` | `YADGAR_VIZ_EDGE_COLOR_MEMORY_WIKI` | string (hex) | `#ffa657` | Memory→wiki edge colour. |
| `VIZ_EDGE_WIDTH_3D_MULTIPLIER` | `YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER` | float | `1.5` | 3D-only edge width multiplier over 2D base (v5.10.11: +50%). |
| `VIZ_EDGE_ARROW_LEN` | `YADGAR_VIZ_EDGE_ARROW_LEN` | int | `5` | Arrow length for directional edge types (transition, memory_wiki, wiki_crossref). |

---

## Physics

| config.yaml key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `VIZ_PHYSICS_CHARGE_STRENGTH` | `YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH` | float | `-12.0` | D3 charge (repulsion) strength. Default -30 spreads clusters too far; -12 = softer repulsion. |
| `VIZ_PHYSICS_LINK_DISTANCE_2D` | `YADGAR_VIZ_PHYSICS_LINK_DISTANCE_2D` | float | `30.0` | D3 link distance in 2D mode. |
| `VIZ_PHYSICS_LINK_DISTANCE_3D` | `YADGAR_VIZ_PHYSICS_LINK_DISTANCE_3D` | float | `36.0` | D3 link distance in 3D mode (v5.10.11: 30 × 1.2). |

---

## Layout

| config.yaml key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `VIZ_LAYOUT_ZOOM_FIT_TICK` | `YADGAR_VIZ_LAYOUT_ZOOM_FIT_TICK` | int | `80` | Engine tick threshold before auto-zoom-fit fires (v5.10.10 default). |
| `VIZ_LAYOUT_ZOOM_FIT_PADDING` | `YADGAR_VIZ_LAYOUT_ZOOM_FIT_PADDING` | int | `50` | Padding (px) passed to `zoomToFit()`. |
| `VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS` | `YADGAR_VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS` | int | `800` | Transition duration ms for `zoomToFit()`. |

---

## Search

| config.yaml key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `VIZ_SEARCH_MATCH_COLOR` | `YADGAR_VIZ_SEARCH_MATCH_COLOR` | string (hex) | `#ffffff` | Stroke colour for nodes that match the current search query. |
| `VIZ_SEARCH_PINNED_COLOR` | `YADGAR_VIZ_SEARCH_PINNED_COLOR` | string (hex) | `#ffd700` | Stroke colour for pinned (clicked-to-pin) nodes. |
| `VIZ_SEARCH_DIM_OPACITY` | `YADGAR_VIZ_SEARCH_DIM_OPACITY` | float | `0.18` | Opacity for non-matched dimmed nodes when a search is active. |

---

## Example config.yaml overrides

```yaml
# Override edge colors only — all other knobs use defaults
VIZ_EDGE_COLOR_SEMANTIC: '#00ff88'
VIZ_EDGE_COLOR_TEMPORAL: '#ff8800'

# Increase node size in 3D
VIZ_NODE_SIZE_3D: 12

# Slower zoom-fit animation
VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS: 1200

# Stronger repulsion
VIZ_PHYSICS_CHARGE_STRENGTH: -20
```

---

## Notes

- Color values: hex only (e.g. `#ff0000`). HSL/RGB/named CSS colors not validated.
- Changes require: `systemctl restart yadgar` (or `docker-compose up -d`) + browser hard-refresh (`Ctrl+Shift+R`).
- Live-reload without restart is out of scope for v5.11.0 (deferred to a future minor).
- Each knob is also settable via `YADGAR_<KEY>=<value>` environment variable — env var takes priority over config.yaml.
