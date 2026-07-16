"""/api/traces/* endpoint contract tests (viz-trace-replay Car B, TDD).

Covers:
  - shape contract for /recent + /{id}/mesh
  - Tempo disabled (empty TEMPO_QUERY_URL) → 200 typed-empty, never 500
  - Tempo-down (httpx ConnectError) → graceful 200 empty, never 500
  - cache-hit path on /{id}/mesh (second fetch does not re-hit Tempo)
  - route module self-registration on mcp_server
  - /api/traces/* is bearer-protected but NOT debug-gated (unlike /api/logs/*)
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

_TOKEN = "test-traces-tok"


# ---------------------------------------------------------------------------
# Fake Tempo transport
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient — records calls, returns canned payloads."""

    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        _FakeClient.calls.append(url)
        if "/api/search" in url:
            # Mirror real Tempo: the MCP root span is "POST /mcp" (rootTraceName),
            # and the matched tool.* boundary span lives in the hit's spanSet —
            # the label must come from there, NOT rootTraceName.
            return _FakeResp(
                200,
                {
                    "traces": [
                        {
                            "traceID": "abc123",
                            "rootTraceName": "POST /mcp",
                            "durationMs": 42.0,
                            "startTimeUnixNano": "200",
                            "spanSet": {"spans": [{"name": "tool.recall"}]},
                        },
                        {
                            "traceID": "def456",
                            "rootTraceName": "POST /mcp",
                            "durationMs": 12.0,
                            "startTimeUnixNano": "100",
                            "spanSets": [{"spans": [{"name": "tool.wiki_read"}]}],
                        },
                    ]
                },
            )
        # by-id fetch — a minimal two-span OTLP trace
        return _FakeResp(
            200,
            {
                "batches": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": "yadgar-core"}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "spanId": "aa",
                                        "parentSpanId": "",
                                        "name": "tool.recall",
                                        "startTimeUnixNano": "0",
                                        "endTimeUnixNano": "10000000",
                                    },
                                    {
                                        "spanId": "bb",
                                        "parentSpanId": "aa",
                                        "name": "yadgar._shared.retrieval.scoring._run_fts_bm25",
                                        "startTimeUnixNano": "1000000",
                                        "endTimeUnixNano": "9000000",
                                    },
                                ]
                            }
                        ],
                    }
                ]
            },
        )


class _ConnErrClient(_FakeClient):
    async def get(self, url, **kwargs):
        raise httpx.ConnectError("tempo down")


# ---------------------------------------------------------------------------
# App / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    _FakeClient.calls = []
    from yadgar.core.server.routes import traces as _t

    _t._mesh_cache.clear()
    yield
    _t._mesh_cache.clear()


def _make_app(monkeypatch, *, tempo_url: str = "http://tempo:3200"):
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("YADGAR_TEMPO_QUERY_URL", tempo_url)

    from yadgar.core.auth_middleware import BearerAuthMiddleware
    from yadgar.core.server.routes.traces import trace_mesh_handler, traces_recent_handler

    app = BearerAuthMiddleware(
        Starlette(
            routes=[
                Route("/api/traces/recent", traces_recent_handler, methods=["GET"]),
                Route("/api/traces/{trace_id}/mesh", trace_mesh_handler, methods=["GET"]),
            ]
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# /recent
# ---------------------------------------------------------------------------


class TestRecent:
    def test_recent_shape(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/recent", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["tempo"] is True
        assert isinstance(body["traces"], list)
        first = body["traces"][0]
        assert set(first) >= {"trace_id", "tool", "total_ms", "status"}
        # label must come from the matched tool.* span in the spanSet, NOT the
        # "POST /mcp" rootTraceName (the MCP root span)
        assert first["tool"] == "tool.recall"
        assert body["traces"][1]["tool"] == "tool.wiki_read"  # from spanSets[]

    def test_recent_tool_label_prefers_spanset_over_root(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/recent", headers=_auth())
        tools = {t["tool"] for t in r.json()["traces"]}
        assert "POST /mcp" not in tools, "sidebar must show tool.* names, not the MCP root span"

    def test_recent_disabled_when_no_url(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch, tempo_url="")
        r = client.get("/api/traces/recent", headers=_auth())
        assert r.status_code == 200  # graceful, never 500
        body = r.json()
        assert body["tempo"] is False
        assert body["traces"] == []

    def test_recent_tempo_down_is_graceful(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _ConnErrClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/recent", headers=_auth())
        assert r.status_code == 200  # ConnectError → empty, not 500
        assert r.json()["traces"] == []

    def test_recent_limit_clamped(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/recent?limit=9999", headers=_auth())
        assert r.status_code == 200  # clamp handled, no crash


# ---------------------------------------------------------------------------
# /{id}/mesh
# ---------------------------------------------------------------------------


class TestMesh:
    def test_mesh_shape(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/abc123/mesh", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["tempo"] is True
        mesh = body["mesh"]
        assert set(mesh) >= {"nodes", "edges", "timeline_ms", "tool", "dropped_boundary"}
        assert mesh["trace_id"] == "abc123"
        assert isinstance(mesh["nodes"], list)

    def test_mesh_disabled_when_no_url(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch, tempo_url="")
        r = client.get("/api/traces/abc123/mesh", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["tempo"] is False
        assert body["mesh"]["nodes"] == []

    def test_mesh_tempo_down_is_graceful(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _ConnErrClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/abc123/mesh", headers=_auth())
        assert r.status_code == 200  # never 500
        assert r.json()["mesh"]["nodes"] == []

    def test_mesh_cache_hit_second_call_skips_tempo(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch)
        r1 = client.get("/api/traces/abc123/mesh", headers=_auth())
        assert r1.json()["cached"] is False
        calls_after_first = len(_FakeClient.calls)
        r2 = client.get("/api/traces/abc123/mesh", headers=_auth())
        assert r2.json()["cached"] is True
        # second call served from cache → no new Tempo fetch
        assert len(_FakeClient.calls) == calls_after_first


# ---------------------------------------------------------------------------
# Auth: bearer-protected, NOT debug-gated
# ---------------------------------------------------------------------------


class TestAuth:
    def test_requires_bearer_token(self, monkeypatch):
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/recent")  # no auth header
        assert r.status_code == 401

    def test_not_debug_gated(self, monkeypatch):
        # /api/traces/* must NOT require YADGAR_DEBUG_APIS_ENABLED (unlike /api/logs/*)
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "off")
        client = _make_app(monkeypatch)
        r = client.get("/api/traces/recent", headers=_auth())
        assert r.status_code == 200  # debug gate off, still served (bearer only)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_route_module_self_registers():
    import yadgar.core.server.routes.traces  # noqa: F401, I001
    from yadgar.core.server._app import mcp_server

    registered = {getattr(r, "path", None) for r in mcp_server._custom_starlette_routes}
    assert any("/api/traces" in str(p) for p in registered), (
        f"Expected /api/traces routes registered, got: {registered}"
    )
