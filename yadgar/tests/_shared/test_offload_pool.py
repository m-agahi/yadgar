"""Unit tests for the tool-body offload primitive (Fix A, server/_offload.py).

Fast (no daemon, no surreal). They prove:
  - offload runs the body on a NON-loop thread (thread-id assertion);
  - the kill-switch (default OFF) keeps the body inline;
  - the O2 GATE: pool_saturated() reflects TRUE occupancy. A wedged op that
    exceeds the wait_for timeout keeps its slot (worker-side decrement), so once
    the pool is full and nothing completes for > grace, pool_saturated() is True;
  - completion-staleness (NOT "full-since"): a healthy peak that keeps draining is
    never flagged saturated.

OTEL is NOT touched at module scope (no os.environ.setdefault). The pytest env
(make test) controls OTEL_SDK_DISABLED.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from yadgar._shared.runtime import offload as _offload


@pytest.fixture(autouse=True)
def _reset_offload_state(monkeypatch):
    """Each test starts with a fresh pool + clean knobs."""
    _offload.shutdown_pool()
    # reset module occupancy counters
    with _offload._STAT_LOCK:
        _offload._inflight = 0
        _offload._last_completion_ts = 0.0
        _offload._pool_full_since = 0.0
    yield
    _offload.shutdown_pool()


# ---------------------------------------------------------------------------
# Offload mechanism
# ---------------------------------------------------------------------------


async def test_disabled_runs_inline_on_loop_thread(monkeypatch):
    """Default OFF → body runs inline on the calling (loop) thread."""
    monkeypatch.delenv("YADGAR_OFFLOAD_TOOLS", raising=False)
    loop_tid = threading.get_ident()

    def body() -> int:
        return threading.get_ident()

    result = await _offload.run_offloaded(body)
    assert result == loop_tid, "disabled offload must run inline on the loop thread"


async def test_enabled_runs_on_worker_thread(monkeypatch):
    """Enabled → body runs on a worker thread, NOT the loop thread (the whole point)."""
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "4")
    loop_tid = threading.get_ident()

    def body() -> int:
        return threading.get_ident()

    worker_tid = await _offload.run_offloaded(body)
    assert worker_tid != loop_tid, "offloaded body must run off the loop thread"


async def test_enabled_propagates_args_and_result(monkeypatch):
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")

    def add(a, b, *, c):
        return a + b + c

    assert await _offload.run_offloaded(add, 1, 2, c=3) == 6


async def test_enabled_propagates_exception(monkeypatch):
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")

    def boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await _offload.run_offloaded(boom)


async def test_loop_stays_free_while_worker_blocks(monkeypatch):
    """A blocking offloaded body must NOT block the loop — concurrent coroutines run."""
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "4")
    monkeypatch.setenv("YADGAR_TOOL_TIMEOUT_SEC", "10")

    progress = []

    def slow():
        time.sleep(0.5)
        return "done"

    async def ticker():
        for _ in range(5):
            progress.append(time.monotonic())
            await asyncio.sleep(0.05)

    t0 = time.monotonic()
    _, _ = await asyncio.gather(_offload.run_offloaded(slow), ticker())
    # The ticker (loop-side) made progress while the worker slept — it ran to
    # completion in ~0.25s, well under the worker's 0.5s sleep.
    assert len(progress) == 5
    assert progress[-1] - t0 < 0.45, "loop was blocked by the worker sleep"


# ---------------------------------------------------------------------------
# O2 GATE — pool saturation (the audit's must-fix #1)
# ---------------------------------------------------------------------------


async def test_not_saturated_when_disabled(monkeypatch):
    monkeypatch.delenv("YADGAR_OFFLOAD_TOOLS", raising=False)
    assert _offload.pool_saturated() is False


async def test_not_saturated_under_draining_peak(monkeypatch):
    """A healthy peak that keeps completing is NEVER flagged saturated.

    Completion-staleness, not full-since: fire many short ops through a small pool
    so it stays full while draining; each completion resets last_completion_ts.
    """
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "2")
    monkeypatch.setenv("YADGAR_TOOL_TIMEOUT_SEC", "5")
    monkeypatch.setenv("YADGAR_TOOL_SATURATION_GRACE_SEC", "0.5")

    def quick():
        time.sleep(0.02)
        return "ok"

    # 20 quick ops through a 2-worker pool: pool is frequently full, but always
    # draining → never stale → never saturated.
    saturated_observed = False

    async def hammer():
        nonlocal saturated_observed
        tasks = [asyncio.create_task(_offload.run_offloaded(quick)) for _ in range(20)]
        while not all(t.done() for t in tasks):
            if _offload.pool_saturated():
                saturated_observed = True
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)

    await hammer()
    assert saturated_observed is False, "draining peak must not be flagged saturated"


async def test_saturated_when_workers_wedged_past_timeout(monkeypatch):
    """THE GATE + the regression guard for worker-side decrement.

    Wedge N ops that sleep >> the wait_for timeout. Each wait_for fires and frees
    the awaiting coroutine, BUT the worker keeps its slot (worker-side decrement).
    With the pool full and nothing completing for > grace, pool_saturated() must
    become True so /health → 503 → P0 can kill.

    With a (wrong) coroutine-side decrement, _inflight would drop to 0 on timeout
    and this assertion would FAIL — that is the RED→GREEN proof the gate is real.
    """
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "2")
    monkeypatch.setenv("YADGAR_TOOL_TIMEOUT_SEC", "0.3")
    monkeypatch.setenv("YADGAR_TOOL_SATURATION_GRACE_SEC", "0.4")

    release = threading.Event()

    def wedged():
        # Sleeps far past the 0.3s wait_for timeout; only releases at teardown.
        release.wait(timeout=10.0)
        return "eventually"

    # Fire N=2 wedged ops to fill the 2-worker pool. wait_for will time out on
    # both; gather captures the TimeoutErrors so the test continues.
    async def fire():
        return await asyncio.gather(
            _offload.run_offloaded(wedged),
            _offload.run_offloaded(wedged),
            return_exceptions=True,
        )

    results = await fire()
    assert all(isinstance(r, (asyncio.TimeoutError, TimeoutError)) for r in results)

    # Workers are still wedged (slots held). Wait past the saturation grace.
    deadline = time.monotonic() + 2.0
    saturated = False
    while time.monotonic() < deadline:
        if _offload.pool_saturated():
            saturated = True
            break
        await asyncio.sleep(0.05)

    try:
        assert saturated, (
            "pool must report saturated while wedged workers hold all slots "
            "(worker-side decrement); a coroutine-side decrement would fail this"
        )
        stats = _offload.pool_stats()
        assert stats["inflight"] == 2, "true occupancy must still count the wedged workers"
    finally:
        release.set()


async def test_slot_released_after_worker_completes(monkeypatch):
    """Once a wedged worker finally returns, its slot frees and saturation clears."""
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "1")
    monkeypatch.setenv("YADGAR_TOOL_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("YADGAR_TOOL_SATURATION_GRACE_SEC", "0.3")

    release = threading.Event()

    def wedged():
        release.wait(timeout=10.0)
        return "ok"

    r = await asyncio.gather(_offload.run_offloaded(wedged), return_exceptions=True)
    assert isinstance(r[0], (asyncio.TimeoutError, TimeoutError))

    # become saturated
    await asyncio.sleep(0.5)
    assert _offload.pool_saturated() is True

    # release the worker → slot frees, completion stamped
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _offload.pool_saturated():
            break
        await asyncio.sleep(0.05)
    assert _offload.pool_saturated() is False
    assert _offload.pool_stats()["inflight"] == 0
