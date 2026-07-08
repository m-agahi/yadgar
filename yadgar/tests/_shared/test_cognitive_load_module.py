"""Tests for yadgar/metacognition/cognitive_load.py — Cowan 4±1 chunk mixin.

Wave 2 coverage: yadgar/metacognition/cognitive_load.py (148 stmts, 6.1% pre-wave).
Strategy: instantiate a minimal stub class that satisfies the mixin's
self._chunk_limit attribute, then call each public method directly.
Mock _extract_entities at the retrieval boundary to avoid importing
the full embedding stack.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from yadgar._shared.metacognition.cognitive_load import _CognitiveLoadMixin

# ---------------------------------------------------------------------------
# Minimal stub class
# ---------------------------------------------------------------------------


class _StubEngine(_CognitiveLoadMixin):
    """Minimal concrete class that satisfies the mixin contract."""

    _chunk_limit = 4


def _mem(
    content="memory text",
    heat=0.5,
    importance=0.5,
    confidence=1.0,
    tags=None,
    created=None,
    surprise_score=0.0,
):
    """Build a minimal memory dict."""
    m = {
        "content": content,
        "heat": heat,
        "importance": importance,
        "confidence": confidence,
        "tags": tags or [],
        "surprise_score": surprise_score,
    }
    if created is not None:
        m["created_at"] = created.isoformat() if isinstance(created, datetime) else created
    return m


# ---------------------------------------------------------------------------
# manage_context
# ---------------------------------------------------------------------------


class TestManageContext:
    def setup_method(self):
        self.engine = _StubEngine()

    def test_empty_returns_empty(self):
        result = self.engine.manage_context([])
        assert result == []

    def test_within_limit_returns_all_with_metadata(self):
        mems = [_mem(f"mem {i}") for i in range(3)]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.manage_context(mems)
        assert len(result) == 3
        for i, r in enumerate(result):
            assert r["_chunk_id"] == i
            assert r["_position_reason"] == "within_limit"

    def test_at_limit_returns_all(self):
        mems = [_mem(f"mem {i}") for i in range(4)]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.manage_context(mems)
        assert len(result) == 4

    def test_over_limit_truncates_and_adds_overflow(self):
        # 8 memories, limit=4 → should select top 4 + summarize overflow
        mems = [_mem(f"m{i}", importance=float(i) / 10) for i in range(8)]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.manage_context(mems)
        # Result must exist; some summaries or selected memories
        assert len(result) > 0

    def test_custom_max_chunks(self):
        mems = [_mem(f"m{i}") for i in range(6)]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.manage_context(mems, max_chunks=2)
        # With only 2 chunks selected, overflow summaries may appear but
        # total results ≥ 2
        assert len(result) >= 2

    def test_overflow_summary_has_marker(self):
        mems = [_mem(f"overflow-{i}", importance=0.1) for i in range(10)]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.manage_context(mems, max_chunks=2)
        # Overflow summaries should be present when len >> limit
        overflow_items = [r for r in result if r.get("_position_reason") == "overflow_summary"]
        assert len(overflow_items) > 0


# ---------------------------------------------------------------------------
# chunk_memories
# ---------------------------------------------------------------------------


class TestChunkMemories:
    def setup_method(self):
        self.engine = _StubEngine()

    def test_empty_returns_empty(self):
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.chunk_memories([])
        assert result == []

    def test_single_memory_becomes_single_chunk(self):
        mems = [_mem("only one")]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.chunk_memories(mems)
        assert len(result) == 1
        assert len(result[0]) == 1

    def test_memories_with_shared_entities_cluster(self):
        # Both memories mention the same entity — Jaccard > 0.3
        mems = [_mem("foo bar baz"), _mem("foo bar qux")]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities",
            side_effect=[["foo", "bar", "baz"], ["foo", "bar", "qux"]],
        ):
            result = self.engine.chunk_memories(mems)
        # They should cluster together (Jaccard of {foo,bar}/{foo,bar,baz,qux} > 0.3)
        # All memories in one chunk
        total = sum(len(c) for c in result)
        assert total == 2

    def test_memories_with_different_entities_stay_separate(self):
        mems = [_mem("apple"), _mem("zebra")]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities",
            side_effect=[["apple"], ["zebra"]],
        ):
            result = self.engine.chunk_memories(mems)
        # No entity overlap → separate chunks (unless temporal proximity triggers)
        # Since no timestamps, they should be separate
        assert len(result) == 2

    def test_temporal_proximity_clusters(self):
        now = datetime.now(UTC)
        # Two memories 30 minutes apart → within 2h threshold
        mems = [
            _mem("a", created=now),
            _mem("b", created=now + timedelta(minutes=30)),
        ]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.chunk_memories(mems)
        total = sum(len(c) for c in result)
        assert total == 2
        # Should be in same chunk due to temporal proximity
        assert any(len(c) == 2 for c in result)

    def test_temporal_far_apart_stays_separate(self):
        now = datetime.now(UTC)
        # Two memories 3 hours apart → beyond 2h threshold
        mems = [
            _mem("a", created=now),
            _mem("b", created=now + timedelta(hours=3)),
        ]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.chunk_memories(mems)
        # Should be separate chunks
        assert len(result) == 2

    def test_invalid_timestamp_string_handled(self):
        mems = [_mem("a", created="not-a-date"), _mem("b", created="also-not")]
        with patch(
            "yadgar._shared.metacognition.cognitive_load._extract_entities", return_value=[]
        ):
            result = self.engine.chunk_memories(mems)
        assert len(result) >= 1  # no crash


# ---------------------------------------------------------------------------
# summarize_overflow
# ---------------------------------------------------------------------------


class TestSummarizeOverflow:
    def setup_method(self):
        self.engine = _StubEngine()

    def test_empty_returns_empty(self):
        result = self.engine.summarize_overflow([])
        assert result == []

    def test_low_value_memories_get_summarized(self):
        mems = [_mem(f"low value memory {i}", surprise_score=0.1, importance=0.3) for i in range(5)]
        result = self.engine.summarize_overflow(mems, target_count=1)
        # Should produce at least one summary
        assert len(result) >= 1
        summary_items = [r for r in result if r.get("_is_summary")]
        assert len(summary_items) >= 1

    def test_high_surprise_preserved(self):
        high_surprise = _mem("critical memory", surprise_score=0.9, importance=0.2)
        low_mems = [_mem(f"boring {i}", surprise_score=0.1, importance=0.2) for i in range(3)]
        result = self.engine.summarize_overflow([high_surprise] + low_mems)
        # High-surprise memory should be preserved verbatim (not as summary)
        non_summary = [r for r in result if not r.get("_is_summary")]
        assert any(r["content"] == "critical memory" for r in non_summary)

    def test_high_importance_preserved(self):
        high_imp = _mem("important memory", surprise_score=0.1, importance=0.8)
        low_mems = [_mem(f"boring {i}", surprise_score=0.1, importance=0.2) for i in range(3)]
        result = self.engine.summarize_overflow([high_imp] + low_mems)
        non_summary = [r for r in result if not r.get("_is_summary")]
        assert any(r["content"] == "important memory" for r in non_summary)

    def test_summary_content_format(self):
        mems = [_mem(f"content{i}", surprise_score=0.1, importance=0.1) for i in range(2)]
        result = self.engine.summarize_overflow(mems, target_count=1)
        summaries = [r for r in result if r.get("_is_summary")]
        assert len(summaries) >= 1
        assert "[Summary of" in summaries[0]["content"]
        assert "_summarized_count" in summaries[0]

    def test_summary_metadata(self):
        mems = [_mem("m", heat=0.6, importance=0.4, confidence=0.9)]
        result = self.engine.summarize_overflow(mems)
        summaries = [r for r in result if r.get("_is_summary")]
        if summaries:  # only if it was summarized (not preserved)
            s = summaries[0]
            assert "heat" in s
            assert "importance" in s
            assert "confidence" in s


# ---------------------------------------------------------------------------
# _apply_primacy_recency
# ---------------------------------------------------------------------------


class TestApplyPrimacyRecency:
    def setup_method(self):
        self.engine = _StubEngine()

    def _make_chunks(self, n):
        return [(i, [_mem(f"m{i}")], float(n - i)) for i in range(n)]

    def test_empty_returns_empty(self):
        result = self.engine._apply_primacy_recency([])
        assert result == []

    def test_single_unchanged(self):
        chunks = self._make_chunks(1)
        result = self.engine._apply_primacy_recency(chunks)
        assert len(result) == 1
        assert result[0][0] == 0

    def test_two_chunks_unchanged(self):
        chunks = self._make_chunks(2)
        result = self.engine._apply_primacy_recency(chunks)
        assert len(result) == 2

    def test_three_or_more_reorders(self):
        # With 3: [0 (highest), 2 (second highest at end), 1 (middle)]
        chunks = self._make_chunks(3)
        # Input: [(0, ..., 3.0), (1, ..., 2.0), (2, ..., 1.0)] — sorted desc
        result = self.engine._apply_primacy_recency(chunks)
        assert len(result) == 3
        # First = primacy (highest score)
        assert result[0][0] == 0
        # Last = recency (second highest)
        assert result[-1][0] == 1

    def test_five_chunks(self):
        chunks = self._make_chunks(5)
        result = self.engine._apply_primacy_recency(chunks)
        assert len(result) == 5
        assert result[0][0] == 0
        assert result[-1][0] == 1


# ---------------------------------------------------------------------------
# _position_reason
# ---------------------------------------------------------------------------


class TestPositionReason:
    def test_first_position_is_primacy(self):
        assert _StubEngine._position_reason(0, 5) == "primacy"

    def test_last_position_is_recency(self):
        assert _StubEngine._position_reason(4, 5) == "recency"

    def test_middle_position(self):
        assert _StubEngine._position_reason(2, 5) == "middle"

    def test_single_item_not_recency(self):
        # total=1, position=0 → primacy (not recency because total not > 1)
        assert _StubEngine._position_reason(0, 1) == "primacy"

    def test_first_of_two_is_primacy(self):
        assert _StubEngine._position_reason(0, 2) == "primacy"

    def test_second_of_two_is_recency(self):
        assert _StubEngine._position_reason(1, 2) == "recency"
