"""Layer 1 — API contract integrity tests for /api/graph.

Validates the *wire format* returned by the HTTP endpoint, not just the
builder's return dict. Catches drift between GraphAPI internals and
serialization/HTTP layer.

Tests:
  - No orphan edges in assembled payload (edge endpoints ⊆ node IDs)
  - Required node fields present (id, type, heat, label)
  - Required edge fields present (source, target, type)
  - Node type values within allowed set (memory, wiki, entity)
  - Edge type values within allowed set
  - /api/graph/stats shape: counts present + non-negative
  - Meta-test: orphan detection is sensitive (catches v5.10.9 bug class)

TDD: these tests are regression guards. The contract already holds after
v5.10.9's orphan filter — any future regression breaks these immediately.
"""

from __future__ import annotations

import pytest

# ── Allowed values ────────────────────────────────────────────────────────────
ALLOWED_NODE_TYPES = {"memory", "wiki", "entity"}
ALLOWED_EDGE_TYPES = {"semantic", "temporal", "causal", "transition", "crossref"}
_TEST_TOKEN = "contract-test-token"


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Start in-process server engines against a fresh temp DB."""
    tmp_path = tmp_path_factory.mktemp("graph_api_contract")
    from yadgar.core import server

    db_path = str(tmp_path / "contract_test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _make_client(monkeypatch):
    """TestClient with auth token set."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TEST_TOKEN)

    from starlette.testclient import TestClient

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_TOKEN}"}


def _seed_memories(n: int = 5) -> None:
    """Insert n memories into the active StorageEngine."""
    import yadgar._shared.runtime.state as _st

    storage = _st._storage
    assert storage is not None, "StorageEngine not initialized"
    for i in range(n):
        storage.insert_memory(
            {
                "content": f"contract-test memory {i}: exploring knowledge graph integrity",
                "directory_context": "/test",
                "tags": ["contract", "test"],
                "heat": float(i + 1) / float(n),
            }
        )


# ── Shape tests ───────────────────────────────────────────────────────────────


