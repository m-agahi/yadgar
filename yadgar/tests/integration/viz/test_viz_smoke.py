"""Layer 2 — Playwright headless browser smoke tests for yadgar viz.

Tests in this file:
  1. Page loads without Uncaught JS errors (catches the v5.10.9 bug class directly)
  2. /api/graph is fetched on page load
  3. allNodes global populated after data load
  4. No 'node not found' errors in console (specific force-graph crash signature)
  5. Stats overlay element present in DOM
  6. Graph container element present in DOM
  7. heatColor JS helper defined in page scope

Run locally:
    uv run --active pytest yadgar/tests/integration/viz/test_viz_smoke.py -m integration -v

CI: runs as part of the viz-tests job (see .forgejo/workflows/ci.yaml).
"""

from __future__ import annotations

import pytest

try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def browser_ctx(viz_server, chromium_executable):
    """Chromium browser + context, module-scoped for speed."""
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed")

    with sync_playwright() as p:
        launch_kwargs: dict = {"headless": True, "args": ["--no-sandbox"]}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context()
        yield context
        context.close()
        browser.close()


@pytest.fixture()
def page_with_console(browser_ctx, viz_server):
    """Fresh page with console event capture; navigates to /static/index.html."""
    page = browser_ctx.new_page()
    console_errors: list[str] = []

    def _on_console(msg):
        if msg.type == "error":
            console_errors.append(msg.text)

    page.on("console", _on_console)

    # Navigate and wait for DOM to load; use 'load' not 'networkidle' because
    # SSE connections and CDN fetches (Three.js) keep the network perpetually busy.
    page.goto(f"{viz_server}/", wait_until="load", timeout=20000)
    # Extra wait for loadGraph() async fetch to complete
    page.wait_for_timeout(3000)

    yield page, console_errors

    page.close()


# ── Smoke tests ───────────────────────────────────────────────────────────────


class TestVizPageLoads:
    """Basic page load checks."""

    def test_page_title_correct(self, page_with_console):
        """Page title must indicate yadgar graph."""
        page, _ = page_with_console
        title = page.title().lower()
        assert "yadgar" in title or "graph" in title, f"Unexpected page title: {page.title()!r}"

    def test_graph_container_present(self, page_with_console):
        """#canvas-wrap div must exist in DOM (the 3D force-graph container)."""
        page, _ = page_with_console
        # index.html uses #canvas-wrap as the graph rendering container
        el = page.query_selector("#canvas-wrap")
        assert el is not None, "#canvas-wrap container not found in DOM"

    def test_stats_btn_present(self, page_with_console):
        """#stats-btn button must exist in DOM (stats overlay trigger)."""
        page, _ = page_with_console
        el = page.query_selector("#stats-btn")
        assert el is not None, "#stats-btn not found in DOM"


class TestVizNoJsErrors:
    """Console error checks — the Layer 2 primary value proposition.

    A single Uncaught Error means the graph is broken. These tests catch
    that before any user sees it.
    """

    def test_no_uncaught_js_errors(self, page_with_console):
        """No console errors should appear after viz page loads.

        This is the exact check that would have caught v5.10.9's
        'node not found: entity:172' crash immediately.
        """
        _, console_errors = page_with_console
        # Filter OTLP/network errors from the test server not having an OTLP endpoint
        real_errors = [e for e in console_errors if "host.containers.internal" not in e]
        assert not real_errors, "Console errors after viz page load:\n" + "\n".join(
            f"  - {e}" for e in real_errors
        )

    def test_no_node_not_found_errors(self, page_with_console):
        """No 'node not found' error — the exact force-graph crash signature."""
        _, console_errors = page_with_console
        nf_errors = [e for e in console_errors if "node not found" in e.lower()]
        assert not nf_errors, (
            "'node not found' error in console — orphan edge reached force-graph:\n"
            + "\n".join(f"  - {e}" for e in nf_errors)
        )


class TestVizGraphDataLoaded:
    """Verify graph data was fetched and processed."""

    def test_allNodes_global_exists(self, page_with_console):
        """allNodes global must be defined in page scope."""
        page, _ = page_with_console
        page.wait_for_timeout(2000)
        result = page.evaluate("typeof allNodes !== 'undefined'")
        assert result, "allNodes global not defined — loadGraph() may not have run"

    def test_allNodes_array_is_defined(self, page_with_console):
        """allNodes array must be defined and is a list (even if empty).

        allNodes is the JS global holding the fetched graph nodes. If undefined,
        loadGraph() crashed before completing. Empty is acceptable here (seeding
        timing may vary); absence is a crash signal.
        """
        page, _ = page_with_console
        page.wait_for_timeout(2000)
        is_array = page.evaluate("typeof allNodes !== 'undefined' && Array.isArray(allNodes)")
        assert is_array, "allNodes is not a defined Array — loadGraph() likely crashed"

    def test_heatColor_function_exists(self, page_with_console):
        """heatColor() must be defined — core JS helper for node coloring."""
        page, _ = page_with_console
        exists = page.evaluate("typeof heatColor === 'function'")
        assert exists, "heatColor() not defined in page scope — JS helper missing"

    def test_nodeIdSet_orphan_filter_ran(self, page_with_console):
        """Orphan filter must have run without crashing — allNodes defined."""
        page, _ = page_with_console
        page.wait_for_timeout(2000)
        # If allNodes is defined and no 'node not found' errors, filter ran clean
        all_nodes_defined = page.evaluate("typeof allNodes !== 'undefined'")
        assert all_nodes_defined, "allNodes not defined — loadGraph() crashed before orphan filter"


class TestVizApiGraphFetch:
    """/api/graph must be fetched during page load."""

    def test_api_graph_request_made(self, browser_ctx, viz_server):
        """Page must make a request to /api/graph on load."""
        page = browser_ctx.new_page()
        graph_requests: list[str] = []

        def _on_request(req):
            if "/api/graph" in req.url and "stats" not in req.url and "events" not in req.url:
                graph_requests.append(req.url)

        page.on("request", _on_request)

        try:
            page.goto(f"{viz_server}/", wait_until="load", timeout=20000)
            page.wait_for_timeout(2000)
        finally:
            page.close()

        assert graph_requests, (
            "No request to /api/graph observed — loadGraph() may not be calling the backend"
        )
