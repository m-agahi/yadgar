"""T3 Car 2 — async side-effects fork: unit tests (RED first).

Covers BOTH halves of the recall side-effect fork:

  A. Core SESSION half (recall_session) — deferred to a single-FIFO-worker
     executor off the tool-response critical path.
  B. Backend DB-WRITE half (the batched boost_memories_access) — deferred to a
     tracked asyncio task, decomposed from the in-place heat mutations (which
     stay inline so the response payload is byte-identical).

Must-holds asserted here:
  - response returns BEFORE side-effect completion (latency off the path);
  - side-effects STILL execute (eventually-consistent);
  - shutdown DRAINS pending work (no lost writes);
  - per-session ORDERING preserved (single FIFO worker);
  - BOUNDING: over the cap the work runs INLINE (backpressure, never piles up);
  - the response payload is byte-identical (backend heat mutation stays inline).
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

# ---------------------------------------------------------------------------
# A. Core session-half fork
# ---------------------------------------------------------------------------


def test_session_fork_defers_off_caller_thread():
    """submit runs the callable on a DIFFERENT thread — caller returns before it finishes."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:
        started = threading.Event()
        release = threading.Event()
        caller_thread = threading.current_thread().name
        ran_thread = {}

        def _work():
            ran_thread["name"] = threading.current_thread().name
            started.set()
            release.wait(5.0)

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            fork.submit_session_side_effect(_work)
            # Caller must not have blocked on the body.
            assert started.wait(2.0), "deferred work never started on the worker"
            release.set()
        fork.drain_session_side_effects(timeout=5.0)
        assert ran_thread["name"] != caller_thread, "work ran on the caller thread, not deferred"
    finally:
        fork.reset_session_executor()


def test_session_fork_disabled_runs_inline():
    """Flag OFF → runs inline on the caller thread (byte-identical to pre-fork)."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:
        caller = threading.current_thread().name
        ran = {}

        def _work():
            ran["thread"] = threading.current_thread().name

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=False):
            fork.submit_session_side_effect(_work)
        assert ran["thread"] == caller, "disabled fork must run inline on caller thread"
    finally:
        fork.reset_session_executor()


def test_session_fork_preserves_fifo_order():
    """Single FIFO worker → submissions execute in submit order (per-session SR chain)."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:
        order: list[int] = []
        lock = threading.Lock()

        def _make(i: int):
            def _w():
                # tiny stagger to expose reordering if the pool were multi-worker
                time.sleep(0.005)
                with lock:
                    order.append(i)

            return _w

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            for i in range(20):
                fork.submit_session_side_effect(_make(i))
        fork.drain_session_side_effects(timeout=10.0)
        assert order == list(range(20)), f"FIFO order violated: {order}"
    finally:
        fork.reset_session_executor()


def test_session_fork_drain_runs_all_pending():
    """drain() blocks until every queued side-effect has executed (no lost writes)."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:
        done = {"n": 0}
        lock = threading.Lock()

        def _w():
            time.sleep(0.01)
            with lock:
                done["n"] += 1

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            for _ in range(15):
                fork.submit_session_side_effect(_w)
            fork.drain_session_side_effects(timeout=10.0)
        assert done["n"] == 15, f"drain dropped pending work: {done['n']}/15"
    finally:
        fork.reset_session_executor()


def test_session_fork_bounded_backpressure_inline():
    """Over the pending cap, submit runs the work INLINE (never unbounded pile-up)."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:
        caller = threading.current_thread().name
        block = threading.Event()
        ran_inline = {"count": 0}

        def _blocker():
            block.wait(10.0)

        inline_threads: list[str] = []

        def _probe():
            inline_threads.append(threading.current_thread().name)

        with (
            patch.object(fork, "_sideeffect_fork_enabled", return_value=True),
            patch.object(fork, "_session_max_pending", return_value=2),
        ):
            # Fill the pending cap with blockers (pending counts submitted-but-not-
            # completed): the first blocker occupies the worker and never completes
            # (both stay counted), so after two submits pending == cap == 2.
            fork.submit_session_side_effect(_blocker)  # pending 1 (on the worker)
            fork.submit_session_side_effect(_blocker)  # pending 2 (== cap, queued)
            # Next submit overflows the cap → must run INLINE on the caller thread.
            fork.submit_session_side_effect(_probe)
            assert inline_threads == [caller], (
                f"overflow did not run inline on caller: {inline_threads}"
            )
            ran_inline["count"] = len(inline_threads)
            block.set()
        fork.drain_session_side_effects(timeout=10.0)
        assert ran_inline["count"] == 1
    finally:
        fork.reset_session_executor()