class TestApiGraphEndpointShape:
    """HTTP-level shape assertions on /api/graph response."""

    def test_endpoint_returns_200(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp = client.get("/api/graph", headers=_auth_headers())
        assert resp.status_code == 200, f"/api/graph returned {resp.status_code}: {resp.text[:200]}"

    def test_response_has_nodes_and_edges_keys(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp = client.get("/api/graph", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data, f"'nodes' key missing: {list(data.keys())}"
        assert "edges" in data, f"'edges' key missing: {list(data.keys())}"

    def test_nodes_is_list(self, monkeypatch):
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        assert isinstance(data["nodes"], list), f"'nodes' is not a list: {type(data['nodes'])}"

    def test_edges_is_list(self, monkeypatch):
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        assert isinstance(data["edges"], list), f"'edges' is not a list: {type(data['edges'])}"


# ── Orphan edge tests ─────────────────────────────────────────────────────────


class TestApiGraphNoOrphanEdges:
    """Core contract: edge endpoints must be a subset of node IDs.

    This is the v5.10.9 root-cause test at HTTP level. force-graph.min.js
    throws 'node not found: X' on first orphan edge, crashing the physics
    simulation. One orphan = zero rendered graph.
    """

    def test_no_orphan_edges_empty_db(self, monkeypatch):
        """Empty DB → no nodes, no edges → trivially no orphans."""
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        node_ids = {n["id"] for n in data["nodes"]}
        for edge in data["edges"]:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            assert src in node_ids, f"Orphan edge source '{src}' not in node set"
            assert tgt in node_ids, f"Orphan edge target '{tgt}' not in node set"

    def test_no_orphan_edges_seeded_db(self, monkeypatch):
        """Seeded DB → nodes present → all edge endpoints must resolve."""
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        node_ids = {n["id"] for n in data["nodes"]}
        orphans = set()
        for edge in data["edges"]:
            src = str(edge.get("source", ""))
            tgt = str(edge.get("target", ""))
            if src not in node_ids:
                orphans.add(src)
            if tgt not in node_ids:
                orphans.add(tgt)
        assert not orphans, (
            f"Orphan edge endpoints in /api/graph payload: {orphans}. "
            f"Node IDs present: {sorted(node_ids)[:10]}"
        )

    def test_edge_endpoints_subset_of_node_ids(self, monkeypatch):
        """Algebraic check: set(edge endpoints) ⊆ set(node IDs)."""
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        node_ids = {n["id"] for n in data["nodes"]}
        edge_endpoints: set[str] = set()
        for e in data["edges"]:
            edge_endpoints.add(str(e.get("source", "")))
            edge_endpoints.add(str(e.get("target", "")))
        orphans = edge_endpoints - node_ids
        assert not orphans, f"Orphan edge endpoints: {orphans}"


# ── Node field tests ──────────────────────────────────────────────────────────


class TestApiGraphNodeFields:
    """Each node must carry required fields with valid types."""

    def test_nodes_have_required_fields(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        assert data["nodes"], "No nodes returned from seeded DB"
        for n in data["nodes"]:
            assert "id" in n, f"Node missing 'id': {n}"
            assert "type" in n, f"Node missing 'type': {n}"
            assert "heat" in n, f"Node missing 'heat': {n}"
            assert "label" in n, f"Node missing 'label': {n}"

    def test_node_ids_are_strings(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        for n in data["nodes"]:
            assert isinstance(n["id"], str), f"Node id is not a string: {n['id']!r}"

    def test_node_types_valid(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        for n in data["nodes"]:
            assert n["type"] in ALLOWED_NODE_TYPES, (
                f"Node {n['id']!r} has unknown type {n['type']!r}. Allowed: {ALLOWED_NODE_TYPES}"
            )

    def test_node_heat_is_float(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        for n in data["nodes"]:
            heat = n.get("heat")
            assert isinstance(heat, (int, float)), f"Node {n['id']!r} heat is not numeric: {heat!r}"

    def test_node_ids_unique(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        ids = [n["id"] for n in data["nodes"]]
        assert len(ids) == len(set(ids)), (
            f"Duplicate node IDs in /api/graph response: {[x for x in ids if ids.count(x) > 1]}"
        )


# ── Edge field tests ──────────────────────────────────────────────────────────


class TestApiGraphEdgeFields:
    """Edges must carry source, target, type fields."""

    def test_edges_have_required_fields(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        for e in data["edges"]:
            assert "source" in e, f"Edge missing 'source': {e}"
            assert "target" in e, f"Edge missing 'target': {e}"
            assert "type" in e, f"Edge missing 'type': {e}"

    def test_edge_types_valid(self, monkeypatch):
        _seed_memories(5)
        client = _make_client(monkeypatch)
        data = client.get("/api/graph", headers=_auth_headers()).json()
        for e in data["edges"]:
            assert e["type"] in ALLOWED_EDGE_TYPES, (
                f"Edge has unknown type {e['type']!r}. Allowed: {ALLOWED_EDGE_TYPES}"
            )


# ── Stats tests ───────────────────────────────────────────────────────────────


class TestApiGraphStatsShape:
    """/api/graph/stats must return counts + non-negative integers."""

    def test_stats_endpoint_200(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp = client.get("/api/graph/stats", headers=_auth_headers())
        assert resp.status_code == 200

    def test_stats_has_count_fields(self, monkeypatch):
        client = _make_client(monkeypatch)
        data = client.get("/api/graph/stats", headers=_auth_headers()).json()
        # Actual field names per GraphAPI.get_graph_stats() response
        for key in ("memory_count", "wiki_page_count", "temporal_edge_count"):
            assert key in data, f"Stats missing '{key}': {list(data.keys())}"

    def test_stats_counts_non_negative(self, monkeypatch):
        client = _make_client(monkeypatch)
        data = client.get("/api/graph/stats", headers=_auth_headers()).json()
        for key, val in data.items():
            if isinstance(val, (int, float)) and "count" in key:
                assert val >= 0, f"Stats field '{key}' is negative: {val}"


# ── Meta-test ─────────────────────────────────────────────────────────────────


class TestApiGraphContractCatchesOrphans:
    """Meta-test: the contract check catches the v5.10.9 bug class.

    Inject orphan edges into a crafted payload, confirm the algebraic
    check fires. Proves test value independent of current correctness.
    """

    def test_orphan_detection_is_sensitive(self):
        """Orphan check fires on a crafted payload with known-bad edges."""
        payload = {
            "nodes": [
                {"id": "mem:1", "type": "memory", "heat": 0.8, "label": "alpha"},
                {"id": "mem:2", "type": "memory", "heat": 0.4, "label": "beta"},
            ],
            "edges": [
                # Valid edge
                {"source": "mem:1", "target": "mem:2", "type": "semantic"},
                # Orphan edge — entity:172 not in nodes (v5.10.9 root cause)
                {"source": "mem:1", "target": "entity:172", "type": "causal"},
            ],
        }
        node_ids = {n["id"] for n in payload["nodes"]}
        edge_endpoints: set[str] = set()
        for e in payload["edges"]:
            edge_endpoints.add(str(e.get("source", "")))
            edge_endpoints.add(str(e.get("target", "")))
        orphans = edge_endpoints - node_ids
        assert "entity:172" in orphans, (
            "Orphan detection did not fire on known-bad payload — contract test not sensitive"
        )
