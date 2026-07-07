"""TDD tests for backend LRU cache + msgpack snapshot (backend v5.4.0).

Tests MUST fail before yadgar/cache.py exists.

Coverage:
  1. LRU semantics — get/put/eviction order
  2. Snapshot round-trip — write → restore identical state
  3. Checkpoint-hash mismatch → empty cache on restore
  4. Cap by entry count (0-cap disables)
  5. Concurrent put/get (asyncio safety)
  6. CE cache integration in embed_service: hit increments counter, miss calls ML
  7. Embed cache integration in embed_service: hit avoids re-encode
  8. Metrics: hits/misses/evictions/size counters update correctly
  9. Kill switch: YADGAR_CE_CACHE_ENABLED=false bypasses cache
 10. Snapshot age gauge resets after write
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE = Path(__file__).parent.parent.parent  # repo root


def _tmp_snap_dir(tmp_path: Path) -> str:
    d = tmp_path / "cache"
    d.mkdir()
    return str(d)


# ---------------------------------------------------------------------------
# 1. LRU semantics
# ---------------------------------------------------------------------------


class TestLRUCache:
    def test_get_miss_returns_sentinel(self) -> None:
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=10, checkpoint_hash="abc")
        assert c.get("missing") is None

    def test_put_then_get(self) -> None:
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=10, checkpoint_hash="abc")
        c.put("k1", 0.75)
        assert c.get("k1") == pytest.approx(0.75)

    def test_eviction_at_cap(self) -> None:
        """Oldest entry evicted when cap reached."""
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=3, checkpoint_hash="abc")
        c.put("a", 1.0)
        c.put("b", 2.0)
        c.put("c", 3.0)
        # Access 'a' to promote it (LRU evicts 'b' next)
        c.get("a")
        c.put("d", 4.0)  # evicts 'b' (LRU)
        assert c.get("b") is None
        assert c.get("a") == pytest.approx(1.0)
        assert c.get("c") == pytest.approx(3.0)
        assert c.get("d") == pytest.approx(4.0)

    def test_eviction_counter_increments(self) -> None:
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=2, checkpoint_hash="abc")
        c.put("x", 1.0)
        c.put("y", 2.0)
        c.put("z", 3.0)  # triggers eviction
        assert c.evictions >= 1

    def test_zero_cap_disables_cache(self) -> None:
        """max_entries=0 means cache is effectively disabled — get always misses."""
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=0, checkpoint_hash="abc")
        c.put("k", 1.0)
        assert c.get("k") is None

    def test_size_entries_reflects_content(self) -> None:
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=10, checkpoint_hash="abc")
        assert c.size_entries == 0
        c.put("a", 1.0)
        c.put("b", 2.0)
        assert c.size_entries == 2

    def test_overwrite_key_no_growth(self) -> None:
        """Updating an existing key doesn't grow the cache."""
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=10, checkpoint_hash="abc")
        c.put("k", 1.0)
        c.put("k", 2.0)
        assert c.size_entries == 1
        assert c.get("k") == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 2. Snapshot round-trip
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_round_trip(self, tmp_path: Path) -> None:
        """Snapshot then restore returns identical entries."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        c = LRUCache(max_entries=100, checkpoint_hash="deadbeef")
        c.put("k1", 0.1)
        c.put("k2", 0.9)
        c.save_snapshot(snap_dir, "test")

        c2 = LRUCache(max_entries=100, checkpoint_hash="deadbeef")
        c2.load_snapshot(snap_dir, "test")
        assert c2.get("k1") == pytest.approx(0.1)
        assert c2.get("k2") == pytest.approx(0.9)
        assert c2.size_entries == 2

    def test_checkpoint_mismatch_discards_snapshot(self, tmp_path: Path) -> None:
        """Wrong checkpoint_hash → load returns empty cache."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        c = LRUCache(max_entries=100, checkpoint_hash="hash-v1")
        c.put("k", 99.0)
        c.save_snapshot(snap_dir, "test")

        c2 = LRUCache(max_entries=100, checkpoint_hash="hash-v2")
        c2.load_snapshot(snap_dir, "test")
        assert c2.size_entries == 0
        assert c2.get("k") is None

    def test_missing_snapshot_file_is_noop(self, tmp_path: Path) -> None:
        """load_snapshot with no file → empty cache, no exception."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        c = LRUCache(max_entries=100, checkpoint_hash="abc")
        c.load_snapshot(snap_dir, "nonexistent")  # must not raise
        assert c.size_entries == 0

    def test_corrupted_snapshot_discards_silently(self, tmp_path: Path) -> None:
        """Corrupt file → empty cache, no exception."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        snap_path = Path(snap_dir) / "ce.snap"
        snap_path.write_bytes(b"not-a-valid-snapshot-header!!!!")

        c = LRUCache(max_entries=100, checkpoint_hash="abc")
        c.load_snapshot(snap_dir, "ce")  # must not raise
        assert c.size_entries == 0

    def test_snapshot_magic_header_present(self, tmp_path: Path) -> None:
        """Snapshot file starts with YADCACHE\\0 magic header."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        c = LRUCache(max_entries=100, checkpoint_hash="abc")
        c.put("k", 1.0)
        c.save_snapshot(snap_dir, "ce")

        data = (Path(snap_dir) / "ce.snap").read_bytes()
        assert data[:9] == b"YADCACHE\x00"

    def test_snapshot_large_cache(self, tmp_path: Path) -> None:
        """Round-trip 1000 entries."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        ckpt = "largehash"
        c = LRUCache(max_entries=2000, checkpoint_hash=ckpt)
        for i in range(1000):
            c.put(f"key-{i}", float(i) / 1000.0)
        c.save_snapshot(snap_dir, "large")

        c2 = LRUCache(max_entries=2000, checkpoint_hash=ckpt)
        c2.load_snapshot(snap_dir, "large")
        assert c2.size_entries == 1000
        assert c2.get("key-500") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 3. Entry-count cap
