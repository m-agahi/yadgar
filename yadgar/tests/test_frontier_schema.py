"""Tests for frontier memory schema: new Memory fields, models, config, tables, and CRUD."""

import json

import pytest

from yadgar.config import Settings
from yadgar.models import (
    CausalDAGEdge,
    Memory,
    MemoryArchive,
    MemoryRule,
    MemoryTransition,
)
from yadgar.storage import StorageEngine


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


def _make_memory(content="frontier test", directory="/tmp/frontier", **kwargs):
    base = {
        "content": content,
        "directory_context": directory,
        "tags": ["test"],
    }
    base.update(kwargs)
    return base


# ---- Memory model field defaults ----


class TestMemoryModelFields:
    def test_plasticity_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.plasticity == 1.0

    def test_stability_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.stability == 0.0

    def test_excitability_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.excitability == 1.0

    def test_last_excitability_update_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.last_excitability_update is None

    def test_store_type_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.store_type == "episodic"

    def test_compression_level_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.compression_level == 0

    def test_original_content_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.original_content is None

    def test_sr_coordinates_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.sr_x == 0.0
        assert m.sr_y == 0.0

    def test_reconsolidation_count_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.reconsolidation_count == 0

    def test_last_reconsolidated_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.last_reconsolidated is None

    def test_provenance_agent_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.provenance_agent == "default"

    def test_vector_clock_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.vector_clock == "{}"

    def test_is_protected_default(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.is_protected is False

    def test_all_frontier_fields_settable(self):
        m = Memory(
            content="x",
            directory_context="/tmp",
            plasticity=0.5,
            stability=0.8,
            excitability=0.3,
            store_type="semantic",
            compression_level=2,
            original_content="full text",
            sr_x=1.5,
            sr_y=-0.5,
            reconsolidation_count=3,
            provenance_agent="agent-2",
            vector_clock='{"a":1}',
            is_protected=True,
        )
        assert m.plasticity == 0.5
        assert m.stability == 0.8
        assert m.excitability == 0.3
        assert m.store_type == "semantic"
        assert m.compression_level == 2
        assert m.original_content == "full text"
        assert m.sr_x == 1.5
        assert m.sr_y == -0.5
        assert m.reconsolidation_count == 3
        assert m.provenance_agent == "agent-2"
        assert m.vector_clock == '{"a":1}'
        assert m.is_protected is True


# ---- New Pydantic model classes ----


class TestNewModels:
    def test_memory_rule_defaults(self):
        r = MemoryRule(
            rule_type="hard", scope="global", condition="tag contains x", action="filter"
        )
        assert r.id is None
        assert r.rule_type == "hard"
        assert r.scope == "global"
        assert r.scope_value is None
        assert r.priority == 0
        assert r.is_active is True
        assert r.created_at is not None

    def test_memory_rule_soft_directory(self):
        r = MemoryRule(
            rule_type="soft",
            scope="directory",
            scope_value="/home/user",
            condition="language == python",
            action="boost:0.3",
            priority=5,
        )
        assert r.rule_type == "soft"
        assert r.scope == "directory"
        assert r.scope_value == "/home/user"
        assert r.priority == 5

    def test_memory_archive_defaults(self):
        a = MemoryArchive(original_memory_id=1, content="old content")
        assert a.id is None
        assert a.original_memory_id == 1
        assert a.content == "old content"
        assert a.embedding is None
        assert a.mismatch_score == 0.0
        assert a.archive_reason == ""
        assert a.archived_at is not None

    def test_memory_archive_with_reason(self):
        a = MemoryArchive(
            original_memory_id=5,
            content="data",
            mismatch_score=0.7,
            archive_reason="reconsolidation",
        )
        assert a.mismatch_score == 0.7
        assert a.archive_reason == "reconsolidation"

    def test_memory_transition_defaults(self):
        t = MemoryTransition(from_memory_id=1, to_memory_id=2)
        assert t.id is None
        assert t.from_memory_id == 1
        assert t.to_memory_id == 2
        assert t.count == 1
        assert t.session_id == ""
        assert t.last_transition is not None

    def test_causal_dag_edge_defaults(self):
        e = CausalDAGEdge(source_entity_id=10, target_entity_id=20)
        assert e.id is None
        assert e.source_entity_id == 10
        assert e.target_entity_id == 20
        assert e.algorithm == "pc"
        assert e.confidence == 1.0
        assert e.is_validated is False
        assert e.discovered_at is not None

    def test_causal_dag_edge_custom(self):
        e = CausalDAGEdge(
            source_entity_id=1,
            target_entity_id=2,
            algorithm="ges",
            confidence=0.85,
            is_validated=True,
        )
        assert e.algorithm == "ges"
        assert e.confidence == 0.85
        assert e.is_validated is True


# ---- Config settings ----


class TestFrontierConfig:
    def test_hopfield_beta(self):
        s = Settings()
        assert s.HOPFIELD_BETA == 8.0
        assert isinstance(s.HOPFIELD_BETA, float)

    def test_hopfield_max_patterns(self):
        s = Settings()
        assert s.HOPFIELD_MAX_PATTERNS == 5000
        assert isinstance(s.HOPFIELD_MAX_PATTERNS, int)

    def test_excitability_settings(self):
        s = Settings()
        assert s.EXCITABILITY_HALF_LIFE_HOURS == 6.0
        assert s.EXCITABILITY_BOOST == 0.5

    def test_write_gate_threshold(self):
        s = Settings()
        assert s.WRITE_GATE_THRESHOLD == 0.0

    def test_sr_settings(self):
        s = Settings()
        assert s.SR_DISCOUNT == 0.9
        assert s.SR_UPDATE_RATE == 0.1

    def test_cognitive_load_limit(self):
        s = Settings()
        assert s.COGNITIVE_LOAD_LIMIT == 4
        assert isinstance(s.COGNITIVE_LOAD_LIMIT, int)

    def test_crdt_agent_id(self):
        s = Settings()
        assert s.CRDT_AGENT_ID == "default"
        assert isinstance(s.CRDT_AGENT_ID, str)


# ---- Schema: tables and columns ----


class TestFrontierSchema:
    def test_new_tables_exist(self, storage):
        """Verify the new SurrealDB tables exist by inserting and querying a record."""
        # memory_rule table
        rid = storage.insert_rule(
            {"rule_type": "hard", "scope": "global", "condition": "x", "action": "filter"}
        )
        assert rid > 0
        # memory_archive table
        mem_id = storage.insert_memory(_make_memory())
        aid = storage.insert_archive({"original_memory_id": mem_id, "content": "v1"})
        assert aid > 0
        # memory_transition table
        m2 = storage.insert_memory(_make_memory("second"))
        tid = storage.insert_transition({"from_memory_id": mem_id, "to_memory_id": m2})
        assert tid > 0
        # causal_dag_edge table
        e1 = storage.insert_entity({"name": "ea", "type": "file"})
        e2 = storage.insert_entity({"name": "eb", "type": "file"})
        eid = storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})
        assert eid > 0

    def test_memory_rules_columns(self, storage):
        """Verify memory_rule record has expected fields."""
        storage.insert_rule(
            {
                "rule_type": "hard",
                "scope": "global",
                "condition": "tag contains secret",
                "action": "filter",
                "priority": 5,
            }
        )
        rules = storage.get_rules_for_scope("global")
        assert len(rules) >= 1
        row = rules[0]
        for field in ("rule_type", "scope", "condition", "action", "priority", "is_active"):
            assert field in row, f"Field {field} missing from memory_rule"

    def test_memory_archives_columns(self, storage):
        """Verify memory_archive record has expected fields."""
        mem_id = storage.insert_memory(_make_memory())
        storage.insert_archive(
            {
                "original_memory_id": mem_id,
                "content": "old content",
                "mismatch_score": 0.5,
                "archive_reason": "test",
            }
        )
        archives = storage.get_archives_for_memory(mem_id)
        row = archives[0]
        for field in ("original_memory_id", "content", "mismatch_score", "archive_reason"):
            assert field in row, f"Field {field} missing from memory_archive"

    def test_memory_transitions_columns(self, storage):
        """Verify memory_transition record has expected fields."""
        m1 = storage.insert_memory(_make_memory("a"))
        m2 = storage.insert_memory(_make_memory("b"))
        storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})
        t = storage.get_transition(m1, m2)
        for field in ("from_memory_id", "to_memory_id", "count"):
            assert field in t, f"Field {field} missing from memory_transition"

    def test_causal_dag_edges_columns(self, storage):
        """Verify causal_dag_edge record has expected fields."""
        e1 = storage.insert_entity({"name": "p", "type": "file"})
        e2 = storage.insert_entity({"name": "q", "type": "file"})
        storage.insert_causal_edge(
            {
                "source_entity_id": e1,
                "target_entity_id": e2,
                "algorithm": "pc",
                "confidence": 0.8,
            }
        )
        edges = storage.get_causal_edges_for_entity(e1)
        row = edges[0]
        for field in (
            "source_entity_id",
            "target_entity_id",
            "algorithm",
            "confidence",
            "is_validated",
        ):
            assert field in row, f"Field {field} missing from causal_dag_edge"

    def test_frontier_memory_columns_exist(self, storage):
        """Verify frontier memory fields are stored and retrieved correctly."""
        mem_id = storage.insert_memory(_make_memory())
        mem = storage.get_memory(mem_id)
        frontier_cols = [
            "plasticity",
            "stability",
            "excitability",
            "store_type",
            "compression_level",
            "sr_x",
            "sr_y",
            "reconsolidation_count",
            "provenance_agent",
            "vector_clock",
            "is_protected",
        ]
        for col in frontier_cols:
            assert col in mem, f"Column {col} missing from memory record"

    def test_indexes_exist(self, storage):
        """Verify indexed lookups work correctly (functional index test)."""
        # memory_transition: lookup by from_memory_id
        m1 = storage.insert_memory(_make_memory("src"))
        m2 = storage.insert_memory(_make_memory("dst"))
        storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})
        assert len(storage.get_transitions_from(m1)) == 1
        # memory_archive: lookup by original_memory_id
        storage.insert_archive({"original_memory_id": m1, "content": "old"})
        assert len(storage.get_archives_for_memory(m1)) == 1
        # causal_dag_edge: lookup by source entity
        e1 = storage.insert_entity({"name": "idx_src", "type": "file"})
        e2 = storage.insert_entity({"name": "idx_dst", "type": "file"})
        storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})
        assert len(storage.get_causal_edges_for_entity(e1)) == 1
        # memory_rule: lookup by scope
        storage.insert_rule(
            {"rule_type": "hard", "scope": "global", "condition": "x", "action": "filter"}
        )
        assert len(storage.get_rules_for_scope("global")) >= 1

    def test_schema_migration_idempotent(self, tmp_path):
        """Running migration twice should not raise errors."""
        db_path = str(tmp_path / "idempotent.db")
        engine1 = StorageEngine(db_path)
        engine1.close()
        # Open again — migration runs again on same DB
        engine2 = StorageEngine(db_path)
        # Verify tables still work by exercising CRUD
        rid = engine2.insert_rule(
            {"rule_type": "hard", "scope": "global", "condition": "x", "action": "filter"}
        )
        assert rid > 0
        engine2.close()


