"""Tests for graph_layout.compute_graph_layout + graph_signature (v5.88).

Server-side precomputed 3D force layout: deterministic per-node positions cached
during consolidation so the viz renders pre-laid-out instead of running a slow
cold client-side d3-force settle.
"""

import math

from yadgar.backend.graph.graph_layout import (
    _LAYOUT_VERSION,
    attach_cached_positions,
    compute_graph_layout,
    galaxy_layout,
    galaxy_membership,
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


# ---------------------------------------------------------------------------
# Car A (ADR-0152): arm-budget fix, connectivity eligibility, membership flag,
# signature-includes-params, place-if-missing.
# ---------------------------------------------------------------------------


def test_galaxy_no_scatter_every_cluster_gets_an_arm():
    """Bug #4: drop the arms*3 spine budget — EVERY multi-member cluster maps to a
    real arm 0..arms-1 (no inter-arm scatter). With arms=2 and 10 clusters, all 10
    land on a real arm."""
    nodes = _nodes([f"mem:{i}" for i in range(50)])
    # 10 clusters of 5 members each; arms=2 → old code scattered clusters past
    # slot 6 (arms*3) to arm=-2. New code: all 10 get a real arm.
    clusters = [_cluster(c, [f"mem:{c * 5 + i}" for i in range(5)]) for c in range(10)]
    mem = galaxy_membership(nodes, [], clusters, arms=2)
    # Every clustered node is on a real arm in [0, 2).
    arm_nodes = [nid for nid, info in mem.items() if not info["loose"]]
    assert arm_nodes
    for nid in arm_nodes:
        assert 0 <= mem[nid]["arm"] < 2, f"{nid} scattered to arm {mem[nid]['arm']}"


def test_galaxy_arms_balanced_not_round_robin():
    """Greedy lightest-arm bin-packing balances node counts across arms — a few big
    clusters do not all pile onto arm 0/1 (the round-robin failure the client fixed)."""
    nodes = _nodes([f"mem:{i}" for i in range(60)])
    # Descending sizes: 20, 15, 10, 8, 4, 3 — greedy packs to balance load.
    sizes = [20, 15, 10, 8, 4, 3]
    clusters = []
    off = 0
    for c, n in enumerate(sizes):
        clusters.append(_cluster(c, [f"mem:{off + i}" for i in range(n)]))
        off += n
    mem = galaxy_membership(nodes[:off], [], clusters, arms=3)
    load = {0: 0, 1: 0, 2: 0}
    for info in mem.values():
        if not info["loose"]:
            load[info["arm"]] += 1
    counts = sorted(load.values())
    # Balanced: the heaviest arm is not absurdly larger than the lightest.
    assert counts[-1] <= counts[0] * 2 + 5


def test_galaxy_entity_with_edges_joins_neighbour_arm():
    """Bug #3a light: an entity node whose edges point into cluster C lands on C's
    arm (leaves the core), via the passed edges — not always-core."""
    nodes = _nodes([f"mem:{i}" for i in range(6)] + ["entity:hub"])
    clusters = [_cluster(1, [f"mem:{i}" for i in range(6)])]  # one real arm cluster
    # entity:hub connects to 3 members of cluster 1 → dominant neighbour = cluster 1.
    edges = _edges([("entity:hub", "mem:0"), ("entity:hub", "mem:1"), ("entity:hub", "mem:2")])
    mem = galaxy_membership(nodes, edges, clusters, arms=4)
    assert mem["entity:hub"]["loose"] is False
    # It shares cluster-1's arm.
    cluster1_arm = mem["mem:0"]["arm"]
    assert mem["entity:hub"]["arm"] == cluster1_arm


def test_galaxy_zero_edge_entity_stays_core():
    """A truly-single (0-edge) entity/wiki node stays loose → core."""
    nodes = _nodes([f"mem:{i}" for i in range(6)] + ["entity:lonely"])
    clusters = [_cluster(1, [f"mem:{i}" for i in range(6)])]
    mem = galaxy_membership(nodes, [], clusters, arms=4)  # no edges
    assert mem["entity:lonely"]["loose"] is True
    assert mem["entity:lonely"]["arm"] == -1


def test_galaxy_membership_deterministic():
    """Membership is deterministic given identical (nodes, edges, clusters, arms)."""
    nodes, clusters, _a, _l = _galaxy_fixture()
    edges = _edges([("entity:0", "mem:0"), ("entity:1", "mem:1")])
    assert galaxy_membership(nodes, edges, clusters, arms=4) == galaxy_membership(
        nodes, edges, clusters, arms=4
    )


def test_galaxy_entity_edges_to_core_only_stays_core():
    """An entity whose only edges point to loose/core nodes (no cluster) stays core."""
    nodes = _nodes([f"mem:{i}" for i in range(6)] + ["mem:99", "entity:x"])
    clusters = [_cluster(1, [f"mem:{i}" for i in range(6)])]
    # entity:x connects only to mem:99 which is loose (not in any cluster).
    edges = _edges([("entity:x", "mem:99")])
    mem = galaxy_membership(nodes, edges, clusters, arms=4)
    assert mem["entity:x"]["loose"] is True


# ── signature folds in _LAYOUT_VERSION + galaxy params (R6) ───────────────────


def test_signature_changes_on_layout_version_bump():
    """R6: bumping _LAYOUT_VERSION changes the signature on an IDENTICAL graph shape
    so new layout math actually recomputes on the nightly (not a no-op)."""
    nodes = _nodes(["a", "b", "c"])
    edges = _edges([("a", "b")])
    sig_v = graph_signature(nodes, edges, layout_version=_LAYOUT_VERSION)
    sig_other = graph_signature(nodes, edges, layout_version=_LAYOUT_VERSION + 1)
    assert sig_v != sig_other


def test_signature_changes_on_galaxy_param_change():
    """R6: changing arms/pitch/core_density changes the signature on an identical
    graph shape so a VIZ_GALAXY_* setting change takes effect on the next cycle."""
    nodes = _nodes(["a", "b", "c"])
    edges = _edges([("a", "b")])
    base = graph_signature(nodes, edges, params={"arms": 4, "pitch": 0.30, "coredens": 1.0})
    diff_arms = graph_signature(nodes, edges, params={"arms": 6, "pitch": 0.30, "coredens": 1.0})
    diff_pitch = graph_signature(nodes, edges, params={"arms": 4, "pitch": 0.50, "coredens": 1.0})
    assert base != diff_arms
    assert base != diff_pitch


def test_signature_backward_compatible_default():
    """graph_signature(nodes, edges) still works (2-arg legacy call) and is stable."""
    nodes = _nodes(["a", "b"])
    edges = _edges([("a", "b")])
    assert graph_signature(nodes, edges) == graph_signature(nodes, edges)


# ── place-if-missing (R1): uncached node gets a served core position ──────────


def test_attach_place_if_missing_gives_uncached_node_a_core_position():
    """R1: since the client stops computing on load, an uncached node MUST get a
    served position (place-if-missing) so it does not render at the origin dot."""
    data = {
        "nodes": [{"id": "a", "type": "memory"}, {"id": "new:1", "type": "entity"}],
        "edges": [],
    }
    cache = {
        "signature": "s",
        "positions": {"a": [10.0, 0.0, 0.0]},  # 'new:1' is uncached
        "layout_mode": "galaxy",
    }
    out = attach_cached_positions(data, cache)
    by_id = {n["id"]: n for n in out["nodes"]}
    # cached node keeps its position
    assert by_id["a"]["x"] == 10.0
    # uncached node gets SOME finite position (not bare, not at literal origin)
    assert "x" in by_id["new:1"] and "y" in by_id["new:1"] and "z" in by_id["new:1"]
    assert math.isfinite(by_id["new:1"]["x"])
    origin_dist = math.hypot(by_id["new:1"]["x"], by_id["new:1"]["y"], by_id["new:1"]["z"])
    assert origin_dist > 0.0, "uncached node must not sit at the literal origin"


def test_attach_place_if_missing_deterministic():
    """Place-if-missing is deterministic per node id (stable across requests)."""
    data1 = {"nodes": [{"id": "new:7", "type": "memory"}], "edges": []}
    data2 = {"nodes": [{"id": "new:7", "type": "memory"}], "edges": []}
    cache = {"signature": "s", "positions": {"other": [1.0, 1.0, 1.0]}, "layout_mode": "galaxy"}
    o1 = attach_cached_positions(data1, cache)
    o2 = attach_cached_positions(data2, cache)
    n1 = o1["nodes"][0]
    n2 = o2["nodes"][0]
    assert (n1["x"], n1["y"], n1["z"]) == (n2["x"], n2["y"], n2["z"])


def test_attach_stamps_membership_flag():
    """Car A seam: the cache carries a membership sibling → attach stamps n['loose']
    and n['arm'] so the client (Car B) reads ONE backend source of truth."""
    data = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": []}
    cache = {
        "signature": "s",
        "positions": {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]},
        "membership": {"a": {"loose": True, "arm": -1}, "b": {"loose": False, "arm": 2}},
        "layout_mode": "galaxy",
    }
    out = attach_cached_positions(data, cache)
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["a"]["loose"] is True
    assert by_id["a"]["arm"] == -1
    assert by_id["b"]["loose"] is False
    assert by_id["b"]["arm"] == 2


