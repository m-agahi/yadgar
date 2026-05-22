"""V1c viz daemon health endpoint tests (v5.6.0).

Tests:
- GET /api/daemon-health returns 200 + valid JSON shape
- Parser extracts core metrics from /metrics text correctly
- Parser extracts backend metrics from /metrics text correctly
- Missing backend (httpx error) → 200 with backend.unavailable=True
- Missing core is impossible (self-scraped) but parser returns empty on bad text
- CB state mapped correctly (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
- scraped_at field present in response
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures: sample /metrics text for core and backend
# ---------------------------------------------------------------------------

_CORE_METRICS = """# HELP process_resident_memory_bytes RSS bytes.
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes 52428800.0
# HELP process_cpu_seconds_total CPU seconds.
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 3.14
# HELP process_open_fds Open file descriptors.
# TYPE process_open_fds gauge
process_open_fds 42.0
# HELP process_start_time_seconds Process start time.
# TYPE process_start_time_seconds gauge
process_start_time_seconds 1748000000.0
# HELP yadgar_circuit_breaker_state CB state (0=CLOSED 1=HALF_OPEN 2=OPEN).
# TYPE yadgar_circuit_breaker_state gauge
yadgar_circuit_breaker_state{endpoint="/rerank/ce"} 0.0
yadgar_circuit_breaker_state{endpoint="/rerank/nli"} 1.0
yadgar_circuit_breaker_state{endpoint="/rerank/pair"} 2.0
# HELP yadgar_queue_depth Queue depth.
# TYPE yadgar_queue_depth gauge
yadgar_queue_depth{queue="queue"} 7.0
# HELP yadgar_dlq_size DLQ size.
# TYPE yadgar_dlq_size gauge
yadgar_dlq_size 2.0
# HELP yadgar_drainer_lag_ms Drainer lag ms.
# TYPE yadgar_drainer_lag_ms histogram
yadgar_drainer_lag_ms_bucket{le="50.0"} 8.0
yadgar_drainer_lag_ms_bucket{le="+Inf"} 10.0
yadgar_drainer_lag_ms_count 10.0
yadgar_drainer_lag_ms_sum 350.0
# HELP yadgar_log_file_size_bytes Log file size.
# TYPE yadgar_log_file_size_bytes gauge
yadgar_log_file_size_bytes{logger="core"} 102400.0
# HELP yadgar_log_file_rotations_total Log rotations.
# TYPE yadgar_log_file_rotations_total counter
yadgar_log_file_rotations_total{logger="core"} 1.0
# HELP yadgar_log_dropped_total Dropped log records.
# TYPE yadgar_log_dropped_total counter
yadgar_log_dropped_total{logger="core",level="INFO",reason="rate_limit"} 5.0
"""

_BACKEND_METRICS = """# HELP process_resident_memory_bytes RSS bytes.
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes 209715200.0
# HELP process_cpu_seconds_total CPU seconds.
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 12.0
# HELP process_open_fds Open file descriptors.
# TYPE process_open_fds gauge
process_open_fds 18.0
# HELP process_start_time_seconds Process start time.
# TYPE process_start_time_seconds gauge
process_start_time_seconds 1748000100.0
# HELP yadgar_embed_rerank_requests_total Rerank requests.
# TYPE yadgar_embed_rerank_requests_total counter
yadgar_embed_rerank_requests_total{mode="ce"} 100.0
yadgar_embed_rerank_requests_total{mode="nli"} 50.0
# HELP yadgar_embed_rerank_503_total Semaphore-busy 503s.
# TYPE yadgar_embed_rerank_503_total counter
yadgar_embed_rerank_503_total{mode="ce"} 3.0
# HELP yadgar_embed_rerank_duration_seconds Rerank latency.
# TYPE yadgar_embed_rerank_duration_seconds histogram
yadgar_embed_rerank_duration_seconds_bucket{mode="ce",le="0.5"} 90.0
yadgar_embed_rerank_duration_seconds_bucket{mode="ce",le="+Inf"} 100.0
yadgar_embed_rerank_duration_seconds_count{mode="ce"} 100.0
yadgar_embed_rerank_duration_seconds_sum{mode="ce"} 45.0
# HELP yadgar_embed_rerank_semaphore_held Inflight rerank slots.
# TYPE yadgar_embed_rerank_semaphore_held gauge
yadgar_embed_rerank_semaphore_held{mode="ce"} 1.0
yadgar_embed_rerank_semaphore_held{mode="nli"} 0.0
# HELP yadgar_embed_model_loaded Model loaded state.
# TYPE yadgar_embed_model_loaded gauge
yadgar_embed_model_loaded{model="ce"} 1.0
yadgar_embed_model_loaded{model="nli"} 1.0
yadgar_embed_model_loaded{model="pair"} 0.0
# HELP yadgar_log_file_size_bytes Log file size.
# TYPE yadgar_log_file_size_bytes gauge
yadgar_log_file_size_bytes{logger="backend"} 204800.0
# HELP yadgar_log_file_rotations_total Log rotations.
# TYPE yadgar_log_file_rotations_total counter
yadgar_log_file_rotations_total{logger="backend"} 0.0
# HELP yadgar_log_dropped_total Dropped log records.
# TYPE yadgar_log_dropped_total counter
yadgar_log_dropped_total{logger="backend",level="DEBUG",reason="rate_limit"} 0.0
"""


# ---------------------------------------------------------------------------
# Parser unit tests (no HTTP server needed)
# ---------------------------------------------------------------------------


class TestParseCorMetrics:
    """parse_core_metrics() extracts all expected fields."""

    def test_rss_bytes(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] == 52428800

    def test_open_fds(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["open_fds"] == 42

    def test_uptime_computed(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["uptime_s"] > 0

    def test_cpu_none_on_first_tick(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        # First tick: no previous sample → cpu_pct is None
        assert result["process"]["cpu_pct"] is None

    def test_cpu_computed_on_second_tick(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        prev_t = time.time() - 5.0
        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=3.04, prev_cpu_t=prev_t)
        # cpu_pct = (3.14 - 3.04) / 5 * 100 ≈ 2.0
        assert result["process"]["cpu_pct"] is not None
        assert 0.0 <= result["process"]["cpu_pct"] <= 100.0

    def test_circuit_breakers(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        cb = result["circuit_breakers"]
        assert cb["/rerank/ce"] == 0
        assert cb["/rerank/nli"] == 1
        assert cb["/rerank/pair"] == 2

    def test_queue(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["queue"]["depth"] == 7
        assert result["queue"]["dlq_size"] == 2

    def test_log_fields(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        log = result["log"]
        assert log["file_size_bytes"] == 102400
        assert log["rotations_total"] == 1

    def test_empty_metrics_text(self) -> None:
        from yadgar.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics("", prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] is None
        assert result["circuit_breakers"] == {}


class TestParseBackendMetrics:
    """parse_backend_metrics() extracts all expected fields."""

    def test_rss_bytes(self) -> None:
        from yadgar.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] == 209715200

    def test_model_loaded(self) -> None:
        from yadgar.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        models = result["models"]
        assert models["ce"] == 1
        assert models["nli"] == 1
        assert models["pair"] == 0

    def test_semaphore_held(self) -> None:
        from yadgar.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        sem = result["rerank"]["semaphore_held"]
        assert sem["ce"] == 1
        assert sem["nli"] == 0

    def test_log_size(self) -> None:
        from yadgar.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["log"]["file_size_bytes"] == 204800

    def test_empty_metrics_text(self) -> None:
        from yadgar.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics("", prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] is None
        assert result["models"] == {}


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


def _build_health_app():
    """Build a minimal Starlette app with just the daemon health route."""
    from yadgar.viz_daemon_health import api_daemon_health

    return Starlette(routes=[Route("/api/daemon-health", api_daemon_health, methods=["GET"])])


class TestDaemonHealthEndpoint:
    """GET /api/daemon-health integration tests."""

    def test_returns_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/api/daemon-health returns HTTP 200."""
        from yadgar import viz_daemon_health as vdh

        monkeypatch.setattr(
            vdh,
            "_health_cache",
            {
                "core": {"process": {}, "log": {}, "circuit_breakers": {}, "queue": {}},
                "backend": {"process": {}, "log": {}, "rerank": {}, "models": {}},
                "scraped_at": "2026-05-22T00:00:00Z",
            },
        )
        client = TestClient(_build_health_app())
        resp = client.get("/api/daemon-health")
        assert resp.status_code == 200

    def test_json_shape_core_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Response contains core + backend + scraped_at keys."""
        from yadgar import viz_daemon_health as vdh

        monkeypatch.setattr(
            vdh,
            "_health_cache",
            {
                "core": {"process": {}, "log": {}, "circuit_breakers": {}, "queue": {}},
                "backend": {"process": {}, "log": {}, "rerank": {}, "models": {}},
                "scraped_at": "2026-05-22T00:00:00Z",
            },
        )
        client = TestClient(_build_health_app())
        resp = client.get("/api/daemon-health")
        body = resp.json()
        assert "core" in body
        assert "backend" in body
        assert "scraped_at" in body

    def test_core_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Core section has process, log, circuit_breakers, queue sub-keys."""
        from yadgar import viz_daemon_health as vdh

        monkeypatch.setattr(
            vdh,
            "_health_cache",
            {
                "core": {"process": {}, "log": {}, "circuit_breakers": {}, "queue": {}},
                "backend": {"process": {}, "log": {}, "rerank": {}, "models": {}},
                "scraped_at": "2026-05-22T00:00:00Z",
            },
        )
        client = TestClient(_build_health_app())
        body = client.get("/api/daemon-health").json()
        core = body["core"]
        for key in ("process", "log", "circuit_breakers", "queue"):
            assert key in core, f"Missing core.{key}"

    def test_backend_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backend section has process, log, rerank, models sub-keys."""
        from yadgar import viz_daemon_health as vdh

        monkeypatch.setattr(
            vdh,
            "_health_cache",
            {
                "core": {"process": {}, "log": {}, "circuit_breakers": {}, "queue": {}},
                "backend": {"process": {}, "log": {}, "rerank": {}, "models": {}},
                "scraped_at": "2026-05-22T00:00:00Z",
            },
        )
        client = TestClient(_build_health_app())
        body = client.get("/api/daemon-health").json()
        backend = body["backend"]
        for key in ("process", "log", "rerank", "models"):
            assert key in backend, f"Missing backend.{key}"

    def test_empty_cache_returns_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty cache → 200 with minimal placeholder payload."""
        from yadgar import viz_daemon_health as vdh

        monkeypatch.setattr(vdh, "_health_cache", None)
        client = TestClient(_build_health_app())
        resp = client.get("/api/daemon-health")
        assert resp.status_code == 200
        body = resp.json()
        assert "core" in body
        assert "backend" in body

    def test_backend_unavailable_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When backend is unreachable, backend section has unavailable=True."""
        from yadgar import viz_daemon_health as vdh

        monkeypatch.setattr(
            vdh,
            "_health_cache",
            {
                "core": {"process": {}, "log": {}, "circuit_breakers": {}, "queue": {}},
                "backend": {"unavailable": True},
                "scraped_at": "2026-05-22T00:00:00Z",
            },
        )
        client = TestClient(_build_health_app())
        body = client.get("/api/daemon-health").json()
        assert body["backend"].get("unavailable") is True


class TestBackendUnavailable:
    """Scraper handles backend /metrics failure gracefully."""

    def test_httpx_error_yields_unavailable(self) -> None:
        """If backend /metrics raises, parsed result has unavailable=True."""
        import asyncio

        import httpx

        from yadgar.viz_daemon_health import scrape_backend_metrics_text

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                mock_instance = AsyncMock()
                mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
                mock_instance.__aexit__ = AsyncMock(return_value=None)
                mock_instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
                MockClient.return_value = mock_instance

                result = await scrape_backend_metrics_text("http://127.0.0.1:8001")
            return result

        text, error = asyncio.run(_run())
        assert text is None
        assert error is not None

    def test_http_500_yields_unavailable(self) -> None:
        """If backend /metrics returns 5xx, scraper treats as unavailable."""
        import asyncio

        import httpx  # noqa: F401

        from yadgar.viz_daemon_health import scrape_backend_metrics_text

        async def _run():
            with patch("httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.status_code = 503
                mock_resp.text = ""
                mock_instance = AsyncMock()
                mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
                mock_instance.__aexit__ = AsyncMock(return_value=None)
                mock_instance.get = AsyncMock(return_value=mock_resp)
                MockClient.return_value = mock_instance

                result = await scrape_backend_metrics_text("http://127.0.0.1:8001")
            return result

        text, error = asyncio.run(_run())
        assert text is None
        assert "503" in str(error)