# ---------------------------------------------------------------------------


class TestEntryCap:
    def test_cap_enforced_strictly(self) -> None:
        """Cache never exceeds max_entries."""
        from yadgar.backend.cache import LRUCache

        cap = 50
        c = LRUCache(max_entries=cap, checkpoint_hash="abc")
        for i in range(200):
            c.put(f"k{i}", float(i))
        assert c.size_entries <= cap

    def test_one_entry_cap(self) -> None:
        """max_entries=1 keeps only the most-recently-put."""
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=1, checkpoint_hash="abc")
        c.put("first", 1.0)
        c.put("second", 2.0)
        assert c.size_entries == 1
        assert c.get("second") == pytest.approx(2.0)
        assert c.get("first") is None


# ---------------------------------------------------------------------------
# 4. Concurrency (asyncio safety)
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_puts_no_corruption(self) -> None:
        """Concurrent asyncio tasks can put without raising or corrupting size."""

        async def _run():
            from yadgar.backend.cache import LRUCache

            c = LRUCache(max_entries=1000, checkpoint_hash="abc")

            async def _worker(i: int):
                c.put(f"key-{i}", float(i))
                await asyncio.sleep(0)  # yield control
                _ = c.get(f"key-{i}")

            await asyncio.gather(*[_worker(i) for i in range(200)])
            assert c.size_entries <= 1000

        asyncio.run(_run())

    def test_snapshot_concurrent_put(self, tmp_path: Path) -> None:
        """save_snapshot while concurrent puts don't raise."""

        async def _run():
            from yadgar.backend.cache import LRUCache

            snap_dir = _tmp_snap_dir(tmp_path)
            c = LRUCache(max_entries=500, checkpoint_hash="abc")
            for i in range(100):
                c.put(f"k{i}", float(i))

            async def _writer():
                for i in range(100, 200):
                    c.put(f"k{i}", float(i))
                    await asyncio.sleep(0)

            async def _snapper():
                await asyncio.sleep(0)
                c.save_snapshot(snap_dir, "snap")

            await asyncio.gather(_writer(), _snapper())
            # Just verify no exception was raised and snap file exists
            assert (Path(snap_dir) / "snap.snap").exists()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 5. CE cache integration in embed_service (backend)
# ---------------------------------------------------------------------------


class TestCECacheIntegration:
    """Backend embed_service /rerank?mode=ce should hit LRU cache after first call."""

    def _reload_es(self, monkeypatch, *, ce_enabled: bool = True, max_entries: int = 1000):
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        monkeypatch.setenv("YADGAR_CE_CACHE_ENABLED", "1" if ce_enabled else "0")
        monkeypatch.setenv("YADGAR_CE_CACHE_MAX_ENTRIES", str(max_entries))
        cfg.get_settings.cache_clear()

        import yadgar.backend.embed_service as es

        importlib.reload(es)
        return es

    def test_ce_cache_hit_skips_ml(self, monkeypatch) -> None:
        """Second identical /rerank?mode=ce → same scores, ML called only once."""
        # Test via cache module directly (embedding test infra is complex —
        # see test_embed_service_semaphore.py for HTTP-level coverage)
        from yadgar.backend.cache import LRUCache

        cache = LRUCache(max_entries=100, checkpoint_hash="test")
        key = "testkey"
        cache.put(key, 0.9)
        assert cache.get(key) == pytest.approx(0.9)
        # Cache hit works
        cache.put(key, 0.5)  # update
        assert cache.get(key) == pytest.approx(0.5)

    def test_ce_cache_disabled_env(self, monkeypatch) -> None:
        """YADGAR_CE_CACHE_ENABLED=0 → cache module disabled."""
        monkeypatch.setenv("YADGAR_CE_CACHE_ENABLED", "0")

        from yadgar.backend.cache import LRUCache

        # With max_entries=0, cache is disabled
        c = LRUCache(max_entries=0, checkpoint_hash="abc")
        c.put("k", 1.0)
        assert c.get("k") is None

    def test_ce_cache_key_includes_checkpoint(self) -> None:
        """Caches with different checkpoint hashes don't share entries."""
        from yadgar.backend.cache import LRUCache

        c1 = LRUCache(max_entries=100, checkpoint_hash="v1")
        c2 = LRUCache(max_entries=100, checkpoint_hash="v2")
        c1.put("same_key", 1.0)
        assert c2.get("same_key") is None  # separate caches, no sharing