# ---------------------------------------------------------------------------
# Car A hardening (task #72): Hypothesis property tests for the pure functions
# (arm assignment, place-if-missing, signature). Fuzz the invariants the unit
# tests pin by example.
# ---------------------------------------------------------------------------

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from yadgar.backend.graph.graph_layout import _place_in_core  # noqa: E402


@settings(max_examples=200)
@given(
    n_clusters=st.integers(min_value=1, max_value=12),
    sizes=st.lists(st.integers(min_value=2, max_value=8), min_size=1, max_size=12),
    arms=st.integers(min_value=1, max_value=8),
)
def test_prop_membership_arm_in_range(n_clusters, sizes, arms):
    """Every NON-loose node lands on a real arm in [0, arms) — never scatter (-2)
    and never out of range — for any cluster shape (bug #4 invariant)."""
    sizes = sizes[:n_clusters] or [2]
    clusters = []
    off = 0
    for c, sz in enumerate(sizes):
        clusters.append(_cluster(c, [f"mem:{off + i}" for i in range(sz)]))
        off += sz
    nodes = _nodes([f"mem:{i}" for i in range(off)])
    mem = galaxy_membership(nodes, [], clusters, arms=arms)
    for info in mem.values():
        if not info["loose"]:
            assert 0 <= info["arm"] < arms


