"""Characterization tests for _collect_fts_scores after FTSParams refactor.

Pins the exact FTS score semantics (BM25 normalization, entity-FTS, COMET
expansion) via a stub retriever that doesn't require SurrealDB.

v5.55 wave — verifies FTSParams interface produces identical output to the
old positional 9-param signature.
"""

from __future__ import annotations

from collections import defaultdict
from unittest.mock import MagicMock

import pytest

from yadgar.backend.retrieval.scoring import FTSParams, _normalize_fts_hits, _ScoringMixin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scores():
    return defaultdict(
        lambda: {"vector": 0.0, "fts": 0.0, "ppr": 0.0, "spread": 0.0, "temporal": 0.0}
    )


def _make_retriever(fts_hits_by_query: dict | None = None, comet_terms: list | None = None):
    """Build a minimal _ScoringMixin instance backed by mocked storage."""

    class _StubRetriever(_ScoringMixin):
        def __init__(self):
            self._storage = MagicMock()
            self._settings = MagicMock()
            self._settings.QUERY_EXPANSION_ENABLED = False

        def _comet_expand_query(self, query: str) -> list:
            return comet_terms or []

    retriever = _StubRetriever()

    # Car C7 (0047 §5 C7): ``search_memories_fts_scored`` gained
    # ``scope_sql``/``scope_params`` (the stage-1 project predicate). The fake
    # MUST accept them: ``_run_fts_bm25`` wraps its call in a bare
    # ``except Exception: pass``, so a TypeError from a stale fake signature is
    # SWALLOWED and every score silently stays 0.0 — the tests then fail on the
    # assertion rather than on the real cause.
    def _fts_scored(query_str, min_heat=0.0, limit=50, *, scope_sql="", scope_params=None):
        if fts_hits_by_query is None:
            return []
        # match on substring for simplicity
        for key, hits in fts_hits_by_query.items():
            if key in query_str:
                return hits
        return []

    retriever._storage.search_memories_fts_scored.side_effect = _fts_scored
    return retriever


# ---------------------------------------------------------------------------
# _normalize_fts_hits unit tests
# ---------------------------------------------------------------------------


