# PLAN — v5.10.7.2: 3D viz lighting fix (Lambert → Basic material)

## ABSORBED INTO v5.10.7.1

This plan was originally designated v5.10.7.2. It was absorbed into v5.10.7.1 to ship both hotfixes in a single release cycle. The implementation is complete in `feat/v5.10.7.1-bundled-hotfix`. See `docs/PLAN_V5_10_7_1_SENTINEL_FILTER_LOCAL_COMMAND_TAGS.md` for the combined plan.

---

**Status:** drafted 2026-05-30. Plan-first per I27. Hotfix for v5.10.7 viz regression observed post-deploy.

**Master at draft time:** v5.10.7 SHIPPED LIVE; viz wiki nodes render as dark fragments instead of solid octahedra.

**Sequencing:** v5.10.7.2 ships AFTER v5.10.7.1 (sentinel filter). Both small hotfixes can bundle into one nix-apply if shipped close together, but tracked as distinct version slots per I27.

---

## Why

User screenshot post-v5.10.7 deploy (2026-05-30): wiki nodes render as fragmented triangular shapes (visible only at edges/back-faces) rather than solid octahedra. Memory node (single visible green sphere) renders correctly.

Visual reproduction:
- 3D mode at `http://localhost:42069/`
- ~80 wiki nodes look like sparse purple triangle clusters
- 1 memory node looks like a normal solid green sphere
- No search active (`__dimmed` shouldn't apply)

## Root cause

`yadgar/static/index.html:818` uses `THREE.MeshLambertMaterial`:

```javascript
const mat = new THREE.MeshLambertMaterial({ color, transparent: true, opacity: node.__dimmed ? 0.18 : 1.0 });
```

Lambert material requires **scene lighting** (ambient + directional lights) to render colour. ForceGraph3D's default scene does NOT add lights — assumes user's custom geometry uses unlit material (Basic) or that the consumer adds lights explicitly.

Without lights:
- Lambert renders at near-black (no diffuse reflection)
- `transparent: true` shows back-faces through → fragmented appearance
- Only edges catch tiny ambient → triangle-shard look

Memory sphere renders OK because... it's a smaller geometry and presumably its dark-but-not-fragmented appearance gets through. Or the user's screenshot caught a single node that happened to render via different code path. (Verify during impl: is the "green" sphere the default ForceGraph3D rendering, not our `_makeNodeThreeObject`?)

## Goals

1. Wiki nodes render as solid coloured octahedra (S2.2 fix actually working).
2. Memory nodes render as solid coloured spheres.
3. Heat gradient visible per node (S2.1 confirmed).
4. No additional scene complexity (don't add lights — that opens more variables).

## Non-goals

- Adding lights to the scene (more invasive; pulls in colour-temperature concerns)
- Changing geometry (octahedron vs sphere stays per S2.2)
- Custom shaders / advanced material (over-engineering)

## Approach

### Fix: `MeshLambertMaterial` → `MeshBasicMaterial`

`THREE.MeshBasicMaterial` is unlit — colour always renders at the set value regardless of scene lights. Standard choice for "I just want this colour to show up" use cases (axis helpers, debug nodes, etc.). Single line change.

```javascript
// Before:
const mat = new THREE.MeshLambertMaterial({ color, transparent: true, opacity: ... });
// After:
const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: ... });
```

`transparent: true` + `opacity` semantics unchanged. Color semantics unchanged. Z-fighting / depth behavior unchanged.

### Tests

Static-asset tests in `yadgar/tests/test_viz_static_assets.py` should grep for `MeshBasicMaterial` (and assert no `MeshLambertMaterial` in `_makeNodeThreeObject` block). Two updates to existing test file:
1. Replace any existing `assert "MeshLambertMaterial" in src` with `assert "MeshBasicMaterial" in src`
2. Add `assert "MeshLambertMaterial" not in node_obj_block` (regression gate)

### Manual smoke procedure

Document in MIGRATION_NOTES v5.10.7.2:
1. Open `http://localhost:42069/` in 3D mode
2. Expect: solid purple octahedra (wiki) + solid coloured spheres (memory)
3. Heat gradient visible — high-heat = bright, low-heat = dim
4. Search highlights — gold tint applies to pinned nodes; dimmed nodes properly translucent (not totally invisible)

## Acceptance

- Static-asset tests green (with MeshBasic regression gate)
- Manual smoke matches expected rendering
- Pre-commit hooks pass
- CHANGELOG + MIGRATION_NOTES updated

## Open questions

1. **Should we add scene lights instead** so other ThreeJS material types (Lambert, Phong, Standard) work for future viz features? — Lean NO for v5.10.7.2 (scope creep). Document as v5.X+ candidate if material variety becomes a real need.
2. **Why did agent pick Lambert originally?** Likely defaulted to "looks 3D" intuition without verifying scene has lights. Lesson recorded — add to DECISIONS.md if there's a generic "test viz changes in browser" entry (likely already covered by manual smoke procedure tradition).

## Files to modify

- `yadgar/static/index.html` — 1 line change (`MeshLambertMaterial` → `MeshBasicMaterial`)
- `yadgar/tests/test_viz_static_assets.py` — assertion updates (~2 lines)
- `CHANGELOG.md` — v5.10.7.2 entry
- `MIGRATION_NOTES.md` — v5.10.7.2 section
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — 5.10.7.1 → 5.10.7.2 (or 5.10.7 → 5.10.7.2 if v5.10.7.1 not yet shipped)

## Effort

~15 minutes code + tests + release artifacts. Faster than v5.10.7.1 sentinel filter.

## Dependencies

- v5.10.7 shipped ✓ (this is its hotfix)
- v5.10.7.1 sentinel filter — independent; either order OK; or bundle into single nix-apply cycle for efficiency

## Risk + rollback

| Risk | Mitigation |
|---|---|
| Switching to Basic loses depth-cue lighting (3D effect) | Acceptable — current Lambert renders invisible anyway. Better visible flat than invisible "3D". |
| Other places in scene rely on Lambert | Search confirms `_makeNodeThreeObject` is the only Lambert-using site in `yadgar/static/index.html`. Other geometry uses force-graph defaults. |

Rollback: revert the 1-line change. Returns to v5.10.7 broken-but-shipped state.
