# PLAN — v5.10.7: Viz UX fixes (S2.1–S2.4)

**Status:** drafted 2026-05-29. Soak-observed since 2026-05-20 (memory id 494192). Originally scoped for v5.5, dropped when v5.5.1 went log-management direction. Re-scoped now after user noticed nothing addresses them. Renumbered v5.11.x→v5.10.7 on 2026-05-30 per all-drafts-get-concrete-version rule.

**Master at draft time:** core v5.10.2 + backend v5.4.0 deployed.

**Sequencing:** v5.10.7 patch. Slots after v5.10.6 (session-end capture). Independent of v5.10.4 (consolidate_now) and v5.10.5 (nightly cycle bugs). Can ship in parallel with v5.11.0 anchor work — different files (`yadgar/static/index.html`, `yadgar/viz_server.py` vs anchor/audit code).

---

## Why

Four viz UX bugs observed in 2026-05-20 soak screenshot, still present 2026-05-29:

### S2.1 — 3D mode does NOT color nodes by heat

Uniform yellow/pale dots in 3D. Heat gradient invisible. 2D mode (V4 toggle from v5.3.7) had this working. Either:
- THREE.js `MeshBasicMaterial` color attr not set from `node.heat`
- Heat-color shader / vertex attribute missing in 3D path
- 3D renderer uses different code path than 2D and never picks up the per-node color

Check viz HTML / TS files: `graph_3d.*`, `scene-setup-*`, `node-material-*`. Probably `node.color` set per heat in 2D path but not propagated to ThreeJS material in 3D.

### S2.2 — Wiki vs memory shape distinction too subtle

Wiki = "polysided shape with a lot of sides" (icosahedron / dodecahedron / sphere with high segment count). Memory = sphere. With many sides on the wiki shape, it looks like a sphere too — visually indistinguishable.

**Fix candidates (pick one in TDD):**
- Use clearly distinct geometry: cube/tetrahedron/octahedron for wiki (lower polygon count = visibly faceted), sphere for memory
- OR torus/ring for wiki, sphere for memory
- OR size discriminator + subtle shape difference

Lean: octahedron for wiki (8-sided, visibly faceted, clear silhouette) vs sphere for memory.

### S2.3 — Semantic search in viz does NOT work

Search box + button present (top toolbar). Click submits but no result. JS console error (verified 2026-05-20):

```
Search error: graph.nodeCanvasObject is not a function
```

Root cause: search handler calls `nodeCanvasObject` which is a 2D-only method on `force-graph` library. Default mode is 3D (per v5.3.7 V4 default). 3D uses `nodeThreeObject` from `3d-force-graph` library — DIFFERENT method name + different signature (returns a THREE.js object, not draws onto canvas).

**Fix:**
- Detect current mode (2D vs 3D) via stored state OR feature-detect graph instance
- Branch:
  - 2D → use existing `nodeCanvasObject` path
  - 3D → use `nodeThreeObject` + custom THREE.Mesh / Sprite for highlight
- Possibly simpler: when search finds a hit, just call `graph.zoomToFit()` + colorMatchedNodes via node attribute (no per-node draw override). Let force-graph render natively.

Fix location: `yadgar/static/index.html` search handler (where V1 search shipped in v5.3.7).

### S2.4 — CPU + DB size trends in Stats panel don't animate / update

Stats panel shows static numbers. Either:
- SSE/poll loop dead in front-end
- OR backend endpoint not emitting
- OR computed but not re-rendered

Check viz Stats panel JS — likely missing `setInterval` / `EventSource` reconnect, or backend endpoint returning static value.

Visible state from soak screenshot: 700 nodes / 118 (clipped). Memories 2211, Wiki 1773, Transitions 0 (unusual — should be > 0 if cogmap active), Temporal 36.

---

## What ships

All four fixes as separate commits on a single branch. Single release commit.

Touched files (expected):
- `yadgar/static/index.html` — search mode detection (S2.3), heat color propagation (S2.1), shape change (S2.2), stats refresh loop (S2.4)
- `yadgar/viz_server.py` — possibly: new endpoint for live stats SSE if S2.4 requires backend change
- `yadgar/tests/test_viz_*.py` — new test_viz_search_3d.py + test_viz_stats_refresh.py (subprocess-style or static-asset-string-check tests)

NO yadgar core schema/MCP-tool changes. Pure frontend + viz proxy.

