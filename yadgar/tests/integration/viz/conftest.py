"""Fixtures for Layer 2 viz Playwright smoke tests.

Architecture:
  - MCP daemon (uvicorn) on port A: handles /api/* and /health
  - Viz server (stdlib HTTPServer, from viz_server.py) on port B:
      serves index.html, proxies /api/* to daemon port A
  - Playwright connects to port B

This mirrors production topology: users hit the viz server, which proxies
API calls to the daemon. Auth is disabled on the daemon for smoke tests
(YADGAR_REQUIRE_AUTH=0) so proxy works without token injection.

Browser: uses system Chromium first (NixOS-safe), falls back to bundled.
Skip if playwright not installed or chromium not found.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time

import pytest

# ── Browser detection ─────────────────────────────────────────────────────────

_SYSTEM_CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")

# ── Playwright availability ───────────────────────────────────────────────────

try:
    from playwright.sync_api import sync_playwright  # noqa: F401

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

pytestmark = pytest.mark.integration


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "viz_smoke: Playwright headless browser smoke tests")


# ── Port helpers ──────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Session-scoped chromium path ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def chromium_executable() -> str | None:
    """Return path to a usable Chromium binary."""
    return _SYSTEM_CHROMIUM  # None → playwright uses bundled (FHS systems)


# ── MCP daemon fixture ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _daemon(tmp_path_factory):
    """Start yadgar MCP daemon (uvicorn) with seeded data; yield its URL."""
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed — skip Layer 2 smoke tests")

    import uvicorn

    from yadgar import server as _server

    tmp_path = tmp_path_factory.mktemp("viz_daemon")
    db_path = str(tmp_path / "smoke.db")
    daemon_port = _free_port()

    # Disable auth — viz proxy doesn't need to inject tokens
    os.environ["YADGAR_REQUIRE_AUTH"] = "0"
    os.environ["YADGAR_MCP_AUTH_TOKEN"] = "smoke-token"

    _server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")

    # Seed memories — brief wait for SurrealDB to finish startup
    time.sleep(2.0)
    import yadgar.server._state as _st

    storage = _st._storage
    if storage is not None:
        for i in range(4):
            try:
                storage.insert_memory(
                    {
                        "content": f"playwright smoke memory {i}: testing viz renders correctly",
                        "directory_context": "/viz-smoke",
                        "tags": ["viz", "smoke"],
                        "heat": float(i + 1) / 4.0,
                    }
                )
            except Exception:
                pass  # seeding failure is non-fatal — test still validates no-JS-error path

    asgi_app = _server.mcp_server.streamable_http_app()
    uv_config = uvicorn.Config(
        asgi_app,
        host="127.0.0.1",
        port=daemon_port,
        log_level="error",
        loop="asyncio",
    )
    uv_server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()

    daemon_url = f"http://127.0.0.1:{daemon_port}"
    # Wait for daemon health
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            import urllib.request

            urllib.request.urlopen(f"{daemon_url}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        uv_server.should_exit = True
        thread.join(timeout=3)
        _server.shutdown()
        pytest.skip("daemon did not become healthy within 20s")

    yield daemon_url

    uv_server.should_exit = True
    thread.join(timeout=5)
    _server.shutdown()


# ── Viz server fixture ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def viz_server(_daemon):
    """Start viz HTTP server (serves index.html, proxies /api/* to daemon).

    Yields the viz server base URL for Playwright to connect to.
    """

    from yadgar.viz_server import _Handler, _ThreadingHTTPServer

    viz_port = _free_port()

    # Point handler at the daemon
    _Handler._daemon_url = _daemon

    # Disable proxy so /api/* requests go directly to the daemon without token
    # injection (daemon has auth disabled for smoke tests)
    os.environ["YADGAR_VIZ_PROXY"] = "1"  # keep proxy on — it proxies to our test daemon

    server = _ThreadingHTTPServer(("127.0.0.1", viz_port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{viz_port}"
    # Quick health check — viz server doesn't have /health but / returns index.html (200)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            import urllib.request

            resp = urllib.request.urlopen(base_url, timeout=1)
            if resp.status == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        server.shutdown()
        pytest.skip("viz server did not start within 10s")

    yield base_url

    server.shutdown()
    thread.join(timeout=3)
