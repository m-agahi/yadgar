"""Tests for v5.6.7 PR-I: background loop heartbeat gauges + error counters.

TDD: these tests MUST fail before implementation is added.

Test plan:
1. loop_heartbeat sets yadgar_loop_last_run_unix_timestamp within 1s of time.time().
2. loop_record_exception increments both loop counter AND PR-H global counter.
3. QueueDrainer.run one iteration — heartbeat gauge advances.
4. Drainer step raises — loop error counter AND PR-H global counter both increment.
5. sample_system_metrics called directly — heartbeat gauge updates.
6. SSE stream yield once — yadgar_loop_last_run_unix_timestamp{loop=sse_event_stream} updates (Option A).
7. Helpers are exception-safe: broken Prometheus state must not propagate.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. loop_heartbeat sets gauge within 1 second of time.time()
# ---------------------------------------------------------------------------


def test_loop_heartbeat_sets_gauge():
    """loop_heartbeat() sets yadgar_loop_last_run_unix_timestamp to ~now."""
    from yadgar.metrics import loop_heartbeat, yadgar_loop_last_run_unix_timestamp

    before = time.time()
    loop_heartbeat("test_loop")
    after = time.time()

    value = yadgar_loop_last_run_unix_timestamp.labels(loop="test_loop")._value.get()
    assert before - 1 <= value <= after + 1, f"Expected gauge value ~{before:.1f}, got {value}"


# ---------------------------------------------------------------------------
# 2. loop_record_exception increments BOTH counters
# ---------------------------------------------------------------------------


def test_loop_record_exception_increments_both_counters():
    """loop_record_exception() increments loop counter AND PR-H global counter."""
    from yadgar.metrics import (
        loop_record_exception,
        yadgar_exception_total,
        yadgar_loop_errors_total,
    )

    exc = ValueError("test error")

    before_loop = _read_counter(
        yadgar_loop_errors_total, {"loop": "test_loop", "error_type": "ValueError"}
    )
    before_global = _read_counter(
        yadgar_exception_total, {"location": "loop.test_loop", "error_type": "ValueError"}
    )

    loop_record_exception("test_loop", exc)

    after_loop = _read_counter(
        yadgar_loop_errors_total, {"loop": "test_loop", "error_type": "ValueError"}
    )
    after_global = _read_counter(
        yadgar_exception_total, {"location": "loop.test_loop", "error_type": "ValueError"}
    )

    assert after_loop - before_loop == 1, "Loop error counter did not increment by 1"
    assert after_global - before_global == 1, "PR-H global counter did not increment by 1"


# ---------------------------------------------------------------------------
# 3. QueueDrainer.run one iteration — heartbeat gauge advances
# ---------------------------------------------------------------------------


def test_drainer_run_updates_heartbeat(tmp_path):
    """QueueDrainer.run() calls loop_heartbeat('queue_drainer') each iteration."""
    from yadgar.file_queue import QueueDrainer
    from yadgar.file_queue.queue import FileQueue
    from yadgar.metrics import yadgar_loop_last_run_unix_timestamp

    queue = FileQueue(base_dir=tmp_path)
    stop_event = threading.Event()

    # Patch _drain_once to do nothing and stop after first call
    call_count = 0

    def fake_drain_once():
        nonlocal call_count
        call_count += 1
        stop_event.set()  # stop the drainer after first iteration
        return 0

    before = time.time()
    drainer = QueueDrainer(queue=queue, storage_factory=lambda: None, drain_interval=0.01)
    drainer._drain_once = fake_drain_once
    drainer._stop_event = stop_event

    drainer.start()
    drainer.join(timeout=2.0)

    assert call_count >= 1, "QueueDrainer did not execute an iteration"

    value = yadgar_loop_last_run_unix_timestamp.labels(loop="queue_drainer")._value.get()
    assert value >= before - 1, (
        f"Heartbeat gauge not updated: got {value}, expected >= {before - 1:.1f}"
    )


# ---------------------------------------------------------------------------
# 4. Drainer step raises — both error counters increment
# ---------------------------------------------------------------------------


def test_drainer_exception_increments_counters(tmp_path):
    """When QueueDrainer._drain_once raises, loop and global error counters both increment."""
    from yadgar.file_queue import QueueDrainer
    from yadgar.file_queue.queue import FileQueue
    from yadgar.metrics import yadgar_exception_total, yadgar_loop_errors_total

    queue = FileQueue(base_dir=tmp_path)
    stop_event = threading.Event()
    call_count = 0

    def fake_drain_once_raises():
        nonlocal call_count
        call_count += 1
        stop_event.set()
        raise RuntimeError("simulated drain failure")

    drainer = QueueDrainer(queue=queue, storage_factory=lambda: None, drain_interval=0.01)
    drainer._drain_once = fake_drain_once_raises
    drainer._stop_event = stop_event

    before_loop = _read_counter(
        yadgar_loop_errors_total, {"loop": "queue_drainer", "error_type": "RuntimeError"}
    )
    before_global = _read_counter(
        yadgar_exception_total, {"location": "loop.queue_drainer", "error_type": "RuntimeError"}
    )

    drainer.start()
    drainer.join(timeout=2.0)

    after_loop = _read_counter(
        yadgar_loop_errors_total, {"loop": "queue_drainer", "error_type": "RuntimeError"}
    )
    after_global = _read_counter(
        yadgar_exception_total, {"location": "loop.queue_drainer", "error_type": "RuntimeError"}
    )

    assert after_loop - before_loop >= 1, (
        "Loop error counter not incremented after drainer exception"
    )
    assert after_global - before_global >= 1, (
        "PR-H global counter not incremented after drainer exception"
    )


# ---------------------------------------------------------------------------
# 5. sample_system_metrics — heartbeat gauge updates
# ---------------------------------------------------------------------------


def test_metrics_sampler_updates_heartbeat():
    """Calling sample_system_metrics() directly updates metrics_sampler heartbeat gauge.

    sample_system_metrics uses /proc files (not psutil). The heartbeat fires at
    the very start of the function, before any /proc read that might fail.
    We call with a real pid (our own) and a fake db_path. All non-heartbeat
    paths are wrapped in try/except inside the function — safe to call.
    """
    import os

    import yadgar.graph_api as ga
    from yadgar.metrics import yadgar_loop_last_run_unix_timestamp

    before = time.time()
    # Call with our own PID so /proc reads work; db_path is non-existent but
    # all db_path usage inside is guarded by try/except.
    try:
        ga.sample_system_metrics(pid=os.getpid(), db_path="/tmp/fake_yadgar_test.db")
    except Exception:
        # Non-heartbeat failures are acceptable here
        pass

    value = yadgar_loop_last_run_unix_timestamp.labels(loop="metrics_sampler")._value.get()
    assert value >= before - 1, (
        f"metrics_sampler heartbeat not updated: got {value}, expected >= {before - 1:.1f}"
    )


# ---------------------------------------------------------------------------
# 6. SSE stream yield — sse_event_stream heartbeat updates (Option A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_updates_heartbeat():
    """SSE event stream yield updates yadgar_loop_last_run_unix_timestamp{loop=sse_event_stream}."""
    from yadgar.metrics import yadgar_loop_last_run_unix_timestamp

    # Build a minimal mock request
    mock_request = MagicMock()
    mock_request.query_params.get.return_value = "0"
    # First call: not disconnected; second call: disconnected (to stop the generator)
    mock_request.is_disconnected = AsyncMock(side_effect=[False, True])

    before = time.time()

    # Import the SSE generator
    from yadgar.server import http as http_module

    # Patch _st and _vdh to avoid needing real state
    with (
        patch.object(http_module, "_st", _make_fake_st()),
        patch.object(http_module, "_vdh", _make_fake_vdh(), create=True),
    ):
        gen = http_module._make_event_stream(mock_request)
        try:
            # Advance the generator one or two steps
            await asyncio.wait_for(anext_or_stop(gen), timeout=2.0)
        except StopAsyncIteration, TimeoutError:
            pass

    value = yadgar_loop_last_run_unix_timestamp.labels(loop="sse_event_stream")._value.get()
    assert value >= before - 1, (
        f"SSE heartbeat not updated: got {value}, expected >= {before - 1:.1f}"
    )


# ---------------------------------------------------------------------------
# 7. Helpers never raise when Prometheus state is broken
# ---------------------------------------------------------------------------


def test_helpers_never_raise_on_broken_prometheus():
    """loop_heartbeat and loop_record_exception must not propagate exceptions."""
    from yadgar.metrics import loop_heartbeat, loop_record_exception

    # Patch the gauge .labels(...) to raise
    with patch("yadgar.metrics.yadgar_loop_last_run_unix_timestamp") as mock_gauge:
        mock_gauge.labels.side_effect = RuntimeError("broken prometheus")
        # Must not raise
        loop_heartbeat("broken_test")

    # Patch the counter .labels(...) to raise
    with patch("yadgar.metrics.yadgar_loop_errors_total") as mock_counter:
        mock_counter.labels.side_effect = RuntimeError("broken counter")
        # Must not raise — even with broken gauge + counter
        loop_record_exception("broken_test", ValueError("x"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_counter(counter_metric, labels: dict) -> float:
    """Read current value of a labeled counter, returning 0.0 if not yet seen."""
    try:
        return counter_metric.labels(**labels)._value.get()
    except Exception:
        return 0.0


class AsyncMock:
    """Minimal async mock for is_disconnected() calls."""

    def __init__(self, side_effect):
        self._side_effect = list(side_effect)
        self._idx = 0

    def __call__(self, *args, **kwargs):
        return self._coro()

    async def _coro(self):
        if self._idx < len(self._side_effect):
            val = self._side_effect[self._idx]
            self._idx += 1
            return val
        return True  # default: disconnected


async def anext_or_stop(gen):
    """Advance async generator, returning None on StopAsyncIteration."""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return None


def _make_fake_st():
    """Return a minimal fake _st object for SSE test."""
    fake = MagicMock()
    fake._event_queue = []
    fake._system_metrics_cache = {}
    fake._metrics_lock = threading.Lock()
    return fake


def _make_fake_vdh():
    """Return a minimal fake _vdh module reference for SSE test."""
    fake = MagicMock()
    fake._health_cache = None
    return fake
