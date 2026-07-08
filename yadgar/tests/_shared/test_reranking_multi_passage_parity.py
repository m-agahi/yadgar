"""Lever-1 parity tests — batched cached multi-passage cluster scoring.

v5.98: multi_passage_rerank previously scored each cluster's combined text with a
per-cluster `score_single_pair` RPC (backend mode=pair, UNcached). This routes all
cluster combined-texts through a single batched `score_cross_encoder` call
(backend mode=ce, LRU-cached). The two are SCORE-IDENTICAL by construction —
`score_pair(q,t) == score_cross_encoder(q,[t])[0]` (same GTE forward pass) —
so the resulting `_retrieval_score` boosts must be byte-identical.

These tests pin that parity, the cluster→score index mapping, and the
circuit-breaker-open path (score_cross_encoder returns None for the whole list →
each cluster must degrade to 0.0, matching score_single_pair's per-cluster None→0.0).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.retrieval._reranking_cross_encoder import _CrossEncoderMixin
from yadgar._shared.retrieval._reranking_multi_passage import _MultiPassageMixin


class _Host(_CrossEncoderMixin, _MultiPassageMixin):
    """Minimal MRO host mirroring Reranker (_CrossEncoderMixin precedes _MultiPassageMixin)."""

    def __init__(self, ml, *, cluster_threshold=0.3, max_cluster_size=3, enabled=True):
        self._settings = MagicMock()
        self._settings.MULTI_PASSAGE_RERANKING_ENABLED = enabled
        self._settings.MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD = cluster_threshold
        self._settings.MULTI_PASSAGE_MAX_CLUSTER_SIZE = max_cluster_size
        self._ml = ml


class _ParityML:
    """ML stub where score_pair(q,t) == score_cross_encoder(q,[t])[0] (parity invariant).

    Scores are a deterministic function of the text so batched and per-pair paths
    return identical values for identical texts.
    """

    def __init__(self, breaker_open=False):
        self.breaker_open = breaker_open
        self.pair_calls = 0
        self.ce_calls = 0

    @staticmethod
    def _score(text: str) -> float:
        # Deterministic, text-dependent, spans a useful range.
        return (len(text) % 7) / 7.0 + 0.1

    def score_pair(self, query: str, text: str):
        self.pair_calls += 1
        if self.breaker_open:
            return None  # RemoteMLClient returns None when breaker open
        return self._score(text)

    def score_cross_encoder(self, query: str, texts: list[str]):
        self.ce_calls += 1
        if self.breaker_open:
            return None  # whole-list None when breaker open
        return [self._score(t) for t in texts]


def _mems():
    # Two overlapping triplets → two clusters of ≥2, plus a singleton.
    return [
        {"content": "melanie loves camping trips in the mountains", "_retrieval_score": 0.9},
        {"content": "melanie enjoys camping trips with her family", "_retrieval_score": 0.5},
        {"content": "camping trips in the mountains are relaxing here", "_retrieval_score": 0.4},
        {
            "content": "quantum chromodynamics gauge symmetry breaking theory",
            "_retrieval_score": 0.8,
        },
        {"content": "quantum chromodynamics gauge fields confine quarks", "_retrieval_score": 0.3},
    ]


# ── Reference implementation: the ORIGINAL per-cluster mode=pair loop ──────────


def _reference_multi_passage(host, query, memories, top_k):
    """Byte-for-byte the pre-v5.98 loop, using score_single_pair per cluster."""
    if not getattr(host._settings, "MULTI_PASSAGE_RERANKING_ENABLED", False):
        return memories[:top_k]
    clusters = host.cluster_memories(memories[:20])
    for cluster_mems in clusters:
        if len(cluster_mems) < 2:
            continue
        combined = " | ".join(m.get("content", "")[:200] for m in cluster_mems[:3])
        combined_score = host.score_single_pair(query, combined)
        max_individual = max(
            m.get("_cross_encoder_score", m.get("_retrieval_score", 0)) for m in cluster_mems
        )
        if combined_score > max_individual:
            boost = (combined_score - max_individual) * 0.5
            for m in cluster_mems:
                m["_retrieval_score"] = m.get("_retrieval_score", 0) + boost
    memories.sort(key=lambda m: m.get("_retrieval_score", 0), reverse=True)
    return memories[:top_k]


class TestMultiPassageParity:
    def test_scores_byte_identical_to_per_pair_loop(self):
        query = "does melanie like camping"
        ref_ml = _ParityML()
        new_ml = _ParityML()
        ref_host = _Host(ref_ml)
        new_host = _Host(new_ml)

        ref_out = _reference_multi_passage(ref_host, query, _mems(), top_k=5)
        new_out = new_host.multi_passage_rerank(query, _mems(), top_k=5)

        ref_scores = [(m["content"], m["_retrieval_score"]) for m in ref_out]
        new_scores = [(m["content"], m["_retrieval_score"]) for m in new_out]
        assert new_scores == ref_scores

    def test_batches_into_single_ce_call(self):
        """New path must issue ONE batched score_cross_encoder, zero score_pair RPCs."""
        query = "does melanie like camping"
        ml = _ParityML()
        host = _Host(ml)
        host.multi_passage_rerank(query, _mems(), top_k=5)
        assert ml.pair_calls == 0
        assert ml.ce_calls == 1  # single batched call for all qualifying clusters

    def test_breaker_open_degrades_to_zero(self):
        """When score_cross_encoder returns None (breaker open), each cluster → 0.0,
        matching the per-pair path where score_single_pair yields 0.0."""
        query = "does melanie like camping"
        ref_ml = _ParityML(breaker_open=True)
        new_ml = _ParityML(breaker_open=True)
        ref_out = _reference_multi_passage(_Host(ref_ml), query, _mems(), top_k=5)
        new_out = _Host(new_ml).multi_passage_rerank(query, _mems(), top_k=5)
        ref_scores = [(m["content"], m["_retrieval_score"]) for m in ref_out]
        new_scores = [(m["content"], m["_retrieval_score"]) for m in new_out]
        assert new_scores == ref_scores

    def test_disabled_returns_slice_unchanged(self):
        query = "q"
        ml = _ParityML()
        host = _Host(ml, enabled=False)
        mems = _mems()
        out = host.multi_passage_rerank(query, mems, top_k=3)
        assert out == mems[:3]
        assert ml.ce_calls == 0
        assert ml.pair_calls == 0

    def test_no_qualifying_cluster_no_ce_call(self):
        """All-singleton clusters → no combined-text scoring at all."""
        query = "q"
        ml = _ParityML()
        host = _Host(ml)
        singletons = [
            {"content": "aaa completely unrelated one", "_retrieval_score": 0.9},
            {"content": "bbb totally different two words", "_retrieval_score": 0.5},
        ]
        host.multi_passage_rerank(query, singletons, top_k=5)
        assert ml.ce_calls == 0
        assert ml.pair_calls == 0
