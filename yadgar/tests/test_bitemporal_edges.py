"""C1 — Bi-temporal edge tests (v5.3.4).

Five tests verifying valid_from / valid_until on all three KG edge tables:
  1. Migration #007 adds valid_from + valid_until columns to all edge tables.
  2. New edge insert defaults valid_from ≈ now(), valid_until = None.
  3. invalidate_edge() sets valid_until ≈ now().
  4. get_full_graph() excludes invalidated edges by default.
  5. get_full_graph(include_invalidated=True) returns all edges.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "bitemporal_test.db"), embedding_dim=384)
    yield engine
    engine.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _insert_bare_memory(storage, content: str = "test") -> int:
    now = storage._now_iso()
    mid = storage._next_id("memory")
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, tags = $tags, directory_context = $dir, "
        "created_at = $ts, last_accessed = $ts, heat = $heat, "
        "is_stale = false, plasticity = 1.0, stability = 0.0, "
        "excitability = 1.0, store_type = $st, compression_level = 0, "
        "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
        "provenance_agent = $agent, vector_clock = $vc, is_protected = false",
        {
            "id": mid,
            "content": content,
            "tags": [],
            "dir": "/tmp",
            "ts": now,
            "heat": 1.0,
            "st": "episodic",
            "agent": "default",
            "vc": "{}",
        },
    )
    return mid


def _insert_bare_entity(storage, name: str) -> int:
    now = storage._now_iso()
    eid = storage._next_id("entity")
    storage._q(
        "CREATE type::record('entity', $id) SET "
        "name = $name, type = 'CONCEPT', created_at = $ts, "
        "last_accessed = $ts, heat = 1.0, archived = false",
        {"id": eid, "name": name, "ts": now},
    )
    return eid


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestMigration007:
    """T1 — Migration #007 adds valid_from + valid_until to all edge tables."""

    def test_columns_added_to_all_edge_tables(self, storage):
        """Running migration #007 produces retrievable valid_from/valid_until fields."""
        # Insert a row into each edge table (migration already ran via _init_schema).
        e1 = _insert_bare_entity(storage, "A")
        e2 = _insert_bare_entity(storage, "B")
        m1 = _insert_bare_memory(storage)
        m2 = _insert_bare_memory(storage, "other")

        # causal_dag_edge
        storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})
        ce_rows = storage._q("SELECT valid_from, valid_until FROM causal_dag_edge")
        assert ce_rows, "No causal_dag_edge row found"
        row = ce_rows[0]
        assert row.get("valid_from") is not None, "valid_from missing from causal_dag_edge"
        assert row.get("valid_until") is None, "valid_until should be NULL on new row"

        # relationship
        storage.insert_typed_relationship(e1, e2, "KNOWS")
        rel_rows = storage._q("SELECT valid_from, valid_until FROM relationship")
        assert rel_rows, "No relationship row found"
        rel_row = rel_rows[0]
        assert rel_row.get("valid_from") is not None, "valid_from missing from relationship"
        assert rel_row.get("valid_until") is None, "valid_until should be NULL on new row"

        # memory_similarity_link
        storage.insert_memory_similarity_link(m1, m2, 0.85)
        msl_rows = storage._q("SELECT valid_from, valid_until FROM memory_similarity_link")
        assert msl_rows, "No memory_similarity_link row found"
        msl_row = msl_rows[0]
        assert msl_row.get("valid_from") is not None, (
            "valid_from missing from memory_similarity_link"
        )
        assert msl_row.get("valid_until") is None, (
            "valid_until should be NULL on new memory_similarity_link"
        )


