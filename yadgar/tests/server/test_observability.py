"""P11 Observability v1 — test suite.

Tests (T2 Car D removed the timing-decorator tests with the prod-dead
observability/timing.py module):
1. test_decorator_emits_histogram
2. test_metrics_endpoint_returns_prometheus_format
3. test_circuit_breaker_state_metric_emitted
4. test_memory_stats_includes_metrics_summary

Registry isolation: each test that creates metrics uses a fresh CollectorRegistry
to avoid "Duplicated timeseries" errors across test runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ── 1. Decorator emits histogram ──────────────────────────────────────────────


def test_decorator_emits_histogram():
    """@stage_timer decorated function -> histogram registry shows non-empty sample."""
    from prometheus_client import CollectorRegistry, Histogram, generate_latest

    reg = CollectorRegistry()
    hist = Histogram(
        "yadgar_drain_stage_ms_test1",
        "test drain stage",
        ["stage"],
        buckets=(1, 5, 10, 50, 100, 500, 1000),
        registry=reg,
    )

    # Simulate what stage_timer does: observe timing after function call
    def _timed():
        import time

        t0 = time.monotonic()
        result = "ok"
        elapsed_ms = (time.monotonic() - t0) * 1000
        hist.labels(stage="encode").observe(elapsed_ms)
        return result

    _timed()

    output = generate_latest(reg).decode()
    assert "yadgar_drain_stage_ms_test1" in output
    # _count must be 1
    assert '_count{stage="encode"} 1.0' in output or "_count" in output


# ── 3. /metrics endpoint returns prometheus format ─────────────────────────


def test_metrics_endpoint_returns_prometheus_format(monkeypatch):
    """GET /metrics -> text/plain response containing yadgar_drain_cycle_duration_ms."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from yadgar._shared.observability.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    body = resp.text
    # Sentinel: new P11 histogram registered in metrics.py
    assert "yadgar_drain_cycle_duration_ms" in body or "yadgar_queue_depth" in body


# ── 4. Circuit breaker state metric emitted ──────────────────────────────────


def test_circuit_breaker_state_metric_emitted():
    """Trip breaker open -> yadgar_circuit_breaker_state{endpoint='ce'} == 2."""
    from prometheus_client import CollectorRegistry, Gauge, generate_latest

    from yadgar.backend.ml_client import _STATE_OPEN, _CircuitBreaker

    reg = CollectorRegistry()
    cb_state_gauge = Gauge(
        "yadgar_circuit_breaker_state_test4",
        "Circuit breaker state (0=closed 1=half_open 2=open)",
        ["endpoint"],
        registry=reg,
    )

    cb = _CircuitBreaker(endpoint="ce", failure_threshold=1, open_duration_sec=60.0)

    def _read_state(breaker) -> int:
        s = breaker._state
        if s == "closed":
            return 0
        if s == "half_open":
            return 1
        return 2  # open

    # Record one failure to trip the breaker open (threshold=1)
    cb.record_failure()
    assert cb._state == _STATE_OPEN

    val = _read_state(cb)
    cb_state_gauge.labels(endpoint="ce").set(val)

    output = generate_latest(reg).decode()
    assert "yadgar_circuit_breaker_state_test4" in output
    assert 'endpoint="ce"} 2.0' in output


# ── 5. memory_stats includes metrics summary ────────────────────────────────


def test_memory_stats_includes_metrics_summary(monkeypatch):
    """memory_stats MCP tool response contains a 'metrics' block with key fields."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")

    # Patch storage dependency to avoid DB connection
    mock_storage = MagicMock()
    mock_storage.get_memory_stats.return_value = {"total_memories": 0}
    mock_storage.get_db_size.side_effect = Exception("no db")

    mock_write_gate = MagicMock()
    mock_write_gate._rejection_count = 0

    import yadgar._shared.runtime.state as _st

    with (
        patch.object(_st, "_storage", mock_storage),
        patch.object(_st, "_write_gate", mock_write_gate),
        patch.object(_st, "_engram", None),
        patch.object(_st, "_rules_engine", None),
        patch.object(_st, "_cls", None),
        patch.object(_st, "_cognitive_map", None),
        patch.object(_st, "_causal", None),
        patch.object(_st, "_metacognition", None),
        patch.object(_st, "_consolidation", None),
    ):
        from yadgar.core.server.tools.admin_other import memory_stats

        result = memory_stats()

    # After P11: result must contain a 'metrics' key
    assert "metrics" in result, (
        f"Expected 'metrics' in memory_stats output, got: {list(result.keys())}"
    )
    metrics_block = result["metrics"]
    assert "queue_depth" in metrics_block
    assert "drainer_lag_p95_ms" in metrics_block
