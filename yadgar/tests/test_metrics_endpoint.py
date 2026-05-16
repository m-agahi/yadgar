"""§15 Prometheus /metrics endpoint tests.

Tests:
- /metrics returns valid Prometheus exposition format
- /metrics is accessible without auth token (exempt path)
- YADGAR_METRICS_ENABLED=False returns 404
- Response Content-Type is text/plain
- Standard collectors present (counter/gauge/histogram samples)
"""

import os

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _make_metrics_app(metrics_enabled: bool = True, require_auth: bool = True):
    """Build a test ASGI app with the metrics route + auth middleware."""
    os.environ["YADGAR_REQUIRE_AUTH"] = "1" if require_auth else "0"
    os.environ["YADGAR_MCP_AUTH_TOKEN"] = "test-token"
    os.environ["YADGAR_METRICS_ENABLED"] = "1" if metrics_enabled else "0"

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    return BearerAuthMiddleware(app)


def test_metrics_returns_200(monkeypatch):
    """/metrics returns HTTP 200."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok")

    from starlette.applications import Starlette
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.metrics import metrics_handler

    app = BearerAuthMiddleware(
        Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    )
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_metrics_not_auth_required(monkeypatch):
    """/metrics is accessible without Authorization header even when REQUIRE_AUTH=True."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "secure-token")

    from starlette.applications import Starlette
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.metrics import metrics_handler

    app = BearerAuthMiddleware(
        Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    )
    client = TestClient(app, raise_server_exceptions=True)
    # No Authorization header — should still pass (/metrics is exempt)
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_metrics_disabled_returns_404(monkeypatch):
    """When YADGAR_METRICS_ENABLED=0, /metrics returns 404."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "0")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from starlette.applications import Starlette
    from starlette.routing import Route

    from yadgar.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/metrics")
    assert resp.status_code == 404


def test_metrics_content_type(monkeypatch):
    """/metrics Content-Type includes text/plain."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from starlette.applications import Starlette
    from starlette.routing import Route

    from yadgar.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")


def test_metrics_prometheus_format(monkeypatch):
    """Response body contains valid Prometheus exposition format lines."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from starlette.applications import Starlette
    from starlette.routing import Route

    from yadgar.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    # Prometheus format: lines starting with # TYPE or metric name
    non_comment_lines = [line for line in body.splitlines() if line and not line.startswith("#")]
    # Must have at least one metric line
    assert len(non_comment_lines) > 0, "Expected at least one metric line in /metrics output"
    # Each metric line should have format: name [labels] value
    for line in non_comment_lines[:5]:  # check first few
        parts = line.split()
        assert len(parts) >= 2, f"Malformed metric line: {line!r}"


def test_metrics_contains_expected_collectors(monkeypatch):
    """Key collector names are present in /metrics output."""
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from starlette.applications import Starlette
    from starlette.routing import Route

    from yadgar.metrics import metrics_handler

    app = Starlette(routes=[Route("/metrics", metrics_handler, methods=["GET"])])
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/metrics")
    body = resp.text
    # Must contain at least one of our named collectors
    expected = ["yadgar_queue_depth", "yadgar_requests_total", "yadgar_consolidation"]
    found = [name for name in expected if name in body]
    assert len(found) >= 1, f"Expected at least one of {expected} in /metrics, got:\n{body[:500]}"
