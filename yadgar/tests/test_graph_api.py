"""Tests for GraphAPI — graph assembly + stats."""

from unittest.mock import MagicMock

import pytest

from yadgar.graph_api import GraphAPI
from yadgar.storage import StorageEngine


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_graph_api.db"))
    yield engine
    engine.close()


def _mem(content):
    return {"content": content, "directory_context": "/proj", "tags": ["t"], "heat": 1.0}


def _make_mock_storage(nodes_rows, causal_edges_rows, entity_rels=None, entity_rows=None):
    """Minimal mock StorageEngine for GraphAPI unit tests.

    v5.54.3: added get_relationships_by_types + get_all_entities mocks.
    """
    s = MagicMock()
    s._q.return_value = nodes_rows
    s.get_all_transitions.return_value = []
    s.get_all_wiki_crossrefs.return_value = []
    s.get_all_causal_edges.return_value = causal_edges_rows
    s.get_relationships_by_types.return_value = entity_rels or []
    s.get_all_entities.return_value = entity_rows or []
    return s


def _mem_row(raw_id, content="test", slot=None, cluster_id=None):
    return {
        "id": raw_id,
        "content": content,
        "heat": 1.0,
        "tags": [],
        "directory_context": "/proj",
        "created_at": "2024-01-01",
        "slot_index": slot,
        "embedding": None,
        "cluster_id": cluster_id,
    }


def _causal(src_id, tgt_id):
    return {
        "source_entity_id": src_id,
        "target_entity_id": tgt_id,
        "confidence": 0.9,
        "algorithm": "pc",
        "source_memory_id": None,
        "valid_until": None,
    }


class TestGraphStatsTemporalEdges:
    def test_slotless_memories_do_not_inflate_temporal_count(self, storage):
        """Memories with no engram slot must be excluded from the temporal-edge
        count. (Regression: `slot_index IS NOT NULL` matched unset NONE fields,
        lumping every slot-less memory into one phantom all-pairs group.)"""
        # 3 memories in slot 0 → C(3,2) = 3 temporal edges
        for i in range(3):
            mid = storage.insert_memory(_mem(f"in-slot {i}"))
            storage.assign_memory_slot(mid, 0)
        # 4 memories with NO slot — must contribute 0 temporal edges
        for i in range(4):
            storage.insert_memory(_mem(f"slotless {i}"))

        stats = GraphAPI(storage).get_graph_stats()
        assert stats["memory_count"] == 7
        assert stats["temporal_edge_count"] == 3, (
            f"slot-less memories leaked into temporal count: got {stats['temporal_edge_count']}"
        )

    def test_no_slots_means_zero_temporal_edges(self, storage):
        for i in range(5):
            storage.insert_memory(_mem(f"m{i}"))
        stats = GraphAPI(storage).get_graph_stats()
        assert stats["temporal_edge_count"] == 0


# ── v5.10.9: Orphan-edge filter ───────────────────────────────────────────────


class TestMemoryNodeClusterId:
    """P2.3 (viz-fix-plan-2026-06-27): memory nodes must carry cluster_id.

    cluster_id is a column on the memory row; the viz tints nodes by cluster.
    Without it on the node payload the frontend can't colour by cluster.
    """

    def test_memory_node_carries_cluster_id(self):
        mem_rows = [_mem_row(1, "alpha", cluster_id=7), _mem_row(2, "beta", cluster_id=None)]
        s = _make_mock_storage(mem_rows, [])
        result = GraphAPI(s).get_full_graph()
        by_id = {n["id"]: n for n in result["nodes"] if n.get("type") == "memory"}
        assert "cluster_id" in by_id["mem:1"]
        assert by_id["mem:1"]["cluster_id"] == 7
        # absent/None cluster_id still surfaces the key (explicitly None)
        assert "cluster_id" in by_id["mem:2"]
        assert by_id["mem:2"]["cluster_id"] is None


class TestOrphanEdgeFilter:
    """v5.10.9: get_full_graph must drop edges whose endpoints are absent from node set.

    Root cause: entity:* nodes are assembled by causal-edge queries but never added to
    the nodes list. force-graph.min.js throws 'node not found: entity:NNN' on the first
    orphan edge, crashing the physics simulation entirely.
    """

    def test_graph_api_filters_orphan_edges(self):
        """Causal edges referencing entity:* IDs (not in node set) must be dropped."""
        mem_rows = [_mem_row(1, "alpha"), _mem_row(2, "beta")]
        causal = [
            _causal(10, 20),
            _causal(20, 30),
            _causal(10, 30),
            _causal(40, 50),
        ]
        s = _make_mock_storage(mem_rows, causal)
        result = GraphAPI(s).get_full_graph()
        causal_out = [e for e in result["edges"] if e.get("type") == "causal"]
        assert causal_out == [], (
            f"Expected 0 causal edges after orphan filter, got {len(causal_out)}: {causal_out}"
        )

    def test_graph_api_orphan_drop_metric(self):
        """Orphan drops must increment yadgar_graph_api_orphan_edges_dropped_total."""
        from yadgar.metrics import yadgar_graph_api_orphan_edges_dropped_total

        before = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        mem_rows = [_mem_row(1, "x"), _mem_row(2, "y")]
        causal = [_causal(99, 100), _causal(101, 102)]
        s = _make_mock_storage(mem_rows, causal)
        GraphAPI(s).get_full_graph()
        after = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        assert after - before >= 2, f"Expected metric delta >=2, got {after - before}"

    def test_graph_api_no_drops_in_healthy_payload(self):
        """When no causal edges present, metric must not increment."""
        from yadgar.metrics import yadgar_graph_api_orphan_edges_dropped_total

        before = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        mem_rows = [_mem_row(1, "a", slot=0), _mem_row(2, "b", slot=0)]
        s = _make_mock_storage(mem_rows, [])
        result = GraphAPI(s).get_full_graph()
        after = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        assert after == before, f"Expected no metric increment, delta={after - before}"
        temporal = [e for e in result["edges"] if e.get("type") == "temporal"]
        assert len(temporal) == 1
