"""BC-VZ1 + BC-VZ2 e2e: graph REST fidelity + viz_search whole-DB behavior.

BC-VZ1: Seeds memories + entities + relations in a live local SurrealDB via the
real StorageEngine, then calls GraphAPI.get_full_graph() (the same code path
behind GET /api/graph) and asserts the returned payload reflects DB truth:

  - seeded entity ids appear as ``entity:{id}`` nodes
  - the seeded co_occurrence edge appears with source/target matching the
    seeded entity ids (edge endpoints match seeded node ids)
  - entity node heat field equals the seeded heat value (scores present)
  - edge weight field equals the seeded weight (DB truth reflected)
  - memory node heat equals its seeded value
  - weak_edges_hidden key is always present

This is NOT circular: GraphAPI is the unit under test; direct storage-layer
seeding (insert_entity, insert_relationship) is the correct setup — same
rationale as Phase 1 Sections B/C/G/H where the unit under test is graph
assembly, not the write pipeline.

BC-VZ2: Seeds memories across MULTIPLE directories, then calls the real
api_viz_search HTTP handler (GET /api/viz/search) and asserts that node IDs
from ALL directories are returned — proving that viz_search's whole-DB
(unscoped) behavior is intentional and locked in for the god's-eye overlay.

Contract: BC-VZ1 graph REST returns entity neighborhood + scores.
          BC-VZ2 viz_search returns matching node ids from all directories
                 (whole-DB, dir-scoping intentionally bypassed).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The identity every memory seed in this file names. C5/ADR-0227 made
#: ``project_id`` mandatory at the storage write chokepoint, so an unnamed seed
#: cannot be inserted. One value suffices: the viz read path is a god's-eye
#: admin overlay with no project scoping, so nothing here turns on two
#: identities being distinguishable.
_TEST_PROJECT = "m-agahi/yadgar"


def _insert_entity_direct(e2e_engines, name: str, heat: float) -> int:
    """Seed an entity row with the given name and heat. Returns the integer id."""
    storage = e2e_engines["storage"]
    eid = storage.insert_entity(
        {
            "name": name,
            "type": "concept",
            "heat": heat,
            "archived": False,
        }
    )
    return eid


def _insert_relationship_direct(
    e2e_engines, src_id: int, tgt_id: int, rel_type: str, weight: float
) -> int:
    """Seed a relationship between two entities. Returns the integer id."""
    storage = e2e_engines["storage"]
    return storage.insert_relationship(
        {
            "source_entity_id": src_id,
            "target_entity_id": tgt_id,
            "relationship_type": rel_type,
            "weight": weight,
        }
    )


def _insert_memory_direct(e2e_engines, content: str, heat: float) -> int:
    """Seed a memory row with real embedding. Returns the integer id."""
    from datetime import UTC, datetime

    storage = e2e_engines["storage"]
    embeddings = e2e_engines["embeddings"]
    emb = embeddings.encode(content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": e2e_engines["yadgar_dir"],
        "project_id": _TEST_PROJECT,
        "heat": heat,
        "tags": ["e2e", "bc-vz1"],
        "last_accessed": now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
    }
    return storage.insert_memory(doc)


# ---------------------------------------------------------------------------
# BC-VZ1 — graph REST fidelity: entity neighborhood + scores
# ---------------------------------------------------------------------------


class TestBCVZ1_GraphRESTEntityNeighborhoodScores:
    """BC-VZ1: graph REST payload returns entity neighborhood + scores from DB.

    All assertions drive GraphAPI.get_full_graph() — the same code path
    behind GET /api/graph — against a real isolated SurrealDB.
    """

    def test_seeded_entity_nodes_appear_in_payload(self, e2e_engines):
        """Entity nodes seeded in SurrealDB MUST appear as entity:{id} nodes in the payload."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Seed two entity nodes with known ids and heat scores
        eid_a = _insert_entity_direct(e2e_engines, "VZ1_EntityAlpha_xbcvz1a", heat=0.9)
        eid_b = _insert_entity_direct(e2e_engines, "VZ1_EntityBeta_xbcvz1b", heat=0.6)

        result = GraphAPI(storage).get_full_graph()

        node_ids = {n["id"] for n in result["nodes"]}
        assert f"entity:{eid_a}" in node_ids, (
            f"BC-VZ1: seeded entity {eid_a} (entity:{eid_a}) MUST appear in graph payload. "
            f"node_ids sample: {list(node_ids)[:10]}"
        )
        assert f"entity:{eid_b}" in node_ids, (
            f"BC-VZ1: seeded entity {eid_b} (entity:{eid_b}) MUST appear in graph payload. "
            f"node_ids sample: {list(node_ids)[:10]}"
        )

    def test_entity_node_heat_reflects_db_value(self, e2e_engines):
        """Entity node heat in the payload MUST equal the seeded DB heat (scores present)."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        seeded_heat = 0.75
        eid = _insert_entity_direct(e2e_engines, "VZ1_HeatEntity_xbcvz1h", heat=seeded_heat)

        result = GraphAPI(storage).get_full_graph()

        entity_nodes = {n["id"]: n for n in result["nodes"] if n.get("type") == "entity"}
        node_id = f"entity:{eid}"
        assert node_id in entity_nodes, (
            f"BC-VZ1: entity node {node_id} absent from payload. "
            f"Available entity nodes: {list(entity_nodes.keys())[:10]}"
        )
        payload_heat = entity_nodes[node_id].get("heat")
        assert payload_heat == round(seeded_heat, 4), (
            f"BC-VZ1: entity node heat must reflect DB truth. "
            f"Seeded={seeded_heat}, payload={payload_heat}"
        )

    def test_co_occurrence_edge_endpoints_match_seeded_entity_ids(self, e2e_engines):
        """A seeded co_occurrence relation MUST appear as an edge with endpoints matching seeded ids."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Seed both endpoint entities (orphan filter requires both to be present)
        eid_src = _insert_entity_direct(e2e_engines, "VZ1_EdgeSrc_xbcvz1src", heat=0.8)
        eid_tgt = _insert_entity_direct(e2e_engines, "VZ1_EdgeTgt_xbcvz1tgt", heat=0.7)

        seeded_weight = 2.5
        _insert_relationship_direct(
            e2e_engines,
            src_id=eid_src,
            tgt_id=eid_tgt,
            rel_type="co_occurrence",
            weight=seeded_weight,
        )

        result = GraphAPI(storage).get_full_graph()

        co_occ_edges = [e for e in result["edges"] if e.get("type") == "co_occurrence"]
        expected_src = f"entity:{eid_src}"
        expected_tgt = f"entity:{eid_tgt}"

        # Find the specific edge we seeded
        matching = [
            e
            for e in co_occ_edges
            if e.get("source") == expected_src and e.get("target") == expected_tgt
        ]
        assert len(matching) >= 1, (
            f"BC-VZ1: seeded co_occurrence edge (src={expected_src}, tgt={expected_tgt}) "
            f"MUST appear in graph payload. "
            f"co_occurrence edges found: {co_occ_edges[:5]}"
        )

    def test_edge_weight_reflects_db_value(self, e2e_engines):
        """Edge weight in the payload MUST equal the seeded DB weight (score fidelity)."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        eid_a = _insert_entity_direct(e2e_engines, "VZ1_WtSrc_xbcvz1ws", heat=0.9)
        eid_b = _insert_entity_direct(e2e_engines, "VZ1_WtTgt_xbcvz1wt", heat=0.8)

        seeded_weight = 3.14
        _insert_relationship_direct(
            e2e_engines,
            src_id=eid_a,
            tgt_id=eid_b,
            rel_type="co_occurrence",
            weight=seeded_weight,
        )

        result = GraphAPI(storage).get_full_graph()

        expected_src = f"entity:{eid_a}"
        expected_tgt = f"entity:{eid_b}"
        matching = [
            e
            for e in result["edges"]
            if e.get("source") == expected_src
            and e.get("target") == expected_tgt
            and e.get("type") == "co_occurrence"
        ]
        assert matching, (
            f"BC-VZ1: no co_occurrence edge found for ({expected_src} → {expected_tgt})"
        )
        payload_weight = matching[0].get("weight")
        assert payload_weight == seeded_weight, (
            f"BC-VZ1: edge weight must reflect DB truth. "
            f"Seeded={seeded_weight}, payload={payload_weight}"
        )

    def test_all_edge_endpoints_reference_present_nodes(self, e2e_engines):
        """Every edge endpoint in the full-graph payload MUST reference a node in the payload.

        BC-VZ1 fidelity invariant: orphan filter is active — no edge must point
        to an absent node. Seeding both endpoints guarantees the edge survives.
        """
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Seed a memory, two entities, and a relationship so the graph has data
        _insert_memory_direct(e2e_engines, "VZ1 orphan filter test memory xbcvz1mem", heat=0.5)
        eid_x = _insert_entity_direct(e2e_engines, "VZ1_OrphanSrc_xbcvz1ox", heat=0.7)
        eid_y = _insert_entity_direct(e2e_engines, "VZ1_OrphanTgt_xbcvz1oy", heat=0.6)
        _insert_relationship_direct(
            e2e_engines, src_id=eid_x, tgt_id=eid_y, rel_type="co_occurrence", weight=1.0
        )

        result = GraphAPI(storage).get_full_graph()

        node_ids = {n["id"] for n in result["nodes"]}
        for edge in result["edges"]:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            assert src in node_ids, (
                f"BC-VZ1: edge source '{src}' not in node set (orphan edge). Edge: {edge}"
            )
            assert tgt in node_ids, (
                f"BC-VZ1: edge target '{tgt}' not in node set (orphan edge). Edge: {edge}"
            )

    def test_memory_node_heat_reflects_db_value(self, e2e_engines):
        """Memory node heat in the payload MUST equal the seeded DB heat (scores present)."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        seeded_heat = 0.88
        mid = _insert_memory_direct(
            e2e_engines,
            "VZ1 memory heat fidelity test xbcvz1mheat unique content",
            heat=seeded_heat,
        )

        result = GraphAPI(storage).get_full_graph()

        node_id = f"mem:{mid}"
        mem_nodes = {n["id"]: n for n in result["nodes"] if n.get("type") == "memory"}
        assert node_id in mem_nodes, (
            f"BC-VZ1: seeded memory node {node_id} absent from payload. "
            f"Available mem nodes (first 5): {list(mem_nodes.keys())[:5]}"
        )
        payload_heat = mem_nodes[node_id].get("heat")
        assert payload_heat == round(seeded_heat, 4), (
            f"BC-VZ1: memory node heat must reflect DB truth. "
            f"Seeded={seeded_heat}, payload={payload_heat}"
        )

    def test_payload_always_carries_weak_edges_hidden_key(self, e2e_engines):
        """get_full_graph payload MUST always carry 'weak_edges_hidden' key (F4 affordance)."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        result = GraphAPI(storage).get_full_graph()

        assert "weak_edges_hidden" in result, (
            "BC-VZ1: payload MUST always carry 'weak_edges_hidden' key. "
            "F4 affordance: never silently drop DB truth about suppressed edges."
        )
        assert isinstance(result["weak_edges_hidden"], int), (
            f"BC-VZ1: 'weak_edges_hidden' must be an int, got {type(result['weak_edges_hidden'])}"
        )

    def test_payload_nodes_and_edges_keys_always_present(self, e2e_engines):
        """get_full_graph payload MUST always carry 'nodes' and 'edges' lists."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        result = GraphAPI(storage).get_full_graph()

        assert "nodes" in result, "BC-VZ1: payload MUST carry 'nodes' key"
        assert "edges" in result, "BC-VZ1: payload MUST carry 'edges' key"
        assert isinstance(result["nodes"], list), "BC-VZ1: 'nodes' must be a list"
        assert isinstance(result["edges"], list), "BC-VZ1: 'edges' must be a list"