def test_session_fork_errors_swallowed_not_raised():
    """A raising side-effect must be logged, never propagate to the caller."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:

        def _boom():
            raise RuntimeError("side-effect blew up")

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            # Must not raise here.
            fork.submit_session_side_effect(_boom)
            fork.drain_session_side_effects(timeout=5.0)
    finally:
        fork.reset_session_executor()


def test_session_fork_copies_contextvars():
    """copy_context() is used so OTEL parent context survives the executor boundary."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    fork.reset_session_executor()
    try:
        with (
            patch.object(fork, "_sideeffect_fork_enabled", return_value=True),
            patch.object(
                fork.contextvars, "copy_context", wraps=fork.contextvars.copy_context
            ) as cc,
        ):
            fork.submit_session_side_effect(lambda: None)
            fork.drain_session_side_effects(timeout=5.0)
        assert cc.called, "copy_context() not used — OTEL span parentage would break"
    finally:
        fork.reset_session_executor()


# ---------------------------------------------------------------------------
# B. Backend DB-write fork (async tracked task)
# ---------------------------------------------------------------------------


def test_db_fork_schedules_task_and_drains():
    """schedule_db_write forks a tracked task; drain awaits it (no lost writes)."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    async def _run():
        fork.reset_db_tasks()
        ran = asyncio.Event()

        async def _write():
            await asyncio.sleep(0.01)
            ran.set()

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            scheduled = fork.schedule_db_write(_write())
            assert scheduled is True, "flag ON should schedule a task"
            assert not ran.is_set(), "write ran inline — should be deferred"
            await fork.drain_db_tasks(timeout=5.0)
            assert ran.is_set(), "drain did not run the pending write"
        assert fork.pending_db_tasks() == 0

    asyncio.run(_run())


def test_db_fork_bounded_returns_false_over_cap():
    """Over the in-flight cap, schedule returns False so the caller runs inline."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    async def _run():
        fork.reset_db_tasks()
        gate = asyncio.Event()

        async def _slow():
            await gate.wait()

        with (
            patch.object(fork, "_sideeffect_fork_enabled", return_value=True),
            patch.object(fork, "_db_max_inflight", return_value=1),
        ):
            first = fork.schedule_db_write(_slow())
            assert first is True
            # cap == 1 and one is in flight → next must refuse (caller runs inline)
            refused = _slow()
            second = fork.schedule_db_write(refused)
            assert second is False, "over-cap schedule must return False (backpressure)"
            refused.close()  # caller owns the coro when refused
            gate.set()
            await fork.drain_db_tasks(timeout=5.0)

    asyncio.run(_run())


def test_db_fork_error_swallowed():
    """A DB-write task that raises is logged, never crashes drain."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    async def _run():
        fork.reset_db_tasks()

        async def _boom():
            raise RuntimeError("db write failed")

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            fork.schedule_db_write(_boom())
            # drain must not raise
            await fork.drain_db_tasks(timeout=5.0)
        assert fork.pending_db_tasks() == 0

    asyncio.run(_run())


def test_db_fork_disabled_returns_false():
    """Flag OFF → schedule_db_write refuses (caller keeps the inline write)."""
    import yadgar._shared.runtime.recall_side_effects_fork as fork

    async def _run():
        fork.reset_db_tasks()

        async def _noop():
            return None

        with patch.object(fork, "_sideeffect_fork_enabled", return_value=False):
            coro = _noop()
            scheduled = fork.schedule_db_write(coro)
            assert scheduled is False
            coro.close()  # caller owns it when refused

    asyncio.run(_run())
