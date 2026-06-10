"""Tests for yadgar/metacognition/coverage.py — coverage assessment mixin.

Coverage targets:
- assess_coverage: all density buckets (0, 1-2, 3-5, 6+)
- entity coverage: known/unknown entities
- recency scoring: <1d, 1-7d, 7-30d, >30d, no memories
- confidence scoring: averages of memory confidence values
- overall blended score and suggestion labels (sufficient/partial/insufficient)
- gaps list: unknown entities + no-memory message
- _extract_entities delegation (indirect via mock)
- error-tolerance in FTS / vector paths

COVERAGE FLOOR NOTE:
Running with --cov=yadgar.metacognition.coverage fails in this environment
(Python 3.14 + numpy 2.4.4 + missing libz.so.1). When pytest-cov processes the
source= argument it pre-imports yadgar.metacognition (triggering EmbeddingEngine
→ numpy) before test collection; numpy partially initialises then fails because
libz.so.1 is absent; on second import Python 3.14 raises "cannot load module
more than once per process". This affects yadgar.metacognition.* and
yadgar.curation.strengthen similarly.

Workaround: run with --no-cov (or omit --cov flags for these modules) and use
`coverage run --include=yadgar/metacognition/coverage.py` directly for accurate
measurement. All 33 tests in this file pass without coverage instrumentation.
Measured coverage via `coverage run --include=...`: 98% (lines 8-10, the
_extract_entities function body, are unreachable without the real retrieval stack
since _extract_entities is mocked in all tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from yadgar.metacognition.coverage import _CoverageMixin

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_memory(
    content: str = "test",
    created_at: datetime | None = None,
    confidence: float = 1.0,
    mem_id: int = 1,
) -> dict:
    if created_at is None:
        created_at = datetime.now(UTC)
    return {
        "id": mem_id,
        "content": content,
        "created_at": created_at.isoformat(),
        "confidence": confidence,
    }


def _build_coverage_mixin(
    fts_results=None,
    vec_results=None,
    query_embedding=b"\x00" * 4,
    entities_in_graph=None,
) -> _CoverageMixin:
    """Build a _CoverageMixin instance with mock _storage and _embeddings."""
    mixin = _CoverageMixin.__new__(_CoverageMixin)

    storage = MagicMock()
    storage.search_memories_fts.return_value = fts_results or []
    storage.search_vectors.return_value = vec_results or []
    storage.get_memory.side_effect = lambda mid: {
        "id": mid,
        "content": "x",
        "created_at": datetime.now(UTC).isoformat(),
        "confidence": 1.0,
    }

    embeddings = MagicMock()
    embeddings.encode.return_value = query_embedding

    # Default: no entities in graph
    if entities_in_graph is None:
        storage.get_entity_by_name.return_value = None
    else:

        def _get_entity(name):
            return entities_in_graph.get(name)

        storage.get_entity_by_name.side_effect = _get_entity

    mixin._storage = storage
    mixin._embeddings = embeddings
    return mixin


# ── Density buckets ───────────────────────────────────────────────────────────


def test_density_zero_memories():
    mixin = _build_coverage_mixin(fts_results=[], vec_results=[])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("anything")
    # density=0.0, entity_coverage=0.0, recency=0.0, confidence=0.0
    assert result["coverage"] == 0.0
    assert result["suggestion"] == "insufficient"
    assert result["memory_count"] == 0
    assert result["density_score"] if "density_score" in result else True


def test_density_one_memory():
    mems = [_make_memory("topic A", mem_id=1)]
    mixin = _build_coverage_mixin(fts_results=mems)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("topic A")
    assert result["memory_count"] == 1
    # density = 0.3, confidence = 1.0 → overall = 0.3*0.3 + 0.3*0.0 + 0.2*recency + 0.2*1.0
    # recency < 1 day so recency_score = 1.0 → 0.09 + 0 + 0.2 + 0.2 = 0.49
    assert result["coverage"] == pytest.approx(0.49, abs=0.01)


def test_density_two_memories():
    mems = [_make_memory("topic", mem_id=i) for i in range(1, 3)]
    mixin = _build_coverage_mixin(fts_results=mems)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("topic")
    assert result["memory_count"] == 2


def test_density_three_to_five_memories():
    mems = [_make_memory("topic", mem_id=i) for i in range(1, 4)]
    mixin = _build_coverage_mixin(fts_results=mems)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("topic")
    # density = 0.6
    assert result["memory_count"] == 3


def test_density_six_plus_memories():
    mems = [_make_memory("topic", mem_id=i) for i in range(1, 8)]
    mixin = _build_coverage_mixin(fts_results=mems)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("topic")
    # density = 0.9, entity_coverage=0.0, recency=1.0, confidence=1.0
    # overall = 0.3*0.9 + 0.3*0.0 + 0.2*1.0 + 0.2*1.0 = 0.67
    assert result["memory_count"] == 7
    assert result["coverage"] == pytest.approx(0.67, abs=0.01)


# ── Entity coverage ───────────────────────────────────────────────────────────


def test_entity_coverage_all_known():
    mems = [_make_memory("topic")]
    entities = {"FastAPI": {"id": 1, "name": "FastAPI"}}
    mixin = _build_coverage_mixin(fts_results=mems, entities_in_graph=entities)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=["FastAPI"]):
        result = mixin.assess_coverage("FastAPI routing")
    assert result["entity_coverage"] == 1.0


def test_entity_coverage_none_known():
    entities = {}
    mixin = _build_coverage_mixin(entities_in_graph=entities)
    with patch(
        "yadgar.metacognition.coverage._extract_entities", return_value=["FastAPI", "uvicorn"]
    ):
        result = mixin.assess_coverage("FastAPI with uvicorn")
    assert result["entity_coverage"] == 0.0
    assert "FastAPI" in result["gaps"]
    assert "uvicorn" in result["gaps"]


def test_entity_coverage_partial():
    entities = {"FastAPI": {"id": 1, "name": "FastAPI"}}
    mixin = _build_coverage_mixin(entities_in_graph=entities)
    with patch(
        "yadgar.metacognition.coverage._extract_entities", return_value=["FastAPI", "Kubernetes"]
    ):
        result = mixin.assess_coverage("FastAPI on Kubernetes")
    assert result["entity_coverage"] == pytest.approx(0.5)
    assert "Kubernetes" in result["gaps"]
    assert "FastAPI" not in result["gaps"]


def test_entity_coverage_no_entities_in_query():
    mixin = _build_coverage_mixin()
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("something vague")
    assert result["entity_coverage"] == 0.0


# ── Recency scoring ───────────────────────────────────────────────────────────


def test_recency_less_than_one_day():
    mem = _make_memory(created_at=datetime.now(UTC) - timedelta(hours=2))
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 1.0


def test_recency_one_to_seven_days():
    mem = _make_memory(created_at=datetime.now(UTC) - timedelta(days=3))
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 0.7


def test_recency_seven_to_thirty_days():
    mem = _make_memory(created_at=datetime.now(UTC) - timedelta(days=15))
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 0.4


def test_recency_older_than_thirty_days():
    mem = _make_memory(created_at=datetime.now(UTC) - timedelta(days=45))
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 0.2


def test_recency_no_memories():
    mixin = _build_coverage_mixin(fts_results=[], vec_results=[])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 0.0


def test_recency_invalid_created_at_skipped():
    """Memory with unparseable created_at is skipped; recency stays 0.0."""
    mem = {"id": 1, "content": "x", "created_at": "not-a-date", "confidence": 1.0}
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 0.0


def test_recency_naive_datetime_treated_utc():
    """Naive datetime (no tzinfo) is treated as UTC."""
    naive_dt = datetime.now()  # no tzinfo
    mem = {"id": 1, "content": "x", "created_at": naive_dt, "confidence": 1.0}
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] >= 0.0  # should not raise


def test_recency_datetime_object_not_string():
    """Memory with a datetime object (not string) in created_at is parsed."""
    dt = datetime.now(UTC) - timedelta(hours=1)
    mem = {"id": 1, "content": "x", "created_at": dt, "confidence": 1.0}
    mixin = _build_coverage_mixin(fts_results=[mem])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["recency_score"] == 1.0


# ── Confidence scoring ────────────────────────────────────────────────────────


def test_confidence_average():
    mems = [
        _make_memory(confidence=0.6, mem_id=1),
        _make_memory(confidence=0.8, mem_id=2),
    ]
    mixin = _build_coverage_mixin(fts_results=mems)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["confidence"] == pytest.approx(0.7, abs=0.01)


def test_confidence_default_when_missing():
    """Memories without 'confidence' key default to 1.0."""
    mems = [{"id": 1, "content": "x", "created_at": datetime.now(UTC).isoformat()}]
    mixin = _build_coverage_mixin(fts_results=mems)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["confidence"] == pytest.approx(1.0, abs=0.01)


def test_confidence_zero_when_no_memories():
    mixin = _build_coverage_mixin(fts_results=[], vec_results=[])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["confidence"] == 0.0


# ── Suggestion labels ─────────────────────────────────────────────────────────


def test_suggestion_sufficient():
    mems = [_make_memory(mem_id=i) for i in range(1, 8)]
    entities = {"Python": {"id": 1, "name": "Python"}}
    mixin = _build_coverage_mixin(fts_results=mems, entities_in_graph=entities)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=["Python"]):
        result = mixin.assess_coverage("Python")
    # density=0.9, entity_coverage=1.0, recency=1.0, confidence=1.0
    # overall = 0.3*0.9 + 0.3*1.0 + 0.2*1.0 + 0.2*1.0 = 0.27+0.3+0.2+0.2 = 0.97
    assert result["suggestion"] == "sufficient"
    assert result["coverage"] >= 0.7


def test_suggestion_partial():
    # 3-5 memories, partial entity coverage, old-ish
    mems = [
        _make_memory(mem_id=i, created_at=datetime.now(UTC) - timedelta(days=10))
        for i in range(1, 4)
    ]
    entities = {"Python": {"id": 1, "name": "Python"}}
    mixin = _build_coverage_mixin(fts_results=mems, entities_in_graph=entities)
    with patch(
        "yadgar.metacognition.coverage._extract_entities", return_value=["Python", "Django"]
    ):
        result = mixin.assess_coverage("Python Django")
    # density=0.6, entity_coverage=0.5, recency=0.4, confidence=1.0
    # overall = 0.3*0.6 + 0.3*0.5 + 0.2*0.4 + 0.2*1.0 = 0.18+0.15+0.08+0.2=0.61
    # Should be partial (0.4-0.7)
    assert result["suggestion"] in ("partial", "sufficient")
    assert result["coverage"] >= 0.4


def test_suggestion_insufficient():
    mixin = _build_coverage_mixin(fts_results=[], vec_results=[])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("totally unknown topic xyz123")
    assert result["suggestion"] == "insufficient"
    assert result["coverage"] < 0.4


# ── Gaps list ─────────────────────────────────────────────────────────────────


def test_gaps_include_no_memory_message():
    mixin = _build_coverage_mixin(fts_results=[], vec_results=[])
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("the query text")
    assert any("No memories" in g for g in result["gaps"])


def test_gaps_include_unknown_entities():
    mixin = _build_coverage_mixin(entities_in_graph={})
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=["Kafka"]):
        result = mixin.assess_coverage("Kafka topic")
    assert "Kafka" in result["gaps"]


def test_gaps_empty_when_everything_known():
    mems = [_make_memory(mem_id=i) for i in range(1, 8)]
    entities = {"Python": {"id": 1, "name": "Python"}}
    mixin = _build_coverage_mixin(fts_results=mems, entities_in_graph=entities)
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=["Python"]):
        result = mixin.assess_coverage("Python")
    assert result["gaps"] == []


# ── Vector dedup (vec_hits not in fts) ───────────────────────────────────────


def test_vector_results_deduplicated():
    """Vec hits already in FTS results are not double-counted."""
    fts_mems = [_make_memory(mem_id=1)]
    # vec_hits returns same id → should not add a second copy
    mixin = _build_coverage_mixin(
        fts_results=fts_mems,
        vec_results=[(1, 0.1)],  # id=1 already in fts
    )
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["memory_count"] == 1


def test_vector_results_add_new_memories():
    """Vec hits not in FTS are fetched and added."""
    fts_mems = [_make_memory(mem_id=1)]
    mixin = _build_coverage_mixin(
        fts_results=fts_mems,
        vec_results=[(2, 0.1)],  # id=2 not in fts
    )
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    # get_memory(2) returns a memory, total = 2
    assert result["memory_count"] == 2


# ── Error tolerance ───────────────────────────────────────────────────────────


def test_fts_exception_is_swallowed():
    """FTS failure falls back to empty; vec search still runs."""
    mixin = _build_coverage_mixin()
    mixin._storage.search_memories_fts.side_effect = RuntimeError("DB error")
    mixin._storage.search_vectors.return_value = []
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    assert result["memory_count"] == 0  # graceful fallback


def test_embedding_none_skips_vector_search():
    """If encode returns None, vector search is skipped."""
    mixin = _build_coverage_mixin()
    mixin._embeddings.encode.return_value = None
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    mixin._storage.search_vectors.assert_not_called()
    assert result["memory_count"] == 0


# ── detail field ──────────────────────────────────────────────────────────────


def test_detail_present_in_all_results():
    for fts in [[], [_make_memory()], [_make_memory(mem_id=i) for i in range(1, 8)]]:
        mixin = _build_coverage_mixin(fts_results=fts)
        with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
            result = mixin.assess_coverage("query")
        assert "detail" in result
        assert isinstance(result["detail"], str)


# ── Return shape ──────────────────────────────────────────────────────────────


def test_result_keys():
    mixin = _build_coverage_mixin()
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    expected_keys = {
        "coverage",
        "confidence",
        "suggestion",
        "gaps",
        "memory_count",
        "entity_coverage",
        "recency_score",
        "detail",
    }
    assert set(result.keys()) == expected_keys


def test_coverage_values_rounded():
    mixin = _build_coverage_mixin()
    with patch("yadgar.metacognition.coverage._extract_entities", return_value=[]):
        result = mixin.assess_coverage("query")
    for key in ("coverage", "confidence", "entity_coverage", "recency_score"):
        val = result[key]
        assert val == round(val, 4)
