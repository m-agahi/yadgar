"""v5.1.5 C5: viz_server bearer-proxy unit tests.

Covers:
- proxy forwards Authorization: Bearer <token> header
- proxy preserves status code + content-type
- proxy preserves query string
- YADGAR_VIZ_PROXY=0 flag disables proxy (falls back to index.html SPA)
- run_viz_server signature still accepts host kwarg (regression)
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(
    method: str = "GET",
    path: str = "/api/graph",
    body: bytes = b"",
    environ: dict[str, str] | None = None,
) -> Any:
    """Build a _ProxyHandler instance wired to a fake socket/rfile/wfile."""
    from yadgar.core.viz.viz_server import _Handler

    handler = _Handler.__new__(_Handler)
    handler.command = method
    handler.path = path
    handler.headers = {}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler._environ_override = environ or {}
    # suppress log output
    handler.log_message = lambda *a, **kw: None
    # stub address_string used by BaseHTTPRequestHandler logging
    handler.address_string = lambda: "127.0.0.1"
    return handler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProxyInjectsBearer:
    def test_bearer_header_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Proxy must add Authorization: Bearer <token> using settings.MCP_AUTH_TOKEN."""
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token-abc")
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "1")

        captured_headers: dict[str, str] = {}

        def _fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            captured_headers.update(kwargs.get("headers", {}))
            return httpx.Response(
                200, content=b'{"nodes":[]}', headers={"content-type": "application/json"}
            )

        from yadgar.core.viz.viz_server import _proxy_request

        result = _proxy_request(
            method="GET",
            upstream_url="http://127.0.0.1:8765/api/graph",
            headers={},
            body=b"",
            token="test-token-abc",
            client_factory=lambda: _FakeClient(_fake_request),
        )

        assert captured_headers.get("Authorization") == "Bearer test-token-abc"
        assert result.status_code == 200

    def test_empty_token_sends_no_auth_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When MCP_AUTH_TOKEN is empty, no Authorization header is injected."""
        captured_headers: dict[str, str] = {}

        def _fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            captured_headers.update(kwargs.get("headers", {}))
            return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

        from yadgar.core.viz.viz_server import _proxy_request

        _proxy_request(
            method="GET",
            upstream_url="http://127.0.0.1:8765/api/graph",
            headers={},
            body=b"",
            token="",
            client_factory=lambda: _FakeClient(_fake_request),
        )

        assert "Authorization" not in captured_headers


class TestProxyPreservesStatusAndContentType:
    def test_upstream_status_propagated(self) -> None:
        """Proxy must return whatever status the upstream returned."""

        def _fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/plain"})

        from yadgar.core.viz.viz_server import _proxy_request

        result = _proxy_request(
            method="GET",
            upstream_url="http://127.0.0.1:8765/api/missing",
            headers={},
            body=b"",
            token="tok",
            client_factory=lambda: _FakeClient(_fake_request),
        )

        assert result.status_code == 404

    def test_content_type_propagated(self) -> None:
        """Proxy must pass through upstream content-type."""

        def _fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                content=b'{"nodes":[]}',
                headers={"content-type": "application/json; charset=utf-8"},
            )

        from yadgar.core.viz.viz_server import _proxy_request

        result = _proxy_request(
            method="GET",
            upstream_url="http://127.0.0.1:8765/api/graph",
            headers={},
            body=b"",
            token="tok",
            client_factory=lambda: _FakeClient(_fake_request),
        )

        assert "application/json" in result.headers.get("content-type", "")


class TestProxyPreservesQueryString:
    def test_query_string_forwarded(self) -> None:
        """Query string from the browser request must reach upstream."""
        captured_url: list[str] = []

        def _fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            captured_url.append(url)
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

        from yadgar.core.viz.viz_server import _proxy_request

        _proxy_request(
            method="GET",
            upstream_url="http://127.0.0.1:8765/api/graph/events?since=42",
            headers={},
            body=b"",
            token="tok",
            client_factory=lambda: _FakeClient(_fake_request),
        )

        assert "since=42" in captured_url[0]


class TestVizProxyEnvFlag:
    def test_proxy_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Proxy should be active when YADGAR_VIZ_PROXY is unset (default on)."""
        monkeypatch.delenv("YADGAR_VIZ_PROXY", raising=False)

        from yadgar.core.viz.viz_server import _proxy_enabled

        assert _proxy_enabled() is True

    def test_proxy_disabled_with_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_VIZ_PROXY=0 must disable proxy."""
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "0")

        from yadgar.core.viz.viz_server import _proxy_enabled

        assert _proxy_enabled() is False

    def test_proxy_enabled_with_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_VIZ_PROXY=1 must enable proxy."""
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "1")

        from yadgar.core.viz.viz_server import _proxy_enabled

        assert _proxy_enabled() is True


