# Viz Trace-Replay View Polish — 5-item batch (task #54)

- **Status:** PROPOSED
- **Date:** 2026-07-18
- **Branch target:** feat/bug-train (or fresh off master)
- **Version:** core **5.150.0** (current 5.149.0). **Core-only — NO backend bump.** (Verdict below.)
- **Scope:** viz "Traces" tab replay view. 5 polish items. Root-caused item-1 empirically.

---

## TL;DR — the verdict that drives the batch

Item-1 (core lane empty) root-caused to **(d): stage-selection emits only DESCENDANTS of
the `tool.recall` boundary; the boundary span itself + the thin core→backend forwarder are
never rendered.** The heavy 18 pipeline stages all *legitimately* carry `svc=yadgar-backend`
(recall is forward-only — FTS/KNN/PPR/spreading/fusion/rerank/profile all run in the backend
process). So the core lane has zero KEPT nodes.

- **NOT (a)** — svc attr is correct on every span.
- **NOT (b)** — `_lane()` (trace_mesh.py:509) defaults any non-`yadgar-backend` svc to `core`;
  a core span **cannot** be mislaned to backend by this code.
- **NOT (c)** — core→backend W3C traceparent **is** propagated (shared trace_id). Proven by
  `yadgar/tests/core/test_backend_traceparent_e2e.py` (passes) + the choke-point
  `_instrument_httpx()` at `tracing.py:520-537` (HTTPXClientInstrumentor, wired in
  `setup_tracing`). The viz fetch-by-id returns ONE trace containing BOTH lanes' spans.

**Fix = core-only mesh change:** inject the boundary `tool.recall` span (label "Recall") as
the first core-lane node in `build_mesh`, and preserve the core forwarder ("Recall 1.3ms" =
the `_forward_to_backend` / core-side httpx `POST` span). No traceparent work, no backend
rebuild.

### Evidence (reproduced, not asserted)

Ran two captures through `build_mesh`:

1. **Stale fixture** `yadgar/tests/fixtures/traces/normal_two_lane.json` — its spans carry
   `svc=yadgar-core` for pipeline work (pre-forward-only shape) → **4 core nodes, 2 backend**.
   The lane logic WORKS when svc is right. This fixture no longer matches reality.
2. **Realistic forward-only recall** (boundary + forwarder core; all pipeline `svc=yadgar-backend`)
   → **core-lane = 0, backend-lane = 8, `dropped_boundary=False`.** Exactly the reported bug.

So the bug is not in lane math — it is that `select_stages(tool)` (trace_mesh.py:469) iterates
`_kept_children(tool, …)` and returns the tool's *children*; `tool.recall` itself is structurally
never a node, and the only intervening core spans (`_forward_to_backend`, `POST`) are
plumbing-collapsed (`POST` matches the PLUMBING regex trace_mesh.py:52; the forwarder's
self-work is trivial vs its ~298ms subtree so `_phase_box` drops it).

---

## Item-1 — CORE LANE EMPTY (root cause + fix)

### Files
- `yadgar/_shared/trace_mesh.py` — `build_mesh` (:529-594), `select_stages` (:468-484),
  `find_tool_span` (:487-500), `_lane` (:508-510), `Span` (:195-216).
- (no JS change needed — `layoutStages` already lanes by `node.lane`.)

### Fix — inject the boundary + forwarder as core-lane nodes

In `build_mesh`, after `tool = find_tool_span(root, tool_base)` and `stages = select_stages(...)`,
**prepend a synthetic core-lane "Recall" boundary node** representing the tool span itself, so
the core lane is never empty for any `tool.*` trace. Two sub-parts:

1. **Boundary node.** When `tool is not None` and `tool.svc` maps to the core lane, build a node
   from `tool` (label via `_friendly` → add alias `"recall": "Recall"` etc., or humanize) and
   make it `stages[0]`. Its `dur_ms` = the core self-time (tool.dur − sum(child dur)), floored at
   a small value so the dwell is visible; `rel_ms = tool.rel` (0.0).
