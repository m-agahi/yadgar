# PLAN — v5.10.9: Viz orphan-edge filter (real root cause of v5.10.7+ chaos)

**Status:** drafted 2026-05-30. Plan-first per I27. Patch on v5.10 minor train (per `docs/VERSIONING.md`).

**Master at draft time:** v5.10.8 LIVE; viz STILL broken; live DevTools console revealed `force-graph.min.js: Uncaught Error: node not found: entity:172`.

**Sequencing:** v5.10.9 patch. The REAL root cause behind v5.10.7 / .7.1 / .7.2 / .7.3 / .10.8 chasing symptoms — all 5 prior viz attempts were treating downstream effects of this single bug.

---

## Why

After v5.10.8 deploy + hard refresh, viz still showed:
- engine tick count = 0 (force simulation never iterated)
- nodes clumped at near-origin
- 2D + 3D both broken

User pasted live DevTools console: **`Uncaught Error: node not found: entity:172`** from `force-graph.min.js:34398` during `f.links` resolution inside `_.update`. The library throws synchronously when a link references a node ID not in the node set. After the throw, the simulation never advances → all symptoms (no ticks, mesh leak, fragments) cascade from this one error.

Earlier investigation report (commit `c3c7fdc` lineage) noted: *"1288/1291 edges have BOTH endpoints in node set. 3 `causal` edges reference `entity:*` IDs that are not in the node list — backend bug, but trivial (3 missing out of 1291), not the user's visual bug."* The investigator dismissed the 3 orphans as cosmetic. **They were fatal.** One orphan edge is enough to crash force-graph's update.

### User's prior hint — confirmed

User said earlier: *"i think this issue started when we broke down the massive python files into smaller ones"*. The renumbered audit history points to v5.0.0 commit `042f42b` ("feat: v5.0.0 — security, observability, branch-aware retrieval, decomposition") creating `yadgar/graph_api.py` from a monolith. The edge-filtering invariant ("every edge endpoint must be in the returned nodes set") was lost during the split. Before v5.0.0, the monolithic builder presumably enforced it implicitly via the same query path. After the split, the nodes query and the edges query are decoupled — edges can reference entities that the nodes query doesn't return (e.g. entity rows that don't satisfy heat/visibility filters but still appear as endpoints in `causal_dag_edge` rows).

### Why prior v5.10.7+ attempts didn't help

Every prior attempt tried to fix the SYMPTOM (mesh material, transparent flag, custom geometry, tick-count guard, mesh-leak hack). None addressed the actual library crash. Once that error fires on `f.links` resolution, NO downstream code runs — no `onEngineTick`, no node positioning, no proper render. So every mesh ends up at `(0,0,0)` (initial position) and the renderer draws those overlapping spheres as the fragmented shards the user saw.

## Goals

1. **Backend fix (primary):** `/api/graph` MUST guarantee `set(link.source for link in edges) ∪ set(link.target for link in edges) ⊆ set(node.id for node in nodes)`. Filter orphan edges out before returning.
2. **Frontend defensive (belt-and-suspenders):** `loadGraph()` filters orphan links after fetch, before passing to library. Logs a console warning if any dropped (so future drift is observable).
3. **Verify v5.10.7+ symptoms disappear** once orphans removed: simulation runs, nodes spread via force layout, no library crash, no mesh accumulation, both 2D and 3D render correctly.

## Non-goals

- Restoring custom 3D mesh / shape distinction (S2.2). Deferred — three failed attempts; can revisit AFTER orphan fix proves the simulation pipeline is actually intact.
- Restoring heat coloring shape variations. Same — verify defaults work first.
- Cache-Control headers on viz HTML. Separate concern; tracked as follow-up.

## Approach

### Backend fix

`yadgar/graph_api.py` builds the `/api/graph` payload. After collecting `nodes` and `edges`, add:

```python
node_ids = {n["id"] for n in nodes}
edges = [
    e for e in edges
    if e["source"] in node_ids and e["target"] in node_ids
]
```

