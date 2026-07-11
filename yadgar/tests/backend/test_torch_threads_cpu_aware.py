"""T3 Car 3: backend torch intra-op threads are CPU-aware (cheapest CE lever).

At the backend lifespan the process sets ``torch.set_num_threads(N)`` where
N = ``torch_intraop_threads()`` (1 at ncpu ≤ 2 = today's implicit single-thread
inference; ncpu//2 above, reserving the other half for the provider gather arms
so the two budgets compose within ncpu). The call is process-global, set once,
and no-ops gracefully when torch is unavailable.
"""

from __future__ import annotations

import sys
import types

import pytest

from yadgar._shared.runtime import cpu as cpu_mod
from yadgar.backend.embed_service import embed_service as es


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("YADGAR_AVAILABLE_CPUS", raising=False)
    monkeypatch.delenv("YADGAR_RECALL_PARALLELISM", raising=False)
    cpu_mod.reset_cpu_cache()
    yield
    cpu_mod.reset_cpu_cache()


def _fake_torch(record: dict) -> types.ModuleType:
    mod = types.ModuleType("torch")

    def _set_num_threads(n):
        record["threads"] = n

    def _get_num_threads():
        return record.get("threads", 0)

    mod.set_num_threads = _set_num_threads
    mod.get_num_threads = _get_num_threads
    return mod


def test_sets_threads_from_cpu_budget(monkeypatch):
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")  # torch_intraop_threads = 4
    cpu_mod.reset_cpu_cache()
    record: dict = {}
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(record))
    applied = es._configure_torch_threads()
    assert applied == 4
    assert record["threads"] == 4


def test_floors_to_one_at_2_cpus(monkeypatch):
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "2")  # floor → 1 (today's behavior)
    cpu_mod.reset_cpu_cache()
    record: dict = {}
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(record))
    applied = es._configure_torch_threads()
    assert applied == 1
    assert record["threads"] == 1


def test_no_torch_is_graceful_noop(monkeypatch):
    """torch unavailable → returns None, does not raise (backend must still boot)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    cpu_mod.reset_cpu_cache()
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch → ImportError
    assert es._configure_torch_threads() is None
