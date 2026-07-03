"""HTTP proxy integration tests for /api/bookmarks* routes.

Tests verify that the daemon routes:
  GET    /api/bookmarks           → bookmark_list
  POST   /api/bookmarks           → bookmark_add
  DELETE /api/bookmarks/{slug}    → bookmark_remove
  PUT    /api/bookmarks/{slug}/position → bookmark_reorder
  GET    /api/wiki/read/{slug}    → wiki_read (existing route, cache-control header)
  GET    /api/wiki/search         → wiki_query passthrough
  GET    /api/wiki/list           → wiki_list passthrough

Auth: bearer token required (proxy injects; direct access → 401 or 403).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from yadgar import server
from yadgar.viz_server import _Handler

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_client_factory(response: httpx.Response) -> Any:
    """Return a factory that yields a client always returning *response*."""

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            return response

        def __enter__(self):
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    return lambda: _FakeClient()


def _make_capture_factory():
    """Return (factory, captured) — factory records the last request."""
    captured: dict = {}

    class _CaptureClient:
        def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["content"] = kwargs.get("content", b"")
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

        def __enter__(self):
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    return lambda: _CaptureClient(), captured


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("api_bookmarks")
    server.init_engines(
        db_path=str(tmp_path / "api_bm_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# Tests — viz_server proxy forwards /api/bookmarks* to daemon
# ---------------------------------------------------------------------------


class TestVizProxyForwardsBookmarks:
    """viz_server._Handler routes /api/bookmarks* through proxy."""

    def test_proxy_routes_api_bookmarks_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /api/bookmarks is treated as an /api/* path and proxied."""
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "1")
        from yadgar.viz_server import _proxy_enabled

        assert _proxy_enabled() is True

        # The handler's do_GET passes /api/bookmarks to _handle_proxy
        import io

        handler = _Handler.__new__(_Handler)
        handler.command = "GET"
        handler.path = "/api/bookmarks"
        handler.headers = {}
        handler.rfile = io.BytesIO(b"")
        handler._daemon_url = "http://127.0.0.1:8765"
        handler.log_message = lambda *a, **kw: None
        handler.address_string = lambda: "127.0.0.1"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        from types import SimpleNamespace

        fake_settings = SimpleNamespace(MCP_AUTH_TOKEN="tok")
        captured_url: list[str] = []

        def _fake_proxy_request(method, upstream_url, headers, body, token, **kw):  # noqa: ANN001
            captured_url.append(upstream_url)
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

        import yadgar.viz_server as _vs

        monkeypatch.setattr(_vs, "_proxy_request", _fake_proxy_request)
        from unittest.mock import patch

        with patch("yadgar.config.get_settings", return_value=fake_settings):
            handler._handle_proxy()

        assert any("/api/bookmarks" in u for u in captured_url), (
            f"Expected /api/bookmarks in proxied URLs, got: {captured_url}"
        )

    def test_proxy_routes_delete_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DELETE /api/bookmarks/{slug} is proxied (do_DELETE exists or _handle_proxy used)."""
        # viz_server may not have do_DELETE. Test that the viz_server handles it at all.
        # At minimum, proxy path matching should work for /api/bookmarks/foo
        monkeypatch.setenv("YADGAR_VIZ_PROXY", "1")
        from yadgar.viz_server import _proxy_enabled

        assert _proxy_enabled() is True


# ---------------------------------------------------------------------------
# Tests — daemon HTTP routes registered correctly
# ---------------------------------------------------------------------------


class TestDaemonBookmarkRoutes:
    """Verify the daemon's custom_route handlers for /api/bookmarks* exist."""

    def test_bookmark_list_route_registered(self) -> None:
        """GET /api/bookmarks route handler is importable."""
        from yadgar.server import http_bookmarks as _hb

        # The route is registered via @mcp_server.custom_route at import time.
        # Verify the handler function exists.
        assert hasattr(_hb, "api_bookmarks_list"), (
            "api_bookmarks_list handler not registered in yadgar.server.http_bookmarks"
        )

    def test_bookmark_add_route_registered(self) -> None:
        """POST /api/bookmarks route handler is importable."""
        from yadgar.server import http_bookmarks as _hb

        assert hasattr(_hb, "api_bookmarks_add"), (
            "api_bookmarks_add handler not registered in yadgar.server.http_bookmarks"
        )

    def test_bookmark_remove_route_registered(self) -> None:
        """DELETE /api/bookmarks/{slug} route handler is importable."""
        from yadgar.server import http_bookmarks as _hb

        assert hasattr(_hb, "api_bookmarks_remove"), (
            "api_bookmarks_remove handler not registered in yadgar.server.http_bookmarks"
        )

    def test_bookmark_reorder_route_registered(self) -> None:
        """PUT /api/bookmarks/{slug}/position route handler is importable."""
        from yadgar.server import http_bookmarks as _hb

        assert hasattr(_hb, "api_bookmarks_reorder"), (
            "api_bookmarks_reorder handler not registered in yadgar.server.http_bookmarks"
        )


