"""F5 fidelity test — single-source-of-truth contract.

Builds a known graph via mock storage, asserts the /api/graph payload
and the panel connection-count formula both derive from the same edge set.

Covers:
- F1: connection count for entity nodes includes entity-relation edge types
- F3: payload carries typed node ids (entity:N, mem:N) that round-trip correctly
- F4: weak transition edges (count<2) are hidden; payload carries weak_edges_hidden count
- F5 invariant: every displayed value is derived from canonical DB records

The "panel connection count" is computed server-side in this test because the
panel count formula lives in graph-detail.js (client-side JS that cannot run in
pytest). We test the PAYLOAD that the panel consumes, asserting it contains all
edges for each node. The JS fix (F1) makes the panel derive its count from the
same allLinks set — we verify the payload is correct so the JS has correct input.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar.backend.graph.graph_api import GraphAPI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_storage(
    memory_rows=None,
    entity_rows=None,
    entity_rels=None,
    transitions=None,
    wiki_rows=None,
    causal_rows=None,
    wiki_crossrefs=None,
):
    s = MagicMock()
    s._q.return_value = memory_rows or []
    s.get_all_transitions.return_value = transitions or []
    s.get_all_wiki_crossrefs.return_value = wiki_crossrefs or []
    s.get_all_causal_edges.return_value = causal_rows or []
    s.get_relationships_by_types.return_value = entity_rels or []
    s.get_all_entities.return_value = entity_rows or []
    return s


def _mem_row(raw_id, heat=1.0, slot=None):
    return {
        "id": raw_id,
        "content": f"memory {raw_id}",
        "heat": heat,
        "tags": [],
        "directory_context": "/proj",
        "created_at": "2024-01-01",
        "slot_index": slot,
        "embedding": None,
    }


def _entity_row(eid, heat=0.5):
    return {"id": eid, "name": f"E{eid}", "heat": heat, "archived": False}


def _entity_rel(src_id, tgt_id, rel_type="co_occurrence"):
    return {
        "source_entity_id": src_id,
        "target_entity_id": tgt_id,
        "relationship_type": rel_type,
        "weight": 1.0,
    }


def _count_edges_for_node(node_id: str, edges: list[dict]) -> int:
    """Count edges incident to node_id in the payload edge list."""
    count = 0
    for e in edges:
        src = e.get("source", "")
        tgt = e.get("target", "")
        if src == node_id or tgt == node_id:
            count += 1
    return count


# ---------------------------------------------------------------------------
# F3 — typed node IDs round-trip correctly
# ---------------------------------------------------------------------------


class TestF3TypedNodeIds:
    """F3: node ids in payload are typed (entity:N, mem:N) and carry type field."""

    def test_memory_nodes_have_typed_id(self):
        """Memory nodes carry id='mem:<int>'."""
        s = _make_storage(memory_rows=[_mem_row(1)])
        result = GraphAPI(s).get_full_graph()
        mem_nodes = [n for n in result["nodes"] if n.get("type") == "memory"]
        assert mem_nodes, "Expected at least one memory node"
        for n in mem_nodes:
            assert n["id"].startswith("mem:"), f"Memory node id not typed: {n['id']}"

    def test_entity_nodes_have_typed_id(self):
        """Entity nodes carry id='entity:<int>'."""
        s = _make_storage(entity_rows=[_entity_row(10)])
        result = GraphAPI(s).get_full_graph()
        ent_nodes = [n for n in result["nodes"] if n.get("type") == "entity"]
        assert ent_nodes, "Expected at least one entity node"
        for n in ent_nodes:
            assert n["id"].startswith("entity:"), f"Entity node id not typed: {n['id']}"

    def test_all_nodes_carry_type_field(self):
        """Every node in the payload has a 'type' field."""
        s = _make_storage(
            memory_rows=[_mem_row(1)],
            entity_rows=[_entity_row(10)],
        )
        result = GraphAPI(s).get_full_graph()
        for n in result["nodes"]:
            assert "type" in n, f"Node missing 'type' field: {n}"
            assert n["type"] in ("memory", "wiki", "entity"), f"Node type unexpected: {n['type']}"


# ---------------------------------------------------------------------------
# F4 — weak transition edges hidden; payload carries weak_edges_hidden count
# ---------------------------------------------------------------------------


class TestF4WeakEdgesAffordance:
    """F4: transitions with count<2 are hidden; payload surfaces weak_edges_hidden."""

    def test_transition_count_lt2_not_in_payload(self):
        """Transition with count=1 must NOT appear in the payload."""
        mem_rows = [_mem_row(1), _mem_row(2)]
        transitions = [{"from_memory_id": 1, "to_memory_id": 2, "count": 1}]
        s = _make_storage(memory_rows=mem_rows, transitions=transitions)
        result = GraphAPI(s).get_full_graph()
        trn_edges = [e for e in result["edges"] if e.get("type") == "transition"]
        assert trn_edges == [], f"Expected 0 transition edges for count=1, got {len(trn_edges)}"

    def test_transition_count_ge2_in_payload(self):
        """Transition with count=2 MUST appear in the payload."""
        mem_rows = [_mem_row(1), _mem_row(2)]
        transitions = [{"from_memory_id": 1, "to_memory_id": 2, "count": 2}]
        s = _make_storage(memory_rows=mem_rows, transitions=transitions)
        result = GraphAPI(s).get_full_graph()
        trn_edges = [e for e in result["edges"] if e.get("type") == "transition"]
        assert len(trn_edges) == 1, f"Expected 1 transition edge for count=2, got {len(trn_edges)}"

    def test_payload_carries_weak_edges_hidden_count(self):
        """When weak edges are suppressed, payload must carry 'weak_edges_hidden' count > 0."""
        mem_rows = [_mem_row(1), _mem_row(2), _mem_row(3)]
        transitions = [
            {"from_memory_id": 1, "to_memory_id": 2, "count": 1},  # weak — hidden
            {"from_memory_id": 2, "to_memory_id": 3, "count": 1},  # weak — hidden
            {"from_memory_id": 1, "to_memory_id": 3, "count": 3},  # strong — included
        ]
        s = _make_storage(memory_rows=mem_rows, transitions=transitions)
        result = GraphAPI(s).get_full_graph()
        assert "weak_edges_hidden" in result, (
            "Payload must carry 'weak_edges_hidden' when transitions are suppressed"
        )
        assert result["weak_edges_hidden"] == 2, (
            f"Expected weak_edges_hidden=2, got {result.get('weak_edges_hidden')}"
        )

    def test_payload_weak_edges_hidden_zero_when_none_suppressed(self):
        """When no edges are suppressed, weak_edges_hidden == 0 (not absent)."""
        mem_rows = [_mem_row(1), _mem_row(2)]
        transitions = [{"from_memory_id": 1, "to_memory_id": 2, "count": 5}]
        s = _make_storage(memory_rows=mem_rows, transitions=transitions)
        result = GraphAPI(s).get_full_graph()
        assert "weak_edges_hidden" in result, "Payload must always carry 'weak_edges_hidden'"
        assert result["weak_edges_hidden"] == 0


# ---------------------------------------------------------------------------
# F1 — connection count derives from full edge set (all 11 types)
# ---------------------------------------------------------------------------


class TestF1ConnectionCount:
    """F1 fidelity: entity nodes have non-zero connection count when edges are present.

    The JS panel counts edges by iterating allLinks (the rendered edge list).
    We test the payload: entity nodes appear in edges, so a correct panel
    implementation would count those edges correctly.

    This test asserts that entity-typed edges ARE present in the payload
    for entity nodes — i.e. the payload has the data needed for an accurate count.
    """

    def test_entity_node_has_incident_edges_in_payload(self):
        """Entity node wired by co_occurrence edges appears as edge endpoint in payload."""
        entity_rows = [_entity_row(1), _entity_row(2)]
        entity_rels = [_entity_rel(1, 2, "co_occurrence")]
        s = _make_storage(entity_rows=entity_rows, entity_rels=entity_rels)
        result = GraphAPI(s).get_full_graph()

        count_e1 = _count_edges_for_node("entity:1", result["edges"])
        assert count_e1 > 0, (
            f"entity:1 has 0 incident edges in payload; entity nodes should have "
            f"co_occurrence edges. Payload edges: {result['edges']}"
        )

    def test_entity_node_connection_count_matches_db_rels(self):
        """Connection count from payload edges matches the entity relation count in DB."""
        entity_rows = [_entity_row(1), _entity_row(2), _entity_row(3)]
        entity_rels = [
            _entity_rel(1, 2, "co_occurrence"),
            _entity_rel(1, 3, "resolved_by"),
        ]
        s = _make_storage(entity_rows=entity_rows, entity_rels=entity_rels)
        result = GraphAPI(s).get_full_graph()

        # entity:1 is the source of both rels → 2 edges
        count = _count_edges_for_node("entity:1", result["edges"])
        assert count == 2, (
            f"entity:1 should have 2 incident edges (co_occurrence + resolved_by), got {count}"
        )

    def test_all_edge_types_reachable_for_count(self):
        """All viz entity-rel EDGE_TYPES can appear in allLinks; panel can count any.

        v5.86 VIZ Batch-2 (P0.4): imports/calls dropped from the viz set.
        """
        from yadgar.core.viz_meta import EDGE_TYPES

        entity_rel_types = ["co_occurrence", "resolved_by", "caused_by"]
        entity_rows = [_entity_row(i) for i in range(1, 12)]
        entity_rels = [_entity_rel(i, i + 5, rt) for i, rt in enumerate(entity_rel_types, 1)]
        s = _make_storage(entity_rows=entity_rows, entity_rels=entity_rels)
        result = GraphAPI(s).get_full_graph()

        rendered_types = {e["type"] for e in result["edges"]}
        # Entity-relation types should all appear
        for rt in entity_rel_types:
            assert rt in rendered_types, (
                f"Edge type '{rt}' missing from payload — panel cannot count it"
            )

        # All types in EDGE_TYPES (v5.87 C3: semantic removed entirely; the
        # subtraction is now a harmless no-op kept for documentation).
        expected_in_default = set(EDGE_TYPES.keys()) - {"semantic"}
        # We only check types that have data in this mock — just entity rels
        for rt in entity_rel_types:
            assert rt in expected_in_default, f"{rt} must be in EDGE_TYPES"


# ---------------------------------------------------------------------------
# F5 — full fidelity invariant: known graph, mutated edge, payload matches DB
# ---------------------------------------------------------------------------


class TestF5FidelityInvariant:
    """F5: build a known graph, mutate an entity edge, assert payload matches DB.

    This is the single-source-of-truth contract test. The 'DB' is the mock
    storage; we change what the mock returns and assert the payload reflects it.
    """

    def test_adding_entity_rel_appears_in_payload(self):
        """When DB gains a new entity relation, payload immediately includes it."""
        entity_rows = [_entity_row(10), _entity_row(20)]

        # Step 1: no entity rels
        s = _make_storage(entity_rows=entity_rows, entity_rels=[])
        result_before = GraphAPI(s).get_full_graph()
        co_before = [e for e in result_before["edges"] if e.get("type") == "co_occurrence"]
        assert co_before == [], "Expected no co_occurrence edges before DB insert"

        # Step 2: DB gains a relation (mock updated)
        s.get_relationships_by_types.return_value = [_entity_rel(10, 20, "co_occurrence")]
        result_after = GraphAPI(s).get_full_graph()
        co_after = [e for e in result_after["edges"] if e.get("type") == "co_occurrence"]
        assert len(co_after) == 1, (
            f"Expected 1 co_occurrence edge after DB insert, got {len(co_after)}"
        )

    def test_heat_mutation_reflected_in_payload(self):
        """When DB heat changes, next /api/graph call reflects the new value."""
        # Step 1: heat = 1.0
        s = _make_storage(memory_rows=[_mem_row(5, heat=1.0)])
        result_before = GraphAPI(s).get_full_graph()
        node_before = next(n for n in result_before["nodes"] if n["id"] == "mem:5")
        assert node_before["heat"] == 1.0

        # Step 2: heat decays to 0.3 in DB
        s._q.return_value = [_mem_row(5, heat=0.3)]
        result_after = GraphAPI(s).get_full_graph()
        node_after = next(n for n in result_after["nodes"] if n["id"] == "mem:5")
        assert node_after["heat"] == 0.3, (
            f"Expected heat=0.3 after DB mutation, got {node_after['heat']}"
        )

    def test_payload_edge_endpoints_match_db_node_ids(self):
        """Every edge endpoint in the payload matches a node id in the payload."""
        entity_rows = [_entity_row(1), _entity_row(2)]
        entity_rels = [_entity_rel(1, 2, "co_occurrence")]
        mem_rows = [_mem_row(100)]
        s = _make_storage(
            memory_rows=mem_rows,
            entity_rows=entity_rows,
            entity_rels=entity_rels,
        )
        result = GraphAPI(s).get_full_graph()
        node_ids = {n["id"] for n in result["nodes"]}
        for edge in result["edges"]:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            assert src in node_ids, f"Edge source '{src}' not in node set"
            assert tgt in node_ids, f"Edge target '{tgt}' not in node set"

    def test_payload_has_nodes_and_edges_keys(self):
        """get_full_graph always returns dict with 'nodes' and 'edges' keys."""
        s = _make_storage()
        result = GraphAPI(s).get_full_graph()
        assert "nodes" in result
        assert "edges" in result
        assert isinstance(result["nodes"], list)
        assert isinstance(result["edges"], list)

    def test_panel_count_formula_using_all_edge_types(self):
        """Verify count-from-allLinks approach gives non-zero for entity nodes.

        This directly validates that the F1 fix (count all rendered edges,
        not just semantic/temporal/transition/memory_wiki) is correct.
        The panel JS does: allLinks.filter(l => l.source===node.id || l.target===node.id).
        We replicate that formula here against the payload edges.
        """
        entity_rows = [_entity_row(7), _entity_row(8)]
        entity_rels = [
            _entity_rel(7, 8, "co_occurrence"),
            _entity_rel(7, 8, "caused_by"),
        ]
        s = _make_storage(entity_rows=entity_rows, entity_rels=entity_rels)
        result = GraphAPI(s).get_full_graph()

        # Replicate the panel JS count formula on the payload
        all_edges = result["edges"]
        node_id = "entity:7"

        # OLD formula (only 4 types) — would give 0 for entity node
        old_types = {"semantic", "temporal", "transition", "memory_wiki"}
        old_count = sum(
            1
            for e in all_edges
            if (e.get("source") == node_id or e.get("target") == node_id)
            and e.get("type") in old_types
        )

        # NEW formula (all rendered types = all edges incident to node)
        new_count = sum(
            1 for e in all_edges if e.get("source") == node_id or e.get("target") == node_id
        )

        assert old_count == 0, (
            "Old 4-type formula should give 0 for entity node — confirms the bug exists"
        )
        assert new_count == 2, f"New all-types formula should give 2 for entity:7, got {new_count}"