---

## What does NOT ship

| Item | Why deferred |
|---|---|
| Viz performance (frame rate, large-graph rendering) | Soak shows 700 nodes — already smooth. Not observed bottleneck. |
| Viz authentication / multi-user | Out of yadgar scope; single-user desktop. |
| Mobile-responsive viz | No request, out of scope. |
| Replace force-graph with another viz library | Way too big a refactor. v5.X+ only if needed. |

---

## Implementation order

1. **S2.3 first (highest user impact)** — search broken since v5.3.7 release. 1-line cause (`nodeCanvasObject` vs `nodeThreeObject`), fix < 30 LOC. TDD via static asset string check or headless-browser test if feasible.
2. **S2.1 second (visible signal)** — heat color in 3D. Investigate where 2D path sets color; replicate in 3D path. ~30 LOC.
3. **S2.2 third (cosmetic)** — shape distinction. Pick octahedron for wiki. ~15 LOC.
4. **S2.4 fourth (UX polish)** — Stats panel refresh loop. Investigate front-end interval logic. ~30 LOC.
5. **Tests** — minimal: assert search handler in HTML has 3D branch; assert stats panel has interval; manual verify in browser before release.
6. **Version bump** — 5.11.0 → 5.11.x (whichever patch slot; lean v5.11.1 if v5.11.0 ships first).
7. **MIGRATION_NOTES.md** + **CHANGELOG.md** entries.

---

## Acceptance criteria

- Browser load `http://127.0.0.1:42069`: 3D mode shows heat-colored nodes (gradient visible from heat=0.1 cold blue → heat=1.0 hot red, or whichever palette current).
- Wiki nodes visibly distinct shape (octahedron) vs memory (sphere) at default zoom.
- Search box submit → matching nodes highlighted, others de-emphasized. Console: zero JS errors.
- Stats panel: numbers update on poll interval (5-10s default).
- I13 + I23 + I24 + I25 + I26 + VER lints exit 0.
- `python scripts/check_versions.py` exit 0.
- Manual browser walkthrough green (no automated headless test required for cosmetic verification).

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Front-end fix breaks 2D mode (currently works) | Test both modes manually before release. Add 2D-vs-3D smoke checks. |
| Heat color formula in 3D differs from 2D (e.g. THREE color encoding) | Document current 2D formula in commit; replicate verbatim. |
| Stats SSE change risks backend resource leak | Use existing `/api/system` polling, not new SSE. Polling cap at 5s interval. |
| Shape change breaks user mental model | Octahedron is geometrically obvious — distinguishable at any zoom. Low risk. |
| Viz tests don't exist; hard to verify without browser | Acceptable — viz is single-user dev tool, manual smoke test is sufficient. Document procedure. |

---

## Estimate

~100 LOC implementation + ~50 LOC manual smoke procedure docs. Single agent dispatch, 45-60 min.

---

## Sequencing

| Plan | Status | Order |
|---|---|---|
| v5.10.3 scan script fix | in-flight | imminent |
| v5.10.4 nightly cycle remaining bugs | drafted (informally) | next hotfix |
| v5.10.5 session-end capture | drafted | after v5.10.4 |
| **v5.11.x viz fixes (this)** | drafted | ship after v5.11.0 OR pull forward as v5.10.6 if user wants viz NOW |
| v5.11.0 anchor cross-project + Jira | drafted | 4-week wait |
| Backend v5.4.1 N+1 hydration | drafted | independent track |

**User can pull viz forward if pain > "wait for v5.11.0".** Pure docs / frontend; no schema. Safe to ship as patch any time.

---

## Open / parked questions

- **2D vs 3D default in 3D-fix:** v5.3.7 V4 default is 3D. Confirm via existing `localStorage` key. Lean: keep 3D default.
- **Stats panel poll interval:** lean 5s (matches existing dbsize cache TTL of 60s — values won't be stale-stale). Configurable knob if needed.
- **Should we add headless-browser test (Playwright)?** Big infra add for a cosmetic fix. Skip; document manual smoke procedure instead.

---

## v5.X+ follow-up (deferred)

- Viz performance for 5K+ node graphs (currently 2K — comfortable).
- Viz dark mode toggle.
- Live anchor highlighting (red border for `_anchor`-tagged nodes).
- Viz "replay last session" mode pulling from action_log.
