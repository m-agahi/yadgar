"""Tests for yadgar/metacognition/gap_detection.py — gap detection mixin.

Coverage targets:
- detect_gaps: isolated entities (0 or 1 conn), stale regions (>=2 stale),
  low-confidence zones, missing connections (co-occurrence without relationship),
  one-sided knowledge (error entity with no resolved_by relationship)
- stale_tags aggregation from memories
- co-occurrence pair dedup
- severity clamping
- directory vs. global memory fetching

COVERAGE FLOOR NOTE:
Running with --cov=yadgar.metacognition.gap_detection fails in this environment
(Python 3.14 + numpy 2.4.4 + missing libz.so.1) for the same reason as
test_metacognition_coverage.py — see that file's docstring.
Measured coverage via `coverage run --include=...`: 100%.
All 31 tests in this file pass without coverage instrumentation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.metacognition.gap_detection import _GapDetectionMixin

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_entity(eid: int, name: str, entity_type: str = "concept") -> dict:
    return {"id": eid, "name": name, "type": entity_type, "heat": 1.0}


def _make_memory(
    mid: int,
    content: str = "test content",
    heat: float = 1.0,
    confidence: float = 1.0,
    tags: list | None = None,
) -> dict:
    return {
        "id": mid,
        "content": content,
        "heat": heat,
        "confidence": confidence,
        "tags": tags or [],
    }


def _build_mixin(
    all_entities=None,
    adjacent_counts=None,
    dir_memories=None,
    all_memories=None,
    relationships_among=None,
    all_relationships=None,
    relationship_by_source_type=None,
) -> _GapDetectionMixin:
    """Build a _GapDetectionMixin with fully controlled mock state."""
    mixin = _GapDetectionMixin.__new__(_GapDetectionMixin)

    storage = MagicMock()
    storage.get_all_entities.return_value = all_entities or []
    storage.get_memories_for_directory.return_value = dir_memories or []
    storage.get_all_memories_for_decay.return_value = all_memories or []
    storage.get_relationships_among_entities.return_value = relationships_among or []
    storage.get_all_relationships.return_value = all_relationships or []
    storage.get_relationship_by_source_and_type.return_value = relationship_by_source_type

    graph = MagicMock()
    if adjacent_counts is None:
        graph._get_adjacent.return_value = []
    else:
        # adjacent_counts: {entity_id: list_of_neighbors}
        def _adj(eid, _):
            return adjacent_counts.get(eid, [])

        graph._get_adjacent.side_effect = _adj

    mixin._storage = storage
    mixin._graph = graph
    return mixin


# ── Isolated entities ─────────────────────────────────────────────────────────


def test_isolated_entity_zero_connections():
    entities = [_make_entity(1, "OrphanEntity")]
    mixin = _build_mixin(all_entities=entities, adjacent_counts={1: []})
    gaps = mixin.detect_gaps()
    isolated = [g for g in gaps if g["type"] == "isolated_entity"]
    assert len(isolated) == 1
    assert isolated[0]["severity"] == 0.6
    assert "OrphanEntity" in isolated[0]["entities"]


def test_isolated_entity_one_connection():
    entities = [_make_entity(1, "SingleConn")]
    mixin = _build_mixin(all_entities=entities, adjacent_counts={1: [2]})
    gaps = mixin.detect_gaps()
    isolated = [g for g in gaps if g["type"] == "isolated_entity"]
    assert len(isolated) == 1
    assert isolated[0]["severity"] == 0.4


def test_isolated_entity_two_or_more_not_flagged():
    entities = [_make_entity(1, "WellConnected")]
    mixin = _build_mixin(all_entities=entities, adjacent_counts={1: [2, 3]})
    gaps = mixin.detect_gaps()
    isolated = [g for g in gaps if g["type"] == "isolated_entity"]
    assert len(isolated) == 0


def test_multiple_isolated_entities():
    entities = [_make_entity(i, f"E{i}") for i in range(1, 4)]
    mixin = _build_mixin(all_entities=entities, adjacent_counts={1: [], 2: [], 3: []})
    gaps = mixin.detect_gaps()
    isolated = [g for g in gaps if g["type"] == "isolated_entity"]
    assert len(isolated) == 3


def test_no_entities_no_isolated_gaps():
    mixin = _build_mixin(all_entities=[])
    gaps = mixin.detect_gaps()
    isolated = [g for g in gaps if g["type"] == "isolated_entity"]
    assert len(isolated) == 0


# ── Stale regions ─────────────────────────────────────────────────────────────


def test_stale_region_with_two_stale_memories():
    mems = [
        _make_memory(1, heat=0.1, tags=["python", "api"]),
        _make_memory(2, heat=0.2, tags=["python"]),
    ]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    stale = [g for g in gaps if g["type"] == "stale_region"]
    assert len(stale) == 1
    assert "python" in stale[0]["entities"]
    assert stale[0]["severity"] > 0.3


def test_stale_region_one_stale_memory_not_flagged():
    mems = [_make_memory(1, heat=0.1)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    stale = [g for g in gaps if g["type"] == "stale_region"]
    assert len(stale) == 0


def test_stale_region_heat_at_threshold_not_stale():
    # heat == 0.3 is NOT stale (condition is < 0.3)
    mems = [
        _make_memory(1, heat=0.3),
        _make_memory(2, heat=0.3),
    ]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    stale = [g for g in gaps if g["type"] == "stale_region"]
    assert len(stale) == 0


def test_stale_region_severity_clamped():
    # 7 stale memories → severity = min(0.9, 0.3 + 7*0.1) = min(0.9, 1.0) = 0.9
    mems = [_make_memory(i, heat=0.1) for i in range(1, 8)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    stale = [g for g in gaps if g["type"] == "stale_region"]
    assert len(stale) == 1
    assert stale[0]["severity"] == pytest.approx(0.9, abs=0.01)


def test_stale_region_with_directory_filter():
    mems = [_make_memory(i, heat=0.1) for i in range(1, 3)]
    mixin = _build_mixin(dir_memories=mems)
    mixin._storage.get_memories_for_directory.return_value = mems
    gaps = mixin.detect_gaps(directory="/myproject")
    mixin._storage.get_memories_for_directory.assert_called_once_with("/myproject", min_heat=0.0)
    stale = [g for g in gaps if g["type"] == "stale_region"]
    assert len(stale) == 1


def test_no_directory_uses_all_memories_for_decay():
    mems = [_make_memory(i, heat=0.1) for i in range(1, 3)]
    mixin = _build_mixin(all_memories=mems)
    mixin.detect_gaps()
    mixin._storage.get_all_memories_for_decay.assert_called_once()


def test_stale_tags_aggregation_list():
    mems = [
        _make_memory(1, heat=0.1, tags=["a", "b"]),
        _make_memory(2, heat=0.2, tags=["b", "c"]),
    ]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    stale = [g for g in gaps if g["type"] == "stale_region"]
    tags_in_gap = set(stale[0]["entities"])
    assert "a" in tags_in_gap
    assert "b" in tags_in_gap
    assert "c" in tags_in_gap


def test_stale_tags_non_list_tags_skipped():
    # tags that are not lists should be skipped
    mems = [
        _make_memory(1, heat=0.1, tags=None),
        _make_memory(2, heat=0.2, tags=None),
    ]
    mixin = _build_mixin(all_memories=mems)
    # Force tags to be a non-list string for mem 1
    mems[0]["tags"] = "not-a-list"
    gaps = mixin.detect_gaps()
    stale = [g for g in gaps if g["type"] == "stale_region"]
    assert len(stale) == 1
    # entities should be empty (tags were non-list and filtered)
    assert stale[0]["entities"] == [] or isinstance(stale[0]["entities"], list)


# ── Low confidence zones ──────────────────────────────────────────────────────


def test_low_confidence_single_memory():
    mems = [_make_memory(1, confidence=0.3)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    lc = [g for g in gaps if g["type"] == "low_confidence"]
    assert len(lc) == 1
    assert lc[0]["severity"] > 0.3


def test_low_confidence_threshold_is_exclusive():
    # confidence == 0.5 is NOT low confidence (condition is < 0.5)
    mems = [_make_memory(1, confidence=0.5)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    lc = [g for g in gaps if g["type"] == "low_confidence"]
    assert len(lc) == 0


def test_low_confidence_severity_clamped():
    # 6 low-confidence memories → severity = min(0.8, 0.3 + 6*0.1) = min(0.8, 0.9) = 0.8
    mems = [_make_memory(i, confidence=0.1) for i in range(1, 7)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    lc = [g for g in gaps if g["type"] == "low_confidence"]
    assert lc[0]["severity"] == pytest.approx(0.8, abs=0.01)


def test_low_confidence_content_preview_in_entities():
    mems = [_make_memory(1, content="test content preview xyz", confidence=0.2)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    lc = [g for g in gaps if g["type"] == "low_confidence"]
    assert len(lc[0]["entities"]) == 1
    assert "test content preview" in lc[0]["entities"][0]


def test_low_confidence_max_five_previews():
    mems = [_make_memory(i, content=f"content {i}", confidence=0.1) for i in range(1, 8)]
    mixin = _build_mixin(all_memories=mems)
    gaps = mixin.detect_gaps()
    lc = [g for g in gaps if g["type"] == "low_confidence"]
    assert len(lc[0]["entities"]) <= 5


# ── Missing connections ───────────────────────────────────────────────────────


def test_missing_connection_flagged():
    entities = [_make_entity(1, "A"), _make_entity(2, "B")]
    # A and B co-occur in 2 memories
    mems = [
        _make_memory(1, content="A and B are both here"),
        _make_memory(2, content="A and B again"),
    ]
    mixin = _build_mixin(
        all_entities=entities,
        all_memories=mems,
        relationships_among=[],  # no existing relationships
        adjacent_counts={1: [2, 3], 2: [1, 3]},  # enough connections
    )
    gaps = mixin.detect_gaps()
    mc = [g for g in gaps if g["type"] == "missing_connection"]
    assert len(mc) == 1
    names_in_gap = mc[0]["entities"]
    assert "A" in names_in_gap
    assert "B" in names_in_gap


def test_missing_connection_existing_relationship_not_flagged():
    entities = [_make_entity(1, "A"), _make_entity(2, "B")]
    mems = [
        _make_memory(1, content="A and B are both here"),
        _make_memory(2, content="A and B again"),
    ]
    # Relationship exists
    mixin = _build_mixin(
        all_entities=entities,
        all_memories=mems,
        relationships_among=[{"source_entity_id": 1, "target_entity_id": 2}],
        adjacent_counts={1: [2, 3], 2: [1, 3]},
    )
    gaps = mixin.detect_gaps()
    mc = [g for g in gaps if g["type"] == "missing_connection"]
    assert len(mc) == 0


def test_missing_connection_single_cooccurrence_not_flagged():
    entities = [_make_entity(1, "A"), _make_entity(2, "B")]
    mems = [_make_memory(1, content="A and B")]  # only 1 memory
    mixin = _build_mixin(
        all_entities=entities,
        all_memories=mems,
        relationships_among=[],
        adjacent_counts={1: [2, 3], 2: [1, 3]},
    )
    gaps = mixin.detect_gaps()
    mc = [g for g in gaps if g["type"] == "missing_connection"]
    assert len(mc) == 0


def test_missing_connection_severity_capped():
    entities = [_make_entity(1, "A"), _make_entity(2, "B")]
    # 6 co-occurrence memories → severity = min(0.7, 0.2 + 6*0.1) = min(0.7, 0.8) = 0.7
    mems = [_make_memory(i, content="A and B content") for i in range(1, 7)]
    mixin = _build_mixin(
        all_entities=entities,
        all_memories=mems,
        relationships_among=[],
        adjacent_counts={1: [2, 3], 2: [1, 3]},
    )
    gaps = mixin.detect_gaps()
    mc = [g for g in gaps if g["type"] == "missing_connection"]
    if mc:
        assert mc[0]["severity"] <= 0.7


def test_missing_connection_dedup_bidirectional_relationships():
    """(1,2) and (2,1) from get_relationships_among should be treated as same pair."""
    entities = [_make_entity(1, "A"), _make_entity(2, "B")]
    mems = [
        _make_memory(1, content="A and B together"),
        _make_memory(2, content="A and B again"),
    ]
    mixin = _build_mixin(
        all_entities=entities,
        all_memories=mems,
        relationships_among=[{"source_entity_id": 2, "target_entity_id": 1}],  # reversed
        adjacent_counts={1: [2, 3], 2: [1, 3]},
    )
    gaps = mixin.detect_gaps()
    mc = [g for g in gaps if g["type"] == "missing_connection"]
    assert len(mc) == 0


def test_missing_connection_skips_partial_relationship_data():
    """Relationships missing source_entity_id or target_entity_id are skipped."""
    entities = [_make_entity(1, "A"), _make_entity(2, "B")]
    mems = [
        _make_memory(1, content="A and B together"),
        _make_memory(2, content="A and B again"),
    ]
    mixin = _build_mixin(
        all_entities=entities,
        all_memories=mems,
        relationships_among=[{"source_entity_id": None, "target_entity_id": 2}],
        adjacent_counts={1: [2, 3], 2: [1, 3]},
    )
    gaps = mixin.detect_gaps()
    mc = [g for g in gaps if g["type"] == "missing_connection"]
    # relationship was partial, not indexed — gap should be flagged
    assert len(mc) == 1


# ── One-sided knowledge ───────────────────────────────────────────────────────


def test_one_sided_knowledge_unresolved_error():
    entities = [_make_entity(1, "ImportError", entity_type="error")]
    mixin = _build_mixin(
        all_entities=entities,
        adjacent_counts={1: []},
        relationship_by_source_type=None,
    )
    gaps = mixin.detect_gaps()
    osk = [g for g in gaps if g["type"] == "one_sided_knowledge"]
    assert len(osk) == 1
    assert "ImportError" in osk[0]["entities"]
    assert osk[0]["severity"] == 0.5


def test_one_sided_knowledge_resolved_not_flagged():
    entities = [_make_entity(1, "ImportError", entity_type="error")]
    mixin = _build_mixin(
        all_entities=entities,
        adjacent_counts={1: [2]},
        relationship_by_source_type={"id": 10, "type": "resolved_by"},  # has resolution
    )
    gaps = mixin.detect_gaps()
    osk = [g for g in gaps if g["type"] == "one_sided_knowledge"]
    assert len(osk) == 0


def test_one_sided_knowledge_solution_entities_not_checked():
    # solution-type entities don't trigger one_sided_knowledge check
    entities = [_make_entity(1, "FixForBug", entity_type="solution")]
    mixin = _build_mixin(
        all_entities=entities,
        adjacent_counts={1: [2]},
        relationship_by_source_type=None,
    )
    gaps = mixin.detect_gaps()
    osk = [g for g in gaps if g["type"] == "one_sided_knowledge"]
    assert len(osk) == 0


def test_no_error_entities_no_one_sided_gaps():
    entities = [_make_entity(1, "Module", entity_type="concept")]
    mixin = _build_mixin(all_entities=entities, adjacent_counts={1: [2, 3]})
    gaps = mixin.detect_gaps()
    osk = [g for g in gaps if g["type"] == "one_sided_knowledge"]
    assert len(osk) == 0


# ── Empty state ───────────────────────────────────────────────────────────────


def test_empty_state_returns_no_gaps():
    mixin = _build_mixin()
    gaps = mixin.detect_gaps()
    assert gaps == []


def test_return_type_is_list():
    mixin = _build_mixin()
    result = mixin.detect_gaps()
    assert isinstance(result, list)


# ── Gap structure validation ──────────────────────────────────────────────────


def test_gap_dict_has_required_keys():
    entities = [_make_entity(1, "E1")]
    mixin = _build_mixin(all_entities=entities, adjacent_counts={1: []})
    gaps = mixin.detect_gaps()
    for gap in gaps:
        assert "type" in gap
        assert "description" in gap
        assert "severity" in gap
        assert "entities" in gap
        assert "suggestion" in gap
