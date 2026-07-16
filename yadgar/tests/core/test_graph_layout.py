"""Tests for graph_layout.compute_graph_layout + graph_signature (v5.88).

Server-side precomputed 3D force layout: deterministic per-node positions cached
during consolidation so the viz renders pre-laid-out instead of running a slow
cold client-side d3-force settle.
"""

import math

from yadgar.backend.graph.graph_layout import (
    compute_graph_layout,
    galaxy_layout,
    graph_signature,
)


def _nodes(ids):
    return [{"id": i} for i in ids]


def _edges(pairs):
    return [{"source": s, "target": t} for s, t in pairs]


def _cluster(cid, members):
    """A /api/graph clusters[] entry (member_node_ids + real member_count)."""
    return {
        "id": cid,
        "member_node_ids": list(members),
        "member_count": len(members),
    }


def _radius(coord):
    """Disk radius (x,z plane) of a galaxy position — arm/core discriminator."""
    return math.hypot(coord[0], coord[2])


def test_one_position_per_node():
    """Every node gets exactly one [x, y, z] position."""
    nodes = _nodes(["mem:1", "mem:2", "wiki:3"])
    edges = _edges([("mem:1", "mem:2"), ("mem:2", "wiki:3")])
    pos = compute_graph_layout(nodes, edges, dim=3)
    assert set(pos.keys()) == {"mem:1", "mem:2", "wiki:3"}
    for coord in pos.values():
        assert len(coord) == 3
        assert all(isinstance(c, float) for c in coord)


def test_positions_finite():
    """No NaN / Inf — every coordinate is a real finite number."""
    import math

    nodes = _nodes([f"mem:{i}" for i in range(20)])
    edges = _edges([(f"mem:{i}", f"mem:{i + 1}") for i in range(19)])
    pos = compute_graph_layout(nodes, edges, dim=3)
    for coord in pos.values():
        assert all(math.isfinite(c) for c in coord)


def test_deterministic_across_runs():
    """Same input → identical positions (seeded). Stable across runs."""
    nodes = _nodes(["a", "b", "c", "d"])
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")])
    p1 = compute_graph_layout(nodes, edges, dim=3)
    p2 = compute_graph_layout(nodes, edges, dim=3)
    assert p1 == p2


def test_deterministic_regardless_of_input_order():
    """Determinism depends on sorted node ids, not input list order."""
    edges = _edges([("a", "b"), ("b", "c"), ("c", "a")])
    p1 = compute_graph_layout(_nodes(["a", "b", "c"]), edges, dim=3)
    p2 = compute_graph_layout(_nodes(["c", "a", "b"]), list(reversed(edges)), dim=3)
    assert p1 == p2


def test_isolated_nodes_get_positions():
    """Nodes with no edges still get a position (no orphan dropped)."""
    nodes = _nodes(["x", "y", "z"])
    pos = compute_graph_layout(nodes, [], dim=3)
    assert set(pos.keys()) == {"x", "y", "z"}


def test_empty_graph_returns_empty():
    assert compute_graph_layout([], [], dim=3) == {}


def test_edges_referencing_missing_nodes_ignored():
    """Edge endpoints absent from the node set don't create phantom positions."""
    nodes = _nodes(["a", "b"])
    edges = _edges([("a", "b"), ("a", "ghost")])
    pos = compute_graph_layout(nodes, edges, dim=3)
    assert set(pos.keys()) == {"a", "b"}


def test_dim_2_returns_two_coords():
    nodes = _nodes(["a", "b"])
    pos = compute_graph_layout(nodes, edges=_edges([("a", "b")]), dim=2)
    for coord in pos.values():
        assert len(coord) == 2


def test_scale_controls_coordinate_extent():
    """Larger scale → larger coordinate magnitudes (matches client link distance)."""
    nodes = _nodes(["a", "b", "c", "d", "e"])
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"), ("e", "a")])
    small = compute_graph_layout(nodes, edges, dim=3, scale=1.0)
    big = compute_graph_layout(nodes, edges, dim=3, scale=40.0)
    small_max = max(abs(c) for coord in small.values() for c in coord)
    big_max = max(abs(c) for coord in big.values() for c in coord)
    assert big_max > small_max * 10  # ~40x extent


