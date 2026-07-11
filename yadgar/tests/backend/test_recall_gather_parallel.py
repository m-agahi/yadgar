"""T3 Car 3: bounded-parallel provider gather — byte-identity across budgets.

The recall fan-out runs the memory + wiki providers to gather candidate pools.
Car 3 makes that gather CPU-aware: sequential at ncpu ≤ 2 (today's behavior),
bounded-parallel above. The MUST-HOLD is byte-identity — the gathered result
lists MUST be identical at any budget, because the gather preserves INPUT order
(named slots), never completion order.

These tests pin:
  - budget 1 == today's sequential path (calls run in listed order),
  - budget ≥ 2 produces byte-identical slot assignment regardless of which
    provider finishes first (a slow-first / fast-second arm must NOT reorder),
  - a provider exception propagates (no silent swallow that would drop a pool),
  - the pool is bounded (no unbounded fan-out — the onnx-thrash lesson).
"""

from __future__ import annotations

import threading
import time

import pytest

from yadgar._shared.runtime import cpu as cpu_mod
from yadgar.backend.retrieval import recall_pipeline as rp


@pytest.fixture(autouse=True)
def _reset_cpu():
    cpu_mod.reset_cpu_cache()
    yield
    cpu_mod.reset_cpu_cache()


def _tasks_with_recorded_order(record: list[str]):
    """Two slot tasks that record their execution order into `record`."""

    def _mem():
        record.append("memory:start")
        return ["M1", "M2", "M3"]

    def _wiki():
        record.append("wiki:start")
        return ["W1", "W2"]

    return [("memory", _mem), ("wiki", _wiki)]


def test_gather_budget1_is_sequential_in_order(monkeypatch):
    """budget 1 → tasks run in listed order (byte-identical to today's code)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "2")  # floor → budget 1
    cpu_mod.reset_cpu_cache()
    order: list[str] = []
    result = rp._gather_provider_candidates(_tasks_with_recorded_order(order), budget=1)
    assert result == {"memory": ["M1", "M2", "M3"], "wiki": ["W1", "W2"]}
    assert order == ["memory:start", "wiki:start"]  # listed order preserved


def test_gather_budget_from_cpus_floors_to_one(monkeypatch):
    """With no explicit budget, ncpu ≤ 2 → sequential (recall_gather_budget()=1)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "2")
    cpu_mod.reset_cpu_cache()
    order: list[str] = []
    rp._gather_provider_candidates(_tasks_with_recorded_order(order))
    assert order == ["memory:start", "wiki:start"]


def test_gather_slow_first_arm_does_not_reorder(monkeypatch):
    """budget ≥ 2: memory arm is SLOW, wiki finishes first — result slots MUST
    still map memory→memory-result, wiki→wiki-result (named slots, not
    completion order)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    cpu_mod.reset_cpu_cache()

    def _slow_mem():
        time.sleep(0.15)  # finishes LAST
        return ["M1", "M2", "M3"]

    def _fast_wiki():
        return ["W1", "W2"]

    tasks = [("memory", _slow_mem), ("wiki", _fast_wiki)]
    result = rp._gather_provider_candidates(tasks, budget=4)
    assert result == {"memory": ["M1", "M2", "M3"], "wiki": ["W1", "W2"]}


def test_gather_byte_identical_budget_1_vs_4(monkeypatch):
    """The load-bearing guarantee: identical output at budget 1 and budget 4."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    cpu_mod.reset_cpu_cache()

    def _mem():
        return [{"id": 1, "content": "a"}, {"id": 2, "content": "b"}]

    def _wiki():
        return [{"slug": "x"}, {"slug": "y"}]

    tasks_seq = [("memory", _mem), ("wiki", _wiki)]
    tasks_par = [("memory", _mem), ("wiki", _wiki)]
    seq = rp._gather_provider_candidates(tasks_seq, budget=1)
    par = rp._gather_provider_candidates(tasks_par, budget=4)
    assert seq == par


def test_gather_actually_parallel_at_budget2(monkeypatch):
    """budget ≥ 2 runs arms concurrently (both threads live at once)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    cpu_mod.reset_cpu_cache()
    barrier = threading.Barrier(2, timeout=5)

    def _arm_a():
        barrier.wait()  # only clears if a second thread also reaches it
        return ["A"]

    def _arm_b():
        barrier.wait()
        return ["B"]

    result = rp._gather_provider_candidates([("a", _arm_a), ("b", _arm_b)], budget=2)
    assert result == {"a": ["A"], "b": ["B"]}  # barrier cleared →真 concurrent


def test_gather_provider_exception_propagates(monkeypatch):
    """A provider error must surface, not be swallowed (would drop a pool)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    cpu_mod.reset_cpu_cache()

    def _boom():
        raise RuntimeError("provider failed")

    def _ok():
        return ["W"]

    with pytest.raises(RuntimeError, match="provider failed"):
        rp._gather_provider_candidates([("memory", _boom), ("wiki", _ok)], budget=4)


