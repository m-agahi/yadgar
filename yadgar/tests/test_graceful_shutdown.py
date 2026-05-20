"""N2 — ASGI graceful shutdown ≤5s budget (v5.3.9).

Two test levels:
  A. Wiring: run_sse_async / run_streamable_http_async pass
     timeout_graceful_shutdown from ASGI_SHUTDOWN_TIMEOUT_SEC.
  B. Behaviour: uvicorn actually abandons a hanging request within budget.
"""

from __future__ import annotations

import asyncio
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

        import yadgar.server._app as app_mod

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

        import yadgar.server._app as app_mod

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

        import yadgar.config as cfg_mod

        importlib.reload(cfg_mod)
        import yadgar.server._app as app_mod

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


async def _lifespan(app):
    yield


_slow_app = Starlette(
    routes=[Route("/hang", _slow_handler)],
    lifespan=_lifespan,
)


def test_uvicorn_abandons_hanging_request_within_budget():
    """Uvicorn must exit ≤6 s after should_exit=True even with a stuck request."""
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

    # Budget: SHUTDOWN_TIMEOUT + 2 s slack.
    assert elapsed < SHUTDOWN_TIMEOUT + 2, (
        f"Server took {elapsed:.2f}s to exit; expected ≤{SHUTDOWN_TIMEOUT + 2}s"
    )