Log a metric / warning when orphan edges are dropped (so we know if/when backend drifts). Use existing `yadgar_*` Prometheus naming if a counter is added. Lean: add `yadgar_graph_api_orphan_edges_dropped_total` counter (I23-compliant — both increment + reset paths covered).

### Frontend defensive filter

`yadgar/static/index.html` `loadGraph()` (line ~1014, before `graph.graphData(...)`):

```javascript
const nodeIdSet = new Set(allNodes.map(n => n.id));
const beforeCount = allLinks.length;
allLinks = allLinks.filter(l => {
  const s = (l.source && l.source.id) || l.source;
  const t = (l.target && l.target.id) || l.target;
  return nodeIdSet.has(s) && nodeIdSet.has(t);
});
const dropped = beforeCount - allLinks.length;
if (dropped > 0) {
  console.warn(`[yadgar viz] dropped ${dropped} orphan edges (endpoints not in node set). Backend payload drift?`);
}
```

This catches future backend drift without needing a redeploy. Also handles transient states where filtered nodes change but edges lag.

### Tests

Backend:
1. `test_graph_api_filters_orphan_edges` — fixture with N nodes + M edges where 2 edges reference missing IDs; assert returned `edges` has M-2 items.
2. `test_graph_api_orphan_drop_metric` — assert `yadgar_graph_api_orphan_edges_dropped_total` increments by orphan count.
3. `test_graph_api_no_drops_in_healthy_payload` — fixture where all edges valid; assert no drops + metric unchanged.

Frontend (static-asset, regression):
4. `test_loadGraph_filters_orphan_links` — assert `nodeIdSet` filter pattern present in `loadGraph` body.
5. `test_loadGraph_logs_dropped_count` — assert `console.warn` with "orphan" or "dropped" present in filter block (observability gate).

## Acceptance

- All new tests green
- Existing viz tests still pass
- Manual smoke after deploy (hard-refresh): viz renders 2D and 3D with nodes spread by force layout, NO library crash in console, mesh count ≈ node count
- Pre-commit hooks pass
- CHANGELOG + MIGRATION_NOTES v5.10.9 entry

## Risks + rollback

| Risk | Mitigation |
|---|---|
| Backend filter drops legitimate edges (false positive) | Tests assert no drops in healthy payload; metric exposes drift live |
| Frontend filter masks future backend bugs by silently dropping | `console.warn` is loud + Prometheus metric counts on backend side |
| Library still crashes for other reason after orphan filter | Manual smoke verifies; if so, escalate as v5.10.10 |

Rollback: revert v5.10.9 commits. Back to v5.10.8 broken state with library crash. Not desirable.

## Files to modify

- `yadgar/graph_api.py` — orphan-edge filter + metric
- `yadgar/metrics.py` (or wherever metrics declared) — new counter `yadgar_graph_api_orphan_edges_dropped_total`
- `yadgar/tests/test_graph_api.py` (or similar — locate during impl) — 3 backend tests
- `yadgar/static/index.html` `loadGraph()` — defensive filter + console.warn
- `yadgar/tests/test_viz_static_assets.py` — 2 frontend static-asset tests
- `CHANGELOG.md` — v5.10.9 entry
- `MIGRATION_NOTES.md` — v5.10.9 section with manual smoke procedure
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.10.8 → 5.10.9

## Effort

~30 min code + tests + release artifacts. Real fix is small; complexity was in diagnosis. Diagnosis is done.

## Cross-references

- `docs/PLAN_V5_10_7_VIZ_FIXES.md` — original S2.1–S2.4 (the original viz scope; can now reconsider after orphan fix proves base case works)
- `docs/PLAN_V5_10_7_3_VIZ_REVERT_TO_DEFAULTS.md` — revert to library defaults (still in effect; v5.10.9 doesn't undo it)
- `docs/PLAN_V5_10_8_VIZ_PHYSICS_AND_MESH_LEAK_FIX.md` — v5.10.8 attempt (tick guard + mesh-leak hack removal; partially worked; main blocker was THIS bug)
- `docs/VERSIONING.md` — patch numbering rule
- `docs/DECISIONS.md` — DECISIONS log for v6+ "viz could use better fixture testing" follow-up
