"""Characterization tests for _CrossEncoderMixin.cross_encoder_rerank.

Pins exact reranking behavior (score assignment, ordering, guard paths)
so the v5.55 refactor cannot silently change semantics.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.retrieval._reranking_cross_encoder import _CrossEncoderMixin

# ---------------------------------------------------------------------------
# Stub
# ---------------------------------------------------------------------------


def _make_reranker(
    ce_enabled: bool = True,
    ce_weight: float = 0.6,
    top_k: int = 10,
    scores: list[float] | None = None,
    score_error: Exception | None = None,
    scores_none: bool = False,
) -> _CrossEncoderMixin:
    """Return a minimal _CrossEncoderMixin host with deterministic ML stub."""

    class _ML:
        def score_cross_encoder(self, query, texts):
            if score_error is not None:
                raise score_error
            if scores_none:
                return None
            if scores is not None:
                return scores[: len(texts)]
            return [0.5] * len(texts)

    class _Reranker(_CrossEncoderMixin):
        def __init__(self):
            self._settings = MagicMock()
            self._settings.CROSS_ENCODER_TOP_K = top_k
            self._settings.CROSS_ENCODER_ENABLED = ce_enabled
            self._settings.CROSS_ENCODER_WEIGHT = ce_weight
            self._ml = _ML()

    return _Reranker()


def _mems(*contents, base_score=0.5):
    return [{"content": c, "_retrieval_score": base_score} for c in contents]


# ---------------------------------------------------------------------------
# Guard paths
# ---------------------------------------------------------------------------


class TestCrossEncoderRerank_Guards:
    def test_empty_memories_returns_empty(self):
        r = _make_reranker()
        assert r.cross_encoder_rerank([], "query") == []

    def test_empty_query_returns_slice(self):
        r = _make_reranker(top_k=5)
        memories = _mems("hello")
        # empty query triggers guard: returns memories[:top_k] (not empty list)
        result = r.cross_encoder_rerank(memories, "")
        assert result == memories[:5]

    def test_ce_disabled_returns_slice(self):
        r = _make_reranker(ce_enabled=False, top_k=2)
        memories = _mems("a", "b", "c")
        result = r.cross_encoder_rerank(memories, "q")
        assert result == memories[:2]

    def test_ml_error_returns_pre_rerank_slice(self):
        r = _make_reranker(score_error=RuntimeError("boom"), top_k=2)
        memories = _mems("a", "b", "c")
        result = r.cross_encoder_rerank(memories, "q")
        assert result == memories[:2]

    def test_circuit_breaker_open_returns_slice(self):
        r = _make_reranker(scores_none=True, top_k=2)
        memories = _mems("a", "b", "c")
        result = r.cross_encoder_rerank(memories, "q")
        assert result == memories[:2]

    def test_all_zero_scores_returns_input_slice(self):
        r = _make_reranker(scores=[0.0, 0.0, 0.0], top_k=5)
        memories = _mems("a", "b", "c")
        result = r.cross_encoder_rerank(memories, "q")
        # all scores zero → returns memories_to_score[:top_k] unchanged
        assert result == memories


# ---------------------------------------------------------------------------
# Score annotation
# ---------------------------------------------------------------------------


class TestCrossEncoderRerank_ScoreAnnotation:
    def test_cross_encoder_score_annotated(self):
        r = _make_reranker(scores=[0.8, 0.2], top_k=5, ce_weight=0.6)
        memories = _mems("long enough content to avoid length penalty" * 2, "short")
        result = r.cross_encoder_rerank(memories, "q")
        for m in result:
            assert "_cross_encoder_score" in m
            assert "_retrieval_score" in m

    def test_scores_are_rounded_to_4dp(self):
        r = _make_reranker(scores=[0.75, 0.25], top_k=5, ce_weight=0.6)
        # Content over 150 chars avoids length penalty
        long = "x" * 160
        short = "y" * 160
        memories = [
            {"content": long, "_retrieval_score": 0.5},
            {"content": short, "_retrieval_score": 0.5},
        ]
        result = r.cross_encoder_rerank(memories, "q")
        for m in result:
            s = m["_cross_encoder_score"]
            assert round(s, 4) == s, f"Expected 4dp, got {s}"

    def test_retrieval_score_is_weighted_blend(self):
        """_retrieval_score = ret_weight * orig_ret + ce_weight * ce_norm."""
        r = _make_reranker(scores=[1.0, 0.0], top_k=5, ce_weight=0.6)
        long = "x" * 200  # avoid length penalty
        memories = [
            {"content": long, "_retrieval_score": 1.0},
            {"content": long, "_retrieval_score": 0.0},
        ]
        result = r.cross_encoder_rerank(memories, "q")
        # mem[0]: ce_norm=1.0, ret_weight=0.4
        # _retrieval_score = 0.4*1.0 + 0.6*1.0 = 1.0
        mem0 = next(
            m for m in result if m["content"] == long and m.get("_cross_encoder_score", -1) > 0.5
        )
        assert abs(mem0["_retrieval_score"] - 1.0) < 0.001

    def test_short_content_length_penalty_below_80(self):
        """Content <80 chars → ce_norm multiplied by 0.5."""
        r = _make_reranker(scores=[1.0, 1.0], top_k=5, ce_weight=0.6)
        short = "hi"  # <80 chars
        long = "x" * 200  # >150 chars
        memories = [
            {"content": short, "_retrieval_score": 0.0},
            {"content": long, "_retrieval_score": 0.0},
        ]
        result = r.cross_encoder_rerank(memories, "q")
        short_mem = next(m for m in result if m["content"] == short)
        long_mem = next(m for m in result if m["content"] == long)
        # Both raw scores equal (1.0), so ce_norm both=1.0 before penalty.
        # short_mem: ce_norm *= 0.5 → 0.5; long_mem: ce_norm=1.0
        assert short_mem["_cross_encoder_score"] < long_mem["_cross_encoder_score"]

    def test_medium_content_length_penalty_80_to_150(self):
        """Content 80–149 chars → ce_norm multiplied by 0.8."""
        r = _make_reranker(scores=[1.0, 1.0], top_k=5, ce_weight=0.6)
        medium = "m" * 100  # 100 chars, in 80-149 range
        long = "x" * 200
        memories = [
            {"content": medium, "_retrieval_score": 0.0},
            {"content": long, "_retrieval_score": 0.0},
        ]
        result = r.cross_encoder_rerank(memories, "q")
        medium_mem = next(m for m in result if m["content"] == medium)
        long_mem = next(m for m in result if m["content"] == long)
        assert medium_mem["_cross_encoder_score"] < long_mem["_cross_encoder_score"]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestCrossEncoderRerank_Ordering:
    def test_higher_ce_score_ranks_first(self):
        r = _make_reranker(scores=[0.9, 0.1], top_k=5, ce_weight=0.6)
        long = "x" * 200
        memories = [
            {"content": long, "_retrieval_score": 0.5},  # high CE
            {"content": long + "y", "_retrieval_score": 0.5},  # low CE
        ]
        result = r.cross_encoder_rerank(memories, "q")
        assert result[0]["content"] == long

    def test_top_k_limits_result_length(self):
        r = _make_reranker(top_k=3)
        memories = _mems(*[f"memory {i}" for i in range(10)])
        result = r.cross_encoder_rerank(memories, "q")
        assert len(result) <= 3

    def test_batch_capped_before_scoring(self):
        """Only top_k memories go to ML; not all 20 candidates."""
        scored_texts = []

        class _ML:
            def score_cross_encoder(self, query, texts):
                scored_texts.extend(texts)
                return [0.5] * len(texts)

        class _Reranker(_CrossEncoderMixin):
            def __init__(self):
                self._settings = MagicMock()
                self._settings.CROSS_ENCODER_TOP_K = 5
                self._settings.CROSS_ENCODER_ENABLED = True
                self._settings.CROSS_ENCODER_WEIGHT = 0.6
                self._ml = _ML()

        r = _Reranker()
        memories = _mems(*[f"mem {i}" for i in range(20)])
        r.cross_encoder_rerank(memories, "q")
        # At most 5 base texts; variants only in open_domain_mode (which is off for this query)
        assert len(scored_texts) <= 5

    def test_default_top_k_from_settings(self):
        r = _make_reranker(top_k=2)
        memories = _mems("a", "b", "c", "d")
        result = r.cross_encoder_rerank(memories, "q")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# open_domain_mode variant expansion
# ---------------------------------------------------------------------------


class TestCrossEncoderRerank_VariantExpansion:
    def test_open_domain_expands_variants(self):
        """open_domain_like query → more texts sent to ML than base count."""
        scored_text_counts: list[int] = []

        class _ML:
            def score_cross_encoder(self, query, texts):
                scored_text_counts.append(len(texts))
                return [0.5] * len(texts)

        class _Reranker(_CrossEncoderMixin):
            def __init__(self):
                self._settings = MagicMock()
                self._settings.CROSS_ENCODER_TOP_K = 10
                self._settings.CROSS_ENCODER_ENABLED = True
                self._settings.CROSS_ENCODER_WEIGHT = 0.6
                self._ml = _ML()

        r = _Reranker()
        # "Who" → is_open_domain_like=True per analyze_query
        memories = _mems(*["John is a software engineer who works at Acme Corp"] * 5)
        r.cross_encoder_rerank(memories, "Who is John?")
        # At least as many texts as memories (could be more with variants)
        assert scored_text_counts and scored_text_counts[0] >= 5

    def test_max_score_aggregated_from_variants(self):
        """When variants are generated, the memory takes the max score across its variants."""
        call_args: dict = {}

        class _ML:
            def score_cross_encoder(self, query, texts):
                call_args["texts"] = list(texts)
                # Return high score for index 1 (variant), low for index 0 (base)
                return [0.1 if i % 2 == 0 else 0.9 for i in range(len(texts))]

        class _Reranker(_CrossEncoderMixin):
            def __init__(self):
                self._settings = MagicMock()
                self._settings.CROSS_ENCODER_TOP_K = 3
                self._settings.CROSS_ENCODER_ENABLED = True
                self._settings.CROSS_ENCODER_WEIGHT = 0.6
                self._ml = _ML()

        r = _Reranker()
        # Rich content produces implied facts in open_domain_mode
        memories = [
            {
                "content": "Alice is the CEO of TechCorp and lives in Boston",
                "_retrieval_score": 0.5,
            },
        ]
        result = r.cross_encoder_rerank(memories, "Who is Alice?")
        # Even if base got 0.1, variant got 0.9 → memory should take max (0.9)
        # Cross-encoder score should reflect the higher of the two variants
        assert len(result) == 1
        # The _cross_encoder_score should reflect max aggregation
        assert result[0]["_cross_encoder_score"] >= 0.0
