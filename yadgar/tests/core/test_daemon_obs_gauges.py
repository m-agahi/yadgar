"""Tests for daemon observability gauges (#80).

TDD: these tests MUST fail before implementation is added.

Background: the armed offload core SIGKILLs under concurrent load — a ~64s
event-loop FREEZE that no metric captured (not even /health/live). These gauges
make the next freeze diagnosable.

Gauges under test:
1. yadgar_event_loop_lag_seconds — loop-lag monitor (histogram + max-gauge).
   A frozen loop → huge lag observation that SURVIVES a post-recovery scrape
   (histogram buckets are cumulative; the max-gauge is a high-water mark).
2. yadgar_tool_pool_inflight / _saturated / _max — pool occupancy from _offload.
3. /metrics renders all of the above.

Test plan:
- (a) loop-lag monitor reports near-0 at idle and a SPIKE when the loop is
      blocked (time.sleep on the loop → recorded lag rises).
- (b) pool gauges reflect _offload state (set _inflight/_POOL_MAX, assert gauge).
- (c) gauges render in the /metrics text.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# (a) Loop-lag monitor
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_loop_lag_max():
    """Reset the process-global monotonic max gauge before each test.

    yadgar_event_loop_lag_max_seconds is a high-water mark that persists across
    tests; without this reset the spike test's ~1.0s value leaks into the idle
    test (xdist load-balancing can run spike-before-idle on one worker → flake).
    """
    from yadgar._shared.observability.metrics import yadgar_event_loop_lag_max_seconds

    yadgar_event_loop_lag_max_seconds.set(0)
    yield


def test_loop_lag_monitor_idle_is_near_zero():
    """At idle, the loop-lag monitor records lag close to 0 (max-gauge low)."""
    from yadgar._shared.observability.metrics import (
        start_loop_lag_monitor,
        stop_loop_lag_monitor,
        yadgar_event_loop_lag_max_seconds,
    )

    async def _run() -> float:
        loop = asyncio.get_running_loop()
        # Tight probe interval so the test completes fast.
        task = start_loop_lag_monitor(loop, interval=0.05)
        try:
            # Let several idle probes fire — loop is free, lag stays small.
            await asyncio.sleep(0.4)
            return yadgar_event_loop_lag_max_seconds._value.get()
        finally:
            await stop_loop_lag_monitor(task)

    max_lag = asyncio.run(_run())
    # Idle lag should be well under the probe interval-plus-slack. Generous
    # ceiling to avoid flakiness on a loaded CI box, but far below the spike.
    assert max_lag < 0.5, f"Idle loop lag unexpectedly high: {max_lag}"


def test_loop_lag_monitor_spikes_when_loop_blocked():
    """Blocking the loop (time.sleep) drives recorded lag up — would catch a freeze.

    The histogram observation and the max-gauge both reflect the spike; the
    histogram is the diagnosable-after-recovery signal.
    """
    from yadgar._shared.observability.metrics import (
        start_loop_lag_monitor,
        stop_loop_lag_monitor,
        yadgar_event_loop_lag_max_seconds,
        yadgar_event_loop_lag_seconds,
    )

    def _hist_bucket_count(le: str) -> float:
        """Cumulative observation count at-or-below the `le` bound."""
        for sample in yadgar_event_loop_lag_seconds.collect()[0].samples:
            if sample.name.endswith("_bucket") and sample.labels.get("le") == le:
                return sample.value
        return 0.0

    async def _run() -> tuple[float, float, float]:
        loop = asyncio.get_running_loop()
        before_high = _hist_bucket_count("1.0")
        task = start_loop_lag_monitor(loop, interval=0.05)
        try:
            await asyncio.sleep(0.2)  # a few clean probes
            # Block the loop synchronously — the probe callback cannot fire,
            # so the NEXT probe records a large lag (~the block duration).
            time.sleep(1.0)
            await asyncio.sleep(0.3)  # let the post-block probe fire + record
            return (
                yadgar_event_loop_lag_max_seconds._value.get(),
                before_high,
                _hist_bucket_count("1.0"),
            )
        finally:
            await stop_loop_lag_monitor(task)

    max_lag, before_high, after_high = asyncio.run(_run())
    # The 1.0s block must show up as a lag spike far above the idle ceiling.
    assert max_lag >= 0.5, f"Expected loop-lag spike >=0.5s after block, got {max_lag}"
    # Histogram buckets are cumulative: an observation > 1.0s does NOT increment
    # the le="1.0" bucket. So a freeze leaves le="1.0" flat while the high/+Inf
    # buckets climb — the exact property that lets a post-recovery scrape still
    # see the spike. Assert the >1.0s observation overflowed past le="1.0".
    after_inf = _hist_bucket_count("+Inf")
    assert after_inf > before_high, "histogram recorded no new observations"
    # The spike observation must NOT have landed in le<=1.0 (it was ~1.0s lag,
    # and overflow into higher buckets is what survives recovery).
    assert after_inf - after_high >= 1, (
        f"expected >=1 observation above the 1.0s bucket (inf={after_inf}, le1.0={after_high})"
    )


def test_loop_lag_monitor_cancels_cleanly():
    """stop_loop_lag_monitor cancels the probe task without raising."""
    from yadgar._shared.observability.metrics import start_loop_lag_monitor, stop_loop_lag_monitor

    async def _run() -> bool:
        loop = asyncio.get_running_loop()
        task = start_loop_lag_monitor(loop, interval=0.05)
        await asyncio.sleep(0.1)
        await stop_loop_lag_monitor(task)
        return task.cancelled() or task.done()

    assert asyncio.run(_run()) is True


# ---------------------------------------------------------------------------
# (b) Pool gauges reflect _offload state
# ---------------------------------------------------------------------------


def test_pool_gauges_reflect_offload_state(monkeypatch):
    """_collect_pool_stats() sets inflight/max/saturated gauges from pool_stats()."""
    import yadgar._shared.runtime.offload as offload
    from yadgar._shared.observability.metrics import (
        _collect_pool_stats,
        yadgar_tool_pool_inflight,
        yadgar_tool_pool_max,
        yadgar_tool_pool_saturated,
    )

    # Force a known pool state. offload_enabled gates pool_saturated(); turn ON.
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setattr(offload, "_POOL_MAX", 8, raising=False)
    monkeypatch.setattr(offload, "_inflight", 3, raising=False)

    _collect_pool_stats()

    assert yadgar_tool_pool_inflight._value.get() == 3
    assert yadgar_tool_pool_max._value.get() == 8
    # inflight (3) < max (8) → not saturated.
    assert yadgar_tool_pool_saturated._value.get() == 0


def test_pool_gauge_saturated_flag(monkeypatch):
    """When pool_saturated() is True, the gauge reads 1."""
    import yadgar._shared.runtime.offload as offload
    from yadgar._shared.observability.metrics import _collect_pool_stats, yadgar_tool_pool_saturated

    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setattr(offload, "pool_saturated", lambda: True, raising=True)

    _collect_pool_stats()

    assert yadgar_tool_pool_saturated._value.get() == 1


# ---------------------------------------------------------------------------
# (c) /metrics renders the new gauges
# ---------------------------------------------------------------------------


def test_new_gauges_render_in_metrics(monkeypatch):
    """/metrics output contains the loop-lag + pool gauge names."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from yadgar._shared.observability.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    client = TestClient(app, raise_server_exceptions=True)
    body = client.get("/metrics").text

    for name in (
        "yadgar_event_loop_lag_seconds",
        "yadgar_event_loop_lag_max_seconds",
        "yadgar_tool_pool_inflight",
        "yadgar_tool_pool_saturated",
        "yadgar_tool_pool_max",
    ):
        assert name in body, f"{name} missing from /metrics output"
