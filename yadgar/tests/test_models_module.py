"""Tests for yadgar/models.py — pydantic dataclasses + enums.

Wave 2 coverage: yadgar/models.py (142 stmts, 0% pre-wave).
Strategy: instantiate every model, assert field defaults, exercise
validators and optional fields. Pure pydantic — no mocks needed.
"""

from datetime import UTC, datetime

import pytest

from yadgar._shared.models import (
    ADR,
    AstrocyteProcess,
    CausalDAGEdge,
    Checkpoint,
    ConsolidationLog,
    Entity,
    Episode,
    FileHash,
    Memory,
    MemoryArchive,
    MemoryCluster,
    MemoryRule,
    MemoryStats,
    MemoryTransition,
    NarrativeEntry,
    ProspectiveMemory,
    Relationship,
)

# ---------------------------------------------------------------------------
# Episode
# ---------------------------------------------------------------------------


class TestEpisode:
    def test_happy_path_required_fields(self):
        ep = Episode(session_id="s1", directory="/home/user", raw_content="hello")
        assert ep.session_id == "s1"
        assert ep.directory == "/home/user"
        assert ep.raw_content == "hello"
        assert ep.id is None
        assert ep.overlap_start is None
        assert ep.overlap_end is None
        assert isinstance(ep.timestamp, datetime)

    def test_optional_overlap_fields(self):
        ep = Episode(
            session_id="s2",
            directory="/tmp",
            raw_content="x",
            overlap_start=5,
            overlap_end=10,
        )
        assert ep.overlap_start == 5
        assert ep.overlap_end == 10

    def test_timestamp_default_is_utc(self):
        ep = Episode(session_id="s3", directory="/tmp", raw_content="y")
        assert ep.timestamp.tzinfo is not None

    def test_id_can_be_set(self):
        ep = Episode(id=42, session_id="s4", directory="/a", raw_content="z")
        assert ep.id == 42


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class TestEntity:
    def test_happy_path(self):
        e = Entity(name="main.py", type="file")
        assert e.name == "main.py"
        assert e.type == "file"
        assert e.heat == 1.0
        assert e.archived is False
        assert e.causal_weight == 0.0
        assert e.domain is None

    def test_all_types_valid(self):
        valid_types = [
            "file",
            "function",
            "variable",
            "dependency",
            "decision",
            "error",
            "solution",
        ]
        for t in valid_types:
            e = Entity(name="x", type=t)
            assert e.type == t

    def test_invalid_type_raises(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Entity(name="x", type="invalid_type")

    def test_with_domain(self):
        e = Entity(name="foo", type="function", domain="backend", causal_weight=0.5)
        assert e.domain == "backend"
        assert e.causal_weight == 0.5


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------


class TestRelationship:
    def test_happy_path(self):
        r = Relationship(source_entity_id=1, target_entity_id=2, relationship_type="imports")
        assert r.source_entity_id == 1
        assert r.target_entity_id == 2
        assert r.relationship_type == "imports"
        assert r.weight == 1.0
        assert r.is_causal is False
        assert r.confidence == 1.0

    def test_v2_fields_defaults(self):
        r = Relationship(source_entity_id=3, target_entity_id=4, relationship_type="calls")
        assert r.event_time is None
        assert isinstance(r.record_time, datetime)

    def test_causal_relationship(self):
        r = Relationship(
            source_entity_id=5,
            target_entity_id=6,
            relationship_type="caused_by",
            is_causal=True,
            confidence=0.8,
        )
        assert r.is_causal is True
        assert r.confidence == 0.8


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class TestMemory:
    def test_happy_path_required_fields(self):
        m = Memory(content="remember this", directory_context="/project")
        assert m.content == "remember this"
        assert m.directory_context == "/project"
        assert m.heat == 1.0
        assert m.is_stale is False
        assert m.surprise_score == 0.0
        assert m.importance == 0.5
        assert m.access_count == 0

    def test_happy_path_defaults(self):
        m = Memory(content="remember this", directory_context="/project")
        assert m.useful_count == 0
        assert m.store_type == "episodic"
        assert m.compression_level == 0
        assert m.plasticity == 1.0
        assert m.stability == 0.0
        assert m.excitability == 1.0
        assert m.is_protected is False
        assert m.vector_clock == "{}"

    def test_embedding_is_optional(self):
        m = Memory(content="x", directory_context="/tmp")
        assert m.embedding is None

    def test_tags_default_empty(self):
        m = Memory(content="y", directory_context="/tmp")
        assert m.tags == []

    def test_semantic_store_type(self):
        m = Memory(content="z", directory_context="/tmp", store_type="semantic")
        assert m.store_type == "semantic"

    def test_v3_frontier_fields(self):
        m = Memory(
            content="frontier",
            directory_context="/project",
            plasticity=0.5,
            stability=0.9,
            excitability=1.2,
            sr_x=0.3,
            sr_y=0.7,
            reconsolidation_count=3,
            provenance_agent="subagent-alpha",
            is_protected=True,
        )
        assert m.plasticity == 0.5
        assert m.stability == 0.9
        assert m.excitability == 1.2
        assert m.sr_x == 0.3
        assert m.sr_y == 0.7
        assert m.reconsolidation_count == 3
        assert m.provenance_agent == "subagent-alpha"
        assert m.is_protected is True

    def test_compressed_memory(self):
        m = Memory(
            content="gist",
            directory_context="/p",
            compressed=True,
            compression_level=1,
            original_content="original full text",
        )
        assert m.compressed is True
        assert m.compression_level == 1
        assert m.original_content == "original full text"


# ---------------------------------------------------------------------------
# ConsolidationLog
# ---------------------------------------------------------------------------


class TestConsolidationLog:
    def test_defaults(self):
        cl = ConsolidationLog()
        assert cl.memories_added == 0
        assert cl.memories_updated == 0
        assert cl.memories_archived == 0
        assert cl.memories_deleted == 0
        assert cl.duration_ms == 0

    def test_with_values(self):
        cl = ConsolidationLog(
            memories_added=5,
            memories_updated=3,
            memories_archived=2,
            memories_deleted=1,
            duration_ms=150,
        )
        assert cl.memories_added == 5
        assert cl.duration_ms == 150


# ---------------------------------------------------------------------------
# FileHash
# ---------------------------------------------------------------------------


class TestFileHash:
    def test_happy_path(self):
        fh = FileHash(filepath="/etc/file.py", hash="abc123")
        assert fh.filepath == "/etc/file.py"
        assert fh.hash == "abc123"
        assert isinstance(fh.last_checked, datetime)


# ---------------------------------------------------------------------------
# MemoryStats
# ---------------------------------------------------------------------------


class TestMemoryStats:
    def test_happy_path(self):
        ms = MemoryStats(
            total_memories=100,
            active_count=80,
            archived_count=15,
            stale_count=5,
            avg_heat=0.6,
        )
        assert ms.total_memories == 100
        assert ms.avg_heat == 0.6
        assert ms.last_consolidation is None

    def test_with_last_consolidation(self):
        now = datetime.now(UTC)
        ms = MemoryStats(
            total_memories=1,
            active_count=1,
            archived_count=0,
            stale_count=0,
            avg_heat=1.0,
            last_consolidation=now,
        )
        assert ms.last_consolidation == now


# ---------------------------------------------------------------------------
# MemoryCluster (v2)
# ---------------------------------------------------------------------------


class TestMemoryCluster:
    def test_defaults(self):
        mc = MemoryCluster(name="cluster-a")
        assert mc.name == "cluster-a"
        assert mc.level == 0
        assert mc.parent_cluster_id is None
        assert mc.summary == ""
        assert mc.member_count == 0
        assert mc.heat == 1.0

    def test_hierarchy(self):
        mc = MemoryCluster(name="root", level=2, parent_cluster_id=10, member_count=50)
        assert mc.level == 2
        assert mc.parent_cluster_id == 10


# ---------------------------------------------------------------------------
# ProspectiveMemory (v2)
# ---------------------------------------------------------------------------


class TestProspectiveMemory:
    def test_directory_match(self):
        pm = ProspectiveMemory(
            content="remind me when in /project",
            trigger_condition="/project",
            trigger_type="directory_match",
        )
        assert pm.trigger_type == "directory_match"
        assert pm.is_active is True
        assert pm.triggered_count == 0
        assert pm.triggered_at is None

    def test_invalid_trigger_type_raises(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ProspectiveMemory(
                content="x",
                trigger_condition="y",
                trigger_type="invalid_type",
            )

    def test_all_trigger_types(self):
        for ttype in ["directory_match", "keyword_match", "entity_match", "time_based"]:
            pm = ProspectiveMemory(content="c", trigger_condition="t", trigger_type=ttype)
            assert pm.trigger_type == ttype


# ---------------------------------------------------------------------------
# NarrativeEntry (v2)
# ---------------------------------------------------------------------------


class TestNarrativeEntry:
    def test_happy_path(self):
        now = datetime.now(UTC)
        ne = NarrativeEntry(
            directory_context="/project",
            summary="did stuff",
            period_start=now,
            period_end=now,
        )
        assert ne.summary == "did stuff"
        assert ne.key_decisions == []
        assert ne.key_events == []
        assert ne.heat == 1.0

    def test_with_decisions_and_events(self):
        now = datetime.now(UTC)
        ne = NarrativeEntry(
            directory_context="/p",
            summary="s",
            period_start=now,
            period_end=now,
            key_decisions=["decided A"],
            key_events=["event B"],
        )
        assert ne.key_decisions == ["decided A"]
        assert ne.key_events == ["event B"]


# ---------------------------------------------------------------------------
# AstrocyteProcess (v2)
# ---------------------------------------------------------------------------


class TestAstrocyteProcess:
    def test_defaults(self):
        ap = AstrocyteProcess(name="proc-1", domain="backend")
        assert ap.name == "proc-1"
        assert ap.domain == "backend"
        assert ap.memory_ids == []
        assert ap.entity_ids == []
        assert ap.heat == 1.0

    def test_with_ids(self):
        ap = AstrocyteProcess(name="proc-2", domain="frontend", memory_ids=[1, 2], entity_ids=[3])
        assert ap.memory_ids == [1, 2]
        assert ap.entity_ids == [3]


# ---------------------------------------------------------------------------
# MemoryRule (v3)
# ---------------------------------------------------------------------------


class TestMemoryRule:
    def test_hard_global_rule(self):
        mr = MemoryRule(
            rule_type="hard",
            scope="global",
            condition="tag contains architecture",
            action="boost:0.5",
        )
        assert mr.rule_type == "hard"
        assert mr.scope == "global"
        assert mr.is_active is True
        assert mr.priority == 0

    def test_all_rule_types(self):
        for rt in ["hard", "soft", "write_block", "write_redact"]:
            mr = MemoryRule(rule_type=rt, scope="global", condition="c", action="a")
            assert mr.rule_type == rt

    def test_directory_scope(self):
        mr = MemoryRule(
            rule_type="soft",
            scope="directory",
            scope_value="/my/project",
            condition="language == python",
            action="penalty:0.2",
        )
        assert mr.scope_value == "/my/project"


# ---------------------------------------------------------------------------
# MemoryArchive (v3)
# ---------------------------------------------------------------------------


class TestMemoryArchive:
    def test_happy_path(self):
        ma = MemoryArchive(original_memory_id=99, content="old content")
        assert ma.original_memory_id == 99
        assert ma.content == "old content"
        assert ma.mismatch_score == 0.0
        assert ma.archive_reason == ""

    def test_with_reason(self):
        ma = MemoryArchive(
            original_memory_id=1,
            content="c",
            archive_reason="reconsolidation",
            mismatch_score=0.8,
        )
        assert ma.archive_reason == "reconsolidation"
        assert ma.mismatch_score == 0.8


# ---------------------------------------------------------------------------
# MemoryTransition (v3)
# ---------------------------------------------------------------------------


class TestMemoryTransition:
    def test_happy_path(self):
        mt = MemoryTransition(from_memory_id=1, to_memory_id=2)
        assert mt.from_memory_id == 1
        assert mt.to_memory_id == 2
        assert mt.count == 1
        assert mt.session_id == ""

    def test_with_session(self):
        mt = MemoryTransition(from_memory_id=3, to_memory_id=4, count=5, session_id="sess-xyz")
        assert mt.count == 5
        assert mt.session_id == "sess-xyz"


# ---------------------------------------------------------------------------
# CausalDAGEdge (v3)
# ---------------------------------------------------------------------------


class TestCausalDAGEdge:
    def test_defaults(self):
        edge = CausalDAGEdge(source_entity_id=1, target_entity_id=2)
        assert edge.algorithm == "pc"
        assert edge.confidence == 1.0
        assert edge.is_validated is False

    def test_validated_ges(self):
        edge = CausalDAGEdge(
            source_entity_id=3,
            target_entity_id=4,
            algorithm="ges",
            confidence=0.75,
            is_validated=True,
        )
        assert edge.algorithm == "ges"
        assert edge.is_validated is True


# ---------------------------------------------------------------------------
# Checkpoint (v4)
# ---------------------------------------------------------------------------


class TestCheckpoint:
    def test_defaults(self):
        cp = Checkpoint(directory_context="/work")
        assert cp.session_id == "default"
        assert cp.current_task == ""
        assert cp.files_being_edited == []
        assert cp.key_decisions == []
        assert cp.open_questions == []
        assert cp.next_steps == []
        assert cp.active_errors == []
        assert cp.custom_context == ""
        assert cp.epoch == 0
        assert cp.is_active is True

    def test_full_checkpoint(self):
        cp = Checkpoint(
            session_id="sess-abc",
            directory_context="/project",
            current_task="implement feature X",
            files_being_edited=["main.py", "tests/test_main.py"],
            key_decisions=["use pydantic v2"],
            open_questions=["how to handle auth?"],
            next_steps=["write tests", "update docs"],
            active_errors=["TypeError on line 42"],
            custom_context="some context",
            epoch=3,
        )
        assert cp.session_id == "sess-abc"
        assert cp.current_task == "implement feature X"
        assert len(cp.files_being_edited) == 2
        assert cp.epoch == 3
        assert cp.is_active is True


# ---------------------------------------------------------------------------
# ADR (Architecture Decision Record) — v5 model
# ---------------------------------------------------------------------------

_VALID_ADR_FIELDS = dict(
    title="Use SurrealDB for persistent storage",
    status="accepted",
    date="2026-06-25",
    context="We need durable key-value + graph storage.",
    decision="Adopt SurrealDB embedded as the single backend.",
    rationale="Supports relational and graph queries; no separate process.",
    alternatives="SQLite (no graph), PostgreSQL (separate process).",
    consequences="Adds ~30MB to binary; migration required.",
    revisit_trigger="If SurrealDB p95 exceeds 500ms.",
    supersedes="none",
)


class TestADR:
    def test_construction_valid(self):
        adr = ADR(**_VALID_ADR_FIELDS)
        assert adr.title == "Use SurrealDB for persistent storage"
        assert adr.status == "accepted"
        assert adr.date == "2026-06-25"
        assert adr.supersedes == "none"

    def test_adr_id_defaults_none(self):
        adr = ADR(**_VALID_ADR_FIELDS)
        assert adr.adr_id is None

    def test_adr_id_can_be_set(self):
        adr = ADR(adr_id="ADR-0003", **_VALID_ADR_FIELDS)
        assert adr.adr_id == "ADR-0003"

    def test_all_ten_content_fields_present(self):
        adr = ADR(**_VALID_ADR_FIELDS)
        for field in (
            "title",
            "status",
            "date",
            "context",
            "decision",
            "rationale",
            "alternatives",
            "consequences",
            "revisit_trigger",
            "supersedes",
        ):
            assert hasattr(adr, field), f"ADR model missing field: {field!r}"

    def test_to_body_dict_round_trip(self):
        """to_body_dict() returns keys matching the flat-bullet rendering in _build_adr_body."""
        adr = ADR(**_VALID_ADR_FIELDS)
        d = adr.to_body_dict()
        assert isinstance(d, dict)
        # Must contain exactly the 9 body fields (excludes title — used as heading)
        expected_keys = {
            "status",
            "date",
            "context",
            "decision",
            "rationale",
            "alternatives",
            "consequences",
            "revisit_trigger",
            "supersedes",
        }
        assert set(d.keys()) == expected_keys, (
            f"to_body_dict() keys mismatch. Expected {expected_keys}, got {set(d.keys())}"
        )
        assert d["status"] == "accepted"
        assert d["supersedes"] == "none"

    def test_to_body_dict_order_matches_rendering(self):
        """to_body_dict() preserves field order matching _build_adr_body bullet sequence."""
        adr = ADR(**_VALID_ADR_FIELDS)
        d = adr.to_body_dict()
        keys = list(d.keys())
        expected_order = [
            "status",
            "date",
            "context",
            "decision",
            "rationale",
            "alternatives",
            "consequences",
            "revisit_trigger",
            "supersedes",
        ]
        assert keys == expected_order, (
            f"to_body_dict() field order wrong. Expected {expected_order}, got {keys}"
        )

    def test_to_markdown_body_format(self):
        """to_markdown_body() produces the exact flat-bullet format adr_add uses."""
        adr = ADR(**_VALID_ADR_FIELDS)
        body = adr.to_markdown_body()
        assert "- status: accepted\n" in body
        assert "- date: 2026-06-25\n" in body
        assert "- supersedes: none\n" in body
        # Must NOT use sub-headings or bold bullets
        assert "### " not in body
        assert "- **" not in body

    def test_no_directory_field(self):
        """directory is NOT a field on ADR — it's a routing arg, not part of the record."""
        adr = ADR(**_VALID_ADR_FIELDS)
        assert not hasattr(adr, "directory")
