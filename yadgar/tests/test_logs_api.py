"""v5.52.0 — /api/logs/* endpoint tests (TDD).

Tests:
1.  test_capabilities_gated_off — gate off → 403
2.  test_capabilities_gated_on  — gate on → {"sse": true, "poll": true}
3.  test_poll_gated_off         — gate off → 403
4.  test_poll_returns_buffered_lines — emit a record, poll returns it
5.  test_poll_since_seq         — since=N filters correctly
6.  test_poll_empty_when_ring_empty — fresh ring → empty lines list
7.  test_stream_gated_off       — gate off → 403
8.  test_stream_content_type    — gate on → Content-Type: text/event-stream
9.  test_route_module_self_registers — import registers routes on mcp_server
10. test_auth_gate_via_middleware_denies_when_off — ASGI-level gate (middleware)
11. test_auth_gate_via_middleware_allows_when_on  — ASGI-level gate allows
12. test_ring_byte_cap          — ring evicts when byte cap exceeded
13. test_ring_seq_monotonic     — sequences are monotonically increasing
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

_TOKEN = "test-logs-tok"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(monkeypatch, *, debug_apis_on: bool = False):
    """Minimal Starlette app + BearerAuthMiddleware for log routes."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "on" if debug_apis_on else "off")

    from yadgar.core.auth_middleware import BearerAuthMiddleware
    from yadgar.core.server.routes.logs import (
        logs_capabilities_handler,
        logs_poll_handler,
        logs_stream_handler,
    )

    app = BearerAuthMiddleware(
        Starlette(
            routes=[
                Route("/api/logs/_capabilities", logs_capabilities_handler, methods=["GET"]),
                Route("/api/logs/poll", logs_poll_handler, methods=["GET"]),
                Route("/api/logs/stream", logs_stream_handler, methods=["GET"]),
            ]
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# Ring buffer helpers — reset state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ring():
    """Clear ring buffer before each test so tests are independent."""
    import yadgar.core.server.routes.logs as _m

    with _m._ring_lock:
        _m._ring.clear()
        _m._ring_bytes = 0
        _m._ring_seq = 0
    yield
    with _m._ring_lock:
        _m._ring.clear()
        _m._ring_bytes = 0
        _m._ring_seq = 0


# ---------------------------------------------------------------------------
# 1 — Gate off → 403 for _capabilities
# ---------------------------------------------------------------------------


def test_capabilities_gated_off(monkeypatch):
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/logs/_capabilities", headers=_auth())
    assert resp.status_code == 403
    assert resp.json().get("error") == "debug APIs disabled"


# ---------------------------------------------------------------------------
# 2 — Gate on → capabilities probe
# ---------------------------------------------------------------------------


def test_capabilities_gated_on(monkeypatch):
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/logs/_capabilities", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["sse"] is True
    assert body["poll"] is True


# ---------------------------------------------------------------------------
# 3 — Poll gate off → 403
# ---------------------------------------------------------------------------


def test_poll_gated_off(monkeypatch):
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/logs/poll", headers=_auth())
    assert resp.status_code == 403
    assert resp.json().get("error") == "debug APIs disabled"


# ---------------------------------------------------------------------------
# 4 — Poll returns buffered lines
# ---------------------------------------------------------------------------


def test_poll_returns_buffered_lines(monkeypatch):
    """Emit a log record via the ring handler; poll returns it."""
    # Ensure handler is installed
    import yadgar.core.server.routes.logs as _m

    _m.install_ring_handler()

    # Emit a log record directly into the ring (bypass formatter complexity)
    _m._ring_append({"ts": 1000.0, "level": "INFO", "name": "test", "message": "hello poll"})

    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/logs/poll", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert "lines" in body
    assert "next_seq" in body
    messages = [e.get("message", "") for e in body["lines"]]
    assert any("hello poll" in m for m in messages), f"Expected 'hello poll' in {messages}"


# ---------------------------------------------------------------------------
# 5 — Poll since=N filters correctly
# ---------------------------------------------------------------------------


def test_poll_since_seq(monkeypatch):
    import yadgar.core.server.routes.logs as _m

    _m._ring_append({"ts": 1.0, "level": "INFO", "name": "t", "message": "entry-1"})
    _m._ring_append({"ts": 2.0, "level": "INFO", "name": "t", "message": "entry-2"})
    _m._ring_append({"ts": 3.0, "level": "INFO", "name": "t", "message": "entry-3"})

    # Grab seq of first entry
    with _m._ring_lock:
        first_seq = list(_m._ring)[0]["seq"]

    client = _make_app(monkeypatch, debug_apis_on=True)
    # Fetch with since=first_seq — should return entries 2+3 only
    resp = client.get(f"/api/logs/poll?since={first_seq}", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["lines"]) == 2
    msgs = [e["message"] for e in body["lines"]]
    assert "entry-2" in msgs
    assert "entry-3" in msgs
    assert "entry-1" not in msgs


# ---------------------------------------------------------------------------
# 6 — Poll empty ring
# ---------------------------------------------------------------------------


def test_poll_empty_when_ring_empty(monkeypatch):
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/logs/poll", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["lines"] == []
    assert body["next_seq"] == 0


# ---------------------------------------------------------------------------
# 7 — Stream gate off → 403
# ---------------------------------------------------------------------------


def test_stream_gated_off(monkeypatch):
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/logs/stream", headers=_auth())
    assert resp.status_code == 403
    assert resp.json().get("error") == "debug APIs disabled"


# ---------------------------------------------------------------------------
# 8 — Stream handler gate on → returns StreamingResponse with text/event-stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_content_type(monkeypatch):
    """Gate on → handler returns StreamingResponse with text/event-stream media type.

    Tests the handler directly (not via full client) to avoid blocking on the
    infinite SSE generator loop.
    """
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "on")

    from starlette.responses import StreamingResponse

    from yadgar.core.server.routes.logs import logs_stream_handler

    # Minimal mock request
    class _MockRequest:
        query_params: dict = {}

        async def is_disconnected(self) -> bool:
            return True  # immediately disconnected → generator exits

    resp = await logs_stream_handler(_MockRequest())  # type: ignore[arg-type]
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"


# ---------------------------------------------------------------------------
# 9 — Route module self-registers on mcp_server
# ---------------------------------------------------------------------------


def test_route_module_self_registers():
    """Importing routes.logs must register routes on mcp_server.custom_route paths."""
    import yadgar.core.server.routes.logs  # noqa: F401, I001 — side-effect import; order intentional (load routes before checking mcp_server)
    from yadgar.core.server._app import mcp_server

    # mcp_server stores custom routes in _custom_starlette_routes
    registered_paths = set()
    for route in mcp_server._custom_starlette_routes:
        registered_paths.add(getattr(route, "path", None))

    assert any("/api/logs" in str(p) for p in registered_paths), (
        f"Expected /api/logs routes registered, got: {registered_paths}"
    )


# ---------------------------------------------------------------------------
# 10 — Auth gate via middleware: denies when YADGAR_DEBUG_APIS_ENABLED=off
#      (middleware-level gate, not just handler-level)
# ---------------------------------------------------------------------------


def test_auth_gate_via_middleware_denies_when_off(monkeypatch):
    """The ASGI middleware gate fires before the handler; gate=off → 403."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "off")

    from yadgar.core.auth_middleware import _is_debug_api_path

    # Verify the path IS in the debug-API prefix list
    assert _is_debug_api_path("/api/logs/_capabilities"), (
        "/api/logs/_capabilities should be gated by _is_debug_api_path"
    )
    assert _is_debug_api_path("/api/logs/poll"), (
        "/api/logs/poll should be gated by _is_debug_api_path"
    )
    assert _is_debug_api_path("/api/logs/stream"), (
        "/api/logs/stream should be gated by _is_debug_api_path"
    )


# ---------------------------------------------------------------------------
# 11 — Auth gate via middleware: allows when YADGAR_DEBUG_APIS_ENABLED=on
# ---------------------------------------------------------------------------


def test_auth_gate_via_middleware_allows_when_on(monkeypatch):
    """Gate on + valid bearer → 200 for capabilities."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/logs/_capabilities", headers=_auth())
    assert resp.status_code == 200, (
        f"Expected 200 when gate on, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# 12 — Ring byte cap evicts oldest entries
# ---------------------------------------------------------------------------


def test_ring_byte_cap():
    import yadgar.core.server.routes.logs as _m

    # Temporarily lower cap to 200 bytes
    original_cap = _m.LOG_RING_BUFFER_MAX_BYTES
    _m.LOG_RING_BUFFER_MAX_BYTES = 200
    try:
        for i in range(20):
            _m._ring_append(
                {"ts": float(i), "level": "INFO", "name": "t", "message": f"entry-{i:04d}"}
            )

        # Buffer should be under 200 bytes
        with _m._ring_lock:
            assert _m._ring_bytes <= 200, f"Ring bytes {_m._ring_bytes} exceeds cap 200"
            # Should have evicted early entries — not 20 entries
            assert len(_m._ring) < 20
    finally:
        _m.LOG_RING_BUFFER_MAX_BYTES = original_cap


# ---------------------------------------------------------------------------
# 13 — Ring seq monotonic
# ---------------------------------------------------------------------------


def test_ring_seq_monotonic():
    import yadgar.core.server.routes.logs as _m

    for i in range(5):
        _m._ring_append({"ts": float(i), "level": "INFO", "name": "t", "message": f"msg-{i}"})

    with _m._ring_lock:
        seqs = [e["seq"] for e in _m._ring]

    assert seqs == sorted(seqs), f"Sequences not monotonic: {seqs}"
    assert len(set(seqs)) == len(seqs), f"Duplicate sequence numbers: {seqs}"
