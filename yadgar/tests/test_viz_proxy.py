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
    from yadgar.viz_server import _Handler

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

        from yadgar.viz_server import _proxy_request

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

        from yadgar.viz_server import _proxy_request

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

        from yadgar.viz_server import _proxy_request

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

        from yadgar.viz_server import _proxy_request

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

        from yadgar.viz_server import _proxy_request

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

        from yadgar.viz_server import _proxy_enabled

        assert _proxy_enabled() is True

    def test_proxy_disabled_with_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_VIZ_PROXY=0 must disable proxy."""
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "0")

        from yadgar.viz_server import _proxy_enabled

        assert _proxy_enabled() is False

    def test_proxy_enabled_with_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_VIZ_PROXY=1 must enable proxy."""
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "1")

        from yadgar.viz_server import _proxy_enabled

        assert _proxy_enabled() is True


class TestRunVizServerSignature:
    def test_host_kwarg_present(self) -> None:
        """run_viz_server must still accept host= kwarg (regression guard)."""
        import inspect

        from yadgar.viz_server import run_viz_server

        sig = inspect.signature(run_viz_server)
        assert "host" in sig.parameters

    def test_daemon_url_kwarg_present(self) -> None:
        """run_viz_server must accept daemon_url= kwarg."""
        import inspect

        from yadgar.viz_server import run_viz_server

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
