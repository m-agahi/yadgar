"""v5.6.7 PR-A: process metrics bridge — unit tests.

Covers:
1. After sample_system_metrics(), yadgar_process_rss_bytes gauge == rss_bytes in cache.
2. Same for yadgar_process_cpu_percent and yadgar_process_open_fds.
3. GC callback: gc.collect() increments yadgar_python_gc_duration_ms histogram count.
4. Idempotency: gc callback registered only once even if module is imported multiple times.
"""

from __future__ import annotations

import gc
import importlib


def _gauge_value(gauge) -> float:
    """Read current value from a prometheus_client Gauge (unlabelled)."""
    samples = list(gauge.collect()[0].samples)
    return samples[0].value


def _histogram_count(histogram, generation: str = "0") -> float:
    """Total _count for a labelled Histogram across all samples with matching generation."""
    total = 0.0
    for family in histogram.collect():
        for sample in family.samples:
            if sample.name.endswith("_count") and sample.labels.get("generation") == generation:
                total += sample.value
    return total


class TestProcessGaugeBridge:
    """sample_system_metrics() must update the three process Gauges."""

    def test_rss_bytes_gauge_matches_cache(self, tmp_path) -> None:
        """yadgar_process_rss_bytes must equal rss_bytes written to _metrics_cache."""
        from yadgar._shared.observability.metrics import yadgar_process_rss_bytes
        from yadgar.core.daemon import system_metrics as graph_api

        # Reset cache and prev-CPU state so test is deterministic.
        graph_api._metrics_cache = {}
        graph_api._prev_cpu_ticks = 0
        graph_api._prev_cpu_time = 0.0

        graph_api.sample_system_metrics(pid=1, db_path=str(tmp_path))

        cache_rss = graph_api._metrics_cache.get("rss_bytes")
        assert cache_rss is not None, "rss_bytes key must be present in _metrics_cache"
        assert cache_rss > 0, "rss_bytes must be a positive integer"
        assert _gauge_value(yadgar_process_rss_bytes) == cache_rss

    def test_cpu_percent_gauge_matches_cache(self, tmp_path) -> None:
        """yadgar_process_cpu_percent must equal daemon_cpu_pct written to _metrics_cache.

        We force a known non-zero cpu_pct via mocked /proc reads so the test
        catches the unwired state even when cpu_pct happens to be 0.0.
        """
        import time
        from unittest.mock import patch

        from yadgar._shared.observability.metrics import yadgar_process_cpu_percent
        from yadgar.core.daemon import system_metrics as graph_api

        graph_api._metrics_cache = {}
        # Prime prev state so delta calculation runs on next call.
        graph_api._prev_cpu_ticks = 100
        graph_api._prev_cpu_time = time.monotonic() - 1.0  # 1 second ago

        # Fake /proc/1/stat: fields 13+14 = 200 ticks → delta=100 over ~1s → cpu_pct > 0.
        fake_stat = " ".join(["0"] * 13 + ["150", "50"] + ["0"] * 20)
        # Fake /proc/1/status: minimal
        fake_status = "VmRSS: 10240 kB\nThreads: 4\n"

        # Bound BEFORE builtins.open is patched below. The passthrough arm used to
        # call the module-global `open`, which by then IS the mock — so any path
        # other than the two faked ones (e.g. /proc/meminfo, opened later in the
        # same sample) recursed into the mock until RecursionError. That error was
        # invisible because `_sample_meminfo`'s caller caught bare `Exception` and
        # zeroed the RAM fields; narrowing that handler surfaced it (ADR-0465).
        _real_open = open

        def _fake_open(path, *args, **kwargs):
            import io

            if "stat" in str(path) and "/proc/1" in str(path):
                return io.StringIO(fake_stat)
            if "status" in str(path) and "/proc/1" in str(path):
                return io.StringIO(fake_status)
            return _real_open(path, *args, **kwargs)  # noqa: SIM115

        with patch("builtins.open", side_effect=_fake_open):
            graph_api.sample_system_metrics(pid=1, db_path=str(tmp_path))

        cache_cpu = graph_api._metrics_cache.get("daemon_cpu_pct")
        assert cache_cpu is not None, "daemon_cpu_pct key must be present in _metrics_cache"
        assert cache_cpu > 0, "mocked cpu_pct must be > 0 (delta=100 ticks over 1s)"
        assert _gauge_value(yadgar_process_cpu_percent) == cache_cpu

    def test_open_fds_gauge_matches_cache(self, tmp_path) -> None:
        """yadgar_process_open_fds must equal open_fds written to _metrics_cache."""
        from yadgar._shared.observability.metrics import yadgar_process_open_fds
        from yadgar.core.daemon import system_metrics as graph_api

        graph_api._metrics_cache = {}

        graph_api.sample_system_metrics(pid=1, db_path=str(tmp_path))

        cache_fds = graph_api._metrics_cache.get("open_fds")
        assert cache_fds is not None, "open_fds key must be present in _metrics_cache"
        assert cache_fds > 0, "open_fds must be > 0 (at least stdin/stdout/stderr)"
        assert _gauge_value(yadgar_process_open_fds) == cache_fds


class TestGcCallbackBridge:
    """gc callback must instrument yadgar_python_gc_duration_ms histogram."""

    def test_gc_collect_increments_histogram(self) -> None:
        """After gc.collect(0), the histogram count for generation=0 must be >= 1."""
        # Ensure graph_api is imported (registers callback at module level).
        import yadgar.core.daemon.system_metrics  # noqa: F401
        from yadgar._shared.observability.metrics import yadgar_python_gc_duration_ms

        before = _histogram_count(yadgar_python_gc_duration_ms, generation="0")
        gc.collect(0)
        after = _histogram_count(yadgar_python_gc_duration_ms, generation="0")

        assert after > before, (
            f"gc.collect(0) did not increment histogram count (before={before}, after={after})"
        )

    def test_gc_callback_idempotent(self) -> None:
        """Importing graph_api multiple times must not register _gc_callback twice."""
        import yadgar.core.daemon.system_metrics as ga

        # Force a reimport via importlib.reload.
        importlib.reload(ga)
        importlib.reload(ga)

        count = sum(
            1
            for cb in gc.callbacks
            if getattr(cb, "__name__", None) == "_gc_callback"
            or getattr(cb, "__qualname__", "").endswith("_gc_callback")
        )
        assert count == 1, (
            f"Expected exactly 1 _gc_callback in gc.callbacks, found {count}. "
            f"gc.callbacks = {gc.callbacks!r}"
        )