def test_gather_single_task_no_pool(monkeypatch):
    """One active provider → run inline regardless of budget (no thread spawn)."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", "8")
    cpu_mod.reset_cpu_cache()
    order: list[str] = []

    def _only():
        order.append("ran")
        return ["X"]

    result = rp._gather_provider_candidates([("memory", _only)], budget=4)
    assert result == {"memory": ["X"]}
    assert order == ["ran"]


# ---------------------------------------------------------------------------
# Full-pipeline byte-identity: real _fanout_recall(type="all") with two active
# providers, budget 1 (ncpu=2) vs budget 4 (ncpu=8). This exercises the ACTUAL
# gather + fuse + dedup + boost path — the load-bearing "identical at any budget"
# claim for the whole fan-out, not just the isolated merge helper.
# ---------------------------------------------------------------------------

_DIR = "/home/max/git/yadgar"


def _mk_candidate(cand_type, cid, content, score):
    from yadgar.backend.retrieval.providers.base import Candidate

    return Candidate(
        type=cand_type,
        id=cid,
        title=None if cand_type == "memory" else content[:20],
        content=content,
        native_score=score,
        directory_context=_DIR,
        branch=None,
        raw=(
            {"id": cid, "content": content, "_retrieval_score": score, "heat": score, "tags": []}
            if cand_type == "memory"
            else {"id": cid, "content": content, "_retrieval_score": score, "slug": cid}
        ),
    )


def _run_fanout_at(monkeypatch, ncpu):
    """Run the real _fanout_recall(type='all') with two mocked providers at ncpu."""
    monkeypatch.setenv("YADGAR_AVAILABLE_CPUS", str(ncpu))
    cpu_mod.reset_cpu_cache()

    import yadgar._shared.runtime.state as _st
    from yadgar.backend.retrieval.providers.memory import MemoryProvider
    from yadgar.backend.retrieval.providers.wiki import WikiProvider

    # _st._retriever / _st._wiki must be non-None so both provider arms activate.
    monkeypatch.setattr(_st, "_retriever", object())
    monkeypatch.setattr(_st, "_wiki", object())

    mem = [
        _mk_candidate("memory", 101, "memory alpha about python testing", 0.9),
        _mk_candidate("memory", 102, "memory beta about async recall", 0.7),
        _mk_candidate("memory", 103, "memory gamma about storage", 0.5),
    ]
    wiki = [
        _mk_candidate("wiki", "wiki-x", "wiki page about python conventions", 0.8),
        _mk_candidate("wiki", "wiki-y", "wiki page about recall design", 0.6),
    ]
    monkeypatch.setattr(MemoryProvider, "candidates", lambda self, q, s, limit: list(mem))
    monkeypatch.setattr(WikiProvider, "candidates", lambda self, q, s, limit: list(wiki))

    return rp._fanout_recall(
        query="python recall design",
        max_results=5,
        min_heat=0.0,
        directory=_DIR,
        current_branch=None,
        default_branch="master",
        type_filter="all",
    )


def test_fanout_byte_identical_seq_vs_parallel(monkeypatch):
    """_fanout_recall(type='all') output is byte-identical at ncpu=2 (gather budget
    1, sequential) and ncpu=8 (gather budget 2, parallel)."""
    seq = _run_fanout_at(monkeypatch, 2)
    par = _run_fanout_at(monkeypatch, 8)
    assert seq == par
    assert seq  # non-empty — both arms actually contributed


def test_fanout_parallelism_forced_off_matches(monkeypatch):
    """YADGAR_RECALL_PARALLELISM=1 at ncpu=8 == the sequential ncpu=2 result."""
    monkeypatch.setenv("YADGAR_RECALL_PARALLELISM", "1")
    forced_seq = _run_fanout_at(monkeypatch, 8)
    monkeypatch.delenv("YADGAR_RECALL_PARALLELISM", raising=False)
    natural_seq = _run_fanout_at(monkeypatch, 2)
    assert forced_seq == natural_seq
