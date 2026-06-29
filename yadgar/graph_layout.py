"""Server-side precomputed 3D graph layout (v5.88).

The viz historically ran a d3-force COLD layout client-side on every load
(~15s for thousands of nodes). This module computes deterministic node
positions ONCE on the server (during the nightly consolidation cycle), caches
them keyed by a graph signature, and lets ``/api/graph`` attach x/y/z so the
viz seeds positions + runs a tiny cooldown for a near-instant first paint.

Compute approach (advisor-chosen): capped-iteration seeded
``networkx.spring_layout(dim=3)``. networkx is already a dep; ``seed=`` makes
it deterministic and ``dim=3`` is native. The Fruchterman-Reingold force model
is O(N^2)/iteration, so we cap iterations low (configurable) and — critically —
only ever run this on the BACKGROUNDED nightly cron path, never in the light
``consolidate_now`` budget. The result is cached so cost is paid once per
graph-shape change, not per ``/api/graph`` request.

Determinism requires stable INPUT ordering, not just the seed: networkx layout
output depends on node insertion order, so we always build the graph from
sorted node ids and add edges in sorted order before laying out.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

# Fixed seed → reproducible layout across runs/processes.
_LAYOUT_SEED = 42

# Default coordinate extent. spring_layout natively returns ~[-1, 1]; the 3D viz
# force model uses a link distance of ~36 (VIZ_PHYSICS_LINK_DISTANCE_3D), so we
# scale the seeded layout to that natural extent. Matching the client's extent
# minimises the "re-fly" where the cooldown would otherwise rescale every node
# outward on first paint. Tunable via compute_graph_layout(scale=...).
_LAYOUT_SCALE = 40.0


def _node_ids(nodes: list[dict]) -> list[str]:
    """Extract non-null node ids as strings, de-duplicated and sorted.

    Sorting is load-bearing: spring_layout's output depends on node insertion
    order, so a stable order is required for the determinism guarantee.
    """
    ids = {str(n["id"]) for n in (nodes or []) if n and n.get("id") is not None}
    return sorted(ids)


def _edge_pairs(edges: list[dict], valid: set[str]) -> list[tuple[str, str]]:
    """Return sorted (source, target) pairs whose endpoints are both present."""
    pairs = set()
    for e in edges or []:
        s = e.get("source")
        t = e.get("target")
        if s is None or t is None:
            continue
        s, t = str(s), str(t)
        if s in valid and t in valid:
            pairs.add((s, t))
    return sorted(pairs)


def graph_signature(nodes: list[dict], edges: list[dict]) -> str:
    """Order-independent hash of the graph shape (node ids + edge endpoints).

    Used as a cache-invalidation key: when the signature is unchanged the cached
    layout is still valid and recompute is skipped. Returns a short hex digest.
    """
    ids = _node_ids(nodes)
    pairs = _edge_pairs(edges, set(ids))
    h = hashlib.sha256()
    h.update(b"N")
    for i in ids:
        h.update(i.encode("utf-8"))
        h.update(b"\x00")
    h.update(b"E")
    for s, t in pairs:
        h.update(s.encode("utf-8"))
        h.update(b"\x01")
        h.update(t.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def compute_graph_layout(
    nodes: list[dict],
    edges: list[dict],
    dim: int = 3,
    iterations: int = 50,
    scale: float = _LAYOUT_SCALE,
) -> dict[str, list[float]]:
    """Compute deterministic per-node positions via seeded spring_layout.

    Args:
        nodes: node dicts (each with an ``id``).
        edges: edge dicts (each with ``source`` + ``target``); endpoints absent
            from the node set are ignored.
        dim: layout dimensionality (3 for the 3D viz; 2 supported for tests).
        iterations: force-sim iteration cap. Lower = faster, looser layout.
        scale: coordinate extent. Defaults to ~the client's 3D link distance so
            the seeded layout renders at the right scale (minimal cooldown re-fly).

    Returns:
        ``{node_id: [c0, c1, ...]}`` with ``dim`` finite float coordinates per
        node. Isolated nodes still receive a position. Empty input → ``{}``.
    """
    ids = _node_ids(nodes)
    if not ids:
        return {}

    import networkx as nx  # noqa: PLC0415 — heavy import, deferred to call time

    g = nx.Graph()
    g.add_nodes_from(ids)
    g.add_edges_from(_edge_pairs(edges, set(ids)))

    raw = nx.spring_layout(
        g, dim=dim, seed=_LAYOUT_SEED, iterations=max(1, iterations), scale=scale
    )
    return {nid: [round(float(c), 6) for c in coords] for nid, coords in raw.items()}


def attach_cached_positions(data: dict, cache: dict | None, enabled: bool) -> dict:
    """Attach cached x/y/z to each served node BY ID. Returns ``data`` mutated.

    No positions are attached when ``enabled`` is False (flag off — preserves
    current behavior exactly) or ``cache`` is None (no layout computed yet).

    Attach is BY NODE-ID, with no serve-side signature gate: the layout is
    computed over the full uncapped graph, so the cached positions are a
    superset of any capped /api/graph subset. Every served node that has a
    cached position gets x/y/z; nodes added since the last precompute aren't in
    the cache, so they stay bare and the client places them. Freshness /
    invalidation is a COMPUTE-side concern — _maybe_precompute_graph_layout
    recomputes when the full-graph signature changes — so the serve path never
    needs to re-litigate it. Stale-by-a-bit seeds are harmless: the viz cooldown
    relaxes them, exactly like the localStorage warm-start.
    """
    if not enabled or not cache:
        return data
    positions = cache.get("positions") or {}
    if not positions:
        return data
    for n in data.get("nodes", []):
        nid = n.get("id")
        if nid is None:
            continue
        coords = positions.get(str(nid))
        if coords and len(coords) >= 3:
            n["x"], n["y"], n["z"] = coords[0], coords[1], coords[2]
    return data