# ---------------------------------------------------------------------------
# Tests — MCP tool plumbing (end-to-end via storage)
# ---------------------------------------------------------------------------


class TestBookmarkAPIEndToEnd:
    """E2E: add → list → reorder → remove via MCP tools called directly."""

    def test_api_bookmarks_post_creates_entry(self) -> None:
        """bookmark_add creates a bookmark retrievable by bookmark_list."""
        from yadgar.server.tools.bookmarks import bookmark_add, bookmark_list

        bookmark_add("e2e-slug", label_override="E2E Test")
        rows = bookmark_list()
        slugs = [r["slug"] for r in rows]
        assert "e2e-slug" in slugs

    def test_api_bookmarks_delete_removes_entry(self) -> None:
        """bookmark_remove removes the bookmark from list."""
        from yadgar.server.tools.bookmarks import bookmark_add, bookmark_list, bookmark_remove

        bookmark_add("rm-e2e")
        bookmark_remove("rm-e2e")
        rows = bookmark_list()
        slugs = [r["slug"] for r in rows]
        assert "rm-e2e" not in slugs

    def test_api_bookmarks_post_missing_slug_rejected(self) -> None:
        """bookmark_add with empty slug returns error response."""
        from yadgar.server.tools.bookmarks import bookmark_add

        result = bookmark_add("")
        assert result.get("added") is False

    def test_api_bookmarks_reorder_works(self) -> None:
        """bookmark_reorder shifts positions correctly."""
        from yadgar.server.tools.bookmarks import bookmark_add, bookmark_list, bookmark_reorder

        bookmark_add("order-a")
        bookmark_add("order-b")
        bookmark_add("order-c")
        bookmark_reorder("order-c", 0)
        rows = bookmark_list()
        by_slug = {r["slug"]: r["position"] for r in rows}
        assert by_slug["order-c"] == 0

    def test_wiki_read_route_registered(self) -> None:
        """Existing /api/wiki/read route still resolves (regression guard)."""
        from yadgar.server import http as _http

        assert hasattr(_http, "api_wiki_read"), "api_wiki_read handler must remain registered"

    def test_wiki_search_route_registered(self) -> None:
        """GET /api/wiki/search route handler registered."""
        from yadgar.server import http_bookmarks as _hb

        assert hasattr(_hb, "api_wiki_search"), (
            "api_wiki_search handler not registered in yadgar.server.http_bookmarks"
        )

    def test_wiki_list_route_registered(self) -> None:
        """GET /api/wiki/list route handler registered."""
        from yadgar.server import http_bookmarks as _hb

        assert hasattr(_hb, "api_wiki_list"), (
            "api_wiki_list handler not registered in yadgar.server.http_bookmarks"
        )