def test_default_scale_is_viz_extent():
    """Default scale puts coords in the client's ~40-unit extent, not ~[-1,1]."""
    nodes = _nodes(["a", "b", "c", "d", "e"])
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")])
    pos = compute_graph_layout(nodes, edges, dim=3)
    max_coord = max(abs(c) for coord in pos.values() for c in coord)
    assert max_coord > 5.0  # well beyond the native [-1, 1]


# ── graph_signature ──────────────────────────────────────────────────────────


def test_signature_stable_for_same_graph():
    nodes = _nodes(["a", "b", "c"])
    edges = _edges([("a", "b"), ("b", "c")])
    assert graph_signature(nodes, edges) == graph_signature(nodes, edges)


def test_signature_order_independent():
    nodes_a = _nodes(["a", "b", "c"])
    nodes_b = _nodes(["c", "b", "a"])
    edges_a = _edges([("a", "b"), ("b", "c")])
    edges_b = _edges([("b", "c"), ("a", "b")])
    assert graph_signature(nodes_a, edges_a) == graph_signature(nodes_b, edges_b)


def test_signature_changes_on_added_node():
    nodes = _nodes(["a", "b"])
    edges = _edges([("a", "b")])
    sig1 = graph_signature(nodes, edges)
    sig2 = graph_signature(_nodes(["a", "b", "c"]), edges)
    assert sig1 != sig2


def test_signature_changes_on_added_edge():
    nodes = _nodes(["a", "b", "c"])
    sig1 = graph_signature(nodes, _edges([("a", "b")]))
    sig2 = graph_signature(nodes, _edges([("a", "b"), ("b", "c")]))
    assert sig1 != sig2


def test_signature_is_short_string():
    """Signature is a compact hex digest suitable for a meta-row value."""
    sig = graph_signature(_nodes(["a"]), [])
    assert isinstance(sig, str)
    assert 0 < len(sig) <= 64


# ---------------------------------------------------------------------------
# Galaxy layout (finish-viz — port of docs/plans/viz-galaxy.mockup.html)
# ---------------------------------------------------------------------------


def _galaxy_fixture():
    """A graph with two real arms + one demoted single-member cluster + loose nodes.

    - cluster 1: 8 members  → real (arm material)
    - cluster 2: 2 members  → real (arm material)
    - cluster 3: 1 member   → demoted to loose (single-member "not a real cluster")
    - entity:*, mem:11..19  → unclustered loose (core material)
    """
    nodes = _nodes([f"mem:{i}" for i in range(20)] + [f"entity:{i}" for i in range(10)])
    clusters = [
        _cluster(1, [f"mem:{i}" for i in range(8)]),  # arm
        _cluster(2, ["mem:8", "mem:9"]),  # arm
        _cluster(3, ["mem:10"]),  # single → loose (core)
    ]
    arm_ids = [f"mem:{i}" for i in range(10)]  # clusters 1 + 2
    loose_ids = (
        ["mem:10"] + [f"mem:{i}" for i in range(11, 20)] + [f"entity:{i}" for i in range(10)]
    )
    return nodes, clusters, arm_ids, loose_ids


def test_galaxy_one_finite_position_per_node():
    """Every node gets exactly one finite [x, y, z] position; empty input → {}."""
    nodes, clusters, _arm, _loose = _galaxy_fixture()
    pos = galaxy_layout(nodes, [], clusters)
    assert set(pos.keys()) == {n["id"] for n in nodes}
    for coord in pos.values():
        assert len(coord) == 3
        assert all(math.isfinite(c) for c in coord)
    assert galaxy_layout([], [], []) == {}


def test_galaxy_deterministic():
    """Seeded PRNG → identical positions across runs."""
    nodes, clusters, _arm, _loose = _galaxy_fixture()
    assert galaxy_layout(nodes, [], clusters) == galaxy_layout(nodes, [], clusters)


