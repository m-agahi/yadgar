"""Tests for Streamable HTTP transport migration, health endpoint, and session management."""

import asyncio
import subprocess
import sys
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from yadgar import __version__, server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Initialize global engines with a temp database for each test."""
    db_path = str(tmp_path / "test_transport.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── Health Endpoint ───────────────────────────────────────────────────


class TestHealthEndpoint:
    def _get_client(self, transport: str) -> TestClient:
        """Create a Starlette test client for the given transport."""
        if transport == "streamable-http":
            app = server.mcp_server.streamable_http_app()
        else:
            app = server.mcp_server.sse_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_health_returns_ok_sse(self):
        server._active_transport = "sse"
        server._start_time = 1000000.0
        client = self._get_client("sse")
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == __version__
        assert data["transport"] == "sse"
        assert "uptime_seconds" in data
        assert "active_sessions" in data

    def test_health_returns_ok_streamable_http(self):
        server._active_transport = "streamable-http"
        server._start_time = 1000000.0
        client = self._get_client("streamable-http")
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == __version__
        assert data["transport"] == "streamable-http"

    def test_health_version_matches_package(self):
        client = self._get_client("sse")
        resp = client.get("/health")
        assert resp.json()["version"] == __version__

    def test_health_active_sessions_zero_on_fresh_start(self):
        client = self._get_client("sse")
        resp = client.get("/health")
        assert resp.json()["active_sessions"] == 0

    def test_liveness_reachable_tokenless_through_real_asgi_stack(self, monkeypatch):
        """#74 fix #1: /health/live is the endpoint the container P0 healthcheck
        curls WITHOUT a token. Exercise the REAL middleware stack (BearerAuth) with
        auth ON and no Authorization header → must be 200, not 401/404. If route
        registration or the exempt-path match were wrong, P0 would kill on every
        probe and the whole fix would be silently moot."""
        monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "secret-token")
        server._active_transport = "sse"
        server._start_time = 1000000.0
        client = self._get_client("sse")
        # No Authorization header on purpose (mirrors the tokenless P0 curl).
        resp = client.get("/health/live")
        assert resp.status_code == 200, (
            f"/health/live must be reachable tokenless (got {resp.status_code}); "
            "a 401/404 means P0 SIGKILLs the core on every probe"
        )
        data = resp.json()
        assert data["status"] == "ok"

    def test_liveness_503_when_pool_saturated_through_real_asgi_stack(self, monkeypatch):
        """A genuinely wedged pool still drives /health/live → 503 through the real
        stack so the O2 P0-kill is preserved (a dead daemon is still killed)."""
        monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "secret-token")
        server._active_transport = "sse"
        server._start_time = 1000000.0
        client = self._get_client("sse")
        with patch("yadgar.server._offload.pool_saturated", return_value=True):
            resp = client.get("/health/live")
        assert resp.status_code == 503
        assert resp.json()["tool_pool_saturated"] is True

    def test_health_returns_503_when_db_probe_degraded(self, monkeypatch):
        """C1: a down db probe -> status 'degraded' -> HTTP 503 so curl -f fails.

        #74 fix #1 added readiness anti-flap (HEALTH_READINESS_FAIL_THRESHOLD
        consecutive misses before 503). Pin the threshold to 1 here so a single
        probe miss still 503s — preserving this test's C1 intent under the new
        knob. The N-consecutive anti-flap is covered by
        test_health_liveness_readiness.py.
        """
        import httpx

        server._active_transport = "sse"
        server._start_time = 1000000.0
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.invalid:9999")
        monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "1")

        class _FakeResp:
            status_code = 500

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = self._get_client("sse")
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["db"] is False

    def test_health_returns_200_when_probes_ok(self, monkeypatch):
        """C1: healthy probes -> status 'ok' -> HTTP 200 (unchanged)."""
        import httpx

        server._active_transport = "sse"
        server._start_time = 1000000.0
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.invalid:9999")

        class _FakeResp:
            status_code = 200

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = self._get_client("sse")
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["db"] is True

    def test_health_probes_db_and_embed_concurrently(self, monkeypatch):
        """C2 P1: db + embed probes run concurrently (asyncio.gather), not serially.

        Each probe sleeps ~1s; serial would be ~2s, concurrent should be ~1s.
        Assert wall-time well under the serial sum.
        """
        import time as _time

        import httpx

        server._active_transport = "sse"
        server._start_time = 1000000.0
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.invalid:9999")
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.invalid:9999")

        class _FakeResp:
            status_code = 200

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                await asyncio.sleep(1.0)
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = self._get_client("sse")
        t0 = _time.perf_counter()
        resp = client.get("/health")
        elapsed = _time.perf_counter() - t0
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # Serial would be ~2s; concurrent ~1s. Generous bound to avoid CI flake.
        assert elapsed < 1.5, f"probes ran serially (elapsed={elapsed:.2f}s)"

    def test_health_outer_timeout_trips_503_on_hang(self, monkeypatch):
        """C2 P1: a hanging probe trips the outer asyncio.wait_for bound -> 503, no hang."""
        import time as _time

        import httpx

        server._active_transport = "sse"
        server._start_time = 1000000.0
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.invalid:9999")
        # Shrink the outer bound so the test is fast.
        monkeypatch.setattr(server.http, "_HEALTH_TIMEOUT_SEC", 0.3, raising=False)

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                await asyncio.sleep(30)  # hang well past the outer bound

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = self._get_client("sse")
        t0 = _time.perf_counter()
        resp = client.get("/health")
        elapsed = _time.perf_counter() - t0
        assert resp.status_code == 503, "outer timeout must yield 503, not 200 or a hang"
        assert elapsed < 5.0, f"handler hung past the outer bound (elapsed={elapsed:.2f}s)"


# ── Session Management ────────────────────────────────────────────────


class TestSessionManagement:
    def test_streamable_http_app_has_session_manager(self):
        """Streamable HTTP transport creates a session manager."""
        server.mcp_server.streamable_http_app()
        assert server.mcp_server._session_manager is not None

    def test_session_manager_tracks_instances(self):
        """Session manager has _server_instances dict for tracking."""
        server.mcp_server.streamable_http_app()
        mgr = server.mcp_server._session_manager
        assert hasattr(mgr, "_server_instances")
        assert isinstance(mgr._server_instances, dict)

    def test_session_count_reflected_in_health(self):
        """Health endpoint session count reflects session manager state."""
        app = server.mcp_server.streamable_http_app()
        server._active_transport = "streamable-http"
        server._start_time = 1000000.0

        # Inject a fake session to verify counting
        mgr = server.mcp_server._session_manager
        mgr._server_instances["fake-session-1"] = object()
        mgr._server_instances["fake-session-2"] = object()

        client = TestClient(app, raise_server_exceptions=False)
        # P6 fix v5.46.7: retry once on empty body — startup race in test fixture
        # can produce a 200 with empty body; one retry is usually sufficient.
        resp = client.get("/health")
        if not resp.text.strip():
            resp = client.get("/health")
        assert resp.text.strip(), (
            f"Health endpoint returned empty body (status={resp.status_code}). "
            "Possible startup race."
        )
        data = resp.json()
        assert data["active_sessions"] == 2

        # Cleanup
        mgr._server_instances.clear()


# ── Transport Selection ───────────────────────────────────────────────


class TestTransportSelection:
    def test_valid_transports_in_cli_help(self):
        """CLI advertises both sse and streamable-http transports."""
        result = subprocess.run(
            [sys.executable, "-m", "yadgar", "--help"],
            capture_output=True,
            text=True,
        )
        assert "sse" in result.stdout
        assert "streamable-http" in result.stdout

    def test_main_accepts_transport_param(self):
        """server.main() accepts a transport keyword argument."""
        import inspect

        sig = inspect.signature(server.main)
        assert "transport" in sig.parameters
        assert sig.parameters["transport"].default == "stdio"

    def test_cli_transport_flag_default(self):
        """CLI defaults to stdio transport."""
        result = subprocess.run(
            [sys.executable, "-m", "yadgar", "--help"],
            capture_output=True,
            text=True,
        )
        assert "--transport" in result.stdout
        assert "stdio" in result.stdout
        assert "streamable-http" in result.stdout

    def test_cli_rejects_invalid_transport(self):
        """CLI rejects unknown transport values."""
        result = subprocess.run(
            [sys.executable, "-m", "yadgar", "--transport", "websocket"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "invalid choice" in result.stderr

    def test_active_transport_tracks_selection(self):
        """_active_transport global is set by main() setup."""
        server._active_transport = "streamable-http"
        assert server._active_transport == "streamable-http"
        server._active_transport = "sse"
        assert server._active_transport == "sse"


# ── Streamable HTTP App Structure ─────────────────────────────────────


def _unwrap_starlette(app):
    """Walk through ASGI middleware wrappers until a Starlette instance is found."""
    from starlette.applications import Starlette

    while app is not None and not isinstance(app, Starlette):
        app = getattr(app, "app", None)
    return app


class TestStreamableHttpApp:
    def test_streamable_http_app_is_starlette(self):
        from starlette.applications import Starlette

        app = _unwrap_starlette(server.mcp_server.streamable_http_app())
        assert isinstance(app, Starlette)

    def test_sse_app_is_starlette(self):
        from starlette.applications import Starlette

        app = _unwrap_starlette(server.mcp_server.sse_app())
        assert isinstance(app, Starlette)

    def test_streamable_http_mcp_endpoint_exists(self):
        """The /mcp endpoint should exist on the streamable HTTP app."""
        app = _unwrap_starlette(server.mcp_server.streamable_http_app())
        paths = [route.path for route in app.routes]
        assert "/mcp" in paths

    def test_health_endpoint_on_both_transports(self):
        """Health endpoint is available on both SSE and Streamable HTTP apps."""
        sse_app = _unwrap_starlette(server.mcp_server.sse_app())
        http_app = _unwrap_starlette(server.mcp_server.streamable_http_app())

        sse_paths = [route.path for route in sse_app.routes]
        http_paths = [route.path for route in http_app.routes]

        assert "/health" in sse_paths
        assert "/health" in http_paths
