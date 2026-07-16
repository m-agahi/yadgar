> ARCHIVED 2026-07-16 — shipped in viz-train (core 5.145.0). ADR-0131.

# Plan: viz render perf — unconditional precomputed layout + /api/graph payload cuts

Date: 2026-07-16 · Status: SHIPPED · Supersedes part of ADR-0010 (default-OFF stance)

## Problem

Graph viz takes very long to appear on page refresh. Client cold-runs ForceGraph3D
d3-force over ~2700 nodes (VIZ_MAX_MEMORIES 500 + VIZ_MAX_WIKI 200 +
VIZ_MAX_ENTITIES 2000) on every load (~15s at ~5000 nodes per ADR-0010 benchmark),
and the `/api/graph` payload build itself is slow.

## Root causes

### H1 — precomputed layout never active (primary)

ADR-0010 / v5.88.0 (#130) built the full server-side layout chain: nightly
consolidation runs `networkx.spring_layout(dim=3, seed=42)`, caches positions in the
`graph_layout_cache` singleton keyed by graph signature; `/api/graph` attaches x/y/z
by node-id so the client seeds positions and caps `cooldownTicks(60)` instead of a
cold settle. All of it is gated behind `VIZ_PRECOMPUTED_LAYOUT_ENABLED`, default
**False**, with no deploy override anywhere — the chain has never run in production.

Evidence (verified on master):
- Flag default: `yadgar/_shared/config/config.py:932`; env entry:
  `yadgar/_shared/config/config_registry.py:293`. NOTE: `config_yaml.py` carries NO
  per-knob schema — it derives sections + coercion from `Settings.model_fields`
  (`get_field_section` / `coerce_value`), so the I25 "three-way" is materially two
  removal points (Settings field + registry entry); config_yaml follows automatically.
- Cache-write gate (hard return when flag off):
  `yadgar/backend/consolidation/service.py:86` in `_maybe_precompute_graph_layout`.
- Position-emit gate (attach skipped when flag off):
  `yadgar/backend/viz_exec/__init__.py:54-63` in `_op_graph` →
  `attach_cached_positions(data, cache, enabled=True)`
  (`yadgar/backend/graph/graph_layout.py:124`).
  (Prompt path drift note: viz_exec lives at `yadgar/backend/viz_exec/`, not
  `yadgar/core/viz/` — moved in T2 Car E3.)
- Client seed logic (`yadgar/core/static/index.html:2565-2586`,
  `viz_positions.js:133-150`): `_countSeededPositions` > 0 → `_warmStarted` →
  capped cooldown. Zero server positions + empty localStorage → full cold layout.

**User verdict (2026-07-16): the knob is redundant.** Precomputed layout is the only
way viz should fetch the graph. Phase 1 removes the flag entirely rather than
flipping its default.

### H2 — /api/graph payload build slow (secondary, independent)

- Semantic O(n²) KNN edges: **already removed** v5.87 C3
  (`yadgar/backend/graph/graph_api.py:14-18` header documents the deletion). Not a
  factor — listed here so nobody re-chases it.
- **N+1 cluster query**: `yadgar/backend/graph/graph_edges.py:246-255`
  `_build_clusters_payload` calls `self._s.get_cluster_members(raw_id)`
  (`yadgar/_shared/storage/cluster.py:174`) once per cluster inside a Python loop
  over `get_memory_clusters()` — ~770 DB round-trips per `/api/graph` call at the
  current ~769 clusters.
- **Unused embedding columns fetched**: `yadgar/backend/graph/graph_nodes.py:30`
  (memory select) and `:79-80` (wiki select) both select `embedding`; neither node
  dict emits it — ~MBs fetched and discarded per request.
- **5 uncapped full scans** at `graph_edges.py` call sites → storage defs:
  - `:51` `get_all_transitions` → `_shared/storage/rules.py:205`
  - `:84` `get_all_wiki_crossrefs` → `_shared/storage/wiki.py:829`
  - `:136` `get_all_causal_edges` → `_shared/storage/causal.py:67`
  - `:176` `get_relationships_by_types` → `_shared/storage/entity.py:179`
  - `:207` `get_all_memory_similarity_links` → `_shared/storage/cluster.py:157`

  Node queries got `_limit_clause` caps in v5.88 FIX2; edge queries never did.

## Phase 1 — remove the knob; precompute unconditional

Goal: delete `VIZ_PRECOMPUTED_LAYOUT_ENABLED` and the flag-off branches. The
precompute always runs in nightly/full consolidation; `/api/graph` always attaches
cached positions when a cache exists.

### CRITICAL — what is NOT dead (do not over-delete)

The client-side d3-force layout is the required **seed-miss fallback**, per
ADR-0010's own consequences:
1. First-ever load before `graph_layout_cache` is populated (empty cache → attach
   is a no-op → zero seeded positions → client cold layout).
2. Nodes created since the last precompute — attach is by node-id, misses get no
   x/y/z and are placed client-side.

Keep: the entire client ForceGraph3D layout path, `_countSeededPositions` /
`_warmStarted` / localStorage warm-start in `index.html` + `viz_positions.js`.
Client changes in this phase = comment updates only.

Dead/removable: the flag (both sync points), both flag guards, the `enabled`
parameter plumbing, every flag-off test branch, doc/comment references.

### Steps (TDD: failing tests first for each behavior change)

1. **Tests first** (red):
   - `test_consolidation_graph_layout.py`: assert `_maybe_precompute_graph_layout`
     writes the cache with NO flag set (today it hard-returns). Delete/invert the
     flag-off cases (`:36` parametrization).
   - `test_graph_api_layout_attach.py`: assert `_op_graph` attaches positions
     whenever a cache row exists, no env var involved. Delete the flag-off case
     (`:120`); rework `:133/:155`.
   - Assert empty-cache path still returns nodes without x/y/z (fallback contract).
2. **Un-gate compute**: `backend/consolidation/service.py:86` — drop the flag
   check. Keep the signature no-op (skip when live graph shape matches cached
   signature) and the non-fatal try/except. Still nightly/full only, never the
   light budget.
3. **Un-gate attach**: `backend/viz_exec/__init__.py:54` — drop the
   `get_settings()` check; always `attach_cached_positions(data, cache)`. Keep
   best-effort try/except. Drop the now-meaningless `enabled` param from
   `attach_cached_positions` (`graph_layout.py:124`) and its tests.
4. **Delete config surface**: `config.py:932` field + comment block,
   `config_registry.py:293` entry. `VIZ_LAYOUT_ITERATIONS` STAYS (genuine tuning
   knob). Config-integrity tests (`test_core_config_integrity.py`,
   `test_backend_config_integrity.py`) assert Settings↔registry parity — removing
   both sides keeps them green; verify. Update the I25 sync test if it counts knobs.
5. **Sweep every reference** (grep `VIZ_PRECOMPUTED_LAYOUT` +
   `YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED`); verified current list:
   - Tests using the flag as a *sample knob* — swap to another bool knob, don't
     delete the test: `test_config_yaml_aware_source.py:85-87`,
     `test_control_api.py:498`, `test_viz_backend_ops.py:92`.
   - Test docs/comments: `test_consolidate_now.py:8`.
   - Docs: `README.md`, `docs/reference/configuration.md`,
     `docs/reference/architecture.md`, `docs/contracts/CAPABILITY_REGISTRY.md`
     (CAP-VIZ-013 — update to "unconditional, knob removed"). `CHANGELOG.md`
     historical entries stay untouched (history, not live docs).
   - Comments only: `core/static/index.html:2565`, `core/static/viz_positions.js:133`,
     `core/server/tools/admin_other.py:93`.
   - Nix: no deploy override exists (confirmed) — nothing to remove; no nix touch.
6. **ADR**: `adr_add` a new ADR, `supersedes="ADR-0010"` (partial supersede — the
   default-OFF/validate-first stance): "Precompute is unconditional; knob removed;
   client cold layout retired as primary path, retained as seed-miss fallback."
7. **First-load bootstrap — DECIDED: option (b), user 2026-07-16.** Backend-startup
   bootstrap — if `get_graph_layout_cache()` is empty on boot, kick
   `_maybe_precompute_graph_layout` in a background thread (non-blocking, same
   non-fatal wrapper). Small, bounded (~20 LOC + test). A fresh deploy warms itself;
   no manual cold load 1.
   (Rejected: (a) accept-cold-load — needs a manual warm every fresh deploy;
   (c) lazy-on-first-request — extra moving parts on the request path.)
8. **Populate live cache once**: after deploy, user runs
   `consolidate_now(mode="full")` (runs the precompute step immediately). Manual
   step regardless of the option-7 choice.

### Validation (no viz harness — smoke-check convention applies)

Per the `viz-frontend-has-no-browser-test-harness` convention: python + vitest
suites cover the logic; render behavior needs a live browser smoke-check.
- Chain check: after `consolidate_now(mode="full")` → cache row exists →
  `curl /api/graph | jq '[.nodes[]|select(.x!=null)]|length'` > 0 → page reload
  paints near-instantly (no ~15s settle).
- ADR-0010 revisit_trigger: if the seeded layout **re-flies** on first paint, tune
  compute `scale` (currently 40, matched to client link-distance ~36) and/or
  client cooldown. If layout quality is poor, raise `VIZ_LAYOUT_ITERATIONS`.
- Suites to run (stale-assertion trap): `test_consolidation_graph_layout.py`,
  `test_graph_api_layout_attach.py`, `test_viz_backend_ops.py`,
  `test_consolidate_now.py`, `test_control_api.py`, `test_config_yaml_aware_source.py`,
  config-integrity pair, viz-tests vitest. Run touched suites at `-n2`
  (`make test` at `-n4` OOMs locally).

## Phase 2 — /api/graph payload cuts (behavior-preserving)

Each item: characterization test capturing current payload shape/counts on a
fixture BEFORE the change; assert identical after.

### 2a — batch the cluster N+1

`_build_clusters_payload` (`graph_edges.py:230-275`): replace the per-cluster
`get_cluster_members(cid)` loop with ONE round-trip — either a new
`get_all_cluster_members() -> dict[int, list[int]]` (single
`SELECT cluster_id, memory_id FROM memory_cluster_member` grouped in Python) or a
bounded IN-query over the fetched cluster ids. Preserve exactly: per-cluster
`member_count` = pre-intersection DB count, `member_node_ids` = intersection with
rendered `mem_ids` (the v5.86 P2.2 semantics — 761/769 clusters render "empty" by
node-cap design; keep that). New storage method gets `@observe` (I33). Keep
`get_cluster_members(cid)` for its other callers — don't change shared semantics.

### 2b — stop fetching embedding columns

`graph_nodes.py:30` (memory) and `:79` (wiki): drop `embedding` from the SELECT
lists. Verified: neither `_assemble_memory_nodes` nor `_assemble_wiki_nodes` reads
it — pure waste (~3MB/request). Characterization: node dict keys/values unchanged.

### 2c — cap the 5 uncapped edge scans

Respect the existing VIZ_MAX_* philosophy (v5.88 FIX2: `0/-1 = unlimited`,
`_limit_clause` at `graph_api.py:46`). Two design constraints:
- **Cap at the graph call sites, not the shared storage methods** —
  `get_all_wiki_crossrefs` etc. have non-viz consumers (e.g. admin invariants);
  changing their semantics is out of scope. Add optional `limit=` params
  defaulting to unlimited, or new capped wrapper queries used only by graph_edges.
- Deterministic ordering under a LIMIT (e.g. `ORDER BY strength/count DESC` where a
  weight exists, else `id`) — an unordered LIMIT gives a random edge subset per
  request.

Knobs — DECIDED: FIVE per-type knobs, user 2026-07-16 (finer per-edge-type control).
Each is I25 (Settings field + registry entry; config_yaml follows), default `0`
(unlimited) so day-one behavior is preserved:
- `VIZ_MAX_TRANSITIONS` (get_all_transitions, order by count DESC)
- `VIZ_MAX_WIKI_CROSSREFS` (get_all_wiki_crossrefs, order by id)
- `VIZ_MAX_CAUSAL_EDGES` (get_all_causal_edges, order by strength/weight DESC else id)
- `VIZ_MAX_RELATIONSHIPS` (get_relationships_by_types, order by weight DESC else id)
- `VIZ_MAX_SIMILARITY_LINKS` (get_all_memory_similarity_links, order by strength DESC)
Five knobs → 5× I25 parity to keep green in the config-integrity pair. Caps applied
at the graph_edges call sites only; shared storage methods gain optional `limit=`
(default unlimited) so non-viz consumers are untouched.

Note: the nightly precompute calls `get_full_graph(0, 8, False, None, 0, 0)`
(uncapped) — 2a/2b speed that path up too; 2c's caps must NOT apply to the
precompute's uncapped call (it deliberately lays out the full graph).

## Sequencing

Phase 1 first: biggest user-visible win (kills the ~15s client settle), small
bounded diff, already-built machinery. Phase 2 second: cuts `/api/graph`
time-to-first-byte and nightly precompute cost. **Independently shippable** — no
ordering dependency; separate PRs. Both fit I13 (≤500 LOC soft) individually.

## Risks / unknowns

- **Layout quality unvalidated live** (ADR-0010's original reason for default-OFF).
  Mitigation: smoke-check + tuning levers (scale, iterations, cooldown). Residual:
  no automated regression net — the no-viz-harness gap stands.
- **No kill-switch after knob removal.** If precompute misbehaves, there's no
  config off-ramp. Mitigation: every step is already non-fatal try/except; worst
  case = empty/stale cache → client fallback = today's behavior. Truly broken
  layout output would need a code revert.
- **Freshness vs cadence**: nightly recompute → nodes created intra-day placed
  client-side; a mixed seeded/unseeded graph may partially re-fly. Attach-by-node-id
  tolerates signature drift (cache is a stale superset; compute-side signature gate
  decides recompute). If churn makes this ugly: shorten cadence or bootstrap-style
  triggers — out of scope here.
- **2c LIMIT semantics**: any non-unlimited default silently drops edges from the
  render. Ship default-unlimited; document the knob.
- **First-deploy cold load** if option 7(a) chosen — see Open questions.

## Open questions for user — RESOLVED 2026-07-16

1. Bootstrap precompute on backend startup when cache empty → **YES, option 7b**
   (startup background bootstrap).
2. Phase 2c cap knobs → **FIVE per-type knobs** (default unlimited each).
3. Post-deploy warm + browser smoke-check → **user runs it** (agent preps the
   curl/jq chain-check; user runs consolidate_now(full) + browser reload).

## Relation to existing artifacts

- **ADR-0010**: chain design stands; its default-OFF/"validate before flipping"
  decision is superseded by the new ADR (step 6). Its revisit_trigger (re-fly →
  tune scale/cooldown) becomes this plan's smoke-check criterion.
- **Task #39** (viz 200-node cap, VIZ_MAX_WIKI): adjacent — node caps exist
  (`VIZ_MAX_MEMORIES/WIKI/ENTITIES`); Phase 2c extends the same cap philosophy to
  edge queries. No conflict.
- **viz-triage-checklist-2026-06-27** (G1 "Loading & Performance"): item 1
  (semantic edges out of default payload) — done v5.87; item 2 (lazy heavy edges)
  — moot for semantic (deleted), partially addressed by 2c caps. This plan is the
  resolution for the "Slow graph load" complaint cluster (items 1, 2); item 62
  (heat SSE) remains separate.
- **ADR-0009**: semantic edge removal — context for why H2 excludes KNN work.
