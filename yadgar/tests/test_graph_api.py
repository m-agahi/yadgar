"""Tests for GraphAPI — graph assembly + stats."""

import pytest

from yadgar.graph_api import GraphAPI
from yadgar.storage import StorageEngine

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_graph_api.db"))
    yield engine
    engine.close()


def _mem(content):
    return {"content": content, "directory_context": "/proj", "tags": ["t"], "heat": 1.0}


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
