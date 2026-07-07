"""Tests for yadgar/retrieval/_reranking_heuristic.py — _HeuristicMixin.

Covers heuristic_rerank(): entity scoring, term coverage, bigram overlap,
exact-match bonus, edge/guard paths (empty memories, empty query, no terms,
missing content key, missing _retrieval_score, top_k defaulting).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.retrieval._reranking_heuristic import _HeuristicMixin

# ---------------------------------------------------------------------------
# Stub class
# ---------------------------------------------------------------------------


class _StubReranker(_HeuristicMixin):
    """Minimal host providing _settings for top_k defaulting."""

    def __init__(self, default_top_k: int = 5):
        self._settings = MagicMock()
        self._settings.RERANKER_TOP_K = default_top_k


@pytest.fixture()
def reranker():
    return _StubReranker(default_top_k=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mem(content: str, retrieval_score: float = 0.5) -> dict:
    return {"content": content, "_retrieval_score": retrieval_score}


# ---------------------------------------------------------------------------
# Early-exit / guard paths
# ---------------------------------------------------------------------------


class TestGuardPaths:
    def test_empty_memories_returns_empty(self, reranker):
        result = reranker.heuristic_rerank([], "some query", top_k=5)
        assert result == []

    def test_empty_query_returns_memories_sliced(self, reranker):
        mems = [_mem("alpha"), _mem("beta"), _mem("gamma")]
        result = reranker.heuristic_rerank(mems, "", top_k=2)
        assert result == mems[:2]

    def test_empty_query_returns_all_when_top_k_larger(self, reranker):
        mems = [_mem("alpha"), _mem("beta")]
        result = reranker.heuristic_rerank(mems, "", top_k=10)
        assert result == mems

    def test_none_top_k_uses_settings(self, reranker):
        """When top_k is None, falls back to _settings.RERANKER_TOP_K."""
        mems = [_mem(f"memory {i}") for i in range(10)]
        result = reranker.heuristic_rerank(mems, "memory", top_k=None)
        assert len(result) == 5  # settings default

    def test_pure_punctuation_query_returns_slice(self, reranker):
        """Query that strips to zero terms triggers `if not query_terms` guard."""
        mems = [_mem("hello"), _mem("world"), _mem("foo")]
        # Every token in this query strips to empty string
        result = reranker.heuristic_rerank(mems, "!!! ??? ... ,,, ---", top_k=2)
        assert result == mems[:2]

    def test_empty_memories_with_nonempty_query(self, reranker):
        result = reranker.heuristic_rerank([], "hello world", top_k=5)
        assert result == []


# ---------------------------------------------------------------------------
# Scoring signal tests — each signal exercised in isolation or combined
# ---------------------------------------------------------------------------


class TestEntityScore:
    def test_entity_coverage_promotes_match(self, reranker):
        """Memory mentioning the capitalized entity ranks above one that doesn't.

        Equal base scores — entity boost (0.35 weight) decides the ordering.
        """
        mems = [
            _mem("unrelated content about databases", retrieval_score=0.5),
            _mem("Alice visited Paris last summer", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, "Where did Alice go?", top_k=2)
        # Both have same base; entity "alice" found in second mem → it wins
        assert "Alice" in result[0]["content"]

    def test_multi_entity_partial_overlap(self, reranker):
        """Partial entity overlap still boosts score.

        Both mems start equal; Alice+Bob entities push the overlapping mem up.
        """
        mems = [
            _mem("Bob played tennis with Alice", retrieval_score=0.5),
            _mem("nothing relevant here at all", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, "Alice and Bob together", top_k=2)
        # Entity overlap boosts "Bob played tennis with Alice"
        assert "Alice" in result[0]["content"]

    def test_single_char_capitalized_token_not_entity(self, reranker):
        """Stripped token of length 1 is excluded from entity set (len>1 check)."""
        # "A" is len 1, should not become an entity; test just checks no crash
        mems = [_mem("something about A topic")]
        result = reranker.heuristic_rerank(mems, "A topic", top_k=1)
        assert len(result) == 1


class TestTermScore:
    def test_content_term_overlap_boosts(self, reranker):
        """Memory sharing content terms with query is preferred."""
        mems = [
            _mem("completely unrelated text here", retrieval_score=0.5),
            _mem("machine learning embeddings neural network", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, "learning embeddings for neural", top_k=2)
        assert "machine learning" in result[0]["content"]

    def test_question_words_excluded_from_terms(self, reranker):
        """Common question words (what, how, the, etc.) don't contribute to term overlap.

        Equal base scores: content-term overlap decides the winner.
        """
        mems = [
            _mem("what how the", retrieval_score=0.5),
            _mem("python machine learning algorithm", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(
            mems, "what are the best machine learning algorithms", top_k=2
        )
        # "machine", "learning", "best", "algorithms" are content terms; second memory wins
        assert "python machine" in result[0]["content"]

    def test_short_terms_excluded(self, reranker):
        """Terms of length ≤2 not added to content terms (len(stripped) > 2 check)."""
        # "is", "to", "by" are ≤2 chars and should be filtered
        mems = [_mem("is to by")]
        # Should not raise; is/to/by not content terms
        result = reranker.heuristic_rerank(mems, "is to by go", top_k=1)
        assert len(result) == 1


class TestBigramScore:
    def test_bigram_overlap_boosts(self, reranker):
        """Exact bigram match boosts score."""
        mems = [
            _mem("totally different subject here", retrieval_score=0.5),
            _mem("python machine learning course", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, "machine learning is great", top_k=2)
        assert "machine learning" in result[0]["content"]

    def test_no_bigrams_in_single_word_query(self, reranker):
        """Single-word query produces no bigrams; bigram_score stays 0.0."""
        mems = [_mem("python"), _mem("java")]
        result = reranker.heuristic_rerank(mems, "python", top_k=2)
        assert len(result) == 2  # no crash, both returned


class TestExactMatch:
    def test_exact_substring_match_bonus(self, reranker):
        """Memory containing the full query string as substring gets exact_match=1.0."""
        query = "machine learning embeddings"
        mems = [
            _mem("unrelated info about databases", retrieval_score=0.5),
            _mem("machine learning embeddings are useful", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, query, top_k=2)
        assert "machine learning embeddings" in result[0]["content"]

    def test_no_exact_match_no_penalty(self, reranker):
        """No exact match doesn't break scoring — exact_match just stays 0.0."""
        mems = [_mem("some memory about things")]
        result = reranker.heuristic_rerank(mems, "different query entirely", top_k=1)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Content key / retrieval score edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_memory_missing_content_key(self, reranker):
        """Memories without 'content' key should not raise."""
        mems = [{"_retrieval_score": 0.7}, {"content": "hello world", "_retrieval_score": 0.5}]
        result = reranker.heuristic_rerank(mems, "hello world", top_k=2)
        assert len(result) == 2

    def test_memory_missing_retrieval_score(self, reranker):
        """Memory without _retrieval_score defaults to 0.0 for combination."""
        mems = [{"content": "something relevant here"}]
        result = reranker.heuristic_rerank(mems, "relevant content here", top_k=1)
        assert len(result) == 1
        assert "_rerank_score" in result[0]
        assert "_retrieval_score" in result[0]

    def test_rerank_score_and_retrieval_score_set(self, reranker):
        """After reranking, each memory has _rerank_score and updated _retrieval_score."""
        mem = _mem("Alice went to Paris", retrieval_score=0.8)
        result = reranker.heuristic_rerank([mem], "Alice Paris trip", top_k=1)
        assert len(result) == 1
        assert 0.0 <= result[0]["_rerank_score"] <= 1.0
        assert 0.0 <= result[0]["_retrieval_score"] <= 1.0

    def test_scores_are_rounded_to_4_decimal_places(self, reranker):
        """Scores stored as round(..., 4)."""
        mem = _mem("Alice loves Paris", retrieval_score=0.123456789)
        result = reranker.heuristic_rerank([mem], "Alice Paris", top_k=1)
        # round(..., 4) means at most 4 decimal places
        score_str = str(result[0]["_rerank_score"])
        decimal_part = score_str.split(".")[-1] if "." in score_str else ""
        assert len(decimal_part) <= 4

    def test_top_k_limits_output(self, reranker):
        """Result is at most top_k items."""
        mems = [_mem(f"memory {i}", retrieval_score=float(i) / 10) for i in range(10)]
        result = reranker.heuristic_rerank(mems, "memory content", top_k=3)
        assert len(result) == 3

    def test_sorted_by_retrieval_score_descending(self, reranker):
        """Output memories sorted by _retrieval_score descending."""
        mems = [
            _mem("low relevance text here about nothing", retrieval_score=0.1),
            _mem("python machine learning deep learning algorithms", retrieval_score=0.3),
            _mem("machine learning python frameworks", retrieval_score=0.2),
        ]
        result = reranker.heuristic_rerank(mems, "machine learning python", top_k=3)
        scores = [m["_retrieval_score"] for m in result]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Combined-signal tests (all four signals in one query)
