"""Car 1 — #41 CE within-request dedup via the `ce` namespace (backend 5.18.0).

The recall pipeline runs up to three CE passes per request:
  1. crossfuse  (`fusion._score_candidates_ce`)   — scores mem+wiki BEFORE fusion
  2. cross_encoder (`_CrossEncoderMixin.cross_encoder_rerank`) — AFTER fusion
  3. multi_passage (`score_documents`)             — synthetic cluster texts

Car 0 folded `_ce_cache` into the unified backend `Cache` as the `ce` namespace,
but recall's CE calls hit `self._ml.score_cross_encoder` DIRECTLY, bypassing it.
Car 1 routes every mode=ce entry point through a single get-or-compute helper
(`_CrossEncoderMixin.score_ce_cached`) that consults an injected `ce` cache:
overlapping (query, text) pairs computed by crossfuse are REUSED by
cross_encoder / multi_passage — the model runs only on NEW texts.

Quality gate (HARD): a CE score for (query, text) is deterministic, so reusing a
cached score is byte-identical → recall output is unchanged. Proven here by
running the real rerank pipeline with a real `ce` Cache vs a `NullCache` and
asserting identical scores/order, plus a model-call-count drop.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from yadgar.backend.cache import Cache, ModelCkpt, NullCache
from yadgar.retrieval._reranking_cross_encoder import _CrossEncoderMixin
from yadgar.retrieval._reranking_multi_passage import _MultiPassageMixin

# ── stubs ────────────────────────────────────────────────────────────────────


class _CountingML:
    """CE stub: deterministic score per text, counts model calls + total texts scored."""

    def __init__(self, breaker_open=False):
        self.breaker_open = breaker_open
        self.ce_calls = 0  # number of score_cross_encoder invocations
        self.texts_scored = 0  # total texts actually sent to the "model"

    @staticmethod
    def _score(text: str) -> float:
        return (len(text) % 11) / 11.0 + 0.05

    def score_cross_encoder(self, query: str, texts: list[str]):
        self.ce_calls += 1
        if self.breaker_open:
            return None
        self.texts_scored += len(texts)
        return [self._score(t) for t in texts]

    def score_pair(self, query: str, text: str):
        return self._score(text)

    def unload_if_idle(self, idle_seconds=None):
        pass


class _Host(_CrossEncoderMixin, _MultiPassageMixin):
    """Minimal MRO host mirroring Reranker; carries an injected ce cache."""

    def __init__(self, ml, ce_cache, *, enabled=True):
        self._settings = MagicMock()
        self._settings.CROSS_ENCODER_ENABLED = True
        self._settings.CROSS_ENCODER_TOP_K = 50
        self._settings.CROSS_ENCODER_WEIGHT = 0.6
        self._settings.MULTI_PASSAGE_RERANKING_ENABLED = enabled
        self._settings.MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD = 0.3
        self._settings.MULTI_PASSAGE_MAX_CLUSTER_SIZE = 3
        self._ml = ml
        self._ce_cache = ce_cache


def _fresh_ce_cache(name="ce_test", budget=1 << 20):
    """A real byte-bounded ce Cache (ModelCkpt, float values, no deep_copy)."""
    return Cache(
        name=name,
        max_bytes=budget,
        invalidation=ModelCkpt(),
        checkpoint_hash="test-ckpt-abc",
        obs_tier="hot",
    )


# ── get-or-compute correctness ────────────────────────────────────────────────


class TestGetOrCompute:
    def test_all_miss_equals_direct_call(self):
        """Cold cache → score_ce_cached returns exactly ml.score_cross_encoder."""
        ml = _CountingML()
        host = _Host(ml, _fresh_ce_cache())
        texts = ["alpha", "beta gamma", "delta"]
        got = host.score_ce_cached("q", texts)
        direct = _CountingML().score_cross_encoder("q", texts)
        assert got == direct
        assert ml.ce_calls == 1
        assert ml.texts_scored == 3

    def test_second_pass_reuses_hits(self):
        """Overlapping texts on a 2nd pass hit the cache; model scores only new texts."""
        ml = _CountingML()
        cache = _fresh_ce_cache()
        host = _Host(ml, cache)
        first = host.score_ce_cached("q", ["a", "bb", "ccc"])
        assert ml.texts_scored == 3
        # Second pass: 2 overlap ("a", "ccc"), 1 new ("dddd").
        second = host.score_ce_cached("q", ["a", "ccc", "dddd"])
        # Only "dddd" hits the model on the 2nd pass.
        assert ml.texts_scored == 4  # 3 + 1
        # Reused scores are byte-identical to the first pass.
        assert second[0] == first[0]  # "a"
        assert second[1] == first[2]  # "ccc"

    def test_order_preserved_on_partial_hit(self):
        ml = _CountingML()
        cache = _fresh_ce_cache()
        host = _Host(ml, cache)
        host.score_ce_cached("q", ["x", "yy"])  # prime x, yy
        out = host.score_ce_cached("q", ["yy", "zzz", "x"])
        assert out == [ml._score("yy"), ml._score("zzz"), ml._score("x")]

    def test_query_scopes_key(self):
        """Same text, different query → distinct key, no cross-query bleed."""
        ml = _CountingML()
        cache = _fresh_ce_cache()
        host = _Host(ml, cache)
        host.score_ce_cached("q1", ["same"])
        host.score_ce_cached("q2", ["same"])
        assert ml.texts_scored == 2  # both computed; q2 did not reuse q1

    def test_none_passthrough_not_cached(self):
        """Circuit-open (None) is passed through and NOT cached — fallback stays intact."""
        ml = _CountingML(breaker_open=True)
        cache = _fresh_ce_cache()
        host = _Host(ml, cache)
        out = host.score_ce_cached("q", ["a", "b"])
        assert out is None
        assert cache.size_entries == 0  # nothing cached
        # Recover: breaker closes → recompute, still nothing stale cached.
        ml.breaker_open = False
        out2 = host.score_ce_cached("q", ["a", "b"])
        assert out2 == [ml._score("a"), ml._score("b")]

    def test_empty_texts(self):
        ml = _CountingML()
        host = _Host(ml, _fresh_ce_cache())
        assert host.score_ce_cached("q", []) == []
        assert ml.ce_calls == 0

    def test_per_element_none_degrades_to_zero(self):
        """A None ELEMENT (not whole-list None) degrades to 0.0 — parity with the
        pre-Car-1 score_documents per-element guard; never float(None)→TypeError."""

        class _NoneElemML:
            def score_cross_encoder(self, query, texts):
                # Second element is None (e.g. a per-pair degrade in the batch).
                return [0.7, None, 0.3]

        host = _Host(_NoneElemML(), _fresh_ce_cache())
        out = host.score_ce_cached("q", ["a", "b", "c"])
        assert out == [0.7, 0.0, 0.3]


# ── DI: Cache vs NullCache ────────────────────────────────────────────────────


class TestDependencyInjection:
    def test_nullcache_never_dedups(self):
        """NullCache injected → today's behavior: every text recomputed every pass."""
        ml = _CountingML()
        host = _Host(ml, NullCache())
        host.score_ce_cached("q", ["a", "bb"])
        host.score_ce_cached("q", ["a", "bb"])  # identical repeat
        assert ml.texts_scored == 4  # no reuse

    def test_real_cache_dedups(self):
        ml = _CountingML()
        host = _Host(ml, _fresh_ce_cache())
        host.score_ce_cached("q", ["a", "bb"])
        host.score_ce_cached("q", ["a", "bb"])  # identical repeat → full hit
        assert ml.texts_scored == 2  # second pass fully reused

    def test_reranker_default_injects_shared_ce(self):
        """Reranker() with no ce_cache → shared registry `ce` instance (feature-on)."""
        from yadgar.backend.cache import get_ce_cache
        from yadgar.retrieval.reranking import Reranker

        r = Reranker(MagicMock(), MagicMock(), ml_client=_CountingML())
        assert r._ce_cache is get_ce_cache()

    def test_reranker_accepts_injected_cache(self):
        from yadgar.retrieval.reranking import Reranker

        null = NullCache()
        r = Reranker(MagicMock(), MagicMock(), ml_client=_CountingML(), ce_cache=null)
        assert r._ce_cache is null


