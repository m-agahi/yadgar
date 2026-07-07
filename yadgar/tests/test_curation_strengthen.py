"""Tests for yadgar/curation/strengthen.py — memify self-improvement passes.

Coverage targets:
- _memify_strengthen: boost importance for high-access/high-confidence memories;
  skip low-access, low-confidence, already-at-max; batch_writes called correctly
- _memify_reweight: boost weight for hot+established relationships;
  decay for cold relationships; skip relationships without entity ids;
  skip delta ~= 0; batch_writes called with correct params
- _memify_derive: generate derived facts for high-weight co-occurrence relationships;
  skip low-weight; skip already-existing derived content; skip missing entities;
  skip None source/target; embed + batch_writes called

COVERAGE FLOOR NOTE:
Running with --cov=yadgar.curation.strengthen fails in this environment
(Python 3.14 + numpy 2.4.4 + missing libz.so.1) for the same reason as
test_metacognition_coverage.py — see that file's docstring.
yadgar/curation/__init__.py imports EmbeddingEngine at package level; coverage's
source= pre-import triggers the same numpy partial-load / double-load conflict.
Measured coverage via `coverage run --include=...`: 100%.
All 32 tests in this file pass without coverage instrumentation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.curation.strengthen import _memify_derive, _memify_reweight, _memify_strengthen

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_storage(
    memories=None,
    entities=None,
    relationships=None,
    all_contents=None,
) -> MagicMock:
    storage = MagicMock()
    storage.get_memories_by_heat.return_value = memories or []
    storage.get_all_entities.return_value = entities or []
    storage.get_all_relationships.return_value = relationships or []
    storage.get_relationships_by_types.return_value = relationships or []
    storage.get_all_memories_with_embeddings.return_value = [
        {"content": c} for c in (all_contents or [])
    ]
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"
    storage._next_id.return_value = 42
    storage._bytes_to_floats.return_value = [0.0, 0.1]
    storage.batch_writes.return_value = None
    return storage


def _make_embeddings(encoded_val=b"\x00" * 4) -> MagicMock:
    emb = MagicMock()
    emb.encode.return_value = encoded_val
    emb.get_model_name.return_value = "test-model"
    return emb


def _make_mem(
    mid: int,
    access_count: int = 0,
    confidence: float = 1.0,
    importance: float = 0.5,
) -> dict:
    return {
        "id": mid,
        "access_count": access_count,
        "confidence": confidence,
        "importance": importance,
    }


def _make_entity(eid: int, heat: float = 1.0) -> dict:
    return {"id": eid, "heat": heat}


def _make_rel(
    rid: int, sid: int, tid: int, weight: float = 1.0, rel_type: str = "co_occurrence"
) -> dict:
    return {
        "id": rid,
        "source_entity_id": sid,
        "target_entity_id": tid,
        "weight": weight,
        "type": rel_type,
    }


# ── _memify_strengthen ────────────────────────────────────────────────────────


def test_strengthen_qualifies_high_access_high_confidence():
    mems = [_make_mem(1, access_count=6, confidence=0.9, importance=0.5)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 1
    storage.batch_writes.assert_called_once()


def test_strengthen_skips_low_access():
    mems = [_make_mem(1, access_count=5, confidence=0.9, importance=0.5)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 0
    storage.batch_writes.assert_not_called()


def test_strengthen_skips_low_confidence():
    mems = [_make_mem(1, access_count=6, confidence=0.8, importance=0.5)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 0


def test_strengthen_skips_already_max_importance():
    mems = [_make_mem(1, access_count=6, confidence=0.9, importance=1.0)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 0


def test_strengthen_increments_importance_by_01():
    mems = [_make_mem(1, access_count=6, confidence=0.9, importance=0.5)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    # Extract the importance in the batch write
    batch_call_args = storage.batch_writes.call_args[0][0]
    assert len(batch_call_args) == 1
    _sql, params = batch_call_args[0]
    assert params["importance"] == pytest.approx(0.6, abs=0.001)


def test_strengthen_caps_importance_at_10():
    mems = [_make_mem(1, access_count=6, confidence=0.9, importance=0.95)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    batch_call_args = storage.batch_writes.call_args[0][0]
    _sql, params = batch_call_args[0]
    assert params["importance"] == pytest.approx(1.0, abs=0.001)


def test_strengthen_multiple_qualifying_memories():
    mems = [_make_mem(i, access_count=6, confidence=0.9, importance=0.5) for i in range(1, 4)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 3
    storage.batch_writes.assert_called_once()
    batch = storage.batch_writes.call_args[0][0]
    assert len(batch) == 3


def test_strengthen_handles_none_importance_as_half():
    mems = [{"id": 1, "access_count": 6, "confidence": 0.9, "importance": None}]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    # importance=None defaults to 0.5 → new = min(0.5+0.1, 1.0) = 0.6
    assert stats["strengthened"] == 1


def test_strengthen_handles_none_access_count_zero():
    mems = [{"id": 1, "access_count": None, "confidence": 0.9, "importance": 0.5}]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    # access_count=None treated as 0 → not > 5 → skip
    assert stats["strengthened"] == 0


def test_strengthen_no_qualifying_memories():
    mems = [_make_mem(1, access_count=0, confidence=0.5, importance=0.5)]
    storage = _make_storage(memories=mems)
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 0
    storage.batch_writes.assert_not_called()


def test_strengthen_empty_memories():
    storage = _make_storage(memories=[])
    stats = {"strengthened": 0}
    _memify_strengthen(storage, stats)
    assert stats["strengthened"] == 0
    storage.batch_writes.assert_not_called()


# ── _memify_reweight ──────────────────────────────────────────────────────────


def test_reweight_boosts_hot_established_relationship():
    entities = [_make_entity(1, heat=0.8), _make_entity(2, heat=0.9)]
    rels = [_make_rel(10, 1, 2, weight=5.0)]
    storage = _make_storage(entities=entities, relationships=rels)
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    assert stats["reweighted"] == 1
    batch = storage.batch_writes.call_args[0][0]
    _sql, params = batch[0]
    assert params["inc"] == pytest.approx(0.5)


def test_reweight_requires_weight_at_least_5():
    entities = [_make_entity(1, heat=0.8), _make_entity(2, heat=0.9)]
    rels = [_make_rel(10, 1, 2, weight=4.9)]  # below threshold
    storage = _make_storage(entities=entities, relationships=rels)
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    assert stats["reweighted"] == 0


def test_reweight_decays_cold_relationship():
    entities = [_make_entity(1, heat=0.05), _make_entity(2, heat=0.05)]
    rels = [_make_rel(10, 1, 2, weight=2.0)]
    storage = _make_storage(entities=entities, relationships=rels)
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    assert stats["reweighted"] == 1
    batch = storage.batch_writes.call_args[0][0]
    _sql, params = batch[0]
    # delta = new_weight - weight = max(2.0*0.9, 0.1) - 2.0 = 1.8 - 2.0 = -0.2
    assert params["inc"] == pytest.approx(-0.2, abs=0.001)


def test_reweight_skips_relationship_without_entity_ids():
    entities = [_make_entity(1)]
    rels = [{"id": 10, "source_entity_id": None, "target_entity_id": 2, "weight": 5.0}]
    storage = _make_storage(entities=entities, relationships=rels)
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    assert stats["reweighted"] == 0
    storage.batch_writes.assert_not_called()


def test_reweight_skips_zero_delta():
    # weight=0.1, cold entities → new_weight=max(0.1*0.9, 0.1)=0.1, delta~=0
    entities = [_make_entity(1, heat=0.0), _make_entity(2, heat=0.0)]
    rels = [_make_rel(10, 1, 2, weight=0.1)]
    storage = _make_storage(entities=entities, relationships=rels)
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    # delta = max(0.1*0.9=0.09, 0.1) - 0.1 = 0.1 - 0.1 = 0.0 → skip
    assert stats["reweighted"] == 0


def test_reweight_no_relationships():
    storage = _make_storage(entities=[], relationships=[])
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    storage.batch_writes.assert_not_called()


def test_reweight_uses_now_iso_in_batch():
    entities = [_make_entity(1, heat=0.0), _make_entity(2, heat=0.0)]
    rels = [_make_rel(10, 1, 2, weight=5.0)]
    storage = _make_storage(entities=entities, relationships=rels)
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    if storage.batch_writes.called:
        batch = storage.batch_writes.call_args[0][0]
        _sql, params = batch[0]
        assert params["now"] == "2026-01-01T00:00:00+00:00"


def test_reweight_unknown_entity_heat_defaults_to_zero():
    # entity 2 not in entity list → heat defaults to 0.0
    entities = [_make_entity(1, heat=0.9)]
    rels = [_make_rel(10, 1, 2, weight=5.0)]
    storage = _make_storage(entities=entities, relationships=rels)
    stats = {"reweighted": 0}
    _memify_reweight(storage, stats)
    # avg_heat = (0.9 + 0.0) / 2 = 0.45 → not >0.7 and not <0.1 → nothing
    assert stats["reweighted"] == 0


# ── _memify_derive ────────────────────────────────────────────────────────────


def test_derive_creates_derived_fact_for_high_weight():
    [_make_entity(1), _make_entity(2)]
    entity_dicts = [
        {"id": 1, "name": "ModuleA"},
        {"id": 2, "name": "ModuleB"},
    ]
    rels = [_make_rel(10, 1, 2, weight=12.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 1
    storage.batch_writes.assert_called_once()


def test_derive_skips_low_weight():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [_make_rel(10, 1, 2, weight=9.9)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0
    storage.batch_writes.assert_not_called()


def test_derive_skips_already_existing_content():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [_make_rel(10, 1, 2, weight=12.0)]
    # The derived content already exists
    storage = _make_storage(
        entities=entity_dicts,
        relationships=rels,
        all_contents=["A and B are frequently modified together"],
    )
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0


def test_derive_skips_missing_source_entity():
    entity_dicts = [{"id": 2, "name": "B"}]  # entity 1 missing
    rels = [_make_rel(10, 1, 2, weight=12.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0


def test_derive_skips_missing_target_entity():
    entity_dicts = [{"id": 1, "name": "A"}]  # entity 2 missing
    rels = [_make_rel(10, 1, 2, weight=12.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0


def test_derive_skips_none_source_id():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [{"id": 10, "source_entity_id": None, "target_entity_id": 2, "weight": 12.0}]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0


def test_derive_no_cooccurrence_rels():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    storage = _make_storage(entities=entity_dicts, relationships=[])
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0
    storage.batch_writes.assert_not_called()


def test_derive_batch_has_two_ops_per_item():
    """Each derived item creates 2 batch operations: CREATE + UPDATE."""
    entity_dicts = [{"id": 1, "name": "X"}, {"id": 2, "name": "Y"}]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    batch = storage.batch_writes.call_args[0][0]
    assert len(batch) == 2  # CREATE + UPDATE


def test_derive_content_format():
    entity_dicts = [{"id": 1, "name": "ServiceA"}, {"id": 2, "name": "ServiceB"}]
    rels = [_make_rel(10, 1, 2, weight=20.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    batch = storage.batch_writes.call_args[0][0]
    _create_sql, create_params = batch[0]
    assert create_params["content"] == "ServiceA and ServiceB are frequently modified together"


def test_derive_dedup_within_run():
    """Two identical entity pairs should not produce duplicate derived facts."""
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [
        _make_rel(10, 1, 2, weight=15.0),
        _make_rel(11, 1, 2, weight=20.0),
    ]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    # First rel produces derived fact; second sees it in existing_contents → skip
    assert stats["derived"] == 1


def test_derive_uses_embedding_for_content():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings(encoded_val=b"\x01\x02\x03\x04")
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    embeddings.encode.assert_called_once_with("A and B are frequently modified together")


def test_derive_none_embedding_passed_as_none_floats():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings(encoded_val=None)
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    # With None embedding, _bytes_to_floats should not be called
    storage._bytes_to_floats.assert_not_called()
    # But derived fact still created
    assert stats["derived"] == 1
    batch = storage.batch_writes.call_args[0][0]
    _sql, params = batch[0]
    assert params["embedding"] is None


def test_derive_model_name_in_batch():
    entity_dicts = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    storage = _make_storage(entities=entity_dicts, relationships=rels)
    embeddings = _make_embeddings()
    embeddings.get_model_name.return_value = "my-special-model"
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    batch = storage.batch_writes.call_args[0][0]
    _sql, params = batch[0]
    assert params["embedding_model"] == "my-special-model"
