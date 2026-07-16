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

from yadgar._shared.observability.observe import observe

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


# ── Galaxy layout (finish-viz; port of docs/plans/viz-galaxy.mockup.html) ────
#
# A Milky-Way arrangement: loose/single nodes (not in a real multi-member
# cluster) form a DENSE spheroidal CORE bulge; real multi-member clusters string
# along K log-spiral ARMS. Radial density is exponential (dense center + arm
# roots, sparse rim). Heat is NOT position (the client keeps heat→brightness/size);
# core membership is purely "is this node loose". Deterministic (seeded PRNG +
# stable node ordering) exactly like compute_graph_layout, so the cache key stays
# valid across runs.
#
# Mockup constants ported verbatim (mockup §3): outer disk radius, core scale,
# exp disk scale-length, spheroid flatten. Scaled to the client 3D link-distance
# extent (_LAYOUT_SCALE) so the seeded galaxy renders at the right size with no
# cooldown re-fly (the client FREEZES physics on a galaxy payload).

# Mockup R_MAX=46 is the natural extent; we scale positions so the disk radius ~
# _LAYOUT_SCALE (matches the spring_layout extent + client link distance).
_GALAXY_R_MAX = 46.0  # mockup outer disk radius (pre-scale)
_GALAXY_R_CORE = 3.2  # mockup core bulge radius scale
_GALAXY_R_SCALE = 12.0  # mockup exp disk scale-length (surface density e^{-r/L})
_GALAXY_SPHEROID_FLATTEN = 0.62  # mockup bulge y-flatten
_GALAXY_SEED = 1337  # galaxy PRNG seed (distinct from spring _LAYOUT_SEED)


class _Mulberry32:
    """Deterministic PRNG — port of the mockup's mulberry32 (uint32 arithmetic).

    A pure-Python re-implementation so the galaxy layout is reproducible across
    runs/processes without pulling numpy's RandomState into the hot path. Matches
    the JS bit-twiddling so behaviour is identical to the approved mockup.
    """

    _MASK = 0xFFFFFFFF

    def __init__(self, seed: int) -> None:
        self._a = seed & self._MASK

    def random(self) -> float:
        """Return a float in [0, 1) — mirror of the JS mulberry32 step."""
        self._a = (self._a + 0x6D2B79F5) & self._MASK
        t = self._a
        t = (t ^ (t >> 15)) * (t | 1) & self._MASK
        t ^= (t + ((t ^ (t >> 7)) * (t | 61) & self._MASK)) & self._MASK
        t &= self._MASK
        return ((t ^ (t >> 14)) & self._MASK) / 4294967296.0

    def gauss(self) -> float:
        """Standard-normal sample via Box-Muller (mockup ``gauss``)."""
        import math  # noqa: PLC0415

        u = 0.0
        v = 0.0
        while u == 0.0:
            u = self.random()
        while v == 0.0:
            v = self.random()
        return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)


def _exp_radius(rng: _Mulberry32, length: float, r_max: float, tight: float) -> float:
    """Exponential-disk radius sampler (mockup ``expRadius``): dense inner, sparse rim.

    Inverse-CDF of ``r·e^{-r/length}`` truncated at ``r_max``; ``tight`` adds an
    extra inward pull (used for the packed core).
    """
    import math  # noqa: PLC0415

    k = 1.0 - math.exp(-r_max / length)
    r = -length * math.log(1.0 - rng.random() * k)
    if tight > 0:
        r *= rng.random() ** (tight * 0.6)
    return min(r_max, r)


