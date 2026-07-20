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

# viz-layout-backend (ADR-0152, R6): the graph SHAPE hash (graph_signature) alone
# no-ops the nightly recompute when the shape is stable, so new layout math never
# takes effect. Fold this version constant + the galaxy params into the signature
# input so shipping new math (bump this) or changing a VIZ_GALAXY_* setting auto-
# invalidates the cache. BUMP on every layout-math change.
_LAYOUT_VERSION = 2

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


def graph_signature(
    nodes: list[dict],
    edges: list[dict],
    layout_version: int = _LAYOUT_VERSION,
    params: dict | None = None,
) -> str:
    """Cache-invalidation hash: graph shape + layout code version + galaxy params.

    Historically hashed only the graph SHAPE (node ids + edge endpoints). That
    no-ops the nightly recompute whenever the shape is stable — so new layout
    math (or a changed VIZ_GALAXY_* setting) would never take effect (ADR-0152
    R6). We now fold ``layout_version`` and the galaxy ``params`` into the digest
    so bumping the code version or changing a param invalidates the cache even on
    an identical graph shape.

    Args:
        nodes: node dicts (each with an ``id``).
        edges: edge dicts (``source`` + ``target``); missing endpoints ignored.
        layout_version: the layout-math version constant (bump on math changes).
        params: galaxy params folded into the hash (arms/pitch/coredens). ``None``
            → params contribute nothing (legacy shape-only behaviour is a subset).

    Returns a short hex digest.
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
    # R6: version + params fold. Serialise deterministically (sorted keys) so the
    # digest is stable across dict insertion order.
    h.update(b"V")
    h.update(str(int(layout_version)).encode("utf-8"))
    h.update(b"P")
    for k in sorted((params or {}).keys()):
        h.update(str(k).encode("utf-8"))
        h.update(b"=")
        h.update(repr((params or {})[k]).encode("utf-8"))
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


def _cluster_member_counts(membership: dict[str, dict]) -> dict:
    """Count PRESENT (rendered) members per real cluster from a membership map."""
    counts: dict = {}
    for info in membership.values():
        if info["loose"]:
            continue
        cid = info["cluster"]
        counts[cid] = counts.get(cid, 0) + 1
    return counts


def _assign_arms_balanced(ranked: list, counts: dict, arms: int) -> dict:
    """Greedy lightest-arm bin-packing: each cluster → the arm with the smallest
    running node-count load. Ties go to the lowest arm index (determinism).

    Port of the client ``assignArmsBalanced`` (galaxy-view.js) — round-robin
    ``i % arms`` dumped the biggest clusters onto arms 0/1 and starved the rest.
    ADR-0152 bug #4: EVERY multi-member cluster maps to exactly ONE real arm
    (0..arms-1); the old ``arms*3`` spine budget + ``arm=-2`` inter-arm scatter
    are gone (clusters past the budget used to scatter randomly).

    Args:
        ranked: cluster ids, largest-first (from ``_rank_clusters``).
        counts: cluster id → present-member count (from ``_cluster_member_counts``).
        arms: arm count (K).

    Returns ``{cluster_id: arm_index}`` with every ranked cluster on a real arm.
    """
    k = max(1, int(arms))
    arm_load = [0] * k
    out: dict = {}
    for cid in ranked:
        best = 0
        for a in range(1, k):
            if arm_load[a] < arm_load[best]:
                best = a
        out[cid] = best
        arm_load[best] += int(counts.get(cid, 0))
    return out


def galaxy_membership(
    nodes: list[dict],
    edges: list[dict] | None,
    clusters: list[dict] | None,
    arms: int = 4,
) -> dict[str, dict]:
    """Per-node ``{id: {'loose': bool, 'arm': int}}`` — the single source of truth
    for galaxy placement (positions) AND edge suppression (client Car B).

    Two layers, in order:
      1. Cluster membership (``_galaxy_node_membership``): nodes in a real
         (>=2-member) cluster are ARM material on that cluster's assigned arm.
      2. Connectivity eligibility (ADR-0152 bug #3a, LIGHT path): a LOOSE node
         (typically an entity/wiki hub that never appears in the memory-only
         ``member_node_ids``) that has edges into a real cluster is promoted onto
         the arm of its DOMINANT neighbour cluster (the cluster most of its edges
         point into). A truly-single (0-edge, or edges only to loose nodes) node
         stays loose → core.

    ``arm`` is -1 for loose/core nodes, else the assigned arm index in [0, arms).
    Deterministic: cluster ranking + greedy arm assignment + sorted tie-breaks.
    """
    membership = _galaxy_node_membership(nodes, clusters)
    ranked = _rank_clusters(clusters, membership)
    counts = _cluster_member_counts(membership)
    arm_of_cluster = _assign_arms_balanced(ranked, counts, arms)

    # Base per-node arm from cluster membership.
    out: dict[str, dict] = {}
    for nid, info in membership.items():
        if info["loose"]:
            out[nid] = {"loose": True, "arm": -1, "cluster": None}
        else:
            out[nid] = {
                "loose": False,
                "arm": int(arm_of_cluster.get(info["cluster"], 0)),
                "cluster": info["cluster"],
            }

    # ── bug #3a light: promote connected loose hubs onto a neighbour arm ──────
    # Build a per-node tally of which real cluster its edges point into, then move
    # any loose node whose edges reach a real cluster onto that cluster's arm.
    # Deterministic: dominant cluster = highest edge count, ties by str(cluster).
    if edges:
        neighbour_clusters: dict[str, dict] = {}
        for e in edges:
            s = e.get("source")
            t = e.get("target")
            if s is None or t is None:
                continue
            s, t = str(s), str(t)
            for a, b in ((s, t), (t, s)):
                # `a` is the (possibly loose) node; `b` its neighbour.
                a_info = out.get(a)
                b_info = out.get(b)
                if a_info is None or b_info is None:
                    continue
                if not a_info["loose"] or b_info["loose"]:
                    continue  # only promote loose nodes via ARM neighbours
                tally = neighbour_clusters.setdefault(a, {})
                cid = b_info["cluster"]
                tally[cid] = tally.get(cid, 0) + 1
        for nid, tally in neighbour_clusters.items():
            # Dominant neighbour cluster (deterministic tie-break).
            dom = sorted(tally, key=lambda c: (-tally[c], str(c)))[0]
            out[nid] = {
                "loose": False,
                "arm": int(arm_of_cluster.get(dom, 0)),
                "cluster": dom,
            }

    # Strip the internal 'cluster' key from the public shape (positions +
    # attach only need loose/arm).
    return {nid: {"loose": v["loose"], "arm": v["arm"]} for nid, v in out.items()}


@observe(tier="stage", metric="backend.graph.galaxy_layout")
def galaxy_layout(
    nodes: list[dict],
    edges: list[dict] | None = None,
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
        edges: real graph edges (``source`` + ``target``). Consumed for the ADR-0152
            bug #3a light path: a loose entity/wiki hub with edges into a real
            cluster is promoted onto that neighbour cluster's arm (leaves the core).
        clusters: the ``/api/graph`` ``clusters[]`` payload (member_node_ids +
            member_count). Multi-member clusters become arms; loose nodes the core.
        arms: number of spiral arms (K). EVERY multi-member cluster maps to exactly
            ONE arm via greedy lightest-arm bin-packing (no spine budget / scatter).
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

    # Single source of truth for placement: loose→core, clustered/connected→arm.
    # ADR-0152 bug #4: every multi-member cluster gets exactly one real arm (no
    # arms*3 budget, no arm=-2 scatter). Bug #3a: connected loose hubs join arms.
    membership = galaxy_membership(nodes, edges, clusters, arms)

    positions: dict[str, list[float]] = {}
    for nid in ids:
        info = membership.get(nid, {"loose": True, "arm": -1})
        loose = info["loose"]
        arm = info["arm"]

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
                # Defensive: with bug #4 fixed every clustered node has arm>=0, so
                # this branch is unreachable. Kept as a non-crashing fallback.
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


def _place_in_core(node_id: str, scale: float = _LAYOUT_SCALE) -> list[float]:
    """Deterministic core-bulge placement for an uncached node (ADR-0152 R1).

    Since the client stops computing on load, an uncached node with no served
    position would render at the origin dot. Place it in the dense central bulge
    (ADR-0134 "intra-day nodes sit near origin" precedent) — deterministic per
    node id (a stable hash seeds a fresh PRNG) so the same node lands in the same
    spot across requests. NEVER returns the literal origin (radius floored > 0).
    """
    import math  # noqa: PLC0415

    seed = int(hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:8], 16)
    rng = _Mulberry32(seed or 1)
    # Same bulge sampler as the loose branch of galaxy_layout, at default density.
    bulge_l = (_GALAXY_R_SCALE * 0.42) / (0.6 + 1.0 * 0.9)
    rr = _exp_radius(rng, bulge_l, _GALAXY_R_MAX * 0.55, 1.4)
    rr = max(rr, _GALAXY_R_CORE * 0.25)  # floor > 0 so it never sits at the origin
    th = rng.random() * math.pi * 2
    ph = math.acos(2 * rng.random() - 1)
    x = rr * math.sin(ph) * math.cos(th)
    z = rr * math.sin(ph) * math.sin(th)
    y = rr * math.cos(ph) * _GALAXY_SPHEROID_FLATTEN
    # Rescale to the layout extent (positions in the cache are already scaled;
    # _GALAXY_R_MAX is the pre-scale disk radius). Approximate scale-up so the
    # placed node sits inside the served core, not at native ~[-46, 46].
    factor = scale / _GALAXY_R_MAX
    return [round(x * factor, 6), round(y * factor, 6), round(z * factor, 6)]


def attach_cached_positions(data: dict, cache: dict | None) -> dict:
    """Attach cached x/y/z (+ loose/arm membership) to each served node BY ID.

    Returns ``data`` mutated. No positions are attached when ``cache`` is None or
    has no positions — the served nodes stay bare and the client runs its cold
    fallback. viz-render-perf removed the VIZ_PRECOMPUTED_LAYOUT_ENABLED gate:
    attach is unconditional given a cache.

    Attach is BY NODE-ID over the full-graph cache (a superset of any capped
    subset). ADR-0152:
      - **place-if-missing (R1):** a node absent from the cache (added since the
        last precompute) gets a deterministic core-bulge position so it never
        renders at the origin dot — the client no longer computes on load.
      - **membership seam:** when the cache carries a ``membership`` sibling
        (``{id: {loose, arm}}``), stamp ``n["loose"]`` + ``n["arm"]`` so the
        client (Car B) reads ONE backend source of truth for positioning AND
        core-node edge suppression.

    finish-viz (galaxy): the cache's ``layout_mode`` is stamped onto the payload as
    ``data["layout_mode"]`` so the client can FREEZE physics on a "galaxy" payload.
    """
    if not cache:
        return data
    positions = cache.get("positions") or {}
    if not positions:
        return data
    membership = cache.get("membership") or {}
    # Surface which generator produced these positions so the client can pick its
    # cooldown strategy (galaxy → freeze; spring → relax).
    mode = str(cache.get("layout_mode") or "spring")
    data["layout_mode"] = mode
    place_missing = mode == "galaxy"  # only galaxy layout has a core to place into
    for n in data.get("nodes", []):
        nid = n.get("id")
        if nid is None:
            continue
        skey = str(nid)
        coords = positions.get(skey)
        if coords and len(coords) >= 3:
            n["x"], n["y"], n["z"] = coords[0], coords[1], coords[2]
        elif place_missing:
            # R1: uncached node → deterministic core placement (not the origin).
            px, py, pz = _place_in_core(skey)
            n["x"], n["y"], n["z"] = px, py, pz
            n.setdefault("loose", True)  # placed in the core → loose by construction
            n.setdefault("arm", -1)
        m = membership.get(skey)
        if m:
            n["loose"] = bool(m.get("loose", True))
            n["arm"] = int(m.get("arm", -1))
    return data
