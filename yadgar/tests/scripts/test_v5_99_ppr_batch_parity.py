"""v5.99.0 — parity gate for the PPR + spreading N+1 fetch fix.

The graph-traversal hot path (`_build_networkx_graph` for PPR, `_spreading_bfs_step`
for spreading activation) previously issued one adjacency query per frontier node,
each of which issued two extra per-row name lookups (`storage/entity.py` enrichment).
The fix:

  1. `get_relationships_for_entity(..., with_names=False)` skips the two per-row name
     lookups. Names are unused by both hot-path consumers.
  2. A batched frontier fetch (`get_relationships_for_frontier`) replaces the one-query-
     per-node fan-out with one query per BFS depth.

This MUST be exact-parity: the same edges, weights, node set, insertion order, and
therefore identical PPR / spreading scores. This module is the gate. If it ever fails,
the fix perturbed ranking and must not ship.
"""

import networkx as nx
import pytest

from yadgar._shared.config import Settings
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.retrieval.graph_helpers import _GraphHelpersMixin
from yadgar._shared.storage import StorageEngine


class _Harness(_GraphHelpersMixin):
    """Minimal object exposing the three attrs the graph mixin reads."""

    def __init__(self, storage, graph, settings):
        self._storage = storage
        self._graph = graph
        self._settings = settings

    # Reproduce the spreading BFS traversal from Retriever.core (the frontier/visited
    # part that determines discovery order) so the parity test exercises the same
    # traversal without pulling in embeddings/reranker. The per-node variant mirrors
    # the pre-v5.99 code; the batched variant mirrors the shipped v5.99 code.
    def _spreading_step_pernode(self, frontier, visited, max_depth):
        nxt = []
        for entity_id, depth in frontier:
            if depth >= max_depth:
                continue
            for neighbor in self._graph._get_adjacent(entity_id, None):
                nid = neighbor["entity_id"]
                if nid in visited:
                    continue
                visited.add(nid)
                nxt.append((nid, depth + 1))
        return nxt

    def _spreading_bfs_step_batched(self, frontier, visited, max_depth):
        nxt = []
        to_expand = [eid for eid, depth in frontier if depth < max_depth]
        adjacency = self._graph._get_adjacent_batch(to_expand, None)
        for entity_id, depth in frontier:
            if depth >= max_depth:
                continue
            for neighbor in adjacency.get(entity_id, []):
                nid = neighbor["entity_id"]
                if nid in visited:
                    continue
                visited.add(nid)
                nxt.append((nid, depth + 1))
        return nxt


@pytest.fixture
def seeded(tmp_path):
    """A small graph with multi-path reachability, weight ties, and a self-loop."""
    storage = StorageEngine(str(tmp_path / "parity.db"))
    settings = Settings()
    graph = KnowledgeGraph(storage, settings)

    names = ["A", "B", "C", "D", "E", "F", "G"]
    eids = {n: storage.insert_entity({"name": n, "type": "file"}) for n in names}

    # Edges chosen so every parity-sensitive case is exercised on a node that is
    # actually EXPANDED (seeds A/B expand at depth 0; C/D at depth 1; E is only added
    # as a leaf and never expanded). The self-loop and min-weight filter are the two
    # places per-node and batched logic structurally differ, so they MUST sit on a
    # seed:
    #  - A and B both reach D (multi-path / diamond)
    #  - B->C and A->C share the SAME weight (tie) to stress float-sum / sort order
    #  - A->A is a self-loop (fan-out {src,tgt}&frontier must count it exactly once)
    #  - A->G has weight below GRAPH_MIN_EDGE_WEIGHT (0.1) -> filtered by PPR build,
    #    so G must NOT become a node
    edges = [
        ("A", "B", 1.0),
        ("A", "C", 0.5),
        ("B", "C", 0.5),
        ("A", "D", 0.7),
        ("B", "D", 0.7),
        ("A", "A", 0.3),  # self-loop on an EXPANDED node
        ("A", "G", 0.05),  # below min edge weight, from an EXPANDED node
        ("C", "E", 0.9),
        ("D", "E", 0.4),
    ]
    for s, t, w in edges:
        storage.insert_relationship(
            {
                "source_entity_id": eids[s],
                "target_entity_id": eids[t],
                "relationship_type": "co_occurrence",
                "weight": w,
            }
        )

    yield storage, graph, settings, eids
    storage.close()