2. **Forwarder node ("Recall 1.3ms").** The core-side `_forward_to_backend` / `POST` span is the
   hand-off. Surface it as a second core node IF present with core svc. Simplest: in
   `select_stages`, stop plumbing-collapsing the single core→backend crossing span — keep exactly
   one "hand-off to backend" node on the core side. Alternative (cleaner, preferred): a dedicated
   `_core_boundary_nodes(tool)` helper that returns `[boundary, forwarder]` (0–2 core nodes) and
   is prepended to `stages`, keeping `select_stages` untouched.

**Preferred shape (pure + testable):**

```python
def core_boundary_stages(tool: Span, backend_stages: list[Span]) -> list[Span]:
    """Return the 0-2 synthetic CORE-lane lead nodes for a forward-only tool trace:
    the boundary tool span (self-time) + the core->backend forwarder, if core-svc.
    Pure — returned Spans get laned by _lane(svc) like any other."""
```

- Prepend its output to `stages` in `build_mesh` before node emission.
- Add ALIAS entries: `"recall": "Recall"`, `"_forward_to_backend": "forward to backend"`.
- Respect `MAX_BOXES` — the +1/+2 lead nodes count toward the cap (`_enforce_cap` already
  treats crossing/mandatory nodes; mark the boundary mandatory so it's never dropped).

### Why core-only (scope confirmation)
No backend change: the backend already emits its stages with `svc=yadgar-backend` correctly and
already receives+continues the traceparent. The only defect is the core-side *rendering* of the
boundary+forwarder. **No `backend_version` bump (per #83).**

### Tests (vitest N/A — this is Python)
- `test_build_mesh_forward_only_recall_has_core_boundary` — realistic backend-only-pipeline span
  list (the repro above) → assert `>=1` core-lane node, first node label "Recall", backend stages
  still present. Add a fixture `yadgar/tests/fixtures/traces/forward_only_recall.json`.
- `test_core_boundary_stages_pure` — unit the helper directly (0 core spans → boundary only;
  boundary + forwarder → 2 nodes).
- Keep `normal_two_lane.json` test green (it already has core nodes — must not double-count).
- Guard: a tool whose boundary is `svc=yadgar-backend` (shouldn't happen) → no phantom core node.

---

## Item-2 — CRAMPED spans (physics/force scatter respecting lanes)

### Root cause (confirmed by reading)
`traces-replay.js`: `layoutStages` sets `x = laneX(i, n)` (evenly spread by **index**) and
`y = LANE_Y[lane]` — **fixed y per lane**. So it is NOT "all at one x"; it is **all at one y per
lane** → every circle in a lane sits on a single horizontal line and the `y=34` labels
(traces-tab.js:343) + `y=-26` dwell texts collide when many stages share a lane. With 18 backend
stages on one y-line, labels overlap into mush.

### Design — lane-banded force scatter
Replace the single-y placement with a **band per lane** + within-band vertical spread:

- Each lane owns a y-BAND: `core` band `[core_y - H/2, core_y + H/2]`, `backend` likewise,
  with `H` sized so bands don't overlap (LANE_Y core=150, backend=318 → ~150px gap → band
  half-height ≤ ~55px keeps a clear divider gap around y≈234).
- **x by start-time** (not index): `x = x0 + (x1-x0) * (rel_ms / total_ms)` — real temporal
  position; clustered fast stages naturally sit close, then y-separation de-overlaps them.
- **y within band by force/jitter:** a small deterministic 1-D relaxation per lane — sort by x,
  then push neighbors apart in y when their x-gap < label-width, alternating up/down from the
  band center. Deterministic (seeded by index) so it's unit-testable and stable across reloads
  (no RAF physics — a closed-form relaxation pass, cheaper + reproducible).

### Pure helper (vitest)
`yadgar/core/static/traces-replay.js`, tested in `traces-replay.test.js`:

```js
/** Assign {x,y} respecting lane bands: x by start-time fraction, y force-separated
 *  within the node's lane band so circles+labels don't overlap. Pure, deterministic.
 *  @param {Array} nodes  mesh nodes ({rel_ms,lane,...})
 *  @param {number} totalMs
 *  @returns {Array} new nodes with {x,y} */
export function scatterLayout(nodes, totalMs) { ... }
```

- Replaces `layoutStages` at the call site (traces-tab.js:248) — keep `layoutStages` exported
  for back-compat/tests or delete if unused.
- Constants: reuse `MESH.x0/x1`, add `LANE_BAND = { half: 50, minGapX: 90 }` (label width).
- **Div-0 / empty-lane guard (ties to item-5):** a lane with 0 nodes must not crash; `totalMs<=0`
  → fall back to index spacing. Item-1's fix guarantees core always has ≥1 node, but core-only
  tools (bookmark_list) leave the backend lane empty — handle both.

### Tests (vitest, `traces-replay.test.js`)
- `scatterLayout: x monotonic in rel_ms`, `y within lane band`, `no two nodes closer than minGap
  in both x AND y` (de-overlap invariant), `empty node list → []`, `single node → centered x`,
  `all-same-rel_ms cluster → y fully spread across band`, `totalMs<=0 → index fallback`.
- Deterministic: same input twice → identical output (no Math.random).

### Risk
Pure math is vitest-covered; the **visual density** (band half-height, minGapX vs actual label
px) is a **user smoke-check** (no browser harness). Ship, ask user to eyeball a recall + a
memorize trace.

---

## Item-3 — brighter ORANGE DOTTED core/backend divider

### Root cause
There is **no divider element**. `_buildMesh` (traces-tab.js:296-313) draws two `.tr-lane-line`
guides (one per lane at y=150 / y=318), styled faint: `stroke: var(--viz-hair); stroke-dasharray:
2 6;` (traces-tab.css:117-120). No midline between the lanes.

### Fix — add a dedicated divider midline
1. **JS** (`traces-tab.js` `_buildMesh`, in `_lanesG`): append one `<line class="tr-lane-divider">`
   at `y = (LANE_Y.core + LANE_Y.backend) / 2` (≈234), `x1=30 … x2=MESH.w-30`. One element, drawn
   with the lane guides.
2. **CSS** (`traces-tab.css`): new rule —
   ```css
   .tr-lane-divider {
     stroke: var(--viz-amber);      /* #ffc35c orange/amber, already in viz-theme.css:61 */
     stroke-width: 1.6;
     stroke-dasharray: 3 5;         /* dotted/dashed */
     opacity: 0.85;                 /* brighter than the --viz-hair guides */
   }
   ```
   (If user wants a truer dotted look use `stroke-dasharray: 1 4` + `stroke-linecap: round`.)

### Test
No pure logic → **user smoke-check** only. Optionally a DOM-count assertion in an existing
jsdom test that `.tr-lane-divider` exists after `_buildMesh` if that path is already exercised.

---

## Item-4 — SPEED MULTIPLIER presets + persistence

### Root cause / current behavior
`traces-replay.js`: `SPEEDS = [0.5, 1, 2, 4]`, `DILATION = 150`. `advanceClock` (:115-124):
`nt = t + (dtWallMs / DILATION) * speed`. So ×1 = 150 wall-ms per 1 trace-ms (a **150× SLOWDOWN**,
not realtime). Speed button cycles the 4 presets (traces-tab.js:449-452); **not persisted**.

### Requested presets (playback ms of wall-time per 1ms of span)
| preset   | ms/1ms | meaning            |
|----------|--------|--------------------|
| slow     | 100    | 1ms → 100ms        |
| medium   | 50     | 1ms → 50ms         |
| fast     | 10     | 1ms → 10ms         |
| realtime | 1      | 1ms → 1ms (DEFAULT)|
| 2×       | 0.5    | 2× realtime        |
| 10×      | 0.1    | 10× realtime       |

> **⚠ AMBIGUITY TO CONFIRM WITH USER:** user wrote "10x = 0.01ms/100×". Treated as a typo:
> planned **10× = 0.1 ms/1ms (10× realtime)**, consistent with the 2× row. If the user truly
> wants a **100× (0.01 ms/1ms)** preset, add a 7th row "100×". **Flagging — do not ship until
> confirmed.**

### Design
This replaces the `SPEEDS × DILATION` model with a direct **ms-per-trace-ms** table:

```js
// traces-replay.js — ordered presets; value = wall-ms played per 1 trace-ms.
export const SPEED_PRESETS = [
  { id: 'slow',     label: 'Slow',     msPerMs: 100 },
  { id: 'medium',   label: 'Medium',   msPerMs: 50  },
  { id: 'fast',     label: 'Fast',     msPerMs: 10  },
  { id: 'realtime', label: 'Realtime', msPerMs: 1   },  // DEFAULT
  { id: '2x',       label: '2×',       msPerMs: 0.5 },
  { id: '10x',      label: '10×',      msPerMs: 0.1 },
];
export const DEFAULT_SPEED_ID = 'realtime';
```

- **Rewrite `advanceClock`** to consume `msPerMs`: `nt = t + dtWallMs / msPerMs`. (Drop `DILATION`
  + the old `SPEEDS[speedIdx]` path; keep clamping + `playing=false` at end.)
- **Persistence** (mirror `galaxy-view.js:117-153` injectable-storage pattern + bookmarks-tab
  localStorage): key `yadgar.traces.speed`. Pure helpers:
  ```js
  export function loadSpeedId(storage) { ... }   // returns a valid id or DEFAULT_SPEED_ID
  export function saveSpeedId(id, storage) { ... }
  export function speedById(id) { ... }          // preset lookup, falls back to realtime
  ```
  `storage` param defaults to `window.localStorage` (testable with a fake store, like galaxy-view).

### UI (traces-tab.js)
- Replace the single cycling `_speedBtn` (:129, :449-452) with either (a) a cycling button that
  shows the current `label` and persists on each click, or (b) a small `<select>` of the 6 presets.
  **Preferred: `<select>`** (6 presets is too many to cycle blindly). On change → `saveSpeedId` +
  update `_state.speedId`.
- On init (`_wireTransport` / `initTracesTab`): `_state.speedId = loadSpeedId()` and reflect in UI.
- `_state.speedIdx` → `_state.speedId` (string). `_tick` (:425-436) passes the preset's `msPerMs`.

### Behavior-change flag
Default becomes **realtime (1:1)** — a recall trace of ~250ms trace-time now replays in ~250ms
(near-instant) vs the old ×1 ≈ 37.5s. **Flag to user:** realtime default may feel "too fast to
watch"; Slow/Medium exist for that. Confirm default = realtime is intended.

### Tests (vitest)
- `advanceClock` with each preset: assert `dt/msPerMs` advance; end-clamp + `playing=false`.
- `loadSpeedId` — empty store → DEFAULT; garbage value → DEFAULT; valid id → that id.
- `saveSpeedId` round-trips through a fake storage.
- `speedById` — unknown id → realtime.

---

## Item-5 — other trace types must not break the mesh

### What could break (assumptions audited)
1. **`_spanset_to_capture` reparenting** (traces.py:340-397, the *fallback* path only): forces the
   `tool.*` span to depth-0 and everything else to depth-1. This is **fallback-only** (Tempo by-id
   500) and is generic over `tool.*` — not recall-specific. LOW risk, but the depth-1-flattening
   loses real hierarchy for deep traces (e.g. memorize's phase tree) → in fallback mode a
   deep-tree tool renders flat. Acceptable degrade; note it.
2. **Empty-lane rendering (ties to item-2):** core-only tools (`bookmark_list`, `block_list`,
   read tools) produce **zero backend spans** → backend lane empty. Item-1's boundary-node fix
   fills the core lane; item-2's `scatterLayout` **must not div-0 on an empty lane**. This is the
   real cross-item hazard.
3. **`dropped_boundary` forest** (`build_mesh` :557-562): audit_anchors-class traces where the
   boundary span was dropped by the OTLP queue → `tool=root`, flat forest. Already handled; the
   item-1 boundary-injection must **skip** when `dropped_boundary` (no real tool span to promote).
4. **`select_stages` recall-specific tuning:** `_merge_repeats` uses "threshold 4 so recall's
   THREE cross-encoder passes stay separate" (trace_mesh.py:426). This is a heuristic, not a
   correctness bug for other tools — a non-recall tool with 4+ same-name siblings merges them,
   which is the intended storm behavior. No fix needed; document.
5. **`total_ms=0` traces** (instant read tools): `computeTimeline` last-stage `end=totalMs=0`,
   dwell floored at 0.01 — OK. `scatterLayout` needs the `totalMs<=0 → index fallback` guard
   (item-2). Playhead `msToFraction` already guards `totalMs<=0 → 0`.

### Guard plan
- Item-1 boundary injection: `if dropped_boundary or tool is root: skip` (no phantom node).
- Item-2 `scatterLayout`: explicit empty-lane + `totalMs<=0` guards (covered in item-2 tests).
- **Add fixtures for 3 non-recall shapes** and assert `build_mesh` produces a valid mesh (no
  raise, sane lane split):
  - `tool.memorize` cold (write pipeline: validate→embed→store, mixed core/backend).
  - `tool.bookmark_list` (core-only, backend lane empty).
  - `tool.checkpoint` (core-heavy).
- These fixtures double as item-2 empty-lane coverage.

### Tests
- `test_build_mesh_bookmark_list_empty_backend_lane` — 0 backend nodes, core nodes present,
  no raise.
- `test_build_mesh_memorize_two_lane` — both lanes populated.
- vitest `scatterLayout` empty-lane cases (item-2).

---

## Build-car breakdown

| Car | Scope | Model tier | Files |
|-----|-------|-----------|-------|
| **A** | Item-1 core-boundary injection + `core_boundary_stages` helper + `forward_only_recall.json` fixture + Python tests | sonnet (mechanical, well-specified) | `trace_mesh.py`, `tests/` |
| **B** | Item-2 `scatterLayout` pure helper + wire into traces-tab + vitest | sonnet | `traces-replay.js`, `traces-tab.js`, `traces-replay.test.js` |
| **C** | Item-3 divider (JS line + CSS) | sonnet (tiny) — or fold into Car B | `traces-tab.js`, `traces-tab.css` |
| **D** | Item-4 speed presets + persistence + UI + vitest | sonnet | `traces-replay.js`, `traces-tab.js`, `traces-replay.test.js` |
| **E** | Item-5 non-recall fixtures + guards + Python tests (depends on A's guard hooks) | sonnet | `trace_mesh.py` guards, `tests/`, `tests/fixtures/traces/` |

Order: **A → E** (E needs A's boundary-skip guard), then **B → C** (C is trivial, can merge into
B's PR), **D** independent. Suggest **one PR** for the whole batch (all core-only, one version
bump) unless the user wants item-4's behavior change isolated for review.

## Test list (consolidated)
- **Python (pytest):** forward-only-recall core-boundary; `core_boundary_stages` unit;
  bookmark_list empty-backend-lane; memorize two-lane; boundary-skip on dropped_boundary;
  keep `normal_two_lane` green.
- **JS (vitest, `traces-replay.test.js`):** `scatterLayout` (monotonic-x, in-band-y, de-overlap,
  empty, single, cluster, totalMs<=0); `advanceClock` per preset + end-clamp; `loadSpeedId` /
  `saveSpeedId` / `speedById`.

## Risks
- **No browser render harness** (repo convention) → layout density (item-2), divider color/weight
  (item-3), speed feel + `<select>` UI (item-4) are **user smoke-checks**. Pure fns get vitest.
- **Item-4 default behavior change** (realtime = near-instant playback) + the **0.01/0.1 typo** —
  both need user sign-off before merge.
- **Item-1 fix must not double-count** on the stale `normal_two_lane` fixture (which already has
  core nodes) — the boundary injection promotes the *tool* span, distinct from its children;
  regression-guarded by keeping that fixture's test green.
- Cross-item: item-2 empty-lane guard is load-bearing for item-5 core-only tools.

## Version note
Entire batch is **core-only** → single bump **core 5.150.0**. No `backend_version` change
(traceparent already propagates; backend span svc already correct). If the user later wants the
core boundary *renamed* server-side or a backend span attr added, THAT would need a backend bump —
not in this batch.