def _galaxy_node_membership(nodes: list[dict], clusters: list[dict] | None) -> dict[str, dict]:
    """Map each node id → {'loose': bool, 'cluster': <id or None>}.

    Membership source mirrors ``_build_clusters_payload`` (the same
    ``member_node_ids`` the viz sidebar renders): a node is ARM material iff it
    belongs to a REAL multi-member cluster (``member_count >= 2``). Everything
    else — singletons, unclustered, and demoted single-member clusters — is LOOSE
    (core material). This is the mockup's ``nd.single`` / ``nd.cluster == -1``
    rule, derived from live DB clusters instead of the synthetic corpus.
    """
    membership: dict[str, dict] = {
        str(n["id"]): {"loose": True, "cluster": None}
        for n in (nodes or [])
        if n and n.get("id") is not None
    }
    for cl in clusters or []:
        # A real cluster only pulls nodes into an arm when it has >=2 members
        # (single-member clusters are "not a real cluster" → demoted to loose,
        # exactly like the mockup's clusterStat.n < 2 demotion).
        if int(cl.get("member_count") or 0) < 2:
            continue
        cid = cl.get("id")
        for nid in cl.get("member_node_ids") or []:
            snid = str(nid)
            if snid in membership:
                membership[snid] = {"loose": False, "cluster": cid}
    return membership


def _rank_clusters(clusters: list[dict] | None, membership: dict[str, dict]) -> list:
    """Rank real (multi-member, present) clusters by a size+heat-blended score.

    Mirrors the mockup ``clusterStat.score = n*0.6 + mean_heat*n*0.4`` but heat is
    unavailable per-cluster here, so we rank by the count of PRESENT (rendered)
    members — the arm spine budget just needs a stable biggest-first ordering.
    Returns cluster ids ordered by descending present-member count then id (ties
    broken deterministically).
    """
    counts: dict = {}
    for info in membership.values():
        if info["loose"]:
            continue
        cid = info["cluster"]
        counts[cid] = counts.get(cid, 0) + 1
    # Deterministic: descending member count, then stable by str(id).
    return sorted(counts, key=lambda cid: (-counts[cid], str(cid)))


@observe(tier="stage", metric="backend.graph.galaxy_layout")
def galaxy_layout(
    nodes: list[dict],
    edges: list[dict] | None = None,  # noqa: ARG001 — parity with compute_graph_layout signature
    clusters: list[dict] | None = None,
    arms: int = 4,
    spiral_pitch: float = 0.30,
    core_density: float = 1.0,
    scale: float = _LAYOUT_SCALE,
) -> dict[str, list[float]]:
    """Milky-Way galaxy positions: loose→dense core bulge, clusters→K spiral arms.

    Port of ``docs/plans/viz-galaxy.mockup.html`` (user-approved). Deterministic
    (seeded PRNG + sorted node ids). Heat is NOT position.

    Args:
        nodes: node dicts (each with an ``id``).
        edges: accepted for signature parity with ``compute_graph_layout`` (galaxy
            positions derive from cluster membership, not edges).
        clusters: the ``/api/graph`` ``clusters[]`` payload (member_node_ids +
            member_count). Multi-member clusters become arms; loose nodes the core.
        arms: number of spiral arms (K). Real clusters bucket round-robin into arms
            by rank; clusters past the spine budget scatter inter-arm.
        spiral_pitch: log-spiral tightness (smaller = tighter winding).
        core_density: packs the core bulge tighter as it rises (mockup ``coredens``).
        scale: coordinate extent (positions rescaled so the disk radius ~ scale).

    Returns:
        ``{node_id: [x, y, z]}`` with 3 finite float coords per node. Empty input
        → ``{}``.
    """
    import math  # noqa: PLC0415

    ids = _node_ids(nodes)
    if not ids:
        return {}

    arms = max(1, int(arms))
    pitch = max(0.02, float(spiral_pitch))
    coredens = max(0.1, float(core_density))
    rng = _Mulberry32(_GALAXY_SEED)

    membership = _galaxy_node_membership(nodes, clusters)
    ranked = _rank_clusters(clusters, membership)

    # Assign multi-member clusters to arms round-robin by rank (mockup spine
    # budget = arms*3). Clusters past the budget scatter inter-arm (arm = -2).
    arm_of_cluster: dict = {}
    n_spine = min(len(ranked), arms * 3)
    for i, cid in enumerate(ranked):
        arm_of_cluster[cid] = (i % arms) if i < n_spine else -2

    positions: dict[str, list[float]] = {}
    for nid in ids:
        info = membership.get(nid, {"loose": True, "cluster": None})
        loose = info["loose"]
        arm = -1 if loose else arm_of_cluster.get(info["cluster"], -2)

        if loose:
            # ── DENSE central bulge: packed loose stars, exponential falloff ──
            bulge_l = (_GALAXY_R_SCALE * 0.42) / (0.6 + coredens * 0.9)
            rr = _exp_radius(rng, bulge_l, _GALAXY_R_MAX * 0.55, 1.4)
            th = rng.random() * math.pi * 2
            ph = math.acos(2 * rng.random() - 1)
            x = rr * math.sin(ph) * math.cos(th)
            z = rr * math.sin(ph) * math.sin(th)
            y = rr * math.cos(ph) * _GALAXY_SPHEROID_FLATTEN
        else:
            # ── ARM material (multi-member clusters) on an exponential disk ──
            radius = _exp_radius(rng, _GALAXY_R_SCALE, _GALAXY_R_MAX, 0.0)
            radius = max(_GALAXY_R_CORE, radius)
            if arm >= 0:
                angle = (arm / arms) * math.pi * 2
                angle += math.log(radius / _GALAXY_R_CORE + 1.0) / pitch
                angle += rng.gauss() * 0.16  # arm width jitter
            else:
                # real cluster beyond the spine budget → inter-arm scatter
                angle = rng.random() * math.pi * 2 + rng.gauss() * 0.5
            x = math.cos(angle) * radius
            z = math.sin(angle) * radius
            thick = 0.9 * (0.35 + 0.65 * (radius / _GALAXY_R_MAX))
            y = rng.gauss() * thick

        positions[nid] = [x, y, z]

    # Rescale so the disk radius ~ `scale` (matches spring_layout extent + client
    # link distance) — keeps the client seed-freeze at the right visual size.
    max_extent = max(
        (max(abs(c) for c in coords) for coords in positions.values()),
        default=1.0,
    )
    if max_extent <= 0:
        max_extent = 1.0
    factor = scale / max_extent
    return {nid: [round(c * factor, 6) for c in coords] for nid, coords in positions.items()}


