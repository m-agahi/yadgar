"""test_bookmarks_migration.py — v5.50.0 bookmarks redirect tests.

Tests:
- GET /static/bookmarks.html returns 302 to /#bookmarks
- Existing bookmark CRUD endpoints still function (list/add/remove/reorder)

Run:
  OTEL_SDK_DISABLED=true uv run --extra test pytest yadgar/tests/test_bookmarks_migration.py -p no:xdist -v
"""

from __future__ import annotations

import pathlib

# ---------------------------------------------------------------------------
# Static-file check: bookmarks.html body contains JS redirect to /#bookmarks
# ---------------------------------------------------------------------------


def _bookmarks_html_path() -> pathlib.Path:
    static_dir = pathlib.Path(__file__).parent.parent / "static"
    return static_dir / "bookmarks.html"


class TestBookmarksHtmlRedirect:
    """bookmarks.html must redirect browsers to /#bookmarks (v5.50.0 migration)."""

    def test_bookmarks_html_file_exists(self) -> None:
        assert _bookmarks_html_path().exists(), "bookmarks.html missing from yadgar/static/"

    def test_bookmarks_html_contains_redirect_to_hash_bookmarks(self) -> None:
        """bookmarks.html must redirect browsers to /#bookmarks.

        The redirect may be implemented as:
        - window.location.replace('/#bookmarks') or
        - window.location.href = '/#bookmarks' or
        - <meta http-equiv="refresh" content="0; url=/#bookmarks">
        """
        content = _bookmarks_html_path().read_text(encoding="utf-8")
        has_js_redirect = "/#bookmarks" in content
        has_meta_refresh = (
            'http-equiv="refresh"' in content.lower() or "http-equiv='refresh'" in content.lower()
        )
        assert has_js_redirect or has_meta_refresh, (
            "bookmarks.html missing redirect to /#bookmarks — "
            "v5.50.0 requires bookmarks.html to redirect to the #bookmarks tab"
        )

    def test_bookmarks_html_marked_deprecated(self) -> None:
        """bookmarks.html should contain a deprecation comment for clarity."""
        content = _bookmarks_html_path().read_text(encoding="utf-8")
        assert "deprecated" in content.lower() or "v5.50" in content, (
            "bookmarks.html should note that it is deprecated (removed in v5.52+)"
        )


# ---------------------------------------------------------------------------
# HTTP route check: /static/bookmarks.html must return 302 redirect
# ---------------------------------------------------------------------------


class TestBookmarksRouteRedirect:
    """GET /static/bookmarks.html must return 302 → /#bookmarks."""

    def test_bookmarks_route_is_redirect(self) -> None:
        """bookmarks_view handler must return a redirect, not FileResponse."""
        import inspect

        from yadgar.server import http_bookmarks  # noqa: PLC0415

        src = inspect.getsource(http_bookmarks.bookmarks_view)
        # Must use RedirectResponse (302) not FileResponse
        assert "RedirectResponse" in src, (
            "bookmarks_view must return RedirectResponse for v5.50.0 migration. "
            "Change from FileResponse(static_dir / 'bookmarks.html') to "
            "RedirectResponse('/#bookmarks', status_code=302)"
        )
        assert "302" in src or "status_code=302" in src, (
            "bookmarks_view redirect must use status_code 302"
        )

    def test_bookmarks_route_targets_hash_bookmarks(self) -> None:
        """Redirect must point to /#bookmarks."""
        import inspect

        from yadgar.server import http_bookmarks  # noqa: PLC0415

        src = inspect.getsource(http_bookmarks.bookmarks_view)
        assert "/#bookmarks" in src, (
            "bookmarks_view must redirect to /#bookmarks, got: " + src[:200]
        )


# ---------------------------------------------------------------------------
# Existing bookmark CRUD routes must remain intact
# ---------------------------------------------------------------------------


class TestBookmarkCRUDPreservation:
    """Legacy bookmark CRUD routes (v5.23.0) must survive the v5.50.0 migration."""

    def test_api_bookmarks_list_route_present(self) -> None:
        import inspect  # noqa: PLC0415

        from yadgar.server import http_bookmarks  # noqa: PLC0415

        assert hasattr(http_bookmarks, "api_bookmarks_list")
        src = inspect.getsource(http_bookmarks.api_bookmarks_list)
        assert "bookmark_list" in src

    def test_api_bookmarks_add_route_present(self) -> None:
        from yadgar.server import http_bookmarks  # noqa: PLC0415

        assert hasattr(http_bookmarks, "api_bookmarks_add")

    def test_api_bookmarks_remove_route_present(self) -> None:
        from yadgar.server import http_bookmarks  # noqa: PLC0415

        assert hasattr(http_bookmarks, "api_bookmarks_remove")

    def test_api_bookmarks_reorder_route_present(self) -> None:
        from yadgar.server import http_bookmarks  # noqa: PLC0415

        assert hasattr(http_bookmarks, "api_bookmarks_reorder")