# ---- CRUD: Memory Rules ----


class TestMemoryRulesCRUD:
    def test_insert_and_get_global_rule(self, storage):
        rule_id = storage.insert_rule(
            {
                "rule_type": "hard",
                "scope": "global",
                "condition": "tag contains secret",
                "action": "filter",
            }
        )
        assert rule_id > 0
        rules = storage.get_rules_for_scope("global")
        assert len(rules) == 1
        assert rules[0]["condition"] == "tag contains secret"
        assert rules[0]["action"] == "filter"
        assert rules[0]["is_active"] is True

    def test_insert_directory_scoped_rule(self, storage):
        storage.insert_rule(
            {
                "rule_type": "soft",
                "scope": "directory",
                "scope_value": "/home/user/project",
                "condition": "language == python",
                "action": "boost:0.3",
                "priority": 10,
            }
        )
        rules = storage.get_rules_for_scope("directory", "/home/user/project")
        assert len(rules) == 1
        assert rules[0]["priority"] == 10

    def test_update_rule(self, storage):
        rule_id = storage.insert_rule(
            {
                "rule_type": "hard",
                "scope": "global",
                "condition": "x",
                "action": "filter",
            }
        )
        storage.update_rule(rule_id, {"priority": 99, "is_active": False})
        # Inactive rule should not appear in get_rules_for_scope
        rules = storage.get_rules_for_scope("global")
        assert len(rules) == 0

    def test_delete_rule(self, storage):
        rule_id = storage.insert_rule(
            {
                "rule_type": "hard",
                "scope": "global",
                "condition": "x",
                "action": "filter",
            }
        )
        storage.delete_rule(rule_id)
        rules = storage.get_rules_for_scope("global")
        assert len(rules) == 0

    def test_rules_ordered_by_priority(self, storage):
        storage.insert_rule(
            {
                "rule_type": "hard",
                "scope": "global",
                "condition": "a",
                "action": "filter",
                "priority": 1,
            }
        )
        storage.insert_rule(
            {
                "rule_type": "soft",
                "scope": "global",
                "condition": "b",
                "action": "boost:0.1",
                "priority": 10,
            }
        )
        storage.insert_rule(
            {
                "rule_type": "hard",
                "scope": "global",
                "condition": "c",
                "action": "filter",
                "priority": 5,
            }
        )
        rules = storage.get_rules_for_scope("global")
        priorities = [r["priority"] for r in rules]
        assert priorities == sorted(priorities, reverse=True)


