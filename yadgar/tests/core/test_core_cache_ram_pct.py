"""Core cache RAM-% retrofit (#49, core v5.112.0).

The core `Cache` (yadgar/cache.py) is retrofit from a fixed `max_entries`
count-cap to a **byte-bounded** LRU eviction sized from a % of the CORE
container RAM — mirroring what backend Car 0 did to `yadgar/backend/cache.py`,
but with the core's own knob (`YADGAR_CORE_CACHE_RAM_PCT`), the core container
memory (`--memory 1g`, NOT the backend's 4 GiB), and the four core namespaces
(project_brief / wiki_read / wiki_query / agent_prompt_prelude) sharing ONE
process budget (weighted split).

MODEL-FREE — no CE/embed model load; safe under the OOM constraint.
"""

from __future__ import annotations

import pytest

from yadgar.core.cache import (
    _CORE_FALLBACK_CONTAINER_BYTES,
    _REGISTRY,
    Cache,
    _core_cache_ram_pct,
    _core_cache_total_budget_bytes,
    _estimate_bytes,
    _namespace_budget_bytes,
    _read_container_memory_bytes,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()


# ── byte estimation ───────────────────────────────────────────────────────────


def test_estimate_bytes_scales_with_value_size():
    """A tiny scalar estimates smaller than a large dict."""
    small = _estimate_bytes(1)
    big = _estimate_bytes({"content": "x" * 5000, "vec": [0.1] * 200})
    assert small < big
    assert big > 5000


# ── byte-bounded LRU eviction ─────────────────────────────────────────────────


def test_byte_bounded_evicts_lru_when_over_budget():
    """Entries past the byte budget evict the LRU one (not a count cap)."""
    # Each value ~ a fixed msgpack size; budget holds ~2 of them.
    payload = "y" * 1000
    two_entries = _estimate_bytes(payload) * 2 + _estimate_bytes(payload) // 2
    c = Cache(name="t_bytes", max_bytes=two_entries)
    c.put("a", payload)
    c.put("b", payload)
    c.put("c", payload)  # over budget → evict LRU "a"
    assert c.get("a") is None
    assert c.get("b") == payload
    assert c.get("c") == payload
    assert c.stats()["evictions"] >= 1
    assert c.stats()["bytes"] <= two_entries


def test_byte_get_promotes_mru_under_budget():
    payload = "z" * 1000
    two = _estimate_bytes(payload) * 2 + _estimate_bytes(payload) // 2
    c = Cache(name="t_bytes_mru", max_bytes=two)
    c.put("a", payload)
    c.put("b", payload)
    c.get("a")  # a → MRU
    c.put("c", payload)  # evict LRU "b", not "a"
    assert c.get("a") == payload
    assert c.get("b") is None


def test_max_bytes_zero_disables():
    """max_bytes=0 = disabled (all puts no-op, all gets miss) — fold-in disable."""
    c = Cache(name="t_bytes_off", max_bytes=0)
    c.put("k", "v")
    assert c.get("k") is None
    assert c.stats()["size"] == 0


def test_stats_reports_bytes():
    c = Cache(name="t_bytes_stats", max_bytes=10_000)
    c.put("k", "hello")
    s = c.stats()
    assert "bytes" in s
    assert s["bytes"] > 0
    assert s["size"] == 1


# ── RAM-% knob → byte budget ──────────────────────────────────────────────────


def test_ram_pct_knob_reads_env(monkeypatch):
    monkeypatch.setenv("YADGAR_CORE_CACHE_RAM_PCT", "25")
    assert _core_cache_ram_pct() == 25.0


def test_ram_pct_default_is_ten(monkeypatch):
    monkeypatch.delenv("YADGAR_CORE_CACHE_RAM_PCT", raising=False)
    # No env; Settings default is 10.0.
    assert _core_cache_ram_pct() == pytest.approx(10.0)


def test_total_budget_is_pct_of_container_memory(monkeypatch):
    """Budget = pct% × container memory (cgroup read)."""
    # Mock the cgroup reader to a known limit.
    import yadgar.core.cache.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_read_container_memory_bytes", lambda: 1_000_000_000)
    budget = _core_cache_total_budget_bytes(10.0)
    assert budget == 100_000_000


def test_total_budget_uses_core_fallback_when_no_cgroup(monkeypatch):
    """No readable cgroup limit → core fallback (1 GiB, NOT backend's 4 GiB)."""
    import yadgar.core.cache.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_read_container_memory_bytes", lambda: None)
    budget = _core_cache_total_budget_bytes(10.0)
    assert budget == int(0.10 * _CORE_FALLBACK_CONTAINER_BYTES)
    # Core container is --memory 1g → fallback is 1 GiB, not 4.
    assert _CORE_FALLBACK_CONTAINER_BYTES == 1 * 1024**3


def test_read_container_memory_parses_cgroup_v2(monkeypatch, tmp_path):
    """cgroup v2 memory.max is read + parsed."""
    import yadgar.core.cache.cache as cache_mod

    limit_file = tmp_path / "memory.max"
    limit_file.write_text("536870912\n")  # 512 MiB
    monkeypatch.setattr(cache_mod, "_CORE_CGROUP_V2", str(limit_file))
    monkeypatch.setattr(cache_mod, "_CORE_CGROUP_V1", str(tmp_path / "nonexistent"))
    assert _read_container_memory_bytes() == 536870912


def test_read_container_memory_max_sentinel_is_unbounded(monkeypatch, tmp_path):
    import yadgar.core.cache.cache as cache_mod

    limit_file = tmp_path / "memory.max"
    limit_file.write_text("max\n")
    monkeypatch.setattr(cache_mod, "_CORE_CGROUP_V2", str(limit_file))
    monkeypatch.setattr(cache_mod, "_CORE_CGROUP_V1", str(tmp_path / "nonexistent"))
    assert _read_container_memory_bytes() is None


# ── namespace budget split across the four core caches ────────────────────────


def test_namespace_budget_splits_across_core_namespaces():
    """The four core namespaces share ONE process budget (weighted split)."""
    total = 1_000_000
    active = ("project_brief", "wiki_read", "wiki_query", "agent_prompt_prelude")
    shares = {n: _namespace_budget_bytes(n, total, active=active) for n in active}
    # Each namespace gets a positive slice; slices sum to ≤ total.
    assert all(v > 0 for v in shares.values())
    assert sum(shares.values()) <= total


def test_namespace_budget_single_active_gets_full():
    total = 1_000_000
    got = _namespace_budget_bytes("project_brief", total, active=("project_brief",))
    assert got == total
