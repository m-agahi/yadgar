"""N2 — ASGI graceful shutdown ≤5s budget (v5.3.9).

Two test levels:
  A. Wiring: run_sse_async / run_streamable_http_async pass
     timeout_graceful_shutdown from ASGI_SHUTDOWN_TIMEOUT_SEC.
  B. Behaviour: uvicorn actually abandons a hanging request within budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

# ── A. Wiring tests ───────────────────────────────────────────────────────────


class TestShutdownTimeoutWiring:
    """Assert both transports inject timeout_graceful_shutdown into uvicorn.Config."""

    def setup_method(self):
        """Snapshot mcp_server before each test so reload() doesn't permanently orphan it.

        importlib.reload(app_mod) creates a fresh FastMCP at _app.mcp_server.  Later
        tests that reload yadgar.server pick up this empty instance, clearing all
        route/tool registrations (Root-A xdist pollution — v5.56 fix).
        """
        import yadgar.core.server as _srv
        import yadgar.core.server._app as _app

        self._saved_app_mcp = _app.mcp_server
        self._saved_srv_mcp = _srv.__dict__.get("mcp_server")

    def teardown_method(self):
        """Restore mcp_server on both _app and server to the original instance."""
        import yadgar.core.server as _srv
        import yadgar.core.server._app as _app

        if self._saved_app_mcp is not None:
            _app.mcp_server = self._saved_app_mcp
        if self._saved_srv_mcp is not None:
            _srv.__dict__["mcp_server"] = self._saved_srv_mcp

    def _capture_config(self, monkeypatch) -> list[dict]:
        """Return a list that collects kwargs passed to uvicorn.Config."""
        captured: list[dict] = []
        original_config = uvicorn.Config

        class _CapturingConfig(original_config):  # type: ignore[misc]
            def __init__(self, app, **kwargs):
                captured.append(kwargs)
                # Don't call super().__init__ — we just want the kwargs.
                # Raise immediately to abort the serve() call cleanly.
                raise SystemExit(0)

        monkeypatch.setattr(uvicorn, "Config", _CapturingConfig)
        return captured

    def test_sse_injects_graceful_timeout_default(self, monkeypatch):
        """run_sse_async must pass timeout_graceful_shutdown=5 by default."""
        os.environ.pop("YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC", None)

        captured = self._capture_config(monkeypatch)

        # Re-import server._app so get_settings() picks up env reset.
        import importlib

        import yadgar.core.server._app as app_mod

        importlib.reload(app_mod)
        mcp_srv = app_mod.mcp_server

        with pytest.raises(SystemExit):
            asyncio.run(mcp_srv.run_sse_async())

        assert captured, "uvicorn.Config was never called"
        assert captured[0].get("timeout_graceful_shutdown") == 5

    def test_streamable_http_injects_graceful_timeout_default(self, monkeypatch):
        """run_streamable_http_async must pass timeout_graceful_shutdown=5 by default."""
        os.environ.pop("YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC", None)

        captured = self._capture_config(monkeypatch)

        import importlib

        import yadgar.core.server._app as app_mod

        importlib.reload(app_mod)
        mcp_srv = app_mod.mcp_server

        with pytest.raises(SystemExit):
            asyncio.run(mcp_srv.run_streamable_http_async())

        assert captured, "uvicorn.Config was never called"
        assert captured[0].get("timeout_graceful_shutdown") == 5

    def test_env_var_overrides_default(self, monkeypatch):
        """YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC env var must override the default."""
        monkeypatch.setenv("YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC", "10")

        captured = self._capture_config(monkeypatch)

        import importlib

        import yadgar._shared.config as cfg_mod
        import yadgar.core.server._app as app_mod

        # Use cache_clear() instead of reload() to pick up fresh env values without
        # replacing the module object in sys.modules.  importlib.reload(yadgar.config)
        # creates a NEW get_settings lru_cache function; the conftest's _isolate_yaml_config
        # only clears the original — later tests calling get_settings() on the old
        # reference see a stale Settings (Root-B xdist pollution — v5.56 fix).
        cfg_mod.get_settings.cache_clear()

        importlib.reload(app_mod)
        mcp_srv = app_mod.mcp_server

        with pytest.raises(SystemExit):
            asyncio.run(mcp_srv.run_sse_async())

        assert captured, "uvicorn.Config was never called"
        assert captured[0].get("timeout_graceful_shutdown") == 10

        # Cleanup env so later tests aren't affected.
        monkeypatch.delenv("YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC", raising=False)


# ── B. Behaviour test ─────────────────────────────────────────────────────────


async def _slow_handler(request: Request) -> PlainTextResponse:
    """Endpoint that hangs 30 s — simulates a stuck httpx call to a dead backend."""
    await asyncio.sleep(30)
    return PlainTextResponse("ok")


@contextlib.asynccontextmanager
async def _lifespan(app):
    # starlette 1.0 deprecates bare async-generator lifespans (Car 3 zero-warning gate)
    yield


_slow_app = Starlette(
    routes=[Route("/hang", _slow_handler)],
    lifespan=_lifespan,
)


def test_uvicorn_abandons_hanging_request_within_budget():
    """Uvicorn must exit ≤6 s after should_exit=True even with a stuck request."""
    import logging

    # uvicorn.Config(log_level=...) permanently sets uvicorn.propagate=False and
    # uvicorn.access.propagate=False, breaking propagation to root for later tests
    # that rely on uvicorn.access records reaching the root JSON handler (Root-C
    # xdist pollution — v5.56 fix).  Snapshot and restore before calling Config.
    _uvicorn_logger_names = ["uvicorn", "uvicorn.access", "uvicorn.error"]
    _saved_uvicorn_propagate = {
        name: logging.getLogger(name).propagate for name in _uvicorn_logger_names
    }
    _saved_uvicorn_level = {name: logging.getLogger(name).level for name in _uvicorn_logger_names}
    _saved_uvicorn_handlers = {
        name: list(logging.getLogger(name).handlers) for name in _uvicorn_logger_names
    }

    SHUTDOWN_TIMEOUT = 1  # 1 s for speed; proves mechanism, not exact budget

    config = uvicorn.Config(
        _slow_app,
        host="127.0.0.1",
        port=0,  # OS-assigned; avoids port conflicts
        log_level="critical",
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
        timeout_keep_alive=1,
    )
    server = uvicorn.Server(config)

    async def _fire_and_forget_request():
        """Issue a request after the server is up; it will hang."""
        # Wait briefly for the server to be ready.
        deadline = time.monotonic() + 3
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not server.started:
            return
        # Find the actual bound port.
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            import httpx

            async with httpx.AsyncClient(timeout=35) as client:
                await client.get(f"http://127.0.0.1:{port}/hang")
        except Exception:
            pass

    async def _trigger_shutdown():
        """Signal shutdown 0.3 s after the server starts."""
        deadline = time.monotonic() + 3
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        server.should_exit = True

    async def _run_all():
        await asyncio.gather(
            server.serve(),
            _fire_and_forget_request(),
            _trigger_shutdown(),
        )

    t0 = time.monotonic()
    asyncio.run(_run_all())
    elapsed = time.monotonic() - t0

    # Restore uvicorn logger state that uvicorn.Config(log_level=...) mutated.
    for name in _uvicorn_logger_names:
        lg = logging.getLogger(name)
        lg.propagate = _saved_uvicorn_propagate[name]
        lg.setLevel(_saved_uvicorn_level[name])
        lg.handlers = _saved_uvicorn_handlers[name]

    # Budget: SHUTDOWN_TIMEOUT + 2 s slack.
    assert elapsed < SHUTDOWN_TIMEOUT + 2, (
        f"Server took {elapsed:.2f}s to exit; expected ≤{SHUTDOWN_TIMEOUT + 2}s"
    )
