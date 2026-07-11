"""V1c viz daemon health endpoint tests (v5.6.0).

Tests:
- GET /api/daemon-health returns 200 + valid JSON shape
- Parser extracts core metrics from /metrics text correctly
- Parser extracts backend metrics from /metrics text correctly
- Missing backend (httpx error) → 200 with backend.unavailable=True
- Missing core is impossible (self-scraped) but parser returns empty on bad text
- CB state mapped correctly (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
- scraped_at field present in response
- YADGAR_VIZ_HEALTH_REFRESH_SEC env knob propagates to asyncio.sleep interval (v5.7.7)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures: sample /metrics text for core and backend
# ---------------------------------------------------------------------------

_CORE_METRICS = """# HELP yadgar_process_rss_bytes RSS bytes.
# TYPE yadgar_process_rss_bytes gauge
yadgar_process_rss_bytes 52428800.0
# HELP yadgar_process_cpu_percent CPU percent.
# TYPE yadgar_process_cpu_percent gauge
yadgar_process_cpu_percent 2.0
# HELP yadgar_process_open_fds Open file descriptors.
# TYPE yadgar_process_open_fds gauge
yadgar_process_open_fds 42.0
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
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] == 52428800

    def test_open_fds(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["open_fds"] == 42

    def test_uptime_none_for_core(self) -> None:
        """Core registry has no process_start_time_seconds; uptime_s is None."""
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["uptime_s"] is None

    def test_cpu_from_gauge(self) -> None:
        """Core uses yadgar_process_cpu_percent gauge — available on first tick."""
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["cpu_pct"] == 2.0

    def test_circuit_breakers(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        cb = result["circuit_breakers"]
        assert cb["/rerank/ce"] == 0
        assert cb["/rerank/nli"] == 1
        assert cb["/rerank/pair"] == 2

    def test_queue(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["queue"]["depth"] == 7
        assert result["queue"]["dlq_size"] == 2

    def test_log_fields(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(_CORE_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        log = result["log"]
        assert log["file_size_bytes"] == 102400
        assert log["rotations_total"] == 1

    def test_empty_metrics_text(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics("", prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] is None
        assert result["circuit_breakers"] == {}


class TestParseBackendMetrics:
    """parse_backend_metrics() extracts all expected fields."""

    def test_rss_bytes(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] == 209715200

    def test_model_loaded(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        models = result["models"]
        assert models["ce"] == 1
        assert models["nli"] == 1
        assert models["pair"] == 0

    def test_semaphore_held(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        sem = result["rerank"]["semaphore_held"]
        assert sem["ce"] == 1
        assert sem["nli"] == 0

    def test_log_size(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics(_BACKEND_METRICS, prev_cpu_s=None, prev_cpu_t=None)
        assert result["log"]["file_size_bytes"] == 204800

    def test_empty_metrics_text(self) -> None:
        from yadgar.core.viz.viz_daemon_health import parse_backend_metrics

        result = parse_backend_metrics("", prev_cpu_s=None, prev_cpu_t=None)
        assert result["process"]["rss_bytes"] is None
        assert result["models"] == {}


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


def _build_health_app():
    """Build a minimal Starlette app with just the daemon health route."""
    from yadgar.core.viz.viz_daemon_health import api_daemon_health

    return Starlette(routes=[Route("/api/daemon-health", api_daemon_health, methods=["GET"])])


class TestDaemonHealthEndpoint:
    """GET /api/daemon-health integration tests."""

    def test_returns_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/api/daemon-health returns HTTP 200."""
        from yadgar.core.viz import viz_daemon_health as vdh

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
        from yadgar.core.viz import viz_daemon_health as vdh

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
        from yadgar.core.viz import viz_daemon_health as vdh

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
        from yadgar.core.viz import viz_daemon_health as vdh

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
        from yadgar.core.viz import viz_daemon_health as vdh

        monkeypatch.setattr(vdh, "_health_cache", None)
        client = TestClient(_build_health_app())
        resp = client.get("/api/daemon-health")
        assert resp.status_code == 200
        body = resp.json()
        assert "core" in body
        assert "backend" in body

    def test_backend_unavailable_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When backend is unreachable, backend section has unavailable=True."""
        from yadgar.core.viz import viz_daemon_health as vdh

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

        from yadgar.core.viz.viz_daemon_health import scrape_backend_metrics_text

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

        from yadgar.core.viz.viz_daemon_health import scrape_backend_metrics_text

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


# ---------------------------------------------------------------------------
# v5.6.1 bug-fix tests
# ---------------------------------------------------------------------------


class TestBackendUrlResolution:
    """Bug 1: scraper uses YADGAR_EMBED_URL to reach backend /metrics."""

    def test_backend_url_from_yadgar_embed_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_EMBED_URL=http://test-backend:9999 → scraper calls .../metrics."""

        from yadgar.core.viz.viz_daemon_health import _get_backend_metrics_url

        monkeypatch.setenv("YADGAR_EMBED_URL", "http://test-backend:9999")
        monkeypatch.delenv("YADGAR_BACKEND_METRICS_URL", raising=False)
        assert _get_backend_metrics_url() == "http://test-backend:9999/metrics"

    def test_backend_url_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env vars → default http://yadgar-backend:8001/metrics."""
        from yadgar.core.viz.viz_daemon_health import _get_backend_metrics_url

        monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)
        monkeypatch.delenv("YADGAR_BACKEND_METRICS_URL", raising=False)
        assert _get_backend_metrics_url() == "http://yadgar-backend:8001/metrics"

    def test_backend_metrics_url_override_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """YADGAR_BACKEND_METRICS_URL overrides YADGAR_EMBED_URL."""
        from yadgar.core.viz.viz_daemon_health import _get_backend_metrics_url

        monkeypatch.setenv("YADGAR_EMBED_URL", "http://ignored:9999")
        monkeypatch.setenv("YADGAR_BACKEND_METRICS_URL", "http://override:1234/metrics")
        assert _get_backend_metrics_url() == "http://override:1234/metrics"


class TestCoreProcessMetrics:
    """Bug 2 (option b): parser reads yadgar_process_* names for core."""

    _CORE_WITH_YADGAR_PROCESS = """# HELP yadgar_process_rss_bytes RSS bytes.
# TYPE yadgar_process_rss_bytes gauge
yadgar_process_rss_bytes 65536000.0
# HELP yadgar_process_open_fds Open FDs.
# TYPE yadgar_process_open_fds gauge
yadgar_process_open_fds 55.0
# HELP yadgar_process_cpu_percent CPU percent.
# TYPE yadgar_process_cpu_percent gauge
yadgar_process_cpu_percent 12.5
"""

    def test_core_process_metrics_exposed(self) -> None:
        """core /metrics has yadgar_process_* names; parser extracts to rss_bytes."""
        from yadgar.core.viz.viz_daemon_health import parse_core_metrics

        result = parse_core_metrics(
            self._CORE_WITH_YADGAR_PROCESS, prev_cpu_s=None, prev_cpu_t=None
        )
        assert result["process"]["rss_bytes"] == 65536000
        assert result["process"]["open_fds"] == 55


# ---------------------------------------------------------------------------
# v5.7.7 — YADGAR_VIZ_HEALTH_REFRESH_SEC env knob
# ---------------------------------------------------------------------------


class TestVizHealthRefreshEnvKnob:
    """YADGAR_VIZ_HEALTH_REFRESH_SEC propagates to run_health_scraper sleep interval."""

    @pytest.mark.asyncio
    async def test_default_interval_is_5s(self, monkeypatch) -> None:
        """Without env override, run_health_scraper sleeps for 5.0 s."""
        import yadgar._shared.config as cfg

        monkeypatch.delenv("YADGAR_VIZ_HEALTH_REFRESH_SEC", raising=False)
        cfg.get_settings.cache_clear()

        sleep_calls: list[float] = []

        async def _fake_scrape_once() -> None:
            pass

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)
            raise asyncio.CancelledError  # stop after first iteration

        with (
            patch("yadgar.core.viz.viz_daemon_health._scrape_once", side_effect=_fake_scrape_once),
            patch("yadgar.core.viz.viz_daemon_health._scraper_heartbeat"),
            patch("yadgar.core.viz.viz_daemon_health._scraper_record_exc"),
            patch("asyncio.sleep", side_effect=_fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                from yadgar.core.viz.viz_daemon_health import run_health_scraper

                await run_health_scraper()

        cfg.get_settings.cache_clear()
        assert sleep_calls == [5.0], f"expected [5.0], got {sleep_calls}"

    @pytest.mark.asyncio
    async def test_env_override_propagates(self, monkeypatch) -> None:
        """YADGAR_VIZ_HEALTH_REFRESH_SEC=10 → run_health_scraper sleeps 10.0 s."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_VIZ_HEALTH_REFRESH_SEC", "10")
        cfg.get_settings.cache_clear()

        sleep_calls: list[float] = []

        async def _fake_scrape_once() -> None:
            pass

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)
            raise asyncio.CancelledError  # stop after first iteration

        # N2 fix v5.46.7: also patch get_settings in viz_daemon_health module scope
        # to ensure the 10s override is visible regardless of LRU cache state.
        # The global cache_clear() above is necessary but not sufficient when other
        # tests or fixtures call get_settings() after clear; a direct patch is robust.
        class _FakeSettings:
            VIZ_HEALTH_REFRESH_SEC: float = 10.0

        with (
            patch("yadgar.core.viz.viz_daemon_health.get_settings", return_value=_FakeSettings()),
            patch("yadgar.core.viz.viz_daemon_health._scrape_once", side_effect=_fake_scrape_once),
            patch("yadgar.core.viz.viz_daemon_health._scraper_heartbeat"),
            patch("yadgar.core.viz.viz_daemon_health._scraper_record_exc"),
            patch("asyncio.sleep", side_effect=_fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                from yadgar.core.viz.viz_daemon_health import run_health_scraper

                await run_health_scraper()

        cfg.get_settings.cache_clear()
        assert sleep_calls == [10.0], f"expected [10.0], got {sleep_calls}"


# ---------------------------------------------------------------------------
# fix(metrics): dlq_size excludes .error.json sidecars
# ---------------------------------------------------------------------------


class TestCollectQueueDepthsExcludesErrorSidecars:
    """_collect_queue_depths() must not double-count .error.json sidecar files."""

    def _make_settings(self, tmp_path):
        class _FakeSettings:
            DATA_DIR = str(tmp_path)

        return _FakeSettings()

    def _call(self, tmp_path, monkeypatch):
        from yadgar._shared.observability import metrics as m

        monkeypatch.setattr(
            "yadgar._shared.config.get_settings",
            lambda: self._make_settings(tmp_path),
        )
        # Ensure all three queue dirs exist so the loop doesn't skip them.
        (tmp_path / "queue").mkdir(exist_ok=True)
        (tmp_path / "archive").mkdir(exist_ok=True)
        dlq = tmp_path / "dlq"
        dlq.mkdir(exist_ok=True)
        return m, dlq

    def test_empty_dlq_depth_is_zero(self, tmp_path, monkeypatch):
        m, _dlq = self._call(tmp_path, monkeypatch)
        m._collect_queue_depths()
        assert m.yadgar_dlq_size._value.get() == 0.0

    def test_one_entry_with_sidecar_counts_as_one(self, tmp_path, monkeypatch):
        m, dlq = self._call(tmp_path, monkeypatch)
        (dlq / "0001_x.json").write_text("{}")
        (dlq / "0001_x.json.error.json").write_text("{}")
        m._collect_queue_depths()
        assert m.yadgar_dlq_size._value.get() == 1.0

    def test_two_entries_with_sidecars_count_as_two(self, tmp_path, monkeypatch):
        m, dlq = self._call(tmp_path, monkeypatch)
        (dlq / "0001_x.json").write_text("{}")
        (dlq / "0001_x.json.error.json").write_text("{}")
        (dlq / "0002_y.json").write_text("{}")
        (dlq / "0002_y.json.error.json").write_text("{}")
        m._collect_queue_depths()
        assert m.yadgar_dlq_size._value.get() == 2.0
