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
    """Return path to a usable Chromium binary.

    Priority:
    1. PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH env var (CI override)
    2. System chromium on PATH (NixOS dev machines)
    3. None → playwright uses bundled default (standard FHS Linux / macOS)
    """
    env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    return _SYSTEM_CHROMIUM  # None → playwright uses bundled


# ── MCP daemon fixture ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _daemon(tmp_path_factory):
    """Start yadgar MCP daemon (uvicorn) with seeded data; yield its URL."""
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed — skip Layer 2 smoke tests")

    import uvicorn

    from yadgar.core import server as _server

    tmp_path = tmp_path_factory.mktemp("viz_daemon")
    db_path = str(tmp_path / "smoke.db")
    daemon_port = _free_port()

    # Disable auth — viz proxy doesn't need to inject tokens
    os.environ["YADGAR_REQUIRE_AUTH"] = "0"
    os.environ["YADGAR_MCP_AUTH_TOKEN"] = "smoke-token"

    _server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")

    # T2 Car E3: the /api/graph* handlers are thin forwarders to the backend
    # POST /viz route (via _forward_viz). This single-process smoke harness runs
    # ONLY the core daemon — there is no backend HTTP server, so every browser
    # /api/graph fetch would raise inside _forward_viz (RuntimeError: EMBED_URL
    # unset). Those repeated forward failures leak unclosed httpx responses that
    # the zero-unraisable-warnings gate escalates into ExceptionGroup ERRORS.
    #
    # Wire patch_viz_bypass (the same seam unit + e2e legs use): _forward_viz →
    # in-process run_viz_op against the _st engine stack init_engines just wired.
    # Module-scoped MonkeyPatch installed BEFORE the uvicorn thread starts so the
    # daemon thread (same process) sees the patched module global; .undo() on
    # teardown. CALL-TIME guarded on YADGAR_EMBED_URL — unset here, so the bypass
    # is active (a real-backend run would fall through to the HTTP forward).
    from yadgar.tests._backend_harness import patch_viz_bypass

    _viz_mp = pytest.MonkeyPatch()
    patch_viz_bypass(_viz_mp)

    # Seed memories — brief wait for SurrealDB to finish startup
    time.sleep(2.0)
    import yadgar._shared.runtime.state as _st

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
            # `insert_memory` fans out over the embedding engine and the storage
            # driver — no importable common base.
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
    # Wait for daemon health. Same LEAK GUARD as the viz_server health check below:
    # context-manage the success response and close HTTPError on 4xx/5xx so an
    # unclosed file-like HTTPError cannot become a GC ResourceWarning under the
    # zero-warning gate.
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{daemon_url}/health", timeout=1):
                pass
            break
        except urllib.error.HTTPError as _http_err:
            _http_err.close()  # file-like; unclosed → ResourceWarning at GC (zero-warning gate)
            time.sleep(0.25)
        except OSError:
            # URLError / socket.timeout; HTTPError is handled above (it is an
            # OSError subclass, so it must stay first).
            time.sleep(0.25)
    else:
        uv_server.should_exit = True
        thread.join(timeout=3)
        _viz_mp.undo()
        _server.shutdown()
        pytest.skip("daemon did not become healthy within 20s")

    yield daemon_url

    uv_server.should_exit = True
    thread.join(timeout=5)
    _viz_mp.undo()
    _server.shutdown()


# ── Viz server fixture ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def viz_server(_daemon):
    """Start viz HTTP server (serves index.html, proxies /api/* to daemon).

    Yields the viz server base URL for Playwright to connect to.
    """

    from yadgar.core.viz.viz_server import _Handler, _ThreadingHTTPServer

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
    # Quick health check — viz server doesn't have /health but / returns index.html (200).
    #
    # LEAK GUARD: urllib.request.urlopen RAISES urllib.error.HTTPError on any 4xx/5xx,
    # and HTTPError is file-like (wraps a tempfile). If the viz server ever serves a
    # non-2xx here (e.g. the t2-car-d3 STATIC_DIR break made every request a 404), each
    # loop iteration leaks one unclosed HTTPError → ResourceWarning at GC → the zero-warning
    # gate escalates the batch into an ExceptionGroup of unraisable-exception ERRORS on
    # EVERY test that requested this module-scoped fixture (the 6×98 CI failure). Close the
    # response on BOTH paths (success via context manager, error via HTTPError.close()) so a
    # future non-2xx can never leak, independent of the production fix.
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url, timeout=1) as resp:
                if resp.status == 200:
                    break
        except urllib.error.HTTPError as _http_err:
            _http_err.close()  # file-like; unclosed → ResourceWarning at GC (zero-warning gate)
            time.sleep(0.1)
        except OSError:
            # URLError / socket.timeout; HTTPError is handled above.
            time.sleep(0.1)
    else:
        server.shutdown()
        pytest.skip("viz server did not start within 10s")

    yield base_url

    server.shutdown()
    thread.join(timeout=3)
