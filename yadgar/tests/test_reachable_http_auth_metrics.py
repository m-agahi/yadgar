"""PR-B failing tests: backend_reachable, requests_total, mcp_auth_check_duration_ms.

TDD — all tests must fail before implementation. After wiring:
1. yadgar_backend_reachable{endpoint="/rerank/ce"} = 1.0 on success, 0.0 on breaker open.
2. yadgar_requests_total{route=...} increments on every HTTP request.
3. Unmatched routes get route="<unmatched>", not raw URL.
4. yadgar_mcp_auth_check_duration_ms_count increments on authenticated requests.
5. Exempt paths (/health, /metrics) do NOT increment auth histogram.
6. Auth fail (bad token) still observes the duration histogram.
"""

from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_counter_value(metric, **labels) -> float:
    """Read a labeled prometheus_client Counter's current value."""
    return metric.labels(**labels)._value.get()


def _get_hist_count(metric) -> float:
    """Read _count from a labelless Histogram via the samples API."""
    for fam in metric.collect():
        for s in fam.samples:
            if s.name.endswith("_count") and not s.labels:
                return s.value
    return 0.0


# ---------------------------------------------------------------------------
# 1 + breaker-open: yadgar_backend_reachable
# ---------------------------------------------------------------------------


def test_backend_reachable_set_on_success():
    """yadgar_backend_reachable{endpoint="/rerank/ce"} = 1.0 after probe success."""
    from yadgar.metrics import yadgar_backend_reachable
    from yadgar.ml_client import _CircuitBreaker

    cb = _CircuitBreaker(
        endpoint="/rerank/ce",
        failure_threshold=3,
        open_duration_sec=30.0,
    )
    # Record a success → should drive reachable to 1
    cb.record_success()

    val = yadgar_backend_reachable.labels(endpoint="/rerank/ce")._value.get()
    assert val == 1.0, f"Expected 1.0, got {val}"


def test_backend_reachable_clears_on_breaker_open():
    """yadgar_backend_reachable{endpoint="/rerank/ce"} = 0.0 when breaker opens."""
    from yadgar.metrics import yadgar_backend_reachable
    from yadgar.ml_client import _CircuitBreaker

    cb = _CircuitBreaker(
        endpoint="/rerank/ce",
        failure_threshold=2,
        open_duration_sec=30.0,
    )
    # First get to 1 via success
    cb.record_success()
    assert yadgar_backend_reachable.labels(endpoint="/rerank/ce")._value.get() == 1.0

    # Now trigger enough failures to open the breaker
    cb.record_failure()
    cb.record_failure()  # threshold=2 → OPEN

    val = yadgar_backend_reachable.labels(endpoint="/rerank/ce")._value.get()
    assert val == 0.0, f"Expected 0.0 after breaker opens, got {val}"


# ---------------------------------------------------------------------------
# 2: yadgar_requests_total increments on every HTTP request
# ---------------------------------------------------------------------------


