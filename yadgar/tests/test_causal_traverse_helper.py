"""§18 dedupe: _traverse_oriented_edges helper.

query_causes and query_effects share a BFS traversal body.  After the
refactor both delegate to _traverse_oriented_edges.  These tests verify:

  1. The helper is importable.
  2. query_causes and query_effects produce the same results as before the
     refactor (parity with the old duplicated bodies).
  3. Direction control: causes follow target→source edges; effects follow
     source→target edges.
"""

from unittest.mock import MagicMock

import pytest

from yadgar.core.causal_discovery import CausalDiscovery as CausalDiscoveryEngine


@pytest.fixture()
def mock_storage():
    s = MagicMock()
    return s


def test_helper_is_importable():
    from yadgar.core.causal_discovery import _traverse_oriented_edges  # noqa: F401


def test_query_causes_empty_when_no_entity(mock_storage):
    mock_storage.get_entity_by_name.return_value = None
    engine = CausalDiscoveryEngine(
        storage=mock_storage, knowledge_graph=MagicMock(), settings=MagicMock()
    )
    result = engine.query_causes("unknown_entity")
    assert result == []


def test_query_effects_empty_when_no_entity(mock_storage):
    mock_storage.get_entity_by_name.return_value = None
    engine = CausalDiscoveryEngine(
        storage=mock_storage, knowledge_graph=MagicMock(), settings=MagicMock()
    )
    result = engine.query_effects("unknown_entity")
    assert result == []


def test_causes_traversal_upstream(mock_storage):
    """Causes follow edges where current entity is TARGET."""
    target_entity = {"id": 10, "name": "effect_A"}
    source_entity = {"id": 20, "name": "cause_B"}

    mock_storage.get_entity_by_name.return_value = target_entity
    # Edge: 20 -> 10 (cause_B causes effect_A)
    mock_storage.get_causal_edges_for_entity.return_value = [
        {"source_entity_id": 20, "target_entity_id": 10, "confidence": 0.9},
    ]
    mock_storage.get_entity_by_id.return_value = source_entity

    engine = CausalDiscoveryEngine(
        storage=mock_storage, knowledge_graph=MagicMock(), settings=MagicMock()
    )
    results = engine.query_causes("effect_A", max_depth=1)

    assert len(results) == 1
    assert results[0]["entity"] == "cause_B"
    assert results[0]["depth"] == 1
    assert results[0]["confidence"] == pytest.approx(0.9)


def test_effects_traversal_downstream(mock_storage):
    """Effects follow edges where current entity is SOURCE."""
    source_entity = {"id": 10, "name": "cause_A"}
    target_entity = {"id": 20, "name": "effect_B"}

    mock_storage.get_entity_by_name.return_value = source_entity
    # Edge: 10 -> 20 (cause_A causes effect_B)
    mock_storage.get_causal_edges_for_entity.return_value = [
        {"source_entity_id": 10, "target_entity_id": 20, "confidence": 0.7},
    ]
    mock_storage.get_entity_by_id.return_value = target_entity

    engine = CausalDiscoveryEngine(
        storage=mock_storage, knowledge_graph=MagicMock(), settings=MagicMock()
    )
    results = engine.query_effects("cause_A", max_depth=1)

    assert len(results) == 1
    assert results[0]["entity"] == "effect_B"
    assert results[0]["depth"] == 1
    assert results[0]["confidence"] == pytest.approx(0.7)


def test_causes_sorted_by_depth_then_confidence(mock_storage):
    """Results are sorted: depth ASC, confidence DESC."""
    root = {"id": 1, "name": "root"}
    a = {"id": 2, "name": "A"}
    b = {"id": 3, "name": "B"}

    entities = {1: root, 2: a, 3: b}

    def get_edges(entity_id):
        if entity_id == 1:
            return [
                {"source_entity_id": 2, "target_entity_id": 1, "confidence": 0.5},
                {"source_entity_id": 3, "target_entity_id": 1, "confidence": 0.9},
            ]
        return []

    mock_storage.get_entity_by_name.return_value = root
    mock_storage.get_causal_edges_for_entity.side_effect = get_edges
    mock_storage.get_entity_by_id.side_effect = lambda eid: entities.get(eid)

    engine = CausalDiscoveryEngine(
        storage=mock_storage, knowledge_graph=MagicMock(), settings=MagicMock()
    )
    results = engine.query_causes("root", max_depth=2)

    assert len(results) == 2
    # Depth equal → sorted by confidence DESC: B(0.9) before A(0.5)
    assert results[0]["entity"] == "B"
    assert results[1]["entity"] == "A"
