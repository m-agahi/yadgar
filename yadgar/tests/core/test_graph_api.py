"""Tests for GraphAPI — graph assembly + stats."""

from unittest.mock import MagicMock

import pytest

from yadgar.backend.graph.graph_api import GraphAPI


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


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


class TestNodeCaps:
    """v5.88 FIX2: each node type has a configurable cap; 0/-1 = unlimited.

    Memory + wiki caps are SQL LIMIT clauses (omitted when unlimited). Entity cap
    is a post-fetch slice on the heat-ordered rows (get_all_entities is shared by
    9 callers, so we slice in _assemble_entity_nodes rather than add a limit arg).
    Tests gate by node-id prefix (mem:/wiki:/entity:) per the existing style.
    """

    @staticmethod
    def _routing_mock(mem_rows, wiki_rows, entity_rows):
        """Mock whose _q routes by query text (memory vs wiki) and records calls."""
        s = MagicMock()
        captured = {"queries": []}

        def _q(surql, params=None):
            captured["queries"].append((surql, params))
            if "FROM wiki_page" in surql:
                return wiki_rows
            if "FROM memory" in surql:
                return mem_rows
            return []

        s._q.side_effect = _q
        s.get_all_transitions.return_value = []
        s.get_all_wiki_crossrefs.return_value = []
        s.get_all_causal_edges.return_value = []
        s.get_relationships_by_types.return_value = []
        s.get_all_entities.return_value = entity_rows
        s._captured = captured
        return s

    @staticmethod
    def _wiki_row(raw_id):
        return {
            "id": raw_id,
            "title": f"page {raw_id}",
            "slug": f"slug-{raw_id}",
            "category": "reference",
            "tags": [],
            "links": [],
            "source_memory_ids": [],
            "embedding": None,
            "updated_at": "2024-01-01",
        }

    @staticmethod
    def _entity_row(raw_id):
        return {"id": raw_id, "name": f"ent-{raw_id}", "heat": 1.0}

    def _mem_query(self, s):
        return next(q for q, _ in s._captured["queries"] if "FROM memory" in q)

    def _wiki_query(self, s):
        return next(q for q, _ in s._captured["queries"] if "FROM wiki_page" in q)

    # ── Memory cap ────────────────────────────────────────────────────────────
    def test_memory_cap_applies_limit(self):
        s = self._routing_mock([_mem_row(1)], [], [])
        GraphAPI(s).get_full_graph(max_memories=42)
        mq = self._mem_query(s)
        assert "LIMIT $lim" in mq, f"memory query missing LIMIT: {mq}"
        params = next(p for q, p in s._captured["queries"] if "FROM memory" in q)
        assert params == {"lim": 42}

    def test_memory_cap_zero_is_unlimited(self):
        s = self._routing_mock([_mem_row(1)], [], [])
        GraphAPI(s).get_full_graph(max_memories=0)
        mq = self._mem_query(s)
        assert "LIMIT" not in mq, f"max_memories=0 must omit LIMIT, got: {mq}"

    # ── Wiki cap ──────────────────────────────────────────────────────────────
    def test_wiki_cap_applies_limit(self):
        wiki_rows = [self._wiki_row(i) for i in range(5)]
        s = self._routing_mock([_mem_row(1)], wiki_rows, [])
        GraphAPI(s).get_full_graph(max_wiki=3)
        wq = self._wiki_query(s)
        assert "LIMIT $lim" in wq, f"wiki query missing parameterised LIMIT: {wq}"
        params = next(p for q, p in s._captured["queries"] if "FROM wiki_page" in q)
        assert params == {"lim": 3}

    def test_wiki_cap_minus_one_is_unlimited(self):
        s = self._routing_mock([_mem_row(1)], [self._wiki_row(1)], [])
        GraphAPI(s).get_full_graph(max_wiki=-1)
        wq = self._wiki_query(s)
        assert "LIMIT" not in wq, f"max_wiki=-1 must omit LIMIT, got: {wq}"

    # ── Entity cap (post-fetch slice) ─────────────────────────────────────────
    def test_entity_cap_slices_node_set(self):
        entity_rows = [self._entity_row(i) for i in range(10)]
        s = self._routing_mock([_mem_row(1)], [], entity_rows)
        result = GraphAPI(s).get_full_graph(max_entities=4)
        ent_nodes = [n for n in result["nodes"] if n["id"].startswith("entity:")]
        assert len(ent_nodes) == 4, f"expected 4 entity nodes, got {len(ent_nodes)}"

    def test_entity_cap_zero_is_unlimited(self):
        entity_rows = [self._entity_row(i) for i in range(10)]
        s = self._routing_mock([_mem_row(1)], [], entity_rows)
        result = GraphAPI(s).get_full_graph(max_entities=0)
        ent_nodes = [n for n in result["nodes"] if n["id"].startswith("entity:")]
        assert len(ent_nodes) == 10, f"max_entities=0 must keep all, got {len(ent_nodes)}"


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
        from yadgar._shared.observability.metrics import yadgar_graph_api_orphan_edges_dropped_total

        before = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        mem_rows = [_mem_row(1, "x"), _mem_row(2, "y")]
        causal = [_causal(99, 100), _causal(101, 102)]
        s = _make_mock_storage(mem_rows, causal)
        GraphAPI(s).get_full_graph()
        after = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        assert after - before >= 2, f"Expected metric delta >=2, got {after - before}"

    def test_graph_api_no_drops_in_healthy_payload(self):
        """When no causal edges present, metric must not increment."""
        from yadgar._shared.observability.metrics import yadgar_graph_api_orphan_edges_dropped_total

        before = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        mem_rows = [_mem_row(1, "a", slot=0), _mem_row(2, "b", slot=0)]
        s = _make_mock_storage(mem_rows, [])
        result = GraphAPI(s).get_full_graph()
        after = yadgar_graph_api_orphan_edges_dropped_total._value.get()
        assert after == before, f"Expected no metric increment, delta={after - before}"
        temporal = [e for e in result["edges"] if e.get("type") == "temporal"]
        assert len(temporal) == 1