class TestEdgeInsertDefaults:
    """T2 — New edge insert defaults valid_from ≈ now(), valid_until = NULL."""

    def test_causal_edge_defaults(self, storage):
        e1 = _insert_bare_entity(storage, "X")
        e2 = _insert_bare_entity(storage, "Y")
        storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})
        rows = storage._q("SELECT valid_from, valid_until FROM causal_dag_edge")
        assert rows, "Expected at least one causal_dag_edge row"
        assert rows[0].get("valid_from") is not None
        assert rows[0].get("valid_until") is None

    def test_relationship_defaults(self, storage):
        e1 = _insert_bare_entity(storage, "P")
        e2 = _insert_bare_entity(storage, "Q")
        storage.insert_typed_relationship(e1, e2, "RELATES")
        rows = storage._q("SELECT valid_from, valid_until FROM relationship")
        assert rows, "Expected at least one relationship row"
        assert rows[0].get("valid_from") is not None
        assert rows[0].get("valid_until") is None

    def test_similarity_link_defaults(self, storage):
        m1 = _insert_bare_memory(storage, "alpha")
        m2 = _insert_bare_memory(storage, "beta")
        storage.insert_memory_similarity_link(m1, m2, 0.9)
        rows = storage._q("SELECT valid_from, valid_until FROM memory_similarity_link")
        assert rows, "Expected at least one memory_similarity_link row"
        assert rows[0].get("valid_from") is not None
        assert rows[0].get("valid_until") is None


class TestInvalidateEdge:
    """T3 — invalidate_edge() sets valid_until ≈ now()."""

    def test_invalidate_causal_edge(self, storage):
        from yadgar._shared.storage.bitemporal import invalidate_edge

        e1 = _insert_bare_entity(storage, "C1")
        e2 = _insert_bare_entity(storage, "C2")
        eid = storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})

        # Before invalidation, valid_until is NULL
        rows_before = storage._q("SELECT valid_until FROM causal_dag_edge")
        assert rows_before[0].get("valid_until") is None

        invalidate_edge(storage, "causal_dag_edge", eid, reason="superseded by new edge")

        rows_after = storage._q("SELECT valid_until FROM causal_dag_edge")
        assert rows_after[0].get("valid_until") is not None, (
            "valid_until not set after invalidation"
        )

    def test_invalidate_relationship(self, storage):
        from yadgar._shared.storage.bitemporal import invalidate_edge

        e1 = _insert_bare_entity(storage, "R1")
        e2 = _insert_bare_entity(storage, "R2")
        rid = storage.insert_typed_relationship(e1, e2, "WORKED_AT")

        invalidate_edge(storage, "relationship", rid, reason="user changed employer")

        rows = storage._q("SELECT valid_until FROM relationship")
        assert rows[0].get("valid_until") is not None


class TestGetFullGraphFiltering:
    """T4 — get_full_graph() excludes invalidated edges by default."""

    def test_invalidated_causal_edge_excluded_by_default(self, storage):
        from yadgar._shared.storage.bitemporal import invalidate_edge
        from yadgar.core.graph_api import GraphAPI

        e1 = _insert_bare_entity(storage, "G1")
        e2 = _insert_bare_entity(storage, "G2")
        eid = storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})

        # Before invalidation: causal edge appears in graph
        api = GraphAPI(storage)
        graph_before = api.get_full_graph()
        causal_edges_before = [e for e in graph_before["edges"] if e["type"] == "causal"]
        assert len(causal_edges_before) >= 1, "Expected at least 1 causal edge before invalidation"

        # Invalidate and re-query
        invalidate_edge(storage, "causal_dag_edge", eid)
        graph_after = api.get_full_graph()
        causal_edges_after = [e for e in graph_after["edges"] if e["type"] == "causal"]
        assert len(causal_edges_after) == 0, "Invalidated causal edge should be excluded by default"


class TestGetFullGraphIncludeInvalidated:
    """T5 — get_full_graph(include_invalidated=True) returns all edges."""

    def test_include_invalidated_returns_all(self, storage):
        from yadgar._shared.storage.bitemporal import invalidate_edge
        from yadgar.core.graph_api import GraphAPI

        e1 = _insert_bare_entity(storage, "H1")
        e2 = _insert_bare_entity(storage, "H2")
        eid = storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})

        invalidate_edge(storage, "causal_dag_edge", eid)

        api = GraphAPI(storage)

        # Default: excluded
        graph_default = api.get_full_graph()
        causal_default = [e for e in graph_default["edges"] if e["type"] == "causal"]
        assert len(causal_default) == 0

        # include_invalidated=True: included
        graph_all = api.get_full_graph(include_invalidated=True)
        causal_all = [e for e in graph_all["edges"] if e["type"] == "causal"]
        assert len(causal_all) >= 1, "include_invalidated=True should return invalidated edges"
