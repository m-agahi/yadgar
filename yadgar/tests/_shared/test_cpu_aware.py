"""T3 Car 3: CPU-awareness helper (`available_cpus()`) parse + budget tests.

`available_cpus()` is the single source of truth for every concurrency budget in
the recall pipeline. It MUST:
  - parse cgroup-v2 `cpu.max` ("quota period" → ceil(quota/period)),
  - fall back to cgroup-v1 (`cpu.cfs_quota_us` / `cpu.cfs_period_us`),
  - fall back to `os.cpu_count()`,
  - NEVER return < 1,
  - be cached, with an explicit `reset_cpu_cache()` invalidation for tests.

The budget derivations built on top (`recall_gather_budget`,
`recall_heavy_concurrency`, `torch_intraop_threads`) MUST floor to today's
behavior at ≤ 2 CPUs: gather budget 1 (sequential), heavy-concurrency 1.
"""

from __future__ import annotations

import pytest

from yadgar._shared.runtime import cpu as cpu_mod
from yadgar._shared.runtime.cpu import (
    available_cpus,
    recall_gather_budget,
    reset_cpu_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cpu_cache()
    yield
    reset_cpu_cache()


# ---------------------------------------------------------------------------
# cgroup-v2 cpu.max parsing
# ---------------------------------------------------------------------------


def test_cgroup_v2_quota_ceils(monkeypatch, tmp_path):
    """cpu.max = "150000 100000" → ceil(1.5) = 2."""
    v2 = tmp_path / "cpu.max"
    v2.write_text("150000 100000\n")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(v2))
    reset_cpu_cache()
    assert available_cpus() == 2


def test_cgroup_v2_exact_quota(monkeypatch, tmp_path):
    """cpu.max = "400000 100000" → 4 (exact, no ceil inflation)."""
    v2 = tmp_path / "cpu.max"
    v2.write_text("400000 100000")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(v2))
    reset_cpu_cache()
    assert available_cpus() == 4


def test_cgroup_v2_max_falls_through(monkeypatch, tmp_path):
    """cpu.max = "max 100000" (no quota) → fall through to os.cpu_count()."""
    v2 = tmp_path / "cpu.max"
    v2.write_text("max 100000")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(v2))
    # v1 unreadable so it falls through to os.cpu_count()
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(tmp_path / "nope-quota"))
    monkeypatch.setattr(cpu_mod, "os", cpu_mod.os)
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 8)
    reset_cpu_cache()
    assert available_cpus() == 8


def test_cgroup_v2_sub_one_quota_floors_to_one(monkeypatch, tmp_path):
    """cpu.max = "50000 100000" (0.5 CPU) → NEVER < 1 → 1."""
    v2 = tmp_path / "cpu.max"
    v2.write_text("50000 100000")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(v2))
    reset_cpu_cache()
    assert available_cpus() == 1


# ---------------------------------------------------------------------------
# cgroup-v1 fallback
# ---------------------------------------------------------------------------


def test_cgroup_v1_quota(monkeypatch, tmp_path):
    """v2 absent; v1 quota=200000 period=100000 → 2."""
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(tmp_path / "no-v2"))
    quota = tmp_path / "cpu.cfs_quota_us"
    period = tmp_path / "cpu.cfs_period_us"
    quota.write_text("200000")
    period.write_text("100000")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(quota))
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_PERIOD", str(period))
    reset_cpu_cache()
    assert available_cpus() == 2


def test_cgroup_v1_unlimited_quota_falls_through(monkeypatch, tmp_path):
    """v1 quota = -1 (unlimited) → fall through to os.cpu_count()."""
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(tmp_path / "no-v2"))
    quota = tmp_path / "cpu.cfs_quota_us"
    quota.write_text("-1")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(quota))
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 6)
    reset_cpu_cache()
    assert available_cpus() == 6


# ---------------------------------------------------------------------------
# os.cpu_count fallback + never < 1
# ---------------------------------------------------------------------------


def test_oscpucount_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(tmp_path / "no-v2"))
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(tmp_path / "no-v1"))
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 12)
    reset_cpu_cache()
    assert available_cpus() == 12


def test_oscpucount_none_floors_to_one(monkeypatch, tmp_path):
    """os.cpu_count() can return None → NEVER < 1."""
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(tmp_path / "no-v2"))
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(tmp_path / "no-v1"))
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: None)
    reset_cpu_cache()
    assert available_cpus() == 1


def test_env_override_forces_value(monkeypatch, tmp_path):
    """YADGAR_AVAILABLE_CPUS positive override wins (ops escape hatch)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "4")
    reset_cpu_cache()
    assert available_cpus() == 4


def test_env_override_zero_is_auto(monkeypatch, tmp_path):
    """YADGAR_AVAILABLE_CPUS=0 is the AUTO sentinel → fall through to detection."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "0")
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(tmp_path / "no-v2"))
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(tmp_path / "no-v1"))
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 9)
    reset_cpu_cache()
    assert available_cpus() == 9  # 0 = auto, not floored to 1


# ---------------------------------------------------------------------------
# Caching + invalidation
# ---------------------------------------------------------------------------


def test_result_is_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(cpu_mod, "_CGROUP_V2_CPU_MAX", str(tmp_path / "no-v2"))
    monkeypatch.setattr(cpu_mod, "_CGROUP_V1_QUOTA", str(tmp_path / "no-v1"))
    calls = {"n": 0}

    def _count():
        calls["n"] += 1
        return 7

    monkeypatch.setattr(cpu_mod.os, "cpu_count", _count)
    reset_cpu_cache()
    assert available_cpus() == 7
    assert available_cpus() == 7
    assert calls["n"] == 1  # second call served from cache
    reset_cpu_cache()
    assert available_cpus() == 7
    assert calls["n"] == 2  # cache invalidated → recomputed


# ---------------------------------------------------------------------------
# Budget derivations — the floor arithmetic that makes ≤2 CPUs byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ncpu, expected_gather",
    [
        (1, 1),  # floor
        (2, 1),  # floor — today's sequential behavior preserved
        (3, 2),
        (4, 2),
        (8, 2),  # gather is provider-fan-out only: 2 providers → never > 2
    ],
)
def test_recall_gather_budget_floor(monkeypatch, ncpu, expected_gather):
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", str(ncpu))
    reset_cpu_cache()
    assert recall_gather_budget() == expected_gather


def test_recall_parallelism_env_forces_sequential(monkeypatch):
    """YADGAR_RECALL_PARALLELISM=1 forces sequential regardless of ncpu (ops escape)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    monkeypatch.setenv("YADGAR_RECALL_PARALLELISM", "1")
    reset_cpu_cache()
    assert recall_gather_budget() == 1


def test_recall_parallelism_env_auto_is_default(monkeypatch):
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    monkeypatch.setenv("YADGAR_RECALL_PARALLELISM", "auto")
    reset_cpu_cache()
    assert recall_gather_budget() == 2


def test_gather_budget_never_exceeds_provider_count(monkeypatch):
    """The gather fans out at most _PROVIDER_COUNT (memory + wiki = 2) arms."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "64")
    reset_cpu_cache()
    assert recall_gather_budget() <= cpu_mod._PROVIDER_COUNT
