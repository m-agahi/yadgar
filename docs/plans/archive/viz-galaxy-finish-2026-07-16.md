# Plan: finish-viz — galaxy layout + trace-replay Phase 3 + F1 cap-affordance

> **ARCHIVED — SHIPPED.** Delivered on `feat/viz-rest` (rides #209). Galaxy layout
> (`galaxy_layout` in `graph_layout.py`, default-on via `VIZ_GALAXY_LAYOUT`),
> trace-replay Phase 3 (`trace_complete` SSE from the tool boundary; p95/rate badges
> DROPPED per plan self-guard), and F1 cap-affordance (`nodes_hidden`/`edges_hidden`
> in `/api/graph` + status-line). Reference mockup kept at
> `docs/plans/viz-galaxy.mockup.html`.

Date: 2026-07-16 · Status: SHIPPED · On `feat/viz-rest` (rides #209, the finish-viz PR).
Spec for the galaxy layout = `docs/plans/viz-galaxy.mockup.html` (user-approved; port its
arrangement to the real precompute). Theme unchanged (oscilloscope).

## 1. Galaxy layout (the headline — port the mockup)
Real galaxy: loose/old stars in a DENSE core; structure in the arms.

**Server** (`yadgar/backend/graph/graph_layout.py` — precompute is unconditional, ADR-0131):
add a `galaxy_layout(nodes, edges/clusters) -> {node_id: (x,y,z)}` generator, selected by a
config knob. Port the mockup's algorithm:
- **Core bulge = loose/single nodes** (nodes NOT in a real multi-member cluster: singletons +
  unclustered). Pack densely into a 3D spheroidal bulge via an exponential inverse-CDF radius
  sampler (`expRadius`). Do NOT put clusters in the core.
- **Arms = real multi-member clusters only.** Bucket the top-K biggest/hottest clusters into K
  log-spiral arms; small clusters scatter inter-arm. Clustered nodes string along their arm.
- **Exponential radial density** — dense center + inner arm-roots, sparse rim (arm scale-length
  ~12, bulge ~3.4 per the mockup).
- Heat is NOT position (stays client-side brightness/size). Core membership = is-single.
- Wire into `_maybe_precompute_graph_layout` (`backend/consolidation/service.py`) — when the
  galaxy knob is on, compute galaxy positions instead of `networkx.spring_layout`; cache in
  `graph_layout_cache` (same singleton). Needs per-node cluster membership + single-flag +
  type — derive from `get_memory_clusters`/`get_cluster_members` (already used by
  `_build_clusters_payload`).

**Client** (`core/static/index.html` + `viz_positions.js`): when galaxy positions are served,
**freeze physics** (`cooldownTicks(0)` / no warmup) so the seeded galaxy shape HOLDS instead of
d3-force relaxing it to a blob. A UI toggle: **Galaxy ↔ Force-directed** (galaxy default when
the knob is on). Force-directed path stays as fallback.

**Knobs** (I25 three-way: config.py Settings + config_registry + config_yaml FIELD_META):
`VIZ_GALAXY_LAYOUT: bool = True` (galaxy is the default — user loves it; force-directed when
false), `VIZ_GALAXY_ARMS: int = 4`, `VIZ_GALAXY_SPIRAL_PITCH: float`, `VIZ_GALAXY_CORE_DENSITY:
float`. Config-integrity test parity.

**Tests:** `test_galaxy_layout` unit — loose nodes get small radius (core), clustered nodes map
to arm angles, exponential density falloff holds, K arms produced. Precompute contract: galaxy
positions cached + served in `/api/graph` x/y/z.

## 2. trace-replay Phase 3 (deferred from viz-train)
- **SSE `trace_complete` event** on `/api/graph/events` when a tool trace completes → the Traces
  tab can live-append. Emit backend-side (where trace boundary spans finalize) via the F2 SSE
  relay path already built (Car C `_op_events` + core `_poll_backend_events`). Frontend handler
  in traces-tab.js appends the new trace to the recent list.
- **Live p95/rate badges: DROP** (no per-stage Prometheus metrics exist — the plan self-guarded
  this). Note it dropped; don't fake it.
- Test: emit-on-complete unit; browser SSE = user smoke-check (BC-VZ-F2 style).

## 3. F1 cap-affordance (minor)
When the per-type edge caps (`VIZ_MAX_TRANSITIONS` etc., default 0=unlimited) OR node caps
actually truncate, surface the truncation count in the `/api/graph` payload + a frontend
affordance (mirror the existing `weak_edges_hidden` pattern — a status-line "N … hidden (cap)").
Only bites when a cap is set >0 (default unlimited → no-op). Small.

## Version / housekeeping
Backend change (graph_layout) → stays on this train's **backend 5.55.0** / core **5.146.0**
(already bumped in the merge; do NOT re-bump — one train, one version, ADR-0088). If a gate
demands it, bump both in all 3 places (server.json + docker-compose + `__init__.py`). I33
`@observe` on new backend fns; I32 CAPABILITY_REGISTRY for the galaxy knobs + trace_complete
event + cap-affordance. Extend the `## [Unreleased]` viz-rest CHANGELOG entry (don't add a
separate release). `pre-commit run --all-files` must exit 0 before push (the sync-version /
Validate gate). Any new `except (A,B):` needs `# fmt: skip`.

## Sequencing
Build all three on `feat/viz-rest` (they share index.html + graph files → one branch, sequential
— galaxy first (must-have), then Phase 3, then F1). Then push once → reopen #209 → CI on the
healthy runner → merge. This is the LAST viz train.

## Tests / no-harness
No browser render harness — cover server layout/emit logic + payload contracts via unit tests;
galaxy render + freeze + toggle + trace live-append = user smoke-check. Run touched suites with
`YADGAR_OTLP_ENDPOINT=''` + `HF_HOME=/home/max/.cache/huggingface`, pytest via a script file.