# ---- CRUD: Memory Archives ----


class TestMemoryArchivesCRUD:
    def test_insert_and_get_archive(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        archive_id = storage.insert_archive(
            {
                "original_memory_id": mem_id,
                "content": "old version of content",
                "mismatch_score": 0.42,
                "archive_reason": "reconsolidation",
            }
        )
        assert archive_id > 0
        archives = storage.get_archives_for_memory(mem_id)
        assert len(archives) == 1
        assert archives[0]["content"] == "old version of content"
        assert archives[0]["mismatch_score"] == pytest.approx(0.42)
        assert archives[0]["archive_reason"] == "reconsolidation"

    def test_multiple_archives_for_same_memory(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        storage.insert_archive({"original_memory_id": mem_id, "content": "v1"})
        storage.insert_archive({"original_memory_id": mem_id, "content": "v2"})
        storage.insert_archive({"original_memory_id": mem_id, "content": "v3"})
        archives = storage.get_archives_for_memory(mem_id)
        assert len(archives) == 3

    def test_archive_with_embedding(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        blob = b"\x00" * 16
        storage.insert_archive(
            {
                "original_memory_id": mem_id,
                "content": "data",
                "embedding": blob,
            }
        )
        archives = storage.get_archives_for_memory(mem_id)
        assert archives[0]["embedding"] == blob

    def test_no_archives_returns_empty(self, storage):
        assert storage.get_archives_for_memory(9999) == []


# ---- CRUD: Memory Transitions ----


class TestMemoryTransitionsCRUD:
    def test_insert_and_get_transition(self, storage):
        m1 = storage.insert_memory(_make_memory("first"))
        m2 = storage.insert_memory(_make_memory("second"))
        tid = storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})
        assert tid > 0
        t = storage.get_transition(m1, m2)
        assert t is not None
        assert t["count"] == 1
        assert t["from_memory_id"] == m1
        assert t["to_memory_id"] == m2

    def test_increment_transition(self, storage):
        m1 = storage.insert_memory(_make_memory("a"))
        m2 = storage.insert_memory(_make_memory("b"))
        storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})
        storage.increment_transition(m1, m2)
        storage.increment_transition(m1, m2)
        t = storage.get_transition(m1, m2)
        assert t["count"] == 3

    def test_get_transitions_from(self, storage):
        m1 = storage.insert_memory(_make_memory("source"))
        m2 = storage.insert_memory(_make_memory("target1"))
        m3 = storage.insert_memory(_make_memory("target2"))
        storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})
        storage.insert_transition({"from_memory_id": m1, "to_memory_id": m3, "count": 5})
        transitions = storage.get_transitions_from(m1)
        assert len(transitions) == 2
        # Ordered by count DESC
        assert transitions[0]["count"] >= transitions[1]["count"]

    def test_unique_constraint(self, storage):
        m1 = storage.insert_memory(_make_memory("a"))
        m2 = storage.insert_memory(_make_memory("b"))
        storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})
        with pytest.raises(Exception):  # noqa: B017
            storage.insert_transition({"from_memory_id": m1, "to_memory_id": m2})

    def test_get_transition_nonexistent(self, storage):
        assert storage.get_transition(9999, 8888) is None


