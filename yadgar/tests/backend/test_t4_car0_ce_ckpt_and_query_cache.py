"""T4 Car 0 — CE-cache `_ckpt` split-brain fix + query-embed cache observability.

Fix (a) — `_ckpt` correctness (plan Car 0(d), t4-ettin-train-2026-07-12.md):
    `_get_ce_checkpoint_hash()` hashed `YADGAR_CE_MODEL` → fallback the EMBEDDING
    model — the reranker model id (`GTE_RERANKER_MODEL`, what
    `ml_client._load_gte_reranker` actually loads) fed `_ckpt` NOWHERE. The `ce`
    score cache is disk-persistent, so swapping the reranker served stale scores
    across restarts. Fix: hash `GTE_RERANKER_MODEL` (same resolution the loader
    uses) + a `CE_SCORING_VERSION` salt (bump when scoring semantics change).

Fix (b) — query-embed cache observability (plan Car 0(a)):
    `EmbeddingEngine._query_cache` hit/miss counters landed only in the shared
    core registry — invisible at backend `:8001/metrics`. Fix: instance counters
    on the engine + a `query_embedding` entry in the backend
    CacheStatsCollector's instance enumeration.

Run: uv run --extra test --extra ml pytest yadgar/tests/backend/test_t4_car0_ce_ckpt_and_query_cache.py
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import pytest

from yadgar._shared.config.config_registry import clear_config_caches

GTE_DEFAULT = "Alibaba-NLP/gte-reranker-modernbert-base"
ETTIN_32M = "cross-encoder/ettin-reranker-32m-v1"


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point YADGAR_CONFIG_FILE at a temp file; clear caches before/after."""
    cfg = tmp_path / "yadgar-t4-car0-test.yaml"
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
    clear_config_caches()
    yield
    clear_config_caches()