def _make_counting_app(require_auth: bool = False, token: str = "tok") -> TestClient:
    """Build a minimal Starlette app with RequestLoggingMiddleware + auth."""
    os.environ["YADGAR_REQUIRE_AUTH"] = "1" if require_auth else "0"
    if require_auth:
        os.environ["YADGAR_MCP_AUTH_TOKEN"] = token

    async def _hello(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    app = Starlette(routes=[Route("/api/hello", _hello, methods=["GET"])])
    return TestClient(BearerAuthMiddleware(RequestLoggingMiddleware(app)))


def test_requests_total_increments_on_http_request(monkeypatch):
    """yadgar_requests_total{route="/api/hello"} increments by N after N requests."""
    from yadgar.metrics import yadgar_requests_total

    before = _get_counter_value(yadgar_requests_total, route="/api/hello")

    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    async def _hello(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    app = Starlette(routes=[Route("/api/hello", _hello, methods=["GET"])])
    client = TestClient(BearerAuthMiddleware(RequestLoggingMiddleware(app)))

    client.get("/api/hello")
    client.get("/api/hello")
    client.get("/api/hello")

    after = _get_counter_value(yadgar_requests_total, route="/api/hello")
    assert after - before == 3.0, f"Expected delta=3, got delta={after - before}"


# ---------------------------------------------------------------------------
# 3: Unmatched routes get route="<unmatched>"
# ---------------------------------------------------------------------------


def test_requests_total_unmatched_route(monkeypatch):
    """404 paths are counted under route='<unmatched>', not the raw URL."""
    from yadgar.metrics import yadgar_requests_total

    before = _get_counter_value(yadgar_requests_total, route="<unmatched>")

    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    # App with NO routes — every request is unmatched
    app = Starlette(routes=[])
    client = TestClient(
        BearerAuthMiddleware(RequestLoggingMiddleware(app)), raise_server_exceptions=False
    )

    client.get("/this/path/does/not/exist")
    client.get("/another/mystery/url?q=blowup")

    after = _get_counter_value(yadgar_requests_total, route="<unmatched>")
    assert after - before == 2.0, f"Expected delta=2, got delta={after - before}"


# ---------------------------------------------------------------------------
# 4: mcp_auth_check_duration_ms_count increments on authenticated requests
# ---------------------------------------------------------------------------


def test_auth_histogram_increments_on_authenticated_request(monkeypatch):
    """After M authenticated requests, auth histogram count increases by M."""
    from yadgar.metrics import yadgar_mcp_auth_check_duration_ms

    before = _get_hist_count(yadgar_mcp_auth_check_duration_ms)

    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "good-token")

    async def _hello(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    app = Starlette(routes=[Route("/api/hello", _hello, methods=["GET"])])
    client = TestClient(BearerAuthMiddleware(RequestLoggingMiddleware(app)))

    M = 3
    for _ in range(M):
        resp = client.get("/api/hello", headers={"Authorization": "Bearer good-token"})
        assert resp.status_code == 200

    after = _get_hist_count(yadgar_mcp_auth_check_duration_ms)
    assert after - before == float(M), f"Expected delta={M}, got delta={after - before}"


# ---------------------------------------------------------------------------
# 5: Exempt paths do NOT increment auth histogram
# ---------------------------------------------------------------------------


def test_auth_histogram_skipped_for_exempt_paths(monkeypatch):
    """Exempt paths (/health, /metrics) do not increment auth histogram."""
    from yadgar.metrics import yadgar_mcp_auth_check_duration_ms

    before = _get_hist_count(yadgar_mcp_auth_check_duration_ms)

    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "good-token")

    async def _health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def _metrics_h(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/metrics", _metrics_h, methods=["GET"]),
        ]
    )
    client = TestClient(BearerAuthMiddleware(RequestLoggingMiddleware(app)))

    client.get("/health")
    client.get("/metrics")

    after = _get_hist_count(yadgar_mcp_auth_check_duration_ms)
    assert after == before, f"Exempt paths should not increment histogram; delta={after - before}"


# ---------------------------------------------------------------------------
# 6: Auth fail (bad token) still observes histogram
# ---------------------------------------------------------------------------


def test_auth_histogram_increments_on_auth_failure(monkeypatch):
    """401 bad-token response still observes the auth duration histogram."""
    from yadgar.metrics import yadgar_mcp_auth_check_duration_ms

    before = _get_hist_count(yadgar_mcp_auth_check_duration_ms)

    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "correct-token")

    async def _hello(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    app = Starlette(routes=[Route("/api/hello", _hello, methods=["GET"])])
    client = TestClient(
        BearerAuthMiddleware(RequestLoggingMiddleware(app)), raise_server_exceptions=False
    )

    resp = client.get("/api/hello", headers={"Authorization": "Bearer WRONG-TOKEN"})
    assert resp.status_code == 401

    after = _get_hist_count(yadgar_mcp_auth_check_duration_ms)
    assert after - before == 1.0, f"Auth fail should still observe; delta={after - before}"