def _graph_signature(G: nx.DiGraph):
    """Order-INSENSITIVE structural signature: node set, edge set, per-edge weight."""
    nodes = frozenset(G.nodes())
    edges = frozenset((u, v, round(d["weight"], 9)) for u, v, d in G.edges(data=True))
    return nodes, edges


def test_networkx_build_parity(seeded):
    """Batched PPR graph build == per-node build: nodes, edges, weights, and PPR scores."""
    storage, graph, settings, eids = seeded
    seeds = [eids["A"], eids["B"]]

    harness = _Harness(storage, graph, settings)

    # Per-node (legacy) build — force with_names path via _get_adjacent default.
    G_legacy = harness._build_networkx_graph_pernode(seeds)
    # Batched build — the shipped implementation.
    G_batched = harness._build_networkx_graph(seeds)

    assert _graph_signature(G_legacy) == _graph_signature(G_batched), (
        "batched graph structure diverged from per-node build"
    )

    # Edge-case coverage — these are the two places per-node and batched logic differ:
    #  1. Self-loop (A,A) must appear EXACTLY once (a list-based fan-out would double it).
    for G, label in ((G_legacy, "legacy"), (G_batched, "batched")):
        self_loops = [(u, v) for u, v in G.edges() if u == v]
        assert self_loops == [(eids["A"], eids["A"])], (
            f"{label} build must have the A->A self-loop exactly once, got {self_loops}"
        )
    #  2. The below-threshold A->G edge is filtered, so G must NOT be a node in either.
    assert eids["G"] not in G_legacy.nodes()
    assert eids["G"] not in G_batched.nodes()

    # Secondary gate: run PPR on both and require identical scores (insertion order
    # must not perturb the float summation).
    ppr_legacy = nx.pagerank(G_legacy, weight="weight")
    ppr_batched = nx.pagerank(G_batched, weight="weight")
    assert ppr_legacy.keys() == ppr_batched.keys()
    # Construction order is byte-identical, so pagerank must be bit-identical — exact ==,
    # not a tolerance. If this ever needs loosening, the builder perturbed insertion order.
    for k in ppr_legacy:
        assert ppr_legacy[k] == ppr_batched[k], (
            f"PPR score for {k} diverged: {ppr_legacy[k]} vs {ppr_batched[k]}"
        )


def test_spreading_bfs_parity(seeded):
    """Batched spreading BFS traversal visits the exact same entities in the same order."""
    storage, graph, settings, eids = seeded
    seed_entities = [eids["A"], eids["B"]]

    harness = _Harness(storage, graph, settings)

    # Legacy per-node traversal
    visited_legacy = set(seed_entities)
    order_legacy: list[int] = list(seed_entities)
    frontier = [(e, 0) for e in seed_entities]
    while frontier:
        nxt = harness._spreading_step_pernode(
            frontier, visited_legacy, settings.GRAPH_SPREADING_MAX_DEPTH
        )
        order_legacy.extend(nid for nid, _ in nxt)
        frontier = nxt

    # Batched traversal
    visited_batched = set(seed_entities)
    order_batched: list[int] = list(seed_entities)
    frontier = [(e, 0) for e in seed_entities]
    while frontier:
        nxt = harness._spreading_bfs_step_batched(
            frontier, visited_batched, settings.GRAPH_SPREADING_MAX_DEPTH
        )
        order_batched.extend(nid for nid, _ in nxt)
        frontier = nxt

    assert visited_legacy == visited_batched, "batched spreading visited a different entity set"
    assert order_legacy == order_batched, (
        f"batched spreading discovery order diverged:\n  legacy={order_legacy}\n  batched={order_batched}"
    )


def test_with_names_false_omits_name_enrichment(seeded):
    """with_names=False must skip the per-row name lookups but keep identical edge data."""
    storage, _graph, _settings, eids = seeded

    named = storage.get_relationships_for_entity(eids["A"])
    unnamed = storage.get_relationships_for_entity(eids["A"], with_names=False)

    # Same relationships, same order, same weights — only the enrichment differs.
    assert [r["id"] for r in named] == [r["id"] for r in unnamed]
    for n, u in zip(named, unnamed, strict=True):
        assert n["source_entity_id"] == u["source_entity_id"]
        assert n["target_entity_id"] == u["target_entity_id"]
        assert n["weight"] == u["weight"]
    # Named path enriches; unnamed path does not carry name keys.
    assert all("source_name" in r for r in named)
    assert all("source_name" not in r for r in unnamed)
