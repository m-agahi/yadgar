"""Tests for graph_layout.compute_graph_layout + graph_signature (v5.88).

Server-side precomputed 3D force layout: deterministic per-node positions cached
during consolidation so the viz renders pre-laid-out instead of running a slow
cold client-side d3-force settle.
"""

from yadgar.backend.graph.graph_layout import compute_graph_layout, graph_signature


def _nodes(ids):
    return [{"id": i} for i in ids]


def _edges(pairs):
    return [{"source": s, "target": t} for s, t in pairs]


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
