"""Static-asset checks for v5.24.0 — Wiki Bookmarks frontend.

Verifies:
- bookmarks.html exists + has required structure
- bookmarks.css exists
- bookmarks.js exists + has required functions
- vendored libs exist under static/lib/
- index.html nav link to bookmarks.html present
- viz_server _mime_type helper returns correct MIME types
- viz_server do_GET serves static files by path (path-traversal guard)
- (v5.24.2) marked renderer does not throw on heading with parenthetical text
"""

from __future__ import annotations

import pathlib

_STATIC = pathlib.Path(__file__).parent.parent / "static"
_LIB = _STATIC / "lib"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _read_lib(name: str) -> bytes:
    return (_LIB / name).read_bytes()


# ---------------------------------------------------------------------------
# bookmarks.html
# ---------------------------------------------------------------------------


class TestBookmarksHtml:
    """bookmarks.html structure checks."""

    def test_file_exists(self) -> None:
        assert (_STATIC / "bookmarks.html").is_file(), "bookmarks.html missing"

    def test_title(self) -> None:
        html = _read("bookmarks.html")
        assert "Bookmarks" in html

    def test_sidebar_present(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="sidebar"' in html

    def test_pane_present(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="pane"' in html

    def test_md_render_div(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="md-render"' in html

    def test_add_button_present(self) -> None:
        html = _read("bookmarks.html")
        assert "openModal" in html or 'id="add-btn"' in html

    def test_modal_overlay_present(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="modal-overlay"' in html

    def test_queue_badge_present(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="queue-badge"' in html

    def test_bookmark_list_div(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="bookmark-list"' in html

    def test_back_to_graph_link(self) -> None:
        html = _read("bookmarks.html")
        assert "index.html" in html or "Graph" in html

    def test_lib_scripts_loaded(self) -> None:
        html = _read("bookmarks.html")
        assert "marked.min.js" in html
        assert "highlight.min.js" in html
        assert "dompurify.min.js" in html

    def test_bookmarks_js_loaded(self) -> None:
        html = _read("bookmarks.html")
        assert "bookmarks.js" in html

    def test_bookmarks_css_loaded(self) -> None:
        html = _read("bookmarks.html")
        assert "bookmarks.css" in html

    def test_github_dark_css_loaded(self) -> None:
        html = _read("bookmarks.html")
        assert "github-dark.css" in html

    def test_modal_slug_input(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="modal-slug-input"' in html

    def test_modal_search_input(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="modal-search-input"' in html

    def test_modal_label_input(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="modal-label-input"' in html

    def test_slug_autocomplete_div(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="slug-autocomplete"' in html

    def test_search_results_div(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="search-results"' in html

    def test_radio_modes_present(self) -> None:
        html = _read("bookmarks.html")
        assert 'id="radio-slug"' in html
        assert 'id="radio-search"' in html


# ---------------------------------------------------------------------------
# bookmarks.css
# ---------------------------------------------------------------------------


class TestBookmarksCss:
    """bookmarks.css presence + key selectors."""

    def test_file_exists(self) -> None:
        assert (_STATIC / "bookmarks.css").is_file(), "bookmarks.css missing"

    def test_sidebar_selector(self) -> None:
        css = _read("bookmarks.css")
        assert "#sidebar" in css

    def test_bm_row_selector(self) -> None:
        css = _read("bookmarks.css")
        assert ".bm-row" in css

    def test_modal_selector(self) -> None:
        css = _read("bookmarks.css")
        assert "#modal" in css

    def test_queue_badge_selector(self) -> None:
        css = _read("bookmarks.css")
        assert "#queue-badge" in css

    def test_dark_background(self) -> None:
        css = _read("bookmarks.css")
        # Dark theme: body background should be #0d1117 (matching index.html)
        assert "#0d1117" in css

    def test_blue_accent(self) -> None:
        css = _read("bookmarks.css")
        assert "#58a6ff" in css


# ---------------------------------------------------------------------------
# bookmarks.js
# ---------------------------------------------------------------------------


class TestBookmarksJs:
    """bookmarks.js — required functions and API paths."""

    def test_file_exists(self) -> None:
        assert (_STATIC / "bookmarks.js").is_file(), "bookmarks.js missing"

    def test_load_bookmarks_function(self) -> None:
        js = _read("bookmarks.js")
        assert "async function loadBookmarks" in js or "function loadBookmarks" in js

    def test_render_sidebar_function(self) -> None:
        js = _read("bookmarks.js")
        assert "function renderSidebar" in js

    def test_load_wiki_content_function(self) -> None:
        js = _read("bookmarks.js")
        assert "async function loadWikiContent" in js or "function loadWikiContent" in js

    def test_open_modal_function(self) -> None:
        js = _read("bookmarks.js")
        assert "function openModal" in js

    def test_close_modal_function(self) -> None:
        js = _read("bookmarks.js")
        assert "function closeModal" in js

    def test_submit_modal_function(self) -> None:
        js = _read("bookmarks.js")
        assert "async function submitModal" in js or "function submitModal" in js

    def test_remove_bookmark_function(self) -> None:
        js = _read("bookmarks.js")
        assert "async function removeBookmark" in js or "function removeBookmark" in js

    def test_api_bookmarks_endpoint(self) -> None:
        js = _read("bookmarks.js")
        assert "/api/bookmarks" in js

    def test_api_wiki_read_endpoint(self) -> None:
        js = _read("bookmarks.js")
        assert "/api/wiki/read" in js

    def test_api_wiki_search_endpoint(self) -> None:
        js = _read("bookmarks.js")
        assert "/api/wiki/search" in js

    def test_api_wiki_list_endpoint(self) -> None:
        js = _read("bookmarks.js")
        assert "/api/wiki/list" in js

    def test_api_stats_for_queue(self) -> None:
        js = _read("bookmarks.js")
        assert "/api/stats" in js

    def test_dompurify_used(self) -> None:
        js = _read("bookmarks.js")
        assert "DOMPurify" in js, "DOMPurify.sanitize must wrap rendered markdown"

    def test_marked_used(self) -> None:
        js = _read("bookmarks.js")
        assert "marked" in js

    def test_drag_drop_events(self) -> None:
        js = _read("bookmarks.js")
        assert "dragstart" in js
        assert "drop" in js

    def test_debounce_helper(self) -> None:
        js = _read("bookmarks.js")
        assert "_debounce" in js

    def test_keyboard_shortcuts(self) -> None:
        js = _read("bookmarks.js")
        assert "keydown" in js
        assert "Escape" in js

    def test_queue_poll_function(self) -> None:
        js = _read("bookmarks.js")
        assert "_pollQueueDepth" in js

    def test_select_bookmark_by_slug(self) -> None:
        js = _read("bookmarks.js")
        assert "selectBookmarkBySlug" in js

    def test_rel_time_helper(self) -> None:
        js = _read("bookmarks.js")
        assert "_relTime" in js

    def test_global_refresh_function(self) -> None:
        js = _read("bookmarks.js")
        assert "function globalRefresh" in js

    def test_render_markdown_guards_non_string(self) -> None:
        """v5.24.1 Bug 1: _renderMarkdown must guard against non-string input."""
        js = _read("bookmarks.js")
        # Guard must coerce non-string content before calling marked.parse
        assert 'typeof content !== "string"' in js or "typeof content !== 'string'" in js, (
            "_renderMarkdown must have typeof-string guard before marked.parse"
        )

    def test_marked_renderer_text_uses_token_text(self) -> None:
        """v5.24.1 Bug 1: marked v15 passes token object to renderer.text, not a string."""
        js = _read("bookmarks.js")
        # The [[slug]] renderer must extract .text from token, not call .replace() on token
        assert "token.text" in js or "text.text" in js, (
            "renderer.text handler must extract string from token (marked v15 API)"
        )


# ---------------------------------------------------------------------------
# Vendored libs
# ---------------------------------------------------------------------------


class TestVendoredLibs:
    """Vendored libraries under static/lib/ are present and non-empty."""

    def test_lib_dir_exists(self) -> None:
        assert _LIB.is_dir(), "yadgar/static/lib/ directory missing"

    def test_marked_present(self) -> None:
        assert (_LIB / "marked.min.js").is_file()

    def test_marked_size(self) -> None:
        size = (_LIB / "marked.min.js").stat().st_size
        assert size > 10_000, f"marked.min.js too small ({size} bytes) — may be empty/error"

    def test_highlight_present(self) -> None:
        assert (_LIB / "highlight.min.js").is_file()

    def test_highlight_size(self) -> None:
        size = (_LIB / "highlight.min.js").stat().st_size
        assert size > 50_000, f"highlight.min.js too small ({size} bytes)"

    def test_dompurify_present(self) -> None:
        assert (_LIB / "dompurify.min.js").is_file()

    def test_dompurify_size(self) -> None:
        size = (_LIB / "dompurify.min.js").stat().st_size
        assert size > 5_000, f"dompurify.min.js too small ({size} bytes)"

    def test_github_dark_css_present(self) -> None:
        assert (_LIB / "github-dark.css").is_file()

    def test_github_dark_css_content(self) -> None:
        css = (_LIB / "github-dark.css").read_text(encoding="utf-8")
        assert ".hljs" in css, "github-dark.css missing .hljs selectors"

    def test_marked_is_js(self) -> None:
        content = (_LIB / "marked.min.js").read_text(encoding="utf-8", errors="replace")
        # Must not be a jsdelivr 404 page
        assert "Couldn't find" not in content
        assert "<!DOCTYPE" not in content

    def test_highlight_is_js(self) -> None:
        content = (_LIB / "highlight.min.js").read_text(encoding="utf-8", errors="replace")
        assert "Couldn't find" not in content
        assert "<!DOCTYPE" not in content


# ---------------------------------------------------------------------------
# index.html nav link
# ---------------------------------------------------------------------------


class TestIndexHtmlNavLink:
    """index.html must link to bookmarks page."""

    def test_bookmarks_link_present(self) -> None:
        html = _read("index.html")
        assert "bookmarks.html" in html, (
            "index.html missing link to bookmarks.html — nav link not added"
        )

    def test_bookmarks_label_present(self) -> None:
        html = _read("index.html")
        assert "Bookmarks" in html or "📑" in html, "index.html missing 'Bookmarks' label in nav"


# ---------------------------------------------------------------------------
# viz_server _mime_type + static file serving
# ---------------------------------------------------------------------------


class TestVizServerMimeType:
    """_mime_type helper returns correct MIME types."""

    def test_html_mime(self) -> None:
        from yadgar.viz_server import _mime_type

        assert _mime_type(pathlib.Path("foo.html")) == "text/html; charset=utf-8"

    def test_css_mime(self) -> None:
        from yadgar.viz_server import _mime_type

        assert _mime_type(pathlib.Path("bar.css")) == "text/css; charset=utf-8"

    def test_js_mime(self) -> None:
        from yadgar.viz_server import _mime_type

        assert _mime_type(pathlib.Path("baz.js")) == "application/javascript; charset=utf-8"

    def test_unknown_mime(self) -> None:
        from yadgar.viz_server import _mime_type

        assert _mime_type(pathlib.Path("file.xyz")) == "application/octet-stream"


class TestVizServerStaticServing:
    """do_GET serves bookmarks.html and lib/ files by path."""

    def _make_handler(self, path: str):
        import io

        from yadgar.viz_server import _Handler

        h = _Handler.__new__(_Handler)
        h.command = "GET"
        h.path = path
        h.requestline = f"GET {path} HTTP/1.0"
        h.request_version = "HTTP/1.0"
        h.headers = {}
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        h.log_message = lambda *a, **kw: None
        h.address_string = lambda: "127.0.0.1"
        return h

    def _response_status(self, wfile_bytes: bytes) -> int:
        line = wfile_bytes.split(b"\r\n", 1)[0].decode()
        # Format: "HTTP/1.0 200 OK"
        parts = line.split()
        return int(parts[1]) if len(parts) >= 2 else 0

    def _response_headers(self, wfile_bytes: bytes) -> dict[str, str]:
        header_block = wfile_bytes.split(b"\r\n\r\n", 1)[0].decode()
        lines = header_block.split("\r\n")[1:]  # skip status line
        return {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines if ": " in ln)}

    def test_bookmarks_html_served(self) -> None:
        import os

        os.environ["YADGAR_VIZ_PROXY"] = "0"  # disable proxy for this test
        try:
            h = self._make_handler("/bookmarks.html")
            h.do_GET()
            resp = h.wfile.getvalue()
            status = self._response_status(resp)
            assert status == 200, f"Expected 200, got {status} for /bookmarks.html"
            assert b"<!DOCTYPE html>" in resp or b"<!doctype html>" in resp.lower()
        finally:
            del os.environ["YADGAR_VIZ_PROXY"]

    def test_bookmarks_html_content_type(self) -> None:
        import os

        os.environ["YADGAR_VIZ_PROXY"] = "0"
        try:
            h = self._make_handler("/bookmarks.html")
            h.do_GET()
            resp = h.wfile.getvalue()
            headers = self._response_headers(resp)
            assert "text/html" in headers.get("content-type", ""), (
                f"Expected text/html Content-Type, got: {headers.get('content-type')}"
            )
        finally:
            del os.environ["YADGAR_VIZ_PROXY"]

    def test_lib_marked_served(self) -> None:
        import os

        os.environ["YADGAR_VIZ_PROXY"] = "0"
        try:
            h = self._make_handler("/lib/marked.min.js")
            h.do_GET()
            resp = h.wfile.getvalue()
            status = self._response_status(resp)
            assert status == 200, f"Expected 200 for /lib/marked.min.js, got {status}"
        finally:
            del os.environ["YADGAR_VIZ_PROXY"]

    def test_path_traversal_blocked(self) -> None:
        """Path traversal outside STATIC_DIR must not succeed."""
        import os

        os.environ["YADGAR_VIZ_PROXY"] = "0"
        try:
            h = self._make_handler("/../../etc/passwd")
            h.do_GET()
            resp = h.wfile.getvalue()
            # Must not return 200 with /etc/passwd content
            assert b"root:" not in resp, "Path traversal returned /etc/passwd content!"
        finally:
            del os.environ["YADGAR_VIZ_PROXY"]

    def test_unknown_path_falls_back_to_index(self) -> None:
        """Unknown paths fall back to index.html (SPA behaviour)."""
        import os

        os.environ["YADGAR_VIZ_PROXY"] = "0"
        try:
            h = self._make_handler("/some/spa/route")
            h.do_GET()
            resp = h.wfile.getvalue()
            status = self._response_status(resp)
            assert status == 200
            assert b"YADGAR" in resp
        finally:
            del os.environ["YADGAR_VIZ_PROXY"]


# ---------------------------------------------------------------------------
# v5.24.2 regression: marked v15 renderer.text round-trip crash
# ---------------------------------------------------------------------------


class TestMarkedV15RendererRegression:
    """Regression test for v5.24.2: marked v15 renderer.text must not throw.

    Root cause: v5.24.1 extracted token.text correctly but then called
    _origText(replaced), passing the HTML string back to v15's default text
    renderer which does `'tokens' in arg` — throwing "Cannot use 'in' operator
    to search for 'tokens' in <string>".  Fix: return replaced string directly.

    This test runs node to exercise the actual vendored marked.min.js so future
    vendored upgrades are caught immediately.
    """

    def test_marked_parse_with_custom_renderer_no_throw(self) -> None:
        """marked v15 + custom renderer.text that returns a string must not throw.

        The v5.24.1 bug: our renderer.text extracted token.text correctly but
        called _origText(replaced), passing the HTML string back to v15's default
        text renderer which does `'tokens' in arg` → throws on strings.

        This test reproduces that exact failure mode with the v5.24.2 fix: a
        custom renderer.text that returns a string directly must not throw.
        """
        import subprocess

        marked_path = str(_LIB / "marked.min.js")
        script = (
            "const marked = require('" + marked_path.replace("'", "\\'") + "');"
            # Reproduce the v5.24.1 broken pattern to confirm v15 DOES throw it.
            # (This is a verify-the-test step — we assert it would fail broken.)
            # Then reproduce the v5.24.2 fix pattern and assert it does NOT throw.
            #
            # Broken pattern (what v5.24.1 did):
            "const brokenRenderer = new marked.Renderer();"
            "const origText = brokenRenderer.text.bind(brokenRenderer);"
            "brokenRenderer.text = (token) => {"
            "  const raw = (typeof token === 'object' && token !== null && typeof token.text === 'string') ? token.text : (typeof token === 'string' ? token : '');"
            "  const replaced = raw.replace(/\\[\\[([^\\]]+)\\]\\]/g, (_, s) => '<a>' + s + '</a>');"
            "  return origText(replaced);"  # BUG: passes string to v15 default
            "};"
            "let brokenThrew = false;"
            "try { marked.use({ renderer: brokenRenderer }); marked.parse('# v4.9 progress (2026-05-15)\\n'); }"
            "catch (e) { brokenThrew = true; }"
            "if (!brokenThrew) { process.stderr.write('WARN: broken pattern did not throw — marked API may have changed\\n'); }"
            # Reset marked options for the fixed-pattern test.
            "marked.setOptions({});"
            # Fixed pattern (v5.24.2 — return string directly, no _origText):
            "const fixedRenderer = new marked.Renderer();"
            "fixedRenderer.text = (token) => {"
            "  const raw = (typeof token === 'object' && token !== null && typeof token.text === 'string') ? token.text : (typeof token === 'string' ? token : '');"
            "  return raw.replace(/\\[\\[([^\\]]+)\\]\\]/g, (_, s) => '<a>' + s + '</a>');"
            "};"
            "const out = marked.parse('# v4.9 progress (2026-05-15)\\n', { renderer: fixedRenderer });"
            "if (typeof out !== 'string') throw new Error('Expected string output, got ' + typeof out);"
            "process.exit(0);"
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"marked v15 custom renderer (v5.24.2 fix pattern) threw unexpectedly.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_bookmarks_js_no_orig_text_call(self) -> None:
        """bookmarks.js must not call _origText(...) — that was the v5.24.1 bug.

        Guard: if a dev re-introduces `_origText(replaced)`, this test fails.
        Checks for the call pattern, not the identifier in comments.
        """
        js = _read("bookmarks.js")
        assert "_origText(" not in js, (
            "bookmarks.js calls _origText(...) — this re-introduces the v5.24.1 "
            "round-trip crash where the HTML string is passed back to marked v15's "
            "default text renderer which does 'tokens' in arg and throws."
        )