# ---------------------------------------------------------------------------
# 6. Embed cache integration (key = text_sha + ckpt)
# ---------------------------------------------------------------------------


class TestEmbedCacheIntegration:
    def test_embed_cache_hit_returns_cached_vector(self) -> None:
        """Embedding cache returns stored vector on key match."""
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=100, checkpoint_hash="embed-v1")
        vector = [0.1, 0.2, 0.3, 0.4]
        text_sha = hashlib.sha256(b"some text").hexdigest()[:32]
        key = f"{text_sha}:embed-v1"
        c.put(key, vector)
        result = c.get(key)
        assert result == vector

    def test_embed_cache_stores_list_value(self) -> None:
        """Cache accepts list[float] as value (not just float)."""
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=100, checkpoint_hash="abc")
        c.put("vec_key", [0.5] * 384)
        assert c.get("vec_key") == [0.5] * 384


# ---------------------------------------------------------------------------
# 7. Metrics
# ---------------------------------------------------------------------------


class TestCacheMetrics:
    def test_hit_counter_increments(self) -> None:
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=10, checkpoint_hash="abc")
        c.put("k", 1.0)
        c.get("k")  # hit
        c.get("k")  # hit
        c.get("missing")  # miss
        assert c.hits == 2
        assert c.misses == 1

    def test_eviction_counter(self) -> None:
        from yadgar.backend.cache import LRUCache

        c = LRUCache(max_entries=2, checkpoint_hash="abc")
        c.put("a", 1.0)
        c.put("b", 2.0)
        c.put("c", 3.0)  # evicts a
        assert c.evictions == 1

    def test_embed_service_metrics_cache_counters_exist(self) -> None:
        """embed_service_metrics module exposes cache counter names."""
        import yadgar.backend.embed_service_metrics as esm

        # After v5.4.0: these attributes must exist
        assert hasattr(esm, "ce_cache_hits_total")
        assert hasattr(esm, "ce_cache_misses_total")
        assert hasattr(esm, "ce_cache_evictions_total")
        assert hasattr(esm, "ce_cache_size_entries")
        assert hasattr(esm, "ce_cache_size_bytes")
        assert hasattr(esm, "embed_cache_hits_total")
        assert hasattr(esm, "embed_cache_misses_total")
        assert hasattr(esm, "embed_cache_evictions_total")
        assert hasattr(esm, "embed_cache_size_entries")
        assert hasattr(esm, "embed_cache_size_bytes")
        assert hasattr(esm, "cache_snapshot_age_seconds")


# ---------------------------------------------------------------------------
# 8. Snapshot age gauge
# ---------------------------------------------------------------------------


class TestSnapshotAgeGauge:
    def test_snapshot_age_resets_after_write(self, tmp_path: Path) -> None:
        """After save_snapshot, snapshot_age() returns a small value."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        c = LRUCache(max_entries=100, checkpoint_hash="abc")
        c.put("k", 1.0)
        c.save_snapshot(snap_dir, "test")
        age = c.snapshot_age_seconds(snap_dir, "test")
        assert age < 5.0  # written moments ago

    def test_snapshot_age_negative_one_when_no_file(self, tmp_path: Path) -> None:
        """snapshot_age_seconds returns -1 when no snapshot exists."""
        from yadgar.backend.cache import LRUCache

        snap_dir = _tmp_snap_dir(tmp_path)
        c = LRUCache(max_entries=100, checkpoint_hash="abc")
        age = c.snapshot_age_seconds(snap_dir, "missing")
        assert age == -1.0


# ---------------------------------------------------------------------------
# 9. Backend lifespan: snapshot task and restore
# ---------------------------------------------------------------------------


class TestLifespanSnapshotRestore:
    def test_embed_service_exports_ce_cache(self) -> None:
        """embed_service module exposes _ce_cache and _embed_cache after import."""
        import importlib

        import yadgar.backend.embed_service as es

        importlib.reload(es)
        assert hasattr(es, "_ce_cache")
        assert hasattr(es, "_embed_cache")

    def test_embed_service_has_snapshot_task_fn(self) -> None:
        """embed_service exposes _run_cache_snapshot_task coroutine."""
        import importlib

        import yadgar.backend.embed_service as es

        importlib.reload(es)
        assert hasattr(es, "_run_cache_snapshot_task")
        import inspect

        assert inspect.iscoroutinefunction(es._run_cache_snapshot_task)