class TestNormalizeFtsHits:
    def test_single_hit_gets_0_5(self):
        scores = _make_scores()
        _normalize_fts_hits([(1, 3.0)], scores, strength=1.0)
        assert scores[1]["fts"] == pytest.approx(0.5)

    def test_two_hits_normalized(self):
        scores = _make_scores()
        # raw: 10 and 20 → range 10 → normalized: 0.0 and 1.0
        _normalize_fts_hits([(1, 10.0), (2, 20.0)], scores, strength=1.0)
        assert scores[1]["fts"] == pytest.approx(0.0)
        assert scores[2]["fts"] == pytest.approx(1.0)

    def test_strength_applied(self):
        scores = _make_scores()
        _normalize_fts_hits([(1, 10.0), (2, 20.0)], scores, strength=0.8)
        assert scores[2]["fts"] == pytest.approx(0.8)

    def test_max_wins_over_existing(self):
        scores = _make_scores()
        scores[1]["fts"] = 0.9
        _normalize_fts_hits([(1, 10.0), (2, 20.0)], scores, strength=1.0)
        # normalized for mid=1 is 0.0, but existing 0.9 wins
        assert scores[1]["fts"] == pytest.approx(0.9)

    def test_existing_lower_gets_replaced(self):
        scores = _make_scores()
        scores[2]["fts"] = 0.5
        _normalize_fts_hits([(1, 10.0), (2, 20.0)], scores, strength=1.0)
        assert scores[2]["fts"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _collect_fts_scores — FTSParams interface
# ---------------------------------------------------------------------------


class TestCollectFtsScores:
    def test_skipped_when_fts_not_in_enabled_signals(self):
        retriever = _make_retriever({"python": [(1, 5.0), (2, 10.0)]})
        scores = _make_scores()
        params = FTSParams(
            query="python tutorial",
            enabled_signals={"vector", "ppr"},  # no "fts"
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=20,
            min_heat=0.0,
        )
        retriever._collect_fts_scores(scores, params)
        assert scores[1]["fts"] == 0.0
        assert scores[2]["fts"] == 0.0

    def test_runs_when_enabled_signals_is_none(self):
        retriever = _make_retriever({"boosted": [(1, 5.0), (2, 10.0)]})
        scores = _make_scores()
        params = FTSParams(
            query="boosted query",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=20,
            min_heat=0.0,
        )
        retriever._collect_fts_scores(scores, params)
        # at least one score updated
        assert scores[1]["fts"] > 0.0 or scores[2]["fts"] > 0.0

    def test_main_query_scored(self):
        # FTS hits keyed by "boosted" (injected by _build_boosted_fts_query)
        retriever = _make_retriever({"boosted": [(10, 2.0), (20, 4.0)]})
        scores = _make_scores()
        params = FTSParams(
            query="boosted query",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever._collect_fts_scores(scores, params)
        # mid=20 should have higher fts score
        assert scores[20]["fts"] > scores[10]["fts"]

    def test_subquery_strength_0_8(self):
        """Subquery hits are weighted 0.8 vs main query 1.0."""
        hits = [(1, 5.0), (2, 10.0)]  # normalized: 0.0 and 1.0
        retriever = _make_retriever({"boosted": hits})
        scores_main = _make_scores()
        scores_sub = _make_scores()

        # main query only
        params_main = FTSParams(
            query="boosted query",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever._collect_fts_scores(scores_main, params_main)

        # subquery only (use a query that won't match "boosted", force subquery path)
        retriever2 = _make_retriever({"boosted": hits})
        params_sub = FTSParams(
            query="nomatch",
            enabled_signals=None,
            open_domain_subqueries=["boosted subquery"],
            open_domain_mode=False,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever2._collect_fts_scores(scores_sub, params_sub)

        # subquery max should be 0.8 (strength=0.8), main should be 1.0 (strength=1.0)
        assert scores_main[2]["fts"] == pytest.approx(1.0)
        assert scores_sub[2]["fts"] == pytest.approx(0.8)

    def test_entity_fts_uppercase_name(self):
        """Uppercase names trigger entity-FTS with strength 0.5 in non-open-domain.

        Uses direct _run_entity_fts to isolate entity-FTS strength without
        interference from the main query (which also fires for an uppercase query).
        """
        hits = [(5, 1.0), (6, 3.0)]
        retriever = _make_retriever({"Alice": hits})
        scores = _make_scores()
        params = FTSParams(
            query="Alice project status",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever._run_entity_fts(params, scores)
        # mid=6 normalized to 1.0 * 0.5 = 0.5
        assert scores[6]["fts"] == pytest.approx(0.5)

    def test_entity_fts_open_domain_strength_0_7(self):
        """In open_domain_mode, entity strength is 0.7 not 0.5."""
        hits = [(5, 1.0), (6, 3.0)]
        retriever = _make_retriever({"Alice": hits})
        scores = _make_scores()
        params = FTSParams(
            query="Alice project status",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=True,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever._run_entity_fts(params, scores)
        # mid=6 normalized to 1.0 * 0.7 = 0.7
        assert scores[6]["fts"] == pytest.approx(0.7)

    def test_comet_expansion_skipped_non_open_domain(self):
        """COMET expansion only runs in open_domain_mode."""
        retriever = _make_retriever({}, comet_terms=["expand", "terms"])
        scores = _make_scores()
        params = FTSParams(
            query="test query",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever._collect_fts_scores(scores, params)
        # storage never called for comet (since open_domain_mode=False)
        # total calls = 1 (main query only, no entity names in "test query")
        call_count = retriever._storage.search_memories_fts_scored.call_count
        assert call_count == 1  # only main query

    def test_comet_expansion_open_domain(self):
        """COMET expansion fires in open_domain_mode, uses first 6 terms."""
        hits = [(99, 1.0), (100, 5.0)]
        retriever = _make_retriever({"expand": hits}, comet_terms=["expand", "more", "terms"])
        scores = _make_scores()
        params = FTSParams(
            query="test query",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=True,
            candidate_k=50,
            min_heat=0.0,
        )
        retriever._collect_fts_scores(scores, params)
        # COMET hits weighted 0.6 → mid=100 normalized to 1.0 * 0.6 = 0.6
        assert scores[100]["fts"] == pytest.approx(0.6)

    def test_storage_exception_swallowed(self):
        """Exceptions from storage do not propagate."""
        retriever = _make_retriever()
        retriever._storage.search_memories_fts_scored.side_effect = RuntimeError("db error")
        scores = _make_scores()
        params = FTSParams(
            query="any query",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=50,
            min_heat=0.0,
        )
        # Should not raise
        retriever._collect_fts_scores(scores, params)
        assert scores[1]["fts"] == 0.0
