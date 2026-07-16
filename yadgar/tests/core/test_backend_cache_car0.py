"""Car 0 — unified backend `Cache` class + ce/embed fold-in (backend 5.17.0).

TDD (red first). Model-free (no CE/embed model load) — safe under OOM constraint.

Covers the Car-0 scope of docs/plans/backend-caching-train-2026-07-06.md:

  1. CacheProtocol conformance (get/put/invalidate/stats) — Cache + NullCache.
  2. Unified `Cache` class: named namespaces via a registry, policy bound at
     construction (ModelCkpt / DataEpoch / TTL / Manual), key_fn, deep_copy.
  3. Byte-bounded LRU eviction (NOT fixed max_entries): add past budget → LRU
     evicted, budget respected. RAM-% knob: mock cgroup limit → budget computed.
  4. Namespace isolation (separate stores, separate counters).
  5. ce/embed fold-in behaviour-neutral: same keys, same values, ModelCkpt
     invalidation (ckpt-in-key), external metric series preserved, snapshot I/O.
  6. Obs-by-construction: hit/miss/evict internal counters + generic
     yadgar_cache_* family on the cold tier.
  7. NullCache disable path (always-miss, put no-op).

These tests MUST fail before the `Cache` / `NullCache` / RAM-% machinery exists.
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fresh_cache(**kw):
    """Build a Cache with a unique name (registry self-registration is global)."""
    import uuid

    from yadgar.backend.cache import Cache

    kw.setdefault("name", f"t_{uuid.uuid4().hex[:8]}")
    kw.setdefault("max_bytes", 10_000)
    return Cache(**kw)


# ---------------------------------------------------------------------------
# 1. CacheProtocol conformance
# ---------------------------------------------------------------------------


class TestCacheProtocol:
    def test_protocol_importable(self) -> None:
        from yadgar.backend.cache import CacheProtocol  # noqa: F401

    def test_cache_satisfies_protocol_methods(self) -> None:
        from yadgar.backend.cache import Cache

        for meth in ("get", "put", "invalidate", "stats"):
            assert callable(getattr(Cache, meth, None)), f"Cache missing {meth}"

    def test_nullcache_satisfies_protocol_methods(self) -> None:
        from yadgar.backend.cache import NullCache

        for meth in ("get", "put", "invalidate", "stats"):
            assert callable(getattr(NullCache, meth, None)), f"NullCache missing {meth}"

    def test_cache_is_runtime_checkable_protocol_instance(self) -> None:
        from yadgar.backend.cache import CacheProtocol, NullCache

        assert isinstance(_fresh_cache(), CacheProtocol)
        assert isinstance(NullCache(), CacheProtocol)


# ---------------------------------------------------------------------------
# 2. Unified Cache: namespaces + policy bound at construction
# ---------------------------------------------------------------------------


class TestUnifiedCache:
    def test_basic_put_get(self) -> None:
        c = _fresh_cache(max_bytes=10_000)
        c.put("k1", 0.75)
        assert c.get("k1") == pytest.approx(0.75)

    def test_miss_returns_none(self) -> None:
        c = _fresh_cache(max_bytes=10_000)
        assert c.get("nope") is None

    def test_self_registers_into_registry(self) -> None:
        from yadgar.backend.cache import _REGISTRY

        c = _fresh_cache(max_bytes=10_000)
        assert _REGISTRY.get(c.name) is c

    def test_ttl_expiry(self) -> None:
        from yadgar.backend.cache import TTL

        clock = {"t": 0.0}
        c = _fresh_cache(max_bytes=10_000, invalidation=TTL(5.0), clock=lambda: clock["t"])
        c.put("k", 1.0)
        assert c.get("k") == pytest.approx(1.0)
        clock["t"] = 6.0
        assert c.get("k") is None  # expired

    def test_deep_copy_isolates_returned_value(self) -> None:
        c = _fresh_cache(max_bytes=100_000, deep_copy=True)
        c.put("k", {"heat": 1})
        got = c.get("k")
        got["heat"] = 999
        assert c.get("k")["heat"] == 1  # cached copy untouched

    def test_key_fn_embeds_freshness(self) -> None:
        # epoch-in-key: same logical key with a new epoch misses the old entry.
        epoch = {"e": 1}
        c = _fresh_cache(max_bytes=10_000, key_fn=lambda k: f"{k}:{epoch['e']}")
        c.put("mem", "v1")
        assert c.get("mem") == "v1"
        epoch["e"] = 2
        assert c.get("mem") is None

    def test_manual_invalidate(self) -> None:
        c = _fresh_cache(max_bytes=10_000)
        c.put("k", 1.0)
        c.invalidate("k")
        assert c.get("k") is None

    def test_stats_shape(self) -> None:
        c = _fresh_cache(max_bytes=10_000)
        c.put("k", 1.0)
        c.get("k")  # hit
        c.get("x")  # miss
        s = c.stats()
        for field in ("hits", "misses", "evictions", "size"):
            assert field in s
        assert s["hits"] == 1
        assert s["misses"] == 1


# ---------------------------------------------------------------------------
# 3. Byte-bounded LRU eviction + RAM-% knob
# ---------------------------------------------------------------------------


class TestByteBoundedEviction:
    def test_evicts_lru_when_over_byte_budget(self) -> None:
        # Each value ~ a 20-int list. Budget tight enough to hold only a few.
        c = _fresh_cache(max_bytes=400)
        for i in range(50):
            c.put(f"k{i}", list(range(20)))
        assert c.stats()["evictions"] > 0
        assert c.current_bytes <= c.max_bytes
        # Oldest key evicted, newest present.
        assert c.get("k0") is None
        assert c.get("k49") is not None

    def test_lru_recency_protects_hot_entry(self) -> None:
        c = _fresh_cache(max_bytes=400)
        c.put("hot", list(range(20)))
        for i in range(50):
            c.get("hot")  # keep it MRU
            c.put(f"k{i}", list(range(20)))
        assert c.get("hot") is not None  # survived despite churn

    def test_zero_budget_disables(self) -> None:
        c = _fresh_cache(max_bytes=0)
        c.put("k", 1.0)
        assert c.get("k") is None

    def test_current_bytes_tracks_content(self) -> None:
        c = _fresh_cache(max_bytes=100_000)
        assert c.current_bytes == 0
        c.put("k", list(range(100)))
        assert c.current_bytes > 0


class TestRamPctBudget:
    def test_budget_from_cgroup_limit(self, monkeypatch) -> None:
        # RAM-% budget machinery moved to cache_budgets (backend 5.51.0 I13 split).
        from yadgar.backend.cache import cache_budgets as cmod

        # 1 GiB container, 10% → ~107 MB total backend cache budget.
        monkeypatch.setattr(cmod, "_read_container_memory_bytes", lambda: 1024**3)
        budget = cmod._backend_cache_total_budget_bytes(pct=10.0)
        assert budget == pytest.approx(0.10 * 1024**3, rel=1e-6)

    def test_budget_fallback_outside_container(self, monkeypatch) -> None:
        from yadgar.backend.cache import cache_budgets as cmod

        monkeypatch.setattr(cmod, "_read_container_memory_bytes", lambda: None)
        budget = cmod._backend_cache_total_budget_bytes(pct=10.0)
        assert budget > 0  # sane non-zero fallback

    def test_namespace_split_sums_within_total(self, monkeypatch) -> None:
        from yadgar.backend.cache import cache_budgets as cmod

        monkeypatch.setattr(cmod, "_read_container_memory_bytes", lambda: 1000)
        total = cmod._backend_cache_total_budget_bytes(pct=100.0)
        ce = cmod._namespace_budget_bytes("ce", total)
        embed = cmod._namespace_budget_bytes("embed", total)
        assert ce > 0 and embed > 0
        # Split never exceeds the total budget.
        assert ce + embed <= total + 1


# ---------------------------------------------------------------------------
# 4. Namespace isolation
# ---------------------------------------------------------------------------


class TestNamespaceIsolation:
    def test_separate_stores(self) -> None:
        a = _fresh_cache(max_bytes=10_000)
        b = _fresh_cache(max_bytes=10_000)
        a.put("k", "in_a")
        assert b.get("k") is None

    def test_separate_counters(self) -> None:
        a = _fresh_cache(max_bytes=10_000)
        b = _fresh_cache(max_bytes=10_000)
        a.put("k", 1.0)
        a.get("k")
        assert a.stats()["hits"] == 1
        assert b.stats()["hits"] == 0

    def test_duplicate_name_overwrites_not_raises(self) -> None:
        # Backend registry tolerates re-registration (importlib.reload of
        # embed_service re-creates the ce/embed namespaces). Differs from core.
        from yadgar.backend.cache import _REGISTRY, Cache

        Cache(name="dup_ns", max_bytes=10_000)
        Cache(name="dup_ns", max_bytes=10_000)  # must NOT raise
        assert "dup_ns" in _REGISTRY


# ---------------------------------------------------------------------------
# 5. ce/embed fold-in behaviour-neutrality
# ---------------------------------------------------------------------------


class TestCeEmbedFoldIn:
    def test_ce_embed_are_unified_cache_instances(self) -> None:
        import yadgar.backend.embed_service.embed_service as es
        from yadgar.backend.cache import Cache

        assert isinstance(es._ce_cache, Cache)
        assert isinstance(es._embed_cache, Cache)

    def test_ce_namespace_name(self) -> None:
        import yadgar.backend.embed_service.embed_service as es

        assert es._ce_cache.name == "ce"
        assert es._embed_cache.name == "embed"

    def test_ce_cache_same_key_same_value(self) -> None:
        # Behaviour-neutral: same key → same value (CE score cache contract).
        c = _fresh_cache(name="ce_probe", max_bytes=100_000)
        key = "qsha:tsha:ckpt"
        c.put(key, 0.9)
        assert c.get(key) == pytest.approx(0.9)

    def test_ckpt_in_key_isolates_models(self) -> None:
        # ModelCkpt invalidation lives in the key (ckpt_sha suffix) — different
        # ckpt → different key → miss. Same behaviour as pre-fold LRUCache.
        c = _fresh_cache(name="ce_ckpt_probe", max_bytes=100_000)
        c.put("qsha:tsha:ckptA", 1.0)
        assert c.get("qsha:tsha:ckptB") is None

    def test_ce_cache_still_exposes_consumer_surface(self) -> None:
        # embed_service consumers + CacheStatsCollector read these attrs.
        import yadgar.backend.embed_service.embed_service as es

        for attr in ("hits", "misses", "evictions", "size_entries"):
            assert hasattr(es._ce_cache, attr), f"_ce_cache missing {attr}"
        assert hasattr(es._ce_cache, "save_snapshot")
        assert hasattr(es._ce_cache, "load_snapshot")
        assert hasattr(es._ce_cache, "snapshot_age_seconds")
        assert hasattr(es._ce_cache, "_ckpt")

    def test_snapshot_roundtrip(self, tmp_path) -> None:
        c = _fresh_cache(name="snap_probe", max_bytes=100_000, checkpoint_hash="deadbeef")
        c.put("k1", 1.0)
        c.put("k2", 2.0)
        snap_dir = str(tmp_path)
        c.save_snapshot(snap_dir, "snap_probe")
        c2 = _fresh_cache(name="snap_probe2", max_bytes=100_000, checkpoint_hash="deadbeef")
        c2.load_snapshot(snap_dir, "snap_probe")
        assert c2.get("k1") == pytest.approx(1.0)
        assert c2.get("k2") == pytest.approx(2.0)

    def test_snapshot_ckpt_mismatch_discards(self, tmp_path) -> None:
        c = _fresh_cache(name="snap_v1", max_bytes=100_000, checkpoint_hash="hashv1")
        c.put("k", 1.0)
        snap_dir = str(tmp_path)
        c.save_snapshot(snap_dir, "snap_v1")
        c2 = _fresh_cache(name="snap_v2", max_bytes=100_000, checkpoint_hash="hashv2")
        c2.load_snapshot(snap_dir, "snap_v1")
        assert c2.get("k") is None

    def test_reload_embed_service_does_not_raise(self, monkeypatch) -> None:
        # importlib.reload re-creates ce/embed namespaces — must not raise on the
        # duplicate registry name.
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        import yadgar.backend.embed_service.embed_service as es

        importlib.reload(es)
        assert isinstance(es._ce_cache.name, str)


# ---------------------------------------------------------------------------
# 6. Obs-by-construction
# ---------------------------------------------------------------------------


class TestObsByConstruction:
    def test_hit_miss_evict_counters_increment(self) -> None:
        c = _fresh_cache(max_bytes=400)
        c.get("x")  # miss
        c.put("a", list(range(20)))
        c.get("a")  # hit
        for i in range(50):
            c.put(f"k{i}", list(range(20)))  # force evictions
        s = c.stats()
        assert s["misses"] >= 1
        assert s["hits"] >= 1
        assert s["evictions"] >= 1

    def test_cold_tier_emits_generic_family(self) -> None:
        from yadgar._shared.observability import metrics

        c = _fresh_cache(name="obs_cold_probe", max_bytes=10_000, obs_tier="cold")
        before = metrics.yadgar_cache_hit_total.labels(cache="obs_cold_probe")._value.get()
        c.put("k", 1.0)
        c.get("k")
        after = metrics.yadgar_cache_hit_total.labels(cache="obs_cold_probe")._value.get()
        assert after == before + 1


# ---------------------------------------------------------------------------
# 7. NullCache disable path
# ---------------------------------------------------------------------------


class TestNullCache:
    def test_always_miss(self) -> None:
        from yadgar.backend.cache import NullCache

        n = NullCache()
        n.put("k", 1.0)
        assert n.get("k") is None

    def test_stats_zeroed(self) -> None:
        from yadgar.backend.cache import NullCache

        n = NullCache()
        s = n.stats()
        assert s["size"] == 0
