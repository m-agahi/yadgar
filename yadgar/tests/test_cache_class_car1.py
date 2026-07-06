"""Car 1 — the generalized `Cache` class (v5.111).

The `Cache` class (yadgar/cache.py) generalizes the backend `LRUCache` into a
core-side, named, N-instance cache with policy bound at CONSTRUCTION:

  - byte-bounded LRU eviction (max_bytes; 0 = disabled kill-switch case) — core
    5.112.0 #49 retrofit from the earlier max_entries count-cap
  - pluggable invalidation: KeyFn (freshness in key) | TTL(secs) | Manual
  - deep-copy-on-return (deep_copy=True) so callers mutating a returned row-dict
    cannot corrupt the cached value
  - observability-by-construction: getters carry @observe (I33 span source);
    hit/miss/evict flow through Car 0's record_cache_* helper (cold tier inline;
    hot tier metric-only via internal ints + a scrape collector).

These tests are MODEL-FREE (no CE/embed model load) — safe under the OOM
constraint.
"""

from __future__ import annotations

import pytest

from yadgar.cache import _REGISTRY, TTL, Cache, KeyFn, Manual


@pytest.fixture(autouse=True)
def _clear_registry():
    # Isolate each test: drop any instances a prior test registered so name
    # collisions do not bleed across tests.
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()


# ── construction + registry ──────────────────────────────────────────────────


def test_construct_self_registers_by_name():
    c = Cache(name="t_reg", max_bytes=1_000_000)
    assert _REGISTRY["t_reg"] is c


def test_duplicate_name_raises():
    Cache(name="t_dup", max_bytes=1_000_000)
    with pytest.raises(ValueError):
        Cache(name="t_dup", max_bytes=1_000_000)


# ── basic hit / miss / put ───────────────────────────────────────────────────


def test_get_miss_returns_none():
    c = Cache(name="t_miss", max_bytes=1_000_000)
    assert c.get("absent") is None


def test_put_then_get_hit():
    c = Cache(name="t_hit", max_bytes=1_000_000)
    c.put("k", 42)
    assert c.get("k") == 42


def test_stats_counts_hits_and_misses():
    c = Cache(name="t_stats", max_bytes=1_000_000)
    c.get("x")  # miss
    c.put("x", 1)
    c.get("x")  # hit
    s = c.stats()
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert s["size"] == 1


# ── byte-bounded LRU eviction (core 5.112.0 #49: bound is bytes, not count) ────


def _two_entry_budget(payload) -> int:
    """A byte budget that holds ~2 copies of `payload` but not a 3rd."""
    from yadgar.cache import _estimate_bytes

    per = _estimate_bytes(payload)
    return per * 2 + per // 2


def test_lru_evicts_oldest_over_budget():
    payload = "x" * 1000
    c = Cache(name="t_evict", max_bytes=_two_entry_budget(payload))
    c.put("a", payload)
    c.put("b", payload)
    c.put("c", payload)  # over byte budget → evicts "a" (LRU)
    assert c.get("a") is None
    assert c.get("b") == payload
    assert c.get("c") == payload
    assert c.stats()["evictions"] == 1


def test_get_promotes_to_mru():
    payload = "x" * 1000
    c = Cache(name="t_mru", max_bytes=_two_entry_budget(payload))
    c.put("a", payload)
    c.put("b", payload)
    c.get("a")  # a now MRU
    c.put("c", payload)  # evicts b (LRU), not a
    assert c.get("a") == payload
    assert c.get("b") is None


def test_generous_budget_holds_many_small_entries():
    # A budget far exceeding the entries' footprint never evicts (the practical
    # behaviour-neutral case: the core budget dwarfs the small read-tool dicts).
    c = Cache(name="t_unbounded", max_bytes=100_000_000)
    for i in range(1000):
        c.put(str(i), i)
    assert c.get("0") == 0
    assert c.get("999") == 999
    assert c.stats()["evictions"] == 0


def test_max_bytes_zero_disables():
    c = Cache(name="t_disabled", max_bytes=0)
    c.put("k", "v")
    assert c.get("k") is None
    assert c.stats()["size"] == 0


# ── TTL invalidation ─────────────────────────────────────────────────────────


def test_ttl_expires_on_read():
    now = [1000.0]
    c = Cache(name="t_ttl", max_bytes=1_000_000, invalidation=TTL(10), clock=lambda: now[0])
    c.put("k", "v")
    assert c.get("k") == "v"  # fresh
    now[0] += 11  # advance past TTL
    assert c.get("k") is None  # expired
    assert c.stats()["misses"] >= 1