@settings(max_examples=200)
@given(node_id=st.text(min_size=1, max_size=40))
def test_prop_place_in_core_finite_nonorigin_deterministic(node_id):
    """place-if-missing: always finite, never the literal origin, deterministic
    per id (R1 — an uncached node must not render at the origin dot)."""
    a = _place_in_core(node_id)
    b = _place_in_core(node_id)
    assert a == b  # deterministic per id
    assert len(a) == 3
    assert all(math.isfinite(c) for c in a)
    assert math.hypot(*a) > 0.0  # not the origin


@settings(max_examples=200)
@given(
    ids=st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=10, unique=True),
    edge_ids=st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=10),
    version=st.integers(min_value=0, max_value=99),
)
def test_prop_signature_reorder_invariant_version_sensitive(ids, edge_ids, version):
    """graph_signature is invariant under node/edge reordering but changes when the
    layout version bumps (R6) — for any graph shape."""
    nodes = _nodes(ids)
    pairs = [(ids[i % len(ids)], ids[(i + 1) % len(ids)]) for i in edge_ids]
    edges = _edges(pairs)
    sig = graph_signature(nodes, edges, layout_version=version)
    sig_reordered = graph_signature(
        _nodes(list(reversed(ids))), _edges(list(reversed(pairs))), layout_version=version
    )
    assert sig == sig_reordered  # order-independent
    assert sig != graph_signature(nodes, edges, layout_version=version + 1)  # version-sensitive
