"""C3 — Citation tracing tests.

Tests verify:
1. Migration #006 adds source_memory_id column to all three edge tables.
2. New edge insert with source_memory_id=42 → stored and retrievable.
3. Edge insert without source_memory_id → stored as None (back-compat).
4. Recall response includes source_memory_id field for edges that have it.
"""

from __future__ import annotations

import pytest

from yadgar.storage import StorageEngine
from yadgar.storage.migrations import _migration_006_source_memory_id

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "citation_test.db"), embedding_dim=384)
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
        "name = $name, entity_type = $et, heat = 1.0, "
        "created_at = $ts, last_accessed = $ts",
        {"id": eid, "name": name, "et": "concept", "ts": now},
    )
    return eid


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_migration_006_adds_column_to_memory_similarity_link(storage):
    """T1a: migration #006 adds source_memory_id to memory_similarity_link."""
    _migration_006_source_memory_id(storage)
    mid_a = _insert_bare_memory(storage, "A")
    mid_b = _insert_bare_memory(storage, "B")
    now = storage._now_iso()
    lid = storage._next_id("memory_similarity_link")
    # Insert with source_memory_id — should not raise
    storage._q(
        "CREATE type::record('memory_similarity_link', $id) SET "
        "source_memory_id = $src, target_memory_id = $tgt, "
        "weight = $w, created_at = $ts, updated_at = $ts, "
        "citation_source_memory_id = $csm",
        {
            "id": lid,
            "src": min(mid_a, mid_b),
            "tgt": max(mid_a, mid_b),
            "w": 0.9,
            "ts": now,
            "csm": mid_a,
        },
    )
    rows = storage._q("SELECT * FROM memory_similarity_link")
    assert len(rows) >= 1
    row = storage._rows_to_dicts(rows)[0]
    assert "citation_source_memory_id" in row


def test_migration_006_adds_column_to_causal_dag_edge(storage):
    """T1b: migration #006 adds source_memory_id to causal_dag_edge."""
    _migration_006_source_memory_id(storage)
    eid_a = _insert_bare_entity(storage, "cause_entity")
    eid_b = _insert_bare_entity(storage, "effect_entity")
    mid_src = _insert_bare_memory(storage, "origin memory")
    now = storage._now_iso()
    edge_id = storage._next_id("causal_dag_edge")
    storage._q(
        "CREATE type::record('causal_dag_edge', $id) SET "
        "source_entity_id = $src, target_entity_id = $tgt, "
        "algorithm = $algo, confidence = $conf, "
        "discovered_at = $ts, is_validated = false, "
        "source_memory_id = $smid",
        {
            "id": edge_id,
            "src": eid_a,
            "tgt": eid_b,
            "algo": "pc",
            "conf": 0.9,
            "ts": now,
            "smid": mid_src,
        },
    )
    rows = storage._q("SELECT * FROM causal_dag_edge")
    assert len(rows) >= 1
    row = storage._rows_to_dicts(rows)[0]
    assert "source_memory_id" in row
    assert int(row["source_memory_id"]) == mid_src


def test_migration_006_adds_column_to_relationship(storage):
    """T1c: migration #006 adds source_memory_id to relationship."""
    _migration_006_source_memory_id(storage)
    eid_a = _insert_bare_entity(storage, "entityA")
    eid_b = _insert_bare_entity(storage, "entityB")
    mid_src = _insert_bare_memory(storage, "source")
    now = storage._now_iso()
    rid = storage._next_id("relationship")
    storage._q(
        "CREATE type::record('relationship', $id) SET "
        "source_entity_id = $src, target_entity_id = $tgt, "
        "relationship_type = $rt, weight = 1.0, "
        "created_at = $ts, last_reinforced = $ts, "
        "event_time = $ts, record_time = $ts, "
        "is_causal = false, confidence = 1.0, "
        "source_memory_id = $smid",
        {"id": rid, "src": eid_a, "tgt": eid_b, "rt": "relates_to", "ts": now, "smid": mid_src},
    )
    rows = storage._q("SELECT * FROM relationship")
    assert len(rows) >= 1
    row = storage._rows_to_dicts(rows)[0]
    assert "source_memory_id" in row
    assert int(row["source_memory_id"]) == mid_src


def test_insert_causal_edge_with_source_memory_id(storage):
    """T2: insert_causal_edge with source_memory_id → stored and retrievable."""
    _migration_006_source_memory_id(storage)
    eid_a = _insert_bare_entity(storage, "src_ent")
    eid_b = _insert_bare_entity(storage, "tgt_ent")
    mid = _insert_bare_memory(storage, "causal origin")

    edge_id = storage.insert_causal_edge(
        {
            "source_entity_id": eid_a,
            "target_entity_id": eid_b,
            "algorithm": "pc",
            "confidence": 0.85,
            "source_memory_id": mid,
        }
    )

    edges = storage.get_all_causal_edges()
    assert any(e["id"] == edge_id and int(e.get("source_memory_id", -1)) == mid for e in edges)


def test_insert_causal_edge_without_source_memory_id(storage):
    """T3: insert_causal_edge without source_memory_id → stored as None (back-compat)."""
    _migration_006_source_memory_id(storage)
    eid_a = _insert_bare_entity(storage, "src_ent2")
    eid_b = _insert_bare_entity(storage, "tgt_ent2")

    edge_id = storage.insert_causal_edge(
        {
            "source_entity_id": eid_a,
            "target_entity_id": eid_b,
            "algorithm": "pc",
            "confidence": 0.9,
        }
    )

    edges = storage.get_all_causal_edges()
    match = [e for e in edges if e["id"] == edge_id]
    assert match
    # source_memory_id should be absent or None
    assert match[0].get("source_memory_id") is None


def test_insert_memory_similarity_link_with_source_memory_id(storage):
    """T2b: insert_memory_similarity_link with origin_memory_id → stored."""
    _migration_006_source_memory_id(storage)
    mid_a = _insert_bare_memory(storage, "memA")
    mid_b = _insert_bare_memory(storage, "memB")

    storage.insert_memory_similarity_link(mid_a, mid_b, 0.85, origin_memory_id=mid_a)

    link = storage.get_memory_similarity_link(mid_a, mid_b)
    assert link is not None
    assert int(link.get("citation_source_memory_id", -1)) == mid_a


def test_graph_api_edges_include_source_memory_id(storage):
    """T4: graph_api returns source_memory_id in causal edge output."""
    _migration_006_source_memory_id(storage)
    from yadgar.graph_api import GraphAPI

    eid_a = _insert_bare_entity(storage, "node_A")
    eid_b = _insert_bare_entity(storage, "node_B")
    mid = _insert_bare_memory(storage, "origin")

    storage.insert_causal_edge(
        {
            "source_entity_id": eid_a,
            "target_entity_id": eid_b,
            "algorithm": "pc",
            "confidence": 0.9,
            "source_memory_id": mid,
        }
    )

    api = GraphAPI(storage)
    graph = api.get_full_graph()
    causal_edges = [e for e in graph.get("edges", []) if e.get("type") == "causal"]
    assert causal_edges, "expected causal edges in graph"
    # At least the one we inserted has source_memory_id
    sourced = [e for e in causal_edges if e.get("source_memory_id") == mid]
    assert sourced, f"no causal edge with source_memory_id={mid}; got {causal_edges}"