# ---- CRUD: Causal DAG Edges ----


class TestCausalDAGEdgesCRUD:
    def test_insert_and_get_edges(self, storage):
        e1 = storage.insert_entity({"name": "file_a", "type": "file"})
        e2 = storage.insert_entity({"name": "file_b", "type": "file"})
        edge_id = storage.insert_causal_edge(
            {
                "source_entity_id": e1,
                "target_entity_id": e2,
                "algorithm": "ges",
                "confidence": 0.9,
            }
        )
        assert edge_id > 0
        edges = storage.get_causal_edges_for_entity(e1)
        assert len(edges) == 1
        assert edges[0]["algorithm"] == "ges"
        assert edges[0]["confidence"] == pytest.approx(0.9)
        assert edges[0]["is_validated"] is False

    def test_edges_for_target_entity(self, storage):
        e1 = storage.insert_entity({"name": "x", "type": "function"})
        e2 = storage.insert_entity({"name": "y", "type": "function"})
        storage.insert_causal_edge({"source_entity_id": e1, "target_entity_id": e2})
        edges = storage.get_causal_edges_for_entity(e2)
        assert len(edges) == 1

    def test_get_all_causal_edges(self, storage):
        e1 = storage.insert_entity({"name": "a", "type": "file"})
        e2 = storage.insert_entity({"name": "b", "type": "file"})
        e3 = storage.insert_entity({"name": "c", "type": "file"})
        storage.insert_causal_edge(
            {"source_entity_id": e1, "target_entity_id": e2, "confidence": 0.5}
        )
        storage.insert_causal_edge(
            {"source_entity_id": e2, "target_entity_id": e3, "confidence": 0.9}
        )
        all_edges = storage.get_all_causal_edges()
        assert len(all_edges) == 2
        # Ordered by confidence DESC
        assert all_edges[0]["confidence"] >= all_edges[1]["confidence"]

    def test_validated_edge(self, storage):
        e1 = storage.insert_entity({"name": "p", "type": "variable"})
        e2 = storage.insert_entity({"name": "q", "type": "variable"})
        storage.insert_causal_edge(
            {
                "source_entity_id": e1,
                "target_entity_id": e2,
                "is_validated": True,
            }
        )
        edges = storage.get_causal_edges_for_entity(e1)
        assert edges[0]["is_validated"] is True

    def test_empty_causal_edges(self, storage):
        assert storage.get_causal_edges_for_entity(9999) == []
        assert storage.get_all_causal_edges() == []