# ---------------------------------------------------------------------------
# BC-VZ2 — viz_search whole-DB behavior (god's-eye overlay, dir-scoping bypassed)
# ---------------------------------------------------------------------------


def _insert_memory_with_dir(e2e_engines, content: str, directory: str, heat: float) -> int:
    """Seed a memory with a given directory_context and real embedding. Returns integer id."""
    from datetime import UTC, datetime

    storage = e2e_engines["storage"]
    embeddings = e2e_engines["embeddings"]
    emb = embeddings.encode(content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        # One project across every seeded DIRECTORY on purpose: BC-VZ2's subject
        # is that viz search is NOT directory-scoped, so the directories must
        # differ while the identity need not. C5/ADR-0227 only requires that one
        # be named.
        "project_id": _TEST_PROJECT,
        "heat": heat,
        "tags": ["e2e", "bc-vz2"],
        "last_accessed": now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
    }
    return storage.insert_memory(doc)


class TestBCVZ2_VizSearchWholeDB:
    """BC-VZ2: viz_search SHALL return matching node IDs from ALL directories.

    The viz is a god's-eye admin overlay rendering the entire memory store.
    GET /api/viz/search must find matching nodes regardless of directory_context —
    dir-scoping is intentionally absent (see comment in api_viz_search, http.py).

    This test drives the REAL api_viz_search HTTP handler (same code path as
    the production endpoint) against a live isolated SurrealDB with memories
    seeded across two different directories.
    """

    def test_viz_search_returns_nodes_from_all_directories(self, e2e_engines):
        """viz_search MUST return mem nodes from ALL project directories (whole-DB, BC-VZ2).

        Seeds two memories sharing a distinctive keyword — one in yadgar_dir, one in
        other_dir.  Calls the real api_viz_search handler via Starlette TestClient.
        Asserts both mem:{id} appear in node_ids, proving the whole-DB bypass.

        Contrast: a dir-scoped recall(directory=yadgar_dir) would exclude the other_dir
        memory.  viz_search intentionally does NOT scope, so both must appear.
        """
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.core.server.http import api_viz_search

        yadgar_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]

        # Shared distinctive keyword that both memories contain — ensures both surface
        # in a recall/wiki query against this term.
        keyword = "xbcvz2wholedb_unique_sentinel"

        mid_a = _insert_memory_with_dir(
            e2e_engines,
            f"BC-VZ2 project alpha memory {keyword}",
            directory=yadgar_dir,
            heat=0.8,
        )
        mid_b = _insert_memory_with_dir(
            e2e_engines,
            f"BC-VZ2 project beta memory {keyword}",
            directory=other_dir,
            heat=0.7,
        )

        # Drive the REAL api_viz_search handler — same code path as production.
        # _st._retriever and _st._wiki are populated by init_engines() in e2e_engines.
        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/api/viz/search?q={keyword}")

        assert resp.status_code == 200, (
            f"BC-VZ2: viz_search must return 200, got {resp.status_code}. Body: {resp.text}"
        )
        data = resp.json()
        node_ids = data.get("node_ids", [])

        assert f"mem:{mid_a}" in node_ids, (
            f"BC-VZ2: memory {mid_a} (dir={yadgar_dir!r}) MUST appear in viz_search results. "
            f"viz_search is whole-DB by design — dir-scoping intentionally bypassed. "
            f"node_ids={node_ids}"
        )
        assert f"mem:{mid_b}" in node_ids, (
            f"BC-VZ2: memory {mid_b} (dir={other_dir!r}) MUST appear in viz_search results. "
            f"viz_search is whole-DB by design — dir-scoping intentionally bypassed. "
            f"node_ids={node_ids}"
        )