# ---------------------------------------------------------------------------


class TestCombinedSignals:
    def test_all_four_signals_fire(self, reranker):
        """One rich memory hits entity, term, bigram, and exact-match signals.

        Equal base scores: all four signals push the matching memory to top.
        rerank_score = entity(0.35) + term(0.30) + bigram(0.20) + exact(0.15) = 1.0
        combined = 0.85*0.5 + 0.15*1.0 = 0.425+0.15 = 0.575 vs 0.85*0.5 = 0.425
        """
        query = "Alice loves machine learning"
        memory_text = "alice loves machine learning systems"
        mems = [
            _mem(memory_text, retrieval_score=0.5),
            _mem("completely irrelevant text here", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, query, top_k=2)
        # Exact match + entity + term + bigram pushes alice memory to top
        assert "machine learning" in result[0]["content"]

    def test_no_entities_term_and_bigram_still_score(self, reranker):
        """Query without capitalized words still scores via term + bigram signals."""
        mems = [
            _mem("fast api server handlers", retrieval_score=0.5),
            _mem("database schema migration", retrieval_score=0.5),
        ]
        result = reranker.heuristic_rerank(mems, "fast api server configuration", top_k=2)
        assert "fast api server" in result[0]["content"]

    def test_deterministic_with_same_inputs(self, reranker):
        """Same inputs always produce same output (no randomness)."""
        mems = [
            _mem("alpha content", retrieval_score=0.5),
            _mem("beta content", retrieval_score=0.6),
        ]
        r1 = reranker.heuristic_rerank(list(mems), "alpha content query", top_k=2)
        # Reset retrieval scores for second call (heuristic_rerank mutates dicts)
        mems2 = [
            _mem("alpha content", retrieval_score=0.5),
            _mem("beta content", retrieval_score=0.6),
        ]
        r2 = reranker.heuristic_rerank(mems2, "alpha content query", top_k=2)
        assert [m["content"] for m in r1] == [m["content"] for m in r2]
