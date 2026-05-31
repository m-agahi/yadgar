# PLAN — v5.10.11: Viz polish — 3D edge thickness +50% + connected-node repulsion +20%

> **STATUS: SHIPPED v5.10.11 (2026-05-30)**

**Status:** drafted 2026-05-30. Plan-first per I27. Patch on v5.10 minor train.

**Master at draft time:** v5.10.10 LIVE (2x 3D node size + auto-zoom-fit shipped).

**Sequencing:** v5.10.11 patch. Last v5.10.x polish before v5.11.0 viz config.yaml refactor.

---

## Why

User feedback post-v5.10.10:
> *"make them 50 percent thiker. only in 3d. also repel the nodes connected with edges 20 percent more."*

Two small tweaks to 3D presentation. Hardcoded values; will be replaced by config.yaml knobs in upcoming v5.11.0.

## Goals

1. **3D edge thickness +50%.** Find current 3D edge width; multiply by 1.5.
2. **Connected-node repulsion +20% in 3D.** ForceGraph3D uses d3 `forceLink` between connected nodes; increase its strength or distance.

## Non-goals

- 2D edge thickness — user said "only in 3D"
- Coloring — explicitly preserve (carrying constraint from v5.10.10)
- Other physics tweaks (charge, center, etc.) — only the connected-edge repulsion changes

## Approach

### Fix 1: 3D edge thickness +50%

Current code (line 858, after `linkColor`):

```javascript
.linkWidth(_linkWidth)
```

`_linkWidth` is shared between 2D and 3D. Don't modify it — that'd change 2D too. Instead override only the 3D init's `.linkWidth` with a 3D-specific wrapper:

```javascript
.linkWidth(l => _linkWidth(l) * 1.5)   // v5.10.11: 3D-only +50% thickness
```

Leave 2D init's `.linkWidth(_linkWidth)` unchanged.

### Fix 2: Connected-node repulsion +20% in 3D

ForceGraph3D's `d3Force('link')` controls the link force between connected nodes. The `distance` parameter is the natural rest length — INCREASING distance = nodes spread further apart along edges = "more repulsion between connected nodes".

Current code (line 918):
```javascript
graph.d3Force('link').distance(30);
```

This applies to BOTH 2D + 3D (set after init in shared post-init block). For 3D-only +20%, gate by mode:

```javascript
if (mode === '3d') {
  graph.d3Force('link').distance(36);   // v5.10.11: 30 * 1.2 = 36 (+20%)
} else {
  graph.d3Force('link').distance(30);
}
```

Or refactor to a constant + multiplier per mode. Lean simple conditional for v5.10.11; v5.11.0 config.yaml will replace with knob.

## Tests (static-asset regression)

`yadgar/tests/test_viz_static_assets.py`:

1. `test_3d_linkWidth_multiplier_present` — assert `_linkWidth(l) * 1.5` (or equivalent multiplier expression) in 3D init block.
2. `test_3d_link_distance_36` — assert `graph.d3Force('link').distance(36)` literal AND `if (mode === '3d')` guard.
3. `test_2d_linkWidth_unchanged` — assert 2D init block still has plain `.linkWidth(_linkWidth)` (no multiplier).

## Acceptance

- 3 new + existing viz tests green
- Pre-commit hooks pass
- CHANGELOG + MIGRATION_NOTES v5.10.11 entry
- Manual smoke after deploy + hard-refresh: 3D edges visibly thicker; 3D nodes connected by edges visibly spaced further apart; 2D unchanged

## Risks + rollback

| Risk | Mitigation |
|---|---|
| 3D edges too thick at high-edge counts | User asked for 50%; trivial revert if "too much" |
| Repulsion increase breaks force-layout convergence | 20% bump is small; force engine stable at 30; 36 well within library defaults (30-100 typical) |

Rollback: revert v5.10.11 commits. v5.10.10 state.

## Files to modify

- `yadgar/static/index.html` — 2 small edits (3D `linkWidth` wrapper + mode-gated link distance)
- `yadgar/tests/test_viz_static_assets.py` — 3 regression tests
- `CHANGELOG.md` — v5.10.11 entry
- `MIGRATION_NOTES.md` — v5.10.11 section
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.10.10 → 5.10.11

## Effort

~15 min. Trivial.

## Forward link

v5.11.0 plan (next: `docs/PLAN_V5_11_0_VIZ_CONFIG_YAML.md`) will refactor ALL viz knobs (node size, colors, physics, edge width/color) into `config.yaml`. v5.10.11 hardcoded values become defaults exposed via config there.

## Cross-references

- `docs/PLAN_V5_10_10_VIZ_NODE_SIZE_AND_ZOOM_FIT.md` — prior viz polish (preserved)
- `docs/PLAN_V5_11_0_VIZ_CONFIG_YAML.md` — successor minor (knob-ification)
- `docs/VERSIONING.md` — patch numbering rule
