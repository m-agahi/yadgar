# PLAN — v5.10.7.3: Revert v5.10.7 custom node geometry; back to ForceGraph3D defaults

**Status:** drafted 2026-05-30 (third viz attempt today). Plan-first per I27. Hotfix for v5.10.7/.1/.2 viz regression.

**Master at draft time:** v5.10.7.2 LIVE; user verified nodes STILL render as fragmented shards despite theory-1 fix (transparent flag conditional).

**Sequencing:** v5.10.7.3 ships immediately. Independent of other v5.10.x slots.

---

## Why

Three attempts at custom 3D node geometry have failed:
- **v5.10.7** (`86f2e8f`): Introduced `_makeNodeThreeObject` with `OctahedronGeometry`/`SphereGeometry` + `MeshLambertMaterial`. Fragmented shards observed (Lambert needs lights ForceGraph3D doesn't add).
- **v5.10.7.1** (`b6bafcd`): Swapped to `MeshBasicMaterial` (unlit). Still fragmented.
- **v5.10.7.2** (`1c198d0`): Conditional `transparent` flag (only when dimming). User confirmed STILL fragmented.

Investigation (2026-05-30) found 3D heat-coloring NEVER worked historically — v5.3.7 (`e6c4057`) introduced 3D mode using ForceGraph3D's default solid spheres (uniform color, library handles all rendering). v5.10.7 was the first time we tried to override 3D node rendering, and it never produced solid mesh.

User's actual ask (verbatim): *"i used to see the spheres and hexagon shapes. they worked though all the same color. now they are not working at all."* User wants solid visible nodes back. Heat coloring is bonus.

## Approach: full revert + .nodeColor() preserved

Remove all custom 3D mesh code from v5.10.7. Keep `.nodeColor(_nodeColorFor)` because it works WITH ForceGraph3D's default rendering (the library applies it to its default sphere material).

### Bonus possibility

Per Theory 2 from investigation: `.nodeColor()` was redundant when `.nodeThreeObject(_makeNodeThreeObject)` was set, because the custom mesh replaced the default sphere. **Removing the custom mesh might actually let `.nodeColor(_nodeColorFor)` work** — meaning we'd get HEAT-COLOURED default spheres. That's the S2.1 (3D heat color) goal, which has never worked historically. Could be an unintended win.

If it doesn't work — at minimum we restore solid uniform spheres (last-known-good from v5.3.7).

### What stays

- 2D mode (`graph_2d.js` / canvas drawing) — untouched. Per investigation, 2D had wiki polygon-shape + memory sphere working all along. Not what's broken.
- `_nodeColorFor` function — kept. May or may not affect default spheres; if it does, bonus heat-coloring.
- Search highlight logic — restored to pre-v5.10.7 simpler 2D-only path; 3D path drops the `.nodeThreeObject` re-call (now no-op anyway).

### What goes

- `_makeNodeThreeObject` function — deleted entirely
- `.nodeThreeObject(_makeNodeThreeObject).nodeThreeObjectExtend(false)` calls in 3D init — removed
- The S2.2 (shape distinction wiki vs memory in 3D) — gone. Default spheres only. User explicitly OK'd this trade ("they worked though all the same color" — user is fine with uniform).
- Tests for `_makeNodeThreeObject` + transparent + OctahedronGeometry + SphereGeometry — deleted
- Regression test ADDED: assert `nodeThreeObject` NOT called in 3D init (prevents future re-introduction without plan)

## Acceptance

- 3D viz at `http://localhost:42069/` renders solid visible nodes (test with hard-refresh `Ctrl+Shift+R`)
- 2D viz unchanged
- All viz static-asset tests green
- Pre-commit hooks pass
- CHANGELOG + MIGRATION_NOTES updated

## Risk + rollback

| Risk | Mitigation |
|---|---|
| User wants octahedra shapes back later | Tracked in DECISIONS.md as "v5.10.7 custom 3D mesh — REVERTED 2026-05-30, three attempts failed; reconsider if user requests + after deeper ThreeJS+ForceGraph3D investigation" |
| Heat coloring with default sphere still doesn't render | Acceptable — user said uniform color is fine |

Rollback: revert v5.10.7.3 commits → back to broken v5.10.7.2 state. Not desirable.

## Files to modify

- `yadgar/static/index.html` — remove `_makeNodeThreeObject` (~5 lines) + `.nodeThreeObject(...).nodeThreeObjectExtend(false)` (~2 lines)
- `yadgar/static/index.html` `_applySearchHighlight` — drop the 3D `nodeThreeObject` re-call line
- `yadgar/tests/test_viz_static_assets.py` — delete TestV510701LightingFix + TestS22OctahedronForWiki + (parts of) other classes that asserted custom 3D mesh. Add new regression class asserting `.nodeThreeObject` NOT present in 3D init block.
- `CHANGELOG.md` — v5.10.7.3 entry
- `MIGRATION_NOTES.md` — v5.10.7.3 section
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.10.7.2 → 5.10.7.3
- `docs/PLAN_V5_10_7_VIZ_FIXES.md` — add "REVERTED IN v5.10.7.3" note at top covering S2.1 + S2.2 portions
- `docs/DECISIONS.md` — add entry under 2026-05-30 audit section noting the 3-attempt failure + revisit triggers
- Update wiki page `yadgar-roadmap-future-improvements` to mark v5.10.7.3 in pipeline + S2.1/S2.2 as "deferred — 3 failed attempts, revisit only with deeper investigation"

## Future work (separate plan, not v5.10.7.3 scope)

3D heat-coloring + shape distinction remain open. If user ever wants them:
- Need expert investigation of ForceGraph3D + ThreeJS interactions (current attempts revealed: scene lights needed for Lambert, transparent flag triggers triangle-sort artifact even at opacity 1.0, and some unknown 3rd cause that v5.10.7.2 didn't address)
- Possibly: switch to 2D-only as primary mode + 3D as experimental fallback
- OR: add explicit ambient + directional lights to ForceGraph3D scene + retry Lambert
- OR: switch to ForceGraph3D's `nodeAutoColorBy` + `nodeVal` API instead of custom mesh

Tracked as DECISIONS.md OQ entry for v5.X+ revisit.