def test_galaxy_loose_nodes_sit_in_dense_core():
    """Loose/single nodes get SMALL radius (dense core); arm nodes sit further out.

    The core is a dense central bulge; arms are on an exponential disk that
    extends to the rim. So the mean loose-node radius must be well under the mean
    arm-node radius.
    """
    import statistics

    nodes, clusters, arm_ids, loose_ids = _galaxy_fixture()
    pos = galaxy_layout(nodes, [], clusters)
    loose_mean = statistics.mean(_radius(pos[n]) for n in loose_ids)
    arm_mean = statistics.mean(_radius(pos[n]) for n in arm_ids)
    assert loose_mean < arm_mean
    # Core is genuinely tight (small radius), not merely "less than arms".
    assert loose_mean < arm_mean * 0.5


def test_galaxy_single_member_cluster_demoted_to_core():
    """A single-member cluster is NOT an arm — its member lands in the core."""
    import statistics

    nodes, clusters, arm_ids, _loose = _galaxy_fixture()
    pos = galaxy_layout(nodes, [], clusters)
    arm_mean = statistics.mean(_radius(pos[n]) for n in arm_ids)
    # mem:10 is the lone member of single-member cluster 3 → demoted to loose/core.
    assert _radius(pos["mem:10"]) < arm_mean


def test_galaxy_produces_k_arms():
    """Clustered nodes map to K distinct spiral-arm angular sectors.

    With 2 real clusters bucketed round-robin into arms, their members occupy
    distinct base angles. We assert the arm-node azimuths form at least 2 clusters
    of direction (K arms), not a single blob.
    """
    nodes = _nodes([f"mem:{i}" for i in range(60)])
    # 4 real clusters, 15 members each → 4 arms populated.
    clusters = [_cluster(c, [f"mem:{c * 15 + i}" for i in range(15)]) for c in range(4)]
    pos = galaxy_layout(nodes, [], clusters, arms=4)
    # Bucket arm-node azimuths; expect several distinct arm directions.
    angles = [math.atan2(pos[n["id"]][2], pos[n["id"]][0]) for n in nodes]
    # Coarse sectorization into 8 bins; K=4 arms should light up >=3 bins.
    bins = {int((a + math.pi) / (2 * math.pi) * 8) % 8 for a in angles}
    assert len(bins) >= 3


def test_galaxy_arm_count_knob_respected():
    """More arms → members spread across more angular sectors."""
    nodes = _nodes([f"mem:{i}" for i in range(90)])
    clusters = [_cluster(c, [f"mem:{c * 15 + i}" for i in range(15)]) for c in range(6)]

    def _sector_count(arms):
        pos = galaxy_layout(nodes, [], clusters, arms=arms)
        angles = [math.atan2(pos[n["id"]][2], pos[n["id"]][0]) for n in nodes]
        return len({int((a + math.pi) / (2 * math.pi) * 12) % 12 for a in angles})

    # 6 arms should occupy at least as many distinct sectors as 2 arms.
    assert _sector_count(6) >= _sector_count(2)


def test_galaxy_exponential_radial_density():
    """Radial density falls off outward — most core mass is near the center.

    Sample a pure-loose corpus (all core): the median radius must be well below
    the max radius (dense center, sparse rim) — i.e. NOT uniform over the disk.
    """
    import statistics

    nodes = _nodes([f"e:{i}" for i in range(400)])  # all loose → all core
    pos = galaxy_layout(nodes, [], [])  # no clusters
    radii = sorted(_radius(c) for c in pos.values())
    median = statistics.median(radii)
    rmax = radii[-1]
    # Exponential falloff: the median sits far below the max (dense center).
    assert median < rmax * 0.5


def test_galaxy_heat_is_not_position():
    """Heat does not move a node — two nodes differing only in heat are placed
    by membership/seed, not by heat value."""
    n_cold = _nodes(["mem:0", "mem:1", "mem:2"])
    for n in n_cold:
        n["heat"] = 0.01
    n_hot = _nodes(["mem:0", "mem:1", "mem:2"])
    for n in n_hot:
        n["heat"] = 0.99
    clusters = [_cluster(1, ["mem:0", "mem:1", "mem:2"])]
    assert galaxy_layout(n_cold, [], clusters) == galaxy_layout(n_hot, [], clusters)