def attach_cached_positions(data: dict, cache: dict | None) -> dict:
    """Attach cached x/y/z to each served node BY ID. Returns ``data`` mutated.

    No positions are attached when ``cache`` is None or empty (no layout computed
    yet) — the served nodes stay bare and the client runs its cold d3-force layout
    (the seed-miss fallback). viz-render-perf (Car A) removed the
    VIZ_PRECOMPUTED_LAYOUT_ENABLED gate: attach is unconditional given a cache.

    Attach is BY NODE-ID, with no serve-side signature gate: the layout is
    computed over the full uncapped graph, so the cached positions are a
    superset of any capped /api/graph subset. Every served node that has a
    cached position gets x/y/z; nodes added since the last precompute aren't in
    the cache, so they stay bare and the client places them. Freshness /
    invalidation is a COMPUTE-side concern — _maybe_precompute_graph_layout
    recomputes when the full-graph signature changes — so the serve path never
    needs to re-litigate it. Stale-by-a-bit seeds are harmless: the viz cooldown
    relaxes them, exactly like the localStorage warm-start.

    finish-viz (galaxy): the cache's ``layout_mode`` is stamped onto the payload as
    ``data["layout_mode"]`` so the client can FREEZE physics (``cooldownTicks(0)``)
    on a "galaxy" payload — the seeded galaxy shape must HOLD, not relax to a blob.
    A "spring" payload keeps the existing warm-start relax behaviour.
    """
    if not cache:
        return data
    positions = cache.get("positions") or {}
    if not positions:
        return data
    # Surface which generator produced these positions so the client can pick its
    # cooldown strategy (galaxy → freeze; spring → relax).
    data["layout_mode"] = str(cache.get("layout_mode") or "spring")
    for n in data.get("nodes", []):
        nid = n.get("id")
        if nid is None:
            continue
        coords = positions.get(str(nid))
        if coords and len(coords) >= 3:
            n["x"], n["y"], n["z"] = coords[0], coords[1], coords[2]
    return data