# ── accessor ──────────────────────────────────────────────────────────────────


class TestGetCeCacheAccessor:
    def test_returns_registered_ce_instance(self):
        """get_ce_cache returns the process-global `ce` namespace registered by embed_service."""
        import yadgar.backend.embed_service as es

        importlib.reload(es)  # re-registers `ce` in _REGISTRY
        from yadgar.backend.cache import get_ce_cache

        ce = get_ce_cache()
        assert ce.name == "ce"

    def test_accessor_is_stable(self):
        from yadgar.backend.cache import get_ce_cache

        assert get_ce_cache() is get_ce_cache()


# ── dedup within the real rerank pipeline (call-count drop) ───────────────────


def _mems():
    return [
        {"content": "melanie loves camping trips in the mountains", "_retrieval_score": 0.9},
        {"content": "melanie enjoys camping trips with her family", "_retrieval_score": 0.5},
        {"content": "camping trips in the mountains are relaxing", "_retrieval_score": 0.4},
        {"content": "quantum chromodynamics gauge symmetry breaking", "_retrieval_score": 0.8},
        {"content": "quantum chromodynamics gauge fields confine quarks", "_retrieval_score": 0.3},
    ]


class TestWithinRequestDedup:
    def test_crossfuse_then_cross_encoder_reuses(self):
        """crossfuse scores texts; cross_encoder over the SAME texts reuses them."""
        ml = _CountingML()
        cache = _fresh_ce_cache()
        host = _Host(ml, cache)
        query = "camping trips"
        texts = [m["content"] for m in _mems()]

        # Pass 1: crossfuse-style scoring of all candidate contents.
        host.score_ce_cached(query, texts)
        after_crossfuse = ml.texts_scored
        assert after_crossfuse == len(texts)

        # Pass 2: cross_encoder over the same (unexpanded) contents → full hit.
        host.cross_encoder_rerank([dict(m) for m in _mems()], query)
        # cross_encoder_rerank builds base pairs (content) for each memory; those
        # overlap crossfuse's texts → no NEW model work for the base pairs.
        assert ml.texts_scored == after_crossfuse  # zero new base-pair scoring

    def test_nullcache_rescore_baseline(self):
        """With NullCache, cross_encoder re-scores the overlapping texts (no dedup)."""
        ml = _CountingML()
        host = _Host(ml, NullCache())
        query = "camping trips"
        texts = [m["content"] for m in _mems()]
        host.score_ce_cached(query, texts)
        after_crossfuse = ml.texts_scored
        host.cross_encoder_rerank([dict(m) for m in _mems()], query)
        assert ml.texts_scored > after_crossfuse  # re-scored, no reuse


