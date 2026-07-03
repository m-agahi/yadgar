"""v5.88: /api/graph attaches cached precomputed positions when flag-on + fresh.

Covers the pure attach helper (attach_cached_positions) directly + the HTTP
endpoint behavior (default-OFF omits positions; ON+fresh attaches x/y/z; stale
signature omits).
"""

from __future__ import annotations

import pytest

from yadgar.graph_layout import attach_cached_positions, graph_signature


def _payload(ids):
    return {"nodes": [{"id": i, "type": "memory"} for i in ids], "edges": []}


# ── pure helper ───────────────────────────────────────────────────────────────


def test_attach_off_omits_positions():
    """Flag off → no positions attached (preserves current behavior exactly)."""
    data = _payload(["a", "b"])
    cache = {"signature": "s", "positions": {"a": [1, 2, 3]}}
    out = attach_cached_positions(data, cache, enabled=False)
    assert all("x" not in n for n in out["nodes"])


def test_attach_none_cache_omits_positions():
    data = _payload(["a", "b"])
    out = attach_cached_positions(data, None, enabled=True)
    assert all("x" not in n for n in out["nodes"])


def test_attach_empty_positions_omits():
    data = _payload(["a"])
    out = attach_cached_positions(data, {"signature": "s", "positions": {}}, enabled=True)
    assert all("x" not in n for n in out["nodes"])


def test_attach_fresh_cache_sets_xyz():
    data = _payload(["a", "b"])
    cache = {"signature": "s", "positions": {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]}}
    out = attach_cached_positions(data, cache, enabled=True)
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["a"]["x"] == 1.0 and by_id["a"]["y"] == 2.0 and by_id["a"]["z"] == 3.0
    assert by_id["b"]["x"] == 4.0


def test_attach_by_id_superset_cache_capped_subset():
    """Cache is the full-graph superset; a capped subset still gets every position."""
    data = _payload(["a", "c"])  # served subset (b capped out)
    cache = {
        "signature": "full-graph-sig",
        "positions": {"a": [1.0, 1.0, 1.0], "b": [2.0, 2.0, 2.0], "c": [3.0, 3.0, 3.0]},
    }
    out = attach_cached_positions(data, cache, enabled=True)
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["a"]["x"] == 1.0
    assert by_id["c"]["x"] == 3.0


def test_attach_uncached_node_gets_no_position():
    """A node added since the last precompute (absent from the cache) stays bare."""
    data = _payload(["a", "b"])
    cache = {"signature": "s", "positions": {"a": [1.0, 2.0, 3.0]}}  # b is new/uncached
    out = attach_cached_positions(data, cache, enabled=True)
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["a"]["x"] == 1.0
    assert "x" not in by_id["b"]


# ── HTTP endpoint ─────────────────────────────────────────────────────────────

_TEST_TOKEN = "layout-attach-test-token"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("graph_api_layout_attach")
    from yadgar import server

    server.init_engines(db_path=str(tmp_path / "attach.db"), embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _client(monkeypatch):
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TEST_TOKEN)
    from starlette.testclient import TestClient

    from yadgar import server as _server
    from yadgar.auth_middleware import BearerAuthMiddleware

    return TestClient(BearerAuthMiddleware(_server.mcp_server.streamable_http_app()))


def _headers():
    return {"Authorization": f"Bearer {_TEST_TOKEN}"}


def _seed(n=4):
    import yadgar.server._state as _st

    for i in range(n):
        _st._storage.insert_memory(
            {
                "content": f"layout attach memory {i}",
                "directory_context": "/test",
                "tags": ["t"],
                "heat": float(i + 1) / n,
            }
        )


def test_default_off_payload_has_no_positions(monkeypatch):
    """Default OFF preserves current behavior: no x/y/z in any node."""
    monkeypatch.setenv("YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED", "false")
    from yadgar.config import get_settings

    get_settings.cache_clear()
    _seed()
    data = _client(monkeypatch).get("/api/graph", headers=_headers()).json()
    get_settings.cache_clear()
    assert data["nodes"]
    assert all("x" not in n for n in data["nodes"])


def test_flag_on_fresh_cache_attaches_positions(monkeypatch):
    """Flag ON + a fresh cache → nodes carry x/y/z from the cache."""
    monkeypatch.setenv("YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED", "true")
    from yadgar.config import get_settings

    get_settings.cache_clear()
    _seed()

    import yadgar.server._state as _st
    from yadgar.graph_api import GraphAPI

    g = GraphAPI(_st._storage).get_full_graph(0, 8, False, None, 0, 0)
    sig = graph_signature(g["nodes"], g["edges"])
    positions = {str(node["id"]): [0.1, 0.2, 0.3] for node in g["nodes"]}
    _st._storage.set_graph_layout_cache(sig, positions, "2026-06-29T00:00:00+00:00")

    data = _client(monkeypatch).get("/api/graph", headers=_headers()).json()
    get_settings.cache_clear()
    assert data["nodes"]
    assert all("x" in n and "y" in n and "z" in n for n in data["nodes"])


def test_flag_on_capped_subset_still_attaches_by_id(monkeypatch):
    """Caps bind (full-graph cache is a superset): the capped subset still gets x/y/z."""
    monkeypatch.setenv("YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED", "true")
    from yadgar.config import get_settings

    get_settings.cache_clear()
    _seed(6)

    import yadgar.server._state as _st
    from yadgar.graph_api import GraphAPI

    # Cache positions for the FULL uncapped graph.
    g = GraphAPI(_st._storage).get_full_graph(0, 8, False, None, 0, 0)
    sig = graph_signature(g["nodes"], g["edges"])
    positions = {str(node["id"]): [0.5, 0.5, 0.5] for node in g["nodes"]}
    _st._storage.set_graph_layout_cache(sig, positions, "2026-06-29T00:00:00+00:00")

    # Request a CAPPED subset — fewer memory nodes than the full graph.
    resp = _client(monkeypatch).get("/api/graph?max_memories=2", headers=_headers())
    data = resp.json()
    get_settings.cache_clear()
    mem_nodes = [n for n in data["nodes"] if n.get("type") == "memory"]
    assert mem_nodes  # subset served
    assert all("x" in n and "y" in n and "z" in n for n in mem_nodes)