def test_ttl_within_window_hits():
    now = [1000.0]
    c = Cache(name="t_ttl2", max_bytes=1_000_000, invalidation=TTL(300), clock=lambda: now[0])
    c.put("k", "v")
    now[0] += 299
    assert c.get("k") == "v"


# ── manual / whole-flush invalidation ────────────────────────────────────────


def test_manual_clear_flushes_all():
    c = Cache(name="t_clear", max_bytes=1_000_000, invalidation=Manual())
    c.put("a", 1)
    c.put("b", 2)
    c.clear()
    assert c.get("a") is None
    assert c.get("b") is None
    assert c.stats()["size"] == 0


def test_invalidate_single_key():
    c = Cache(name="t_inv", max_bytes=1_000_000, invalidation=Manual())
    c.put("a", 1)
    c.put("b", 2)
    c.invalidate("a")
    assert c.get("a") is None
    assert c.get("b") == 2


# ── key_fn ───────────────────────────────────────────────────────────────────


def test_key_fn_derives_effective_key():
    # key_fn folds an external "epoch" into the key: a bump changes the key so a
    # prior entry is never returned.
    epoch = [0]
    c = Cache(
        name="t_keyfn",
        max_bytes=1_000_000,
        invalidation=KeyFn(),
        key_fn=lambda user_key: (user_key, epoch[0]),
    )
    c.put("k", "v0")
    assert c.get("k") == "v0"
    epoch[0] = 1  # structural bump: key moved
    assert c.get("k") is None  # old entry not reachable under new epoch


# ── deep-copy isolation (THE correctness bit) ────────────────────────────────


def test_deep_copy_isolates_returned_value():
    c = Cache(name="t_dc", max_bytes=1_000_000, deep_copy=True)
    c.put("k", {"heat": 1, "nested": {"x": [1, 2]}})
    got = c.get("k")
    got["heat"] = 999
    got["nested"]["x"].append(3)
    again = c.get("k")
    assert again["heat"] == 1  # cache uncorrupted
    assert again["nested"]["x"] == [1, 2]


def test_no_deep_copy_returns_reference():
    c = Cache(name="t_ref", max_bytes=1_000_000, deep_copy=False)
    c.put("k", {"heat": 1})
    got = c.get("k")
    got["heat"] = 999
    again = c.get("k")
    # Without deep_copy the caller shares the stored object (documented; used for
    # float/vector values that are never mutated).
    assert again["heat"] == 999


# ── observability: metric emission on the generic Car 0 family ────────────────


def test_cold_tier_emits_hit_miss_metric():
    from yadgar import metrics

    c = Cache(name="t_obs_cold", max_bytes=1_000_000, obs_tier="cold")
    h0 = metrics.yadgar_cache_hit_total.labels(cache="t_obs_cold")._value.get()
    m0 = metrics.yadgar_cache_miss_total.labels(cache="t_obs_cold")._value.get()
    c.get("k")  # miss
    c.put("k", 1)
    c.get("k")  # hit
    h1 = metrics.yadgar_cache_hit_total.labels(cache="t_obs_cold")._value.get()
    m1 = metrics.yadgar_cache_miss_total.labels(cache="t_obs_cold")._value.get()
    assert h1 == h0 + 1
    assert m1 == m0 + 1


def test_eviction_emits_metric():
    from yadgar import metrics

    payload = "x" * 1000
    from yadgar.cache import _estimate_bytes

    c = Cache(name="t_obs_evict", max_bytes=_estimate_bytes(payload) + 1, obs_tier="cold")
    e0 = metrics.yadgar_cache_evictions_total.labels(cache="t_obs_evict")._value.get()
    c.put("a", payload)
    c.put("b", payload)  # over budget → evict a
    e1 = metrics.yadgar_cache_evictions_total.labels(cache="t_obs_evict")._value.get()
    assert e1 == e0 + 1


def test_get_and_put_carry_observe_span_sentinel():
    # I33: the getters must carry a span source (@observe). The decorator sets a
    # sentinel attribute on the wrapped fn.
    from yadgar.observability.observe import _OBSERVE_SPAN_SENTINEL

    assert getattr(Cache.get, _OBSERVE_SPAN_SENTINEL, False)
    assert getattr(Cache.put, _OBSERVE_SPAN_SENTINEL, False)