# ── QUALITY-NEUTRALITY (the hard gate) ────────────────────────────────────────


class TestQualityNeutral:
    def test_cross_encoder_rerank_identical_cache_vs_null(self):
        """Real cross_encoder_rerank output byte-identical: real ce cache vs NullCache."""
        query = "quantum chromodynamics"

        ml_cache = _CountingML()
        host_cache = _Host(ml_cache, _fresh_ce_cache())
        # Prime the cache with a crossfuse-style pass over the same contents.
        host_cache.score_ce_cached(query, [m["content"] for m in _mems()])
        out_cache = host_cache.cross_encoder_rerank([dict(m) for m in _mems()], query)

        ml_null = _CountingML()
        host_null = _Host(ml_null, NullCache())
        host_null.score_ce_cached(query, [m["content"] for m in _mems()])
        out_null = host_null.cross_encoder_rerank([dict(m) for m in _mems()], query)

        # Same ids/order/scores — the hard gate.
        assert [m["content"] for m in out_cache] == [m["content"] for m in out_null]
        assert [m["_retrieval_score"] for m in out_cache] == [
            m["_retrieval_score"] for m in out_null
        ]

    def test_multi_passage_identical_cache_vs_null(self):
        query = "camping trips"

        ml_cache = _CountingML()
        host_cache = _Host(ml_cache, _fresh_ce_cache())
        out_cache = host_cache.multi_passage_rerank(query, [dict(m) for m in _mems()], top_k=5)

        ml_null = _CountingML()
        host_null = _Host(ml_null, NullCache())
        out_null = host_null.multi_passage_rerank(query, [dict(m) for m in _mems()], top_k=5)

        assert [m["content"] for m in out_cache] == [m["content"] for m in out_null]
        assert [m.get("_retrieval_score") for m in out_cache] == [
            m.get("_retrieval_score") for m in out_null
        ]


# ── version canary ────────────────────────────────────────────────────────────


def test_backend_version_bumped():
    """Car 1 bumped the backend image track to at least 5.18.0.

    Asserts a floor, not exact equality, so later stacked cars (Car 2 → 5.19.0, …)
    don't break this — the exact current value is pinned by the canonical test
    (test_v5_46_12_backend_version_canonical)."""
    import yadgar

    parts = tuple(int(x) for x in yadgar.BACKEND_VERSION.split("."))
    assert parts >= (5, 18, 0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
