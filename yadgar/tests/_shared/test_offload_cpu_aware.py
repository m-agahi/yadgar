"""T3 Car 3: offload heavy-concurrency is CPU-aware via the 0=auto sentinel.

`RECALL_HEAVY_CONCURRENCY` default becomes the sentinel `0` (= auto). At `0`,
`_heavy_concurrency()` derives from `available_cpus()` via
`recall_heavy_concurrency_default()` (1 at ncpu ≤ 2 = today's behavior). An
explicit positive value still wins (ops override), clamped to ≤ pool workers.
"""

from __future__ import annotations

import pytest

from yadgar._shared.runtime import cpu as cpu_mod
from yadgar._shared.runtime import offload as _offload


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("YADGAR_RECALL_HEAVY_CONCURRENCY", raising=False)
    monkeypatch.delenv("YADGAR_TOOL_POOL_WORKERS", raising=False)
    monkeypatch.delenv("YADGAR_AVAILABLE_CPUS", raising=False)
    monkeypatch.delenv("YADGAR_RECALL_PARALLELISM", raising=False)
    cpu_mod.reset_cpu_cache()
    _offload.reset_rerank_gate()
    yield
    cpu_mod.reset_cpu_cache()
    _offload.reset_rerank_gate()


def test_sentinel_zero_floors_to_one_at_2_cpus(monkeypatch):
    """Default (unset → sentinel 0=auto) at ncpu ≤ 2 → 1 (today's behavior)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "2")
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "0")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "8")  # pool wide enough not to clamp
    cpu_mod.reset_cpu_cache()
    assert _offload._heavy_concurrency() == 1


def test_sentinel_zero_scales_with_cpus(monkeypatch):
    """sentinel 0=auto at ncpu 8 → recall_heavy_concurrency_default()=4 (clamped ≤ pool)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "0")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "8")
    cpu_mod.reset_cpu_cache()
    assert _offload._heavy_concurrency() == 4  # ncpu // 2


def test_explicit_value_overrides_auto(monkeypatch):
    """A positive explicit value wins over auto (ops override), clamped to pool."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "2")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "8")
    cpu_mod.reset_cpu_cache()
    assert _offload._heavy_concurrency() == 2


def test_auto_clamped_to_pool_workers(monkeypatch):
    """auto derivation is still clamped to ≤ pool workers (the gate invariant)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")  # auto would want 4
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "0")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "2")  # clamp to 2
    cpu_mod.reset_cpu_cache()
    assert _offload._heavy_concurrency() == 2