def _expected_hash(model: str, scoring_version: str) -> str:
    return hashlib.sha256(f"{model}:{scoring_version}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fix (a) — _ckpt tracks the RERANKER model, not the embedding model
# ---------------------------------------------------------------------------


class TestCeCkptTracksReranker:
    def test_hash_derives_from_reranker_model(self, monkeypatch):
        """`_get_ce_checkpoint_hash` must hash GTE_RERANKER_MODEL (the model the
        loader at ml_client._load_gte_reranker actually loads)."""
        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", ETTIN_32M)
        from yadgar.backend.embed_service import embed_service

        expected = _expected_hash(ETTIN_32M, embed_service.CE_SCORING_VERSION)
        assert embed_service._get_ce_checkpoint_hash() == expected

    def test_reranker_swap_changes_hash(self, monkeypatch):
        """Changing the reranker setting changes `_ckpt` — the swap-busts-cache
        guarantee the disk snapshot discard path keys on."""
        from yadgar.backend.embed_service import embed_service

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", GTE_DEFAULT)
        hash_gte = embed_service._get_ce_checkpoint_hash()
        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", ETTIN_32M)
        hash_ettin = embed_service._get_ce_checkpoint_hash()
        assert hash_gte != hash_ettin

    def test_embedding_model_change_does_not_bust_ce_hash(self, monkeypatch):
        """The old wrong coupling: embedding-model changes must NOT change the
        CE checkpoint hash anymore."""
        from yadgar.backend.embed_service import embed_service

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", GTE_DEFAULT)
        monkeypatch.setenv("YADGAR_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        hash_a = embed_service._get_ce_checkpoint_hash()
        monkeypatch.setenv("YADGAR_EMBEDDING_MODEL", "some-other-embedder")
        hash_b = embed_service._get_ce_checkpoint_hash()
        assert hash_a == hash_b

    def test_ce_model_env_no_longer_consulted(self, monkeypatch):
        """The phantom `YADGAR_CE_MODEL` env (never a Settings field, unset in
        prod) must no longer feed the CE hash."""
        from yadgar.backend.embed_service import embed_service

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", GTE_DEFAULT)
        hash_without = embed_service._get_ce_checkpoint_hash()
        monkeypatch.setenv("YADGAR_CE_MODEL", "phantom-model")
        hash_with = embed_service._get_ce_checkpoint_hash()
        assert hash_without == hash_with

    def test_scoring_version_salt_busts_hash(self, monkeypatch):
        """Bumping CE_SCORING_VERSION changes the hash with the model id
        unchanged — scoring-semantics changes invalidate the snapshot."""
        from yadgar.backend.embed_service import embed_service

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", GTE_DEFAULT)
        hash_v = embed_service._get_ce_checkpoint_hash()
        monkeypatch.setattr(embed_service, "CE_SCORING_VERSION", "test-bump")
        hash_bumped = embed_service._get_ce_checkpoint_hash()
        assert hash_v != hash_bumped
        assert hash_bumped == _expected_hash(GTE_DEFAULT, "test-bump")

    def test_make_ce_cache_wires_reranker_hash(self, monkeypatch):
        """The process-global `ce` cache built by `_make_ce_cache` must carry the
        reranker-derived checkpoint hash."""
        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", ETTIN_32M)
        from yadgar.backend.embed_service import embed_service

        cache = embed_service._make_ce_cache()
        expected = _expected_hash(ETTIN_32M, embed_service.CE_SCORING_VERSION)
        assert cache._ckpt == expected

    def test_reranker_swap_discards_disk_snapshot(self, monkeypatch, tmp_path):
        """End-to-end discard proof: a snapshot written under the GTE hash is
        DISCARDED on load by a cache constructed under the Ettin hash — old
        GTE-keyed scores cannot survive a reranker swap."""
        from yadgar.backend.cache import Cache, ModelCkpt
        from yadgar.backend.embed_service import embed_service

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", GTE_DEFAULT)
        old = Cache(
            name="ce_t4_test",
            max_bytes=1 << 20,
            invalidation=ModelCkpt(),
            checkpoint_hash=embed_service._get_ce_checkpoint_hash(),
        )
        old.put(f"qsha:tsha:{old._ckpt}", 0.42)
        old.save_snapshot(str(tmp_path), "ce_t4_test")

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", ETTIN_32M)
        new = Cache(
            name="ce_t4_test",
            max_bytes=1 << 20,
            invalidation=ModelCkpt(),
            checkpoint_hash=embed_service._get_ce_checkpoint_hash(),
        )
        new.load_snapshot(str(tmp_path), "ce_t4_test")
        assert new.size_entries == 0  # ckpt mismatch → snapshot discarded

        # Control: same hash → snapshot loads.
        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", GTE_DEFAULT)
        same = Cache(
            name="ce_t4_test",
            max_bytes=1 << 20,
            invalidation=ModelCkpt(),
            checkpoint_hash=embed_service._get_ce_checkpoint_hash(),
        )
        same.load_snapshot(str(tmp_path), "ce_t4_test")
        assert same.size_entries == 1


# ---------------------------------------------------------------------------
# Fix (b) — query-embed cache counters visible at backend /metrics
# ---------------------------------------------------------------------------


class TestQueryEmbedCacheObservability:
    def _engine_with_cached_entry(self):
        from yadgar._shared.embeddings import EmbeddingEngine

        eng = EmbeddingEngine.__new__(EmbeddingEngine)
        eng.model_name = "unit-test-model"
        eng._model = None
        eng._unavailable = False
        eng._query_cache = OrderedDict()
        eng.hits = 0
        eng.misses = 0
        eng.evictions = 0
        eng._query_cache["hello"] = b"cached-vector"
        return eng

    def test_engine_counts_hits(self):
        eng = self._engine_with_cached_entry()
        out = eng.encode("hello")
        assert out == b"cached-vector"
        assert eng.hits == 1
        assert eng.misses == 0

    def test_engine_size_entries(self):
        eng = self._engine_with_cached_entry()
        assert eng.size_entries == 1

    def test_backend_instances_include_query_embedding(self, monkeypatch):
        """`_default_backend_cache_instances` must surface the retriever's
        engine under the `query_embedding` name when the shared engine is up."""
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.embed_service.embed_service_metrics import (
            _default_backend_cache_instances,
        )

        eng = self._engine_with_cached_entry()
        monkeypatch.setattr(_st, "_embeddings", eng)
        instances = _default_backend_cache_instances()
        assert "query_embedding" in instances
        assert instances["query_embedding"] is eng

    def test_collector_emits_query_embedding_series(self, monkeypatch):
        """The CacheStatsCollector output must include the query_embedding
        series with the engine's counters."""
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.embed_service.embed_service_metrics import (
            CacheStatsCollector,
            _default_backend_cache_instances,
        )

        eng = self._engine_with_cached_entry()
        eng.encode("hello")  # 1 hit
        monkeypatch.setattr(_st, "_embeddings", eng)

        collector = CacheStatsCollector(instances_fn=_default_backend_cache_instances)
        by_name = {mf.name: mf for mf in collector.collect()}
        hit_samples = {s.labels["cache"]: s.value for s in by_name["yadgar_cache_hit"].samples}
        assert hit_samples.get("query_embedding") == 1
        size_samples = {
            s.labels["cache"]: s.value for s in by_name["yadgar_cache_size_entries"].samples
        }
        assert size_samples.get("query_embedding") == 1

    def test_absent_engine_does_not_break_instances(self, monkeypatch):
        """No shared engine (core-side / early startup) → no query_embedding
        entry, no crash."""
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.embed_service.embed_service_metrics import (
            _default_backend_cache_instances,
        )

        monkeypatch.setattr(_st, "_embeddings", None)
        instances = _default_backend_cache_instances()
        assert "query_embedding" not in instances
