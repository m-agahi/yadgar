"""Unit tests for the read_query surface: parse-guard, core forward, MCP tool.

These do NOT need a live DB (they mock the forward / storage seam). The live
write-rejection go/no-go lives in test_read_query_ro.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Backend parse-guard (defense-in-depth — NOT the primary guard; the RO
# connection is. Asserted here only as the cheap early-reject layer.)
# ---------------------------------------------------------------------------


def _get_contains_write_keyword():
    # embed_service_routes must be reached THROUGH the parent embed_service module
    # (it imports `_es` at module top and is registered as a reload-aware sibling;
    # a direct top-level import trips a partially-initialised circular import).
    import yadgar.backend.embed_service.embed_service  # noqa: F401
    from yadgar.backend.embed_service.embed_service_routes import _contains_write_keyword

    return _contains_write_keyword


def test_parse_guard_rejects_write_keywords():
    _contains_write_keyword = _get_contains_write_keyword()

    for q in (
        "UPDATE memory SET x = 1",
        "DELETE memory:1",
        "CREATE foo SET a = 1",
        "SELECT 1; DELETE memory",  # multi-statement — the exact defeat case
        "define user hacker on root",  # case-insensitive
        "REMOVE TABLE memory",
        "RELATE a->b->c",
        "UPSERT memory:1 SET x = 1",
        "INSERT INTO memory (x) VALUES (1)",
    ):
        assert _contains_write_keyword(q) is True, q


def test_parse_guard_allows_reads_and_avoids_false_positives():
    _contains_write_keyword = _get_contains_write_keyword()

    for q in (
        "SELECT * FROM memory",
        "SELECT updated_at FROM memory",  # 'updated_at' must NOT trip UPDATE
        "INFO FOR DB",
        "SELECT created_at, id FROM wiki_page WHERE id = $id",
    ):
        assert _contains_write_keyword(q) is False, q


# ---------------------------------------------------------------------------
# Core forward helper — forward-only (RuntimeError when YADGAR_EMBED_URL unset).
# ---------------------------------------------------------------------------


def test_forward_read_query_requires_embed_url(monkeypatch):
    from yadgar.core.server.tools._forward import _forward_read_query

    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)
    with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
        _forward_read_query("SELECT 1")


def test_forward_read_query_posts_to_backend(monkeypatch):
    from yadgar.core.server.tools import _forward as fwd

    monkeypatch.setenv("YADGAR_EMBED_URL", "http://backend:8001")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok")

    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"rows": [{"id": 1}], "row_count": 1, "truncated": False}
        return resp

    with patch("httpx.post", side_effect=_fake_post):
        out = fwd._forward_read_query("SELECT * FROM memory", {"a": 1}, timeout_ms=3000)

    assert captured["url"] == "http://backend:8001/read_query"
    assert captured["json"] == {
        "query": "SELECT * FROM memory",
        "params": {"a": 1},
        "timeout_ms": 3000,
    }
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert out == {"rows": [{"id": 1}], "row_count": 1, "truncated": False}


# ---------------------------------------------------------------------------
# Core route — debug-flag gated (403 when off) + forwards when on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_route_gated_403_when_debug_off(monkeypatch):
    from yadgar.core.server.routes import debug_query

    monkeypatch.setattr(debug_query, "_is_debug_apis_enabled", lambda: False)

    req = MagicMock()

    resp = await debug_query.read_query_handler(req)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_core_route_forwards_when_enabled(monkeypatch):
    from yadgar.core.server.routes import debug_query

    monkeypatch.setattr(debug_query, "_is_debug_apis_enabled", lambda: True)

    async def _json():
        return {"query": "SELECT * FROM memory", "params": {}, "timeout_ms": 5000}

    req = MagicMock()
    req.json = _json

    with patch.object(
        debug_query,
        "_forward_read_query",
        return_value={"rows": [{"id": 1}], "row_count": 1, "truncated": False},
    ) as m:
        resp = await debug_query.read_query_handler(req)

    assert resp.status_code == 200
    m.assert_called_once()


@pytest.mark.asyncio
async def test_core_route_propagates_backend_400(monkeypatch):
    from yadgar.core.server.routes import debug_query

    monkeypatch.setattr(debug_query, "_is_debug_apis_enabled", lambda: True)

    async def _json():
        return {"query": "DELETE memory", "params": {}, "timeout_ms": 5000}

    req = MagicMock()
    req.json = _json

    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.text = "rejected"
    err_resp.json.return_value = {"detail": "write keyword rejected"}
    http_err = httpx.HTTPStatusError("400", request=MagicMock(), response=err_resp)

    with patch.object(debug_query, "_forward_read_query", side_effect=http_err):
        resp = await debug_query.read_query_handler(req)

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# MCP tool — debug-flag gated + row-cap clamp (never raises the ceiling).
# ---------------------------------------------------------------------------


def test_db_inspect_gated_when_debug_off(monkeypatch):
    import importlib

    di = importlib.import_module("yadgar.core.server.tools.db_inspect")

    monkeypatch.setattr(di, "_is_debug_apis_enabled", lambda: False)
    out = di.db_inspect("SELECT 1")
    assert "error" in out
    assert "debug" in out["error"].lower()


def test_db_inspect_forwards_and_clamps_limit(monkeypatch):
    import importlib

    di = importlib.import_module("yadgar.core.server.tools.db_inspect")

    monkeypatch.setattr(di, "_is_debug_apis_enabled", lambda: True)

    with patch.object(
        di,
        "_forward_read_query",
        return_value={"rows": [{"id": 1}], "row_count": 1, "truncated": False},
    ) as m:
        out = di.db_inspect("SELECT * FROM memory", {"a": 1}, limit=100000)

    # The forward is called (row cap enforced backend-side); the tool returns the rows.
    m.assert_called_once()
    assert out["row_count"] == 1
    assert out["truncated"] is False


def test_db_inspect_limit_clamps_rows_locally(monkeypatch):
    import importlib

    di = importlib.import_module("yadgar.core.server.tools.db_inspect")

    monkeypatch.setattr(di, "_is_debug_apis_enabled", lambda: True)

    # Backend returns 5 rows; caller limit=2 → tool clamps to 2 + flags truncated.
    with patch.object(
        di,
        "_forward_read_query",
        return_value={
            "rows": [{"n": i} for i in range(5)],
            "row_count": 5,
            "truncated": False,
        },
    ):
        out = di.db_inspect("SELECT n FROM foo", limit=2)

    assert out["row_count"] == 2
    assert out["truncated"] is True


def test_db_inspect_maps_backend_error(monkeypatch):
    import importlib

    di = importlib.import_module("yadgar.core.server.tools.db_inspect")

    monkeypatch.setattr(di, "_is_debug_apis_enabled", lambda: True)

    err_resp = MagicMock()
    err_resp.status_code = 400
    err_resp.text = "rejected"
    err_resp.json.return_value = {"detail": "write keyword rejected"}
    http_err = httpx.HTTPStatusError("400", request=MagicMock(), response=err_resp)

    with patch.object(di, "_forward_read_query", side_effect=http_err):
        out = di.db_inspect("DELETE memory")

    assert out["error"] == "write keyword rejected"