class TestHandleProxyLazyImport:
    """v5.1.6 Bug 1: _handle_proxy lazy import must use get_settings(), not settings.

    This test exercises the lazy-import path inside _handle_proxy so any future
    regression of the ImportError would be caught at test time, not first request.
    """

    def test_handle_proxy_reads_token_via_get_settings(self) -> None:
        """_handle_proxy must not raise ImportError when called.

        On v5.1.5 this failed with:
            ImportError: cannot import name 'settings' from 'yadgar.config'
        because only get_settings() is exported, not a bare `settings` name.

        The fix is ``from yadgar.config import get_settings`` + ``get_settings().MCP_AUTH_TOKEN``.
        """
        import io
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        fake_settings = SimpleNamespace(MCP_AUTH_TOKEN="lazy-import-test-token")

        from yadgar.core.viz.viz_server import _Handler

        handler = _Handler.__new__(_Handler)
        handler.command = "GET"
        handler.path = "/api/graph"
        handler.headers = {}
        handler.rfile = io.BytesIO(b"")
        handler._daemon_url = "http://127.0.0.1:8765"
        handler.log_message = lambda *a, **kw: None
        handler.address_string = lambda: "127.0.0.1"

        captured_token: list[str] = []

        def _fake_proxy_request(method, upstream_url, headers, body, token, **kw):  # noqa: ANN001
            captured_token.append(token)
            return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

        # send_response / send_header / end_headers / wfile are noops
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        with (
            patch("yadgar._shared.config.get_settings", return_value=fake_settings),
            patch("yadgar.core.viz.viz_server._proxy_request", side_effect=_fake_proxy_request),
        ):
            # Must NOT raise ImportError — that's the regression we're guarding
            handler._handle_proxy()

        assert captured_token == ["lazy-import-test-token"], (
            "_handle_proxy did not read MCP_AUTH_TOKEN via get_settings()"
        )


class TestProxyTimeout:
    """V6: default client must use 60s read timeout, not httpx default 5s."""

    def test_default_client_factory_uses_60s_timeout(self) -> None:
        """When no client_factory is provided, proxy uses 60s read timeout."""
        import inspect

        from yadgar.core.viz import viz_server

        # Re-read the source to verify the lambda sets Timeout(60.0, …)
        src = inspect.getsource(viz_server._proxy_request)
        assert "60.0" in src, "Expected 60s timeout in _proxy_request default client"

    def test_custom_client_factory_still_works(self) -> None:
        """Explicit client_factory override is not broken by the timeout default."""
        called: list[bool] = []

        def _fake(method: str, url: str, **kwargs: Any) -> httpx.Response:
            called.append(True)
            return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

        from yadgar.core.viz.viz_server import _proxy_request

        result = _proxy_request(
            method="GET",
            upstream_url="http://127.0.0.1:8765/api/graph",
            headers={},
            body=b"",
            token="tok",
            client_factory=lambda: _FakeClient(_fake),
        )
        assert result.status_code == 200
        assert called


class TestVizStaticDirResolves:
    """Regression guard for the t2-car-d3 static-path break.

    Commit 7dd2a016 moved viz_server.py into yadgar/core/viz/ but left
    ``STATIC_DIR = Path(__file__).parent / "static"`` — which then resolved to
    the NONEXISTENT yadgar/core/viz/static/ instead of the real
    yadgar/core/static/. Result: the viz server returned 404 "Visualization UI
    not found" for every page load in production, and the integration health
    check leaked 98 unclosed HTTPError(404) objects (ExceptionGroup unraisable
    ERRORS in CI). These assertions fail on the broken path and pass on the fix.
    """

    def test_index_html_exists(self) -> None:
        """viz_server.INDEX_HTML must resolve to an existing file."""
        from yadgar.core.viz.viz_server import INDEX_HTML

        assert INDEX_HTML.is_file(), (
            f"viz INDEX_HTML does not exist: {INDEX_HTML} — STATIC_DIR is "
            "mis-resolved (t2-car-d3 package move broke the parent depth)."
        )

    def test_static_dir_matches_canonical_core_static(self) -> None:
        """viz_server.STATIC_DIR must be the same dir the daemon /graph route serves.

        The daemon resolves ``Path(http.py).parent.parent / 'static'`` =
        yadgar/core/static. viz_server must point at the SAME directory so both
        serve the identical bundled UI.
        """
        from yadgar.core.viz.viz_server import STATIC_DIR

        canonical = Path(__file__).resolve().parents[3] / "yadgar" / "core" / "static"
        assert STATIC_DIR.resolve() == canonical.resolve(), (
            f"viz STATIC_DIR {STATIC_DIR.resolve()} != canonical {canonical.resolve()}"
        )

    def test_bundled_static_assets_present(self) -> None:
        """Key bundled assets (index.html + graph.html) must live under STATIC_DIR."""
        from yadgar.core.viz.viz_server import STATIC_DIR

        assert (STATIC_DIR / "index.html").is_file()
        assert (STATIC_DIR / "graph.html").is_file()


class TestRunVizServerSignature:
    def test_host_kwarg_present(self) -> None:
        """run_viz_server must still accept host= kwarg (regression guard)."""
        import inspect

        from yadgar.core.viz.viz_server import run_viz_server

        sig = inspect.signature(run_viz_server)
        assert "host" in sig.parameters

    def test_daemon_url_kwarg_present(self) -> None:
        """run_viz_server must accept daemon_url= kwarg."""
        import inspect

        from yadgar.core.viz.viz_server import run_viz_server

        sig = inspect.signature(run_viz_server)
        assert "daemon_url" in sig.parameters


# ---------------------------------------------------------------------------
# Internal fake httpx client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal httpx.Client stand-in for synchronous proxy tests."""

    def __init__(self, request_fn: Any) -> None:
        self._fn = request_fn

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._fn(method, url, **kwargs)

    def stream(self, method: str, url: str, **kwargs: Any):  # noqa: ANN201
        # For stream tests we just return the response as a context manager.
        import contextlib

        resp = self._fn(method, url, **kwargs)

        @contextlib.contextmanager
        def _cm():
            yield resp

        return _cm()

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: Any) -> None:
        pass
