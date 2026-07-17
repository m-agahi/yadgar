"""Car D — GET /api/debug/dlq endpoint tests (TDD).

The DLQ view is a debug-gated, read-only wrapper over the filesystem-backed
``dlq_inspect()`` tool. No DB access (ADR-0078 not engaged). Mirrors the
``/api/debug/read_query`` and ``/api/logs/*`` gate patterns.

Tests:
1.  test_dlq_gated_off              — gate off → 403 (handler-level)
2.  test_dlq_gated_off_middleware   — gate off → 403 (ASGI middleware level)
3.  test_dlq_gated_on_empty         — gate on, empty DLQ → {entries: [], count: 0}
4.  test_dlq_gated_on_returns_shape — gate on, seeded DLQ → dlq_inspect entry shape
5.  test_dlq_filter_forwarded       — ?filter=rejections narrows to rejection entries
6.  test_route_self_registers       — importing the module registers the route
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

_TOKEN = "test-dlq-tok"


def _make_app(monkeypatch, *, debug_apis_on: bool = False):
    """Minimal Starlette app + BearerAuthMiddleware for the DLQ route."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "on" if debug_apis_on else "off")

    from yadgar.core.auth_middleware import BearerAuthMiddleware
    from yadgar.core.server.routes.debug_dlq import dlq_handler

    app = BearerAuthMiddleware(
        Starlette(routes=[Route("/api/debug/dlq", dlq_handler, methods=["GET"])])
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _seed_dlq(monkeypatch, tmp_path, entries):
    """Create a FileQueue-shaped DLQ dir under tmp_path and point dlq_inspect at it.

    entries: list of (filename, sidecar_meta_dict). Writes a main file + its
    ``.error.json`` sidecar so dlq_inspect enumerates it.
    """
    from yadgar._shared.file_queue.queue import FileQueue

    fq = FileQueue(tmp_path)
    for fname, meta in entries:
        (fq.dlq_dir / fname).write_text("{}")
        (fq.dlq_dir / (fname + ".error.json")).write_text(json.dumps(meta))

    import yadgar.core.server.tools.admin_dlq as _m

    monkeypatch.setattr(_m, "_get_file_queue", lambda: fq)
    return fq


# ---------------------------------------------------------------------------
# 1 — gate off → 403 (handler-level defence-in-depth)
# ---------------------------------------------------------------------------


def test_dlq_gated_off(monkeypatch):
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/debug/dlq", headers=_auth())
    assert resp.status_code == 403
    assert resp.json().get("error") == "debug APIs disabled"


# ---------------------------------------------------------------------------
# 2 — gate off → 403 (ASGI middleware, before the handler)
# ---------------------------------------------------------------------------


def test_dlq_gated_off_middleware(monkeypatch):
    """The middleware gates /api/debug/dlq via _DEBUG_API_PREFIXES."""
    from yadgar.core.auth_middleware.auth_middleware import _DEBUG_API_PREFIXES

    assert "/api/debug/dlq" in _DEBUG_API_PREFIXES
    # And a real request with gate off is denied 403 regardless of handler.
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/debug/dlq", headers=_auth())
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 3 — gate on, empty DLQ → {entries: [], count: 0}
# ---------------------------------------------------------------------------


def test_dlq_gated_on_empty(monkeypatch, tmp_path):
    _seed_dlq(monkeypatch, tmp_path, [])
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/debug/dlq", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"entries": [], "count": 0}


# ---------------------------------------------------------------------------
# 4 — gate on, seeded DLQ → dlq_inspect entry shape
# ---------------------------------------------------------------------------


def test_dlq_gated_on_returns_shape(monkeypatch, tmp_path):
    _seed_dlq(
        monkeypatch,
        tmp_path,
        [
            (
                "0001778139482800_abc.json",
                {
                    "op_type": "wiki_add",
                    "attempts": 3,
                    "classification": "transient",
                    "last_error": "boom",
                    "failure_reason": "permanent_error",
                },
            )
        ],
    )
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/debug/dlq", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    entry = body["entries"][0]
    assert entry["file"] == "0001778139482800_abc.json"
    assert entry["op_type"] == "wiki_add"
    assert entry["attempts"] == 3
    assert entry["failure_reason"] == "permanent_error"


# ---------------------------------------------------------------------------
# 5 — ?filter=rejections is forwarded to dlq_inspect
# ---------------------------------------------------------------------------


def test_dlq_filter_forwarded(monkeypatch, tmp_path):
    _seed_dlq(
        monkeypatch,
        tmp_path,
        [
            ("a.json", {"op_type": "wiki_add", "failure_reason": "duplicate_detected"}),
            ("b.json", {"op_type": "memorize", "failure_reason": "permanent_error"}),
        ],
    )
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/debug/dlq?filter=rejections", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    # Only the duplicate_detected (rejection taxonomy) entry survives the filter.
    assert body["count"] == 1
    assert body["entries"][0]["failure_reason"] == "duplicate_detected"


# ---------------------------------------------------------------------------
# 6 — module import self-registers the route on mcp_server
# ---------------------------------------------------------------------------


def test_route_self_registers():
    import yadgar.core.server.routes.debug_dlq  # noqa: F401
    from yadgar.core.server._app import mcp_server

    paths = {getattr(r, "path", None) for r in mcp_server._custom_starlette_routes}
    assert "/api/debug/dlq" in paths