# ---- New memory columns persist through insert/get cycle ----


class TestFrontierMemoryPersistence:
    def test_default_values_persist(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        mem = storage.get_memory(mem_id)
        assert mem["plasticity"] == 1.0
        assert mem["stability"] == 0.0
        assert mem["excitability"] == 1.0
        assert mem["store_type"] == "episodic"
        assert mem["compression_level"] == 0
        assert mem["sr_x"] == 0.0
        assert mem["sr_y"] == 0.0
        assert mem["reconsolidation_count"] == 0
        assert mem["provenance_agent"] == "default"
        assert mem["vector_clock"] == "{}"
        assert mem["is_protected"] is False

    def test_nullable_columns_default_to_none(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        mem = storage.get_memory(mem_id)
        assert mem["last_excitability_update"] is None
        assert mem["original_content"] is None
        assert mem["last_reconsolidated"] is None

    def test_update_frontier_fields(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        storage._q(
            "UPDATE type::record('memory', $id) SET plasticity = 0.7, stability = 0.5, "
            "store_type = 'semantic', compression_level = 1, sr_x = 2.5, sr_y = -1.3, is_protected = true",
            {"id": mem_id},
        )
        mem = storage.get_memory(mem_id)
        assert mem["plasticity"] == pytest.approx(0.7)
        assert mem["stability"] == pytest.approx(0.5)
        assert mem["store_type"] == "semantic"
        assert mem["compression_level"] == 1
        assert mem["sr_x"] == pytest.approx(2.5)
        assert mem["sr_y"] == pytest.approx(-1.3)
        assert mem["is_protected"] is True

    def test_vector_clock_json_persists(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        vc = json.dumps({"agent1": 3, "agent2": 1})
        storage._q(
            "UPDATE type::record('memory', $id) SET vector_clock = $vc",
            {"id": mem_id, "vc": vc},
        )
        mem = storage.get_memory(mem_id)
        assert json.loads(mem["vector_clock"]) == {"agent1": 3, "agent2": 1}
