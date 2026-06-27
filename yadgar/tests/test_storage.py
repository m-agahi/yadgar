import pytest

from yadgar.storage import _FTS_STOP_WORDS, StorageEngine


@pytest.fixture
def storage(tmp_path):
    db_path = str(tmp_path / "test_memory.db")
    engine = StorageEngine(db_path)
    yield engine
    engine.close()


def _make_memory(content="test memory", directory="/tmp/project", **kwargs):
    base = {
        "content": content,
        "directory_context": directory,
        "tags": ["test"],
    }
    base.update(kwargs)
    return base


class TestSchemaCreation:
    def test_tables_usable(self, storage):
        """Verify that all core tables are usable by inserting and querying data."""
        # memory table
        mid = storage.insert_memory(_make_memory(content="schema check"))
        assert storage.get_memory(mid) is not None

        # entity table
        eid = storage.insert_entity({"name": "schema_entity", "type": "file"})
        assert storage.get_entity_by_name("schema_entity") is not None

        # relationship table
        eid2 = storage.insert_entity({"name": "schema_entity2", "type": "file"})
        rid = storage.insert_relationship(
            {
                "source_entity_id": eid,
                "target_entity_id": eid2,
                "relationship_type": "imports",
            }
        )
        assert rid is not None

        # episode table
        epid = storage.insert_episode(
            {
                "session_id": "sess-schema",
                "directory": "/tmp",
                "raw_content": "schema test",
            }
        )
        assert epid is not None

        # consolidation_log table
        cid = storage.insert_consolidation_log({"memories_added": 1})
        assert cid is not None

        # memory_cluster table
        clid = storage.insert_cluster({"name": "schema-cluster", "level": 0})
        assert storage.get_cluster(clid) is not None

        # prospective_memory table
        pmid = storage.insert_prospective_memory(
            {
                "content": "schema check pm",
                "trigger_condition": "test",
                "trigger_type": "keyword_match",
            }
        )
        assert pmid is not None

        # narrative_entry table
        nid = storage.insert_narrative_entry(
            {
                "directory_context": "/tmp",
                "summary": "schema narrative",
                "period_start": "2026-01-01T00:00:00",
                "period_end": "2026-01-01T23:59:59",
            }
        )
        assert nid is not None

        # astrocyte_process table
        aid = storage.insert_astrocyte_process(
            {
                "name": "schema-proc",
                "domain": "test",
            }
        )
        assert aid is not None

    def test_schema_is_idempotent(self, tmp_path):
        """Opening StorageEngine twice on the same path should not raise."""
        db_path = str(tmp_path / "idempotent.db")
        e1 = StorageEngine(db_path)
        e1.close()
        e2 = StorageEngine(db_path)
        e2.close()


class TestMemoryCRUD:
    def test_insert_and_get_memory(self, storage):
        mem = _make_memory(content="pytest is great", tags=["python", "testing"])
        mem_id = storage.insert_memory(mem)
        assert mem_id is not None

        retrieved = storage.get_memory(mem_id)
        assert retrieved is not None
        assert retrieved["content"] == "pytest is great"
        assert retrieved["directory_context"] == "/tmp/project"
        assert retrieved["tags"] == ["python", "testing"]
        assert retrieved["heat"] == 1.0
        assert retrieved["is_stale"] is False

    def test_get_nonexistent_memory(self, storage):
        assert storage.get_memory(9999) is None

    def test_delete_memory(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        storage.delete_memory(mem_id)
        assert storage.get_memory(mem_id) is None

    def test_delete_memory_removes_similarity_links(self, storage):
        a = storage.insert_memory(_make_memory(content="link source"))
        b = storage.insert_memory(_make_memory(content="link target"))
        c = storage.insert_memory(_make_memory(content="unrelated"))
        storage.insert_memory_similarity_link(a, b, 0.9)
        storage.insert_memory_similarity_link(b, c, 0.85)
        assert len(storage.get_all_memory_similarity_links()) == 2

        storage.delete_memory(b)

        # Both links touched b → both gone, no dangling rows left behind.
        assert storage.get_all_memory_similarity_links() == []

    def test_delete_memory_removes_memory_entity_and_relationships(self, storage):
        """delete_memory must clean up memory:<id> entity rows and their relationships."""
        mid = storage.insert_memory(_make_memory(content="entity cleanup test"))

        # Simulate the entity row that curation/cls_store/sleep_compute create
        ent_name = f"memory:{mid}"
        eid = storage.insert_entity({"name": ent_name, "type": "memory"})
        assert storage.get_entity_by_name(ent_name) is not None

        # Create a second entity and a relationship referencing the memory entity
        other_eid = storage.insert_entity({"name": "other-entity", "type": "concept"})
        storage.insert_relationship(
            {
                "source_entity_id": eid,
                "target_entity_id": other_eid,
                "relationship_type": "related_to",
            }
        )

        storage.delete_memory(mid)

        # Entity row must be gone
        assert storage.get_entity_by_name(ent_name) is None
        # Relationship rows referencing the deleted entity must be gone
        remaining = storage.get_relationships_for_entity(eid)
        assert remaining == []


class TestFTSSearch:
    def test_fts_search(self, storage):
        storage.insert_memory(_make_memory(content="fastapi server configuration"))
        storage.insert_memory(_make_memory(content="database migration script"))
        storage.insert_memory(_make_memory(content="react component rendering"))

        results = storage.search_memories_fts("fastapi")
        assert len(results) == 1
        assert results[0]["content"] == "fastapi server configuration"

    def test_fts_search_no_results(self, storage):
        storage.insert_memory(_make_memory(content="python coding"))
        results = storage.search_memories_fts("javascript")
        assert len(results) == 0

    def test_fts_respects_min_heat(self, storage):
        storage.insert_memory(_make_memory(content="hot memory about fastapi", heat=0.8))
        storage.insert_memory(_make_memory(content="cold memory about fastapi", heat=0.01))

        results = storage.search_memories_fts("fastapi", min_heat=0.1)
        assert len(results) == 1
        assert results[0]["heat"] == 0.8


class TestFTSPreprocessing:
    def test_fts_finds_individual_words(self, storage):
        """SurrealDB FTS tokenizes and lowercases; individual words are searchable."""
        storage.insert_memory(_make_memory(content="DatabaseConnection pool timeout"))
        # Individual lowercased words match
        results = storage.search_memories_fts("pool")
        assert len(results) == 1
        assert results[0]["content"] == "DatabaseConnection pool timeout"

        results2 = storage.search_memories_fts("pool timeout")
        assert len(results2) == 1

    def test_fts_snake_case_words(self, storage):
        """Underscored identifiers: SurrealDB treats them as one token."""
        storage.insert_memory(_make_memory(content="auth middleware handles tokens"))
        # Individual words are searchable
        results2 = storage.search_memories_fts("auth")
        assert len(results2) == 1
        results3 = storage.search_memories_fts("middleware")
        assert len(results3) == 1

    def test_fts_stop_words_expanded(self, storage):
        # Coding-domain stop words should be in the set
        coding_stops = {"use", "using", "used", "just", "get", "code", "file", "thing"}
        assert coding_stops.issubset(_FTS_STOP_WORDS)

        # A query of only stop words should fall back to original query
        storage.insert_memory(_make_memory(content="just use the code"))
        result = storage.search_memories_fts("just use the code")
        assert len(result) == 1


class TestMemoryHeatFiltering:
    def test_get_memories_by_heat(self, storage):
        storage.insert_memory(_make_memory(content="hot", heat=0.9))
        storage.insert_memory(_make_memory(content="warm", heat=0.5))
        storage.insert_memory(_make_memory(content="cold", heat=0.02))

        hot = storage.get_memories_by_heat(min_heat=0.7)
        assert len(hot) == 1
        assert hot[0]["content"] == "hot"

        warm_plus = storage.get_memories_by_heat(min_heat=0.4)
        assert len(warm_plus) == 2

    def test_update_memory_heat(self, storage):
        mem_id = storage.insert_memory(_make_memory(heat=1.0))
        storage.update_memory_heat(mem_id, 0.3)
        updated = storage.get_memory(mem_id)
        assert updated["heat"] == 0.3

    def test_update_memory_staleness(self, storage):
        mem_id = storage.insert_memory(_make_memory())
        assert storage.get_memory(mem_id)["is_stale"] is False

        storage.update_memory_staleness(mem_id, True)
        assert storage.get_memory(mem_id)["is_stale"] is True

    def test_get_stale_memories(self, storage):
        storage.insert_memory(_make_memory(content="fresh"))
        stale_id = storage.insert_memory(_make_memory(content="stale"))
        storage.update_memory_staleness(stale_id, True)

        stale = storage.get_stale_memories()
        assert len(stale) == 1
        assert stale[0]["content"] == "stale"


class TestEntities:
    def test_insert_and_get_entity(self, storage):
        entity_id = storage.insert_entity(
            {
                "name": "storage.py",
                "type": "file",
            }
        )
        assert entity_id is not None

        retrieved = storage.get_entity_by_name("storage.py")
        assert retrieved is not None
        assert retrieved["name"] == "storage.py"
        assert retrieved["type"] == "file"
        assert retrieved["heat"] == 1.0
        assert retrieved["archived"] is False

    def test_get_nonexistent_entity(self, storage):
        assert storage.get_entity_by_name("nonexistent") is None

    def test_archive_entity(self, storage):
        entity_id = storage.insert_entity({"name": "old_func", "type": "function"})
        storage.archive_entity(entity_id)
        entity = storage.get_entity_by_name("old_func")
        assert entity["archived"] is True

    def test_get_all_entities_excludes_archived(self, storage):
        storage.insert_entity({"name": "active", "type": "file"})
        archived_id = storage.insert_entity({"name": "archived", "type": "file"})
        storage.archive_entity(archived_id)

        active = storage.get_all_entities()
        assert len(active) == 1
        assert active[0]["name"] == "active"

        all_entities = storage.get_all_entities(include_archived=True)
        assert len(all_entities) == 2

    def test_update_entity_heat(self, storage):
        entity_id = storage.insert_entity({"name": "func", "type": "function"})
        storage.update_entity_heat(entity_id, 0.5)
        entity = storage.get_entity_by_name("func")
        assert entity["heat"] == 0.5


class TestFileHashOperations:
    def test_upsert_and_get_file_hash(self, storage):
        storage.upsert_file_hash("/path/to/file.py", "abc123")
        assert storage.get_file_hash("/path/to/file.py") == "abc123"

    def test_upsert_updates_existing(self, storage):
        storage.upsert_file_hash("/path/to/file.py", "abc123")
        storage.upsert_file_hash("/path/to/file.py", "def456")
        assert storage.get_file_hash("/path/to/file.py") == "def456"

    def test_get_nonexistent_hash(self, storage):
        assert storage.get_file_hash("/no/such/file") is None

    def test_get_memories_by_file_hash(self, storage):
        storage.insert_memory(_make_memory(content="linked", file_hash="hash1"))
        storage.insert_memory(_make_memory(content="unlinked", file_hash="hash2"))

        results = storage.get_memories_by_file_hash("hash1")
        assert len(results) == 1
        assert results[0]["content"] == "linked"


class TestMemoryStats:
    def test_empty_stats(self, storage):
        stats = storage.get_memory_stats()
        assert stats["total_memories"] == 0
        assert stats["active_count"] == 0
        assert stats["archived_count"] == 0
        assert stats["stale_count"] == 0
        assert stats["avg_heat"] == 0.0
        assert stats["last_consolidation"] is None

    def test_stats_with_data(self, storage):
        storage.insert_memory(_make_memory(content="active hot", heat=0.8))
        storage.insert_memory(_make_memory(content="active warm", heat=0.5))
        storage.insert_memory(_make_memory(content="cold", heat=0.01))
        stale_id = storage.insert_memory(_make_memory(content="stale", heat=0.6))
        storage.update_memory_staleness(stale_id, True)

        storage.insert_consolidation_log(
            {
                "memories_added": 4,
                "duration_ms": 120,
            }
        )

        stats = storage.get_memory_stats()
        assert stats["total_memories"] == 4
        assert stats["stale_count"] == 1
        assert stats["archived_count"] == 1  # cold < 0.05
        assert stats["last_consolidation"] is not None


class TestDirectoryMemories:
    def test_get_memories_for_directory(self, storage):
        storage.insert_memory(_make_memory(content="proj a", directory="/proj/a"))
        storage.insert_memory(_make_memory(content="proj b", directory="/proj/b"))

        results = storage.get_memories_for_directory("/proj/a")
        assert len(results) == 1
        assert results[0]["content"] == "proj a"


class TestRelationships:
    def test_insert_relationship(self, storage):
        src = storage.insert_entity({"name": "main.py", "type": "file"})
        tgt = storage.insert_entity({"name": "utils.py", "type": "file"})
        rel_id = storage.insert_relationship(
            {
                "source_entity_id": src,
                "target_entity_id": tgt,
                "relationship_type": "imports",
            }
        )
        assert rel_id is not None

    def test_get_relationship_between(self, storage):
        src = storage.insert_entity({"name": "a.py", "type": "file"})
        tgt = storage.insert_entity({"name": "b.py", "type": "file"})
        storage.insert_relationship(
            {
                "source_entity_id": src,
                "target_entity_id": tgt,
                "relationship_type": "calls",
            }
        )
        rel = storage.get_relationship_between(src, tgt)
        assert rel is not None
        assert rel["relationship_type"] == "calls"


class TestEpisodes:
    def test_insert_episode(self, storage):
        ep_id = storage.insert_episode(
            {
                "session_id": "sess-001",
                "directory": "/home/user/project",
                "raw_content": "git status\n# output here",
            }
        )
        assert ep_id is not None

    def test_get_session_episodes(self, storage):
        storage.insert_episode(
            {
                "session_id": "sess-abc",
                "directory": "/proj",
                "raw_content": "content 1",
            }
        )
        storage.insert_episode(
            {
                "session_id": "sess-abc",
                "directory": "/proj",
                "raw_content": "content 2",
            }
        )
        storage.insert_episode(
            {
                "session_id": "sess-other",
                "directory": "/proj",
                "raw_content": "other",
            }
        )
        eps = storage.get_session_episodes("sess-abc")
        assert len(eps) == 2


class TestConsolidationLog:
    def test_insert_consolidation_log(self, storage):
        log_id = storage.insert_consolidation_log(
            {
                "memories_added": 10,
                "memories_updated": 3,
                "memories_archived": 2,
                "memories_deleted": 1,
                "duration_ms": 500,
            }
        )
        assert log_id is not None


class TestMemoryClusters:
    def test_insert_and_get_cluster(self, storage):
        cid = storage.insert_cluster(
            {
                "name": "python-debugging",
                "level": 0,
                "summary": "Memories about debugging Python code",
            }
        )
        assert cid is not None
        cluster = storage.get_cluster(cid)
        assert cluster["name"] == "python-debugging"
        assert cluster["level"] == 0
        assert cluster["summary"] == "Memories about debugging Python code"
        assert cluster["member_count"] == 0
        assert cluster["heat"] == 1.0

    def test_get_nonexistent_cluster(self, storage):
        assert storage.get_cluster(9999) is None

    def test_get_clusters_by_level(self, storage):
        storage.insert_cluster({"name": "leaf-1", "level": 0})
        storage.insert_cluster({"name": "leaf-2", "level": 0})
        storage.insert_cluster({"name": "root-1", "level": 2})

        leaves = storage.get_clusters_by_level(0)
        assert len(leaves) == 2
        roots = storage.get_clusters_by_level(2)
        assert len(roots) == 1
        assert roots[0]["name"] == "root-1"

    def test_update_cluster(self, storage):
        cid = storage.insert_cluster({"name": "old-name", "summary": "old"})
        storage.update_cluster(cid, {"name": "new-name", "member_count": 5})
        cluster = storage.get_cluster(cid)
        assert cluster["name"] == "new-name"
        assert cluster["member_count"] == 5

    def test_update_cluster_ignores_invalid_fields(self, storage):
        cid = storage.insert_cluster({"name": "test"})
        storage.update_cluster(cid, {"nonexistent_field": "nope"})
        cluster = storage.get_cluster(cid)
        assert cluster["name"] == "test"


class TestProspectiveMemories:
    def test_insert_and_get_active(self, storage):
        pm_id = storage.insert_prospective_memory(
            {
                "content": "Remind about testing",
                "trigger_condition": "pytest",
                "trigger_type": "keyword_match",
                "target_directory": "/home/user/project",
            }
        )
        assert pm_id is not None

        active = storage.get_active_prospective_memories()
        assert len(active) == 1
        assert active[0]["content"] == "Remind about testing"
        assert active[0]["trigger_type"] == "keyword_match"
        assert active[0]["is_active"] is True
        assert active[0]["triggered_count"] == 0

    def test_trigger_prospective_memory(self, storage):
        pm_id = storage.insert_prospective_memory(
            {
                "content": "Check docs",
                "trigger_condition": "docs/",
                "trigger_type": "directory_match",
            }
        )
        storage.trigger_prospective_memory(pm_id)

        active = storage.get_active_prospective_memories()
        assert active[0]["triggered_count"] == 1
        assert active[0]["triggered_at"] is not None

    def test_trigger_increments_count(self, storage):
        pm_id = storage.insert_prospective_memory(
            {
                "content": "Check docs",
                "trigger_condition": "docs/",
                "trigger_type": "directory_match",
            }
        )
        storage.trigger_prospective_memory(pm_id)
        storage.trigger_prospective_memory(pm_id)

        active = storage.get_active_prospective_memories()
        assert active[0]["triggered_count"] == 2

    def test_inactive_not_returned(self, storage):
        storage.insert_prospective_memory(
            {
                "content": "inactive",
                "trigger_condition": "x",
                "trigger_type": "keyword_match",
                "is_active": False,
            }
        )
        assert len(storage.get_active_prospective_memories()) == 0


class TestNarrativeEntries:
    def test_insert_and_get_narratives(self, storage):
        nid = storage.insert_narrative_entry(
            {
                "directory_context": "/home/user/project",
                "summary": "Set up project structure and CI",
                "period_start": "2026-03-01T00:00:00",
                "period_end": "2026-03-01T23:59:59",
                "key_decisions": ["Use FastAPI", "SurrealDB WAL"],
                "key_events": ["Init repo", "First test pass"],
            }
        )
        assert nid is not None

        entries = storage.get_narratives_for_directory("/home/user/project")
        assert len(entries) == 1
        assert entries[0]["summary"] == "Set up project structure and CI"
        assert entries[0]["key_decisions"] == ["Use FastAPI", "SurrealDB WAL"]
        assert entries[0]["key_events"] == ["Init repo", "First test pass"]
        assert entries[0]["heat"] == 1.0

    def test_narratives_filtered_by_directory(self, storage):
        storage.insert_narrative_entry(
            {
                "directory_context": "/proj/a",
                "summary": "A stuff",
                "period_start": "2026-03-01T00:00:00",
                "period_end": "2026-03-01T23:59:59",
            }
        )
        storage.insert_narrative_entry(
            {
                "directory_context": "/proj/b",
                "summary": "B stuff",
                "period_start": "2026-03-01T00:00:00",
                "period_end": "2026-03-01T23:59:59",
            }
        )
        results = storage.get_narratives_for_directory("/proj/a")
        assert len(results) == 1
        assert results[0]["summary"] == "A stuff"


class TestAstrocyteProcesses:
    def test_insert_and_get_processes(self, storage):
        pid = storage.insert_astrocyte_process(
            {
                "name": "consolidator",
                "domain": "memory-management",
                "specialization": "heat decay",
                "memory_ids": [1, 2, 3],
                "entity_ids": [10, 20],
            }
        )
        assert pid is not None

        procs = storage.get_astrocyte_processes()
        assert len(procs) == 1
        assert procs[0]["name"] == "consolidator"
        assert procs[0]["domain"] == "memory-management"
        assert procs[0]["memory_ids"] == [1, 2, 3]
        assert procs[0]["entity_ids"] == [10, 20]
        assert procs[0]["heat"] == 1.0

    def test_update_astrocyte_process(self, storage):
        pid = storage.insert_astrocyte_process(
            {
                "name": "proc1",
                "domain": "test",
            }
        )
        storage.update_astrocyte_process(
            pid,
            {
                "heat": 0.5,
                "memory_ids": [4, 5],
                "specialization": "clustering",
            },
        )
        procs = storage.get_astrocyte_processes()
        assert procs[0]["heat"] == 0.5
        assert procs[0]["memory_ids"] == [4, 5]
        assert procs[0]["specialization"] == "clustering"

    def test_update_ignores_invalid_fields(self, storage):
        pid = storage.insert_astrocyte_process(
            {
                "name": "proc1",
                "domain": "test",
            }
        )
        storage.update_astrocyte_process(pid, {"bad_field": "nope"})
        procs = storage.get_astrocyte_processes()
        assert procs[0]["name"] == "proc1"


class TestContextManager:
    def test_context_manager(self, tmp_path):
        db_path = str(tmp_path / "ctx_test.db")
        with StorageEngine(db_path) as engine:
            engine.insert_memory(_make_memory())


class TestMemoryRules:
    def test_insert_and_get_rule(self, storage):
        rid = storage.insert_rule(
            {
                "rule_type": "filter",
                "scope": "global",
                "condition": "heat < 0.1",
                "action": "archive",
            }
        )
        assert rid is not None

        rules = storage.get_rules_for_scope("global")
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "filter"

    def test_delete_rule(self, storage):
        rid = storage.insert_rule(
            {
                "rule_type": "filter",
                "scope": "global",
                "condition": "heat < 0.1",
                "action": "archive",
            }
        )
        storage.delete_rule(rid)
        rules = storage.get_rules_for_scope("global")
        assert len(rules) == 0


class TestMemoryTransitions:
    def test_insert_and_get_transition(self, storage):
        mid1 = storage.insert_memory(_make_memory(content="mem1"))
        mid2 = storage.insert_memory(_make_memory(content="mem2"))

        tid = storage.insert_transition(
            {
                "from_memory_id": mid1,
                "to_memory_id": mid2,
                "count": 1,
                "session_id": "sess-test",
            }
        )
        assert tid is not None

        trans = storage.get_transition(mid1, mid2)
        assert trans is not None
        assert trans["count"] == 1

    def test_get_transitions_from(self, storage):
        mid1 = storage.insert_memory(_make_memory(content="source"))
        mid2 = storage.insert_memory(_make_memory(content="target1"))
        mid3 = storage.insert_memory(_make_memory(content="target2"))

        storage.insert_transition({"from_memory_id": mid1, "to_memory_id": mid2, "session_id": "s"})
        storage.insert_transition({"from_memory_id": mid1, "to_memory_id": mid3, "session_id": "s"})

        transitions = storage.get_transitions_from(mid1)
        assert len(transitions) == 2


class TestMemoryScores:
    def test_update_memory_scores(self, storage):
        mid = storage.insert_memory(_make_memory())
        storage.update_memory_scores(mid, surprise_score=0.7, importance=0.8, emotional_valence=0.3)
        mem = storage.get_memory(mid)
        assert mem["surprise_score"] == 0.7
        assert mem["importance"] == 0.8
        assert mem["emotional_valence"] == 0.3


class TestEpisodePrune:
    """prune_old_episodes removes stale episode rows."""

    def _insert_ep(self, storage, timestamp: str, content: str = "test") -> int:
        return storage.insert_episode(
            {
                "session_id": "s1",
                "directory": "/tmp",
                "raw_content": content,
                "timestamp": timestamp,
            }
        )

    def test_prune_removes_old_episodes(self, storage):
        from datetime import UTC, datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        new_ts = datetime.now(UTC).isoformat()

        old_ep = self._insert_ep(storage, old_ts)
        new_ep = self._insert_ep(storage, new_ts)

        pruned = storage.prune_old_episodes(older_than_days=14)
        assert pruned >= 1

        # Old row gone
        old_rows = storage._q(f"SELECT id FROM episode:{old_ep}")
        assert old_rows == []

        # Recent row stays
        new_rows = storage._q(f"SELECT id FROM episode:{new_ep}")
        assert len(new_rows) == 1

    def test_get_recent_episodes_capped(self, storage):
        """get_recent_episodes returns at most limit rows in ascending order."""
        from datetime import UTC, datetime, timedelta

        for i in range(10):
            ts = (datetime.now(UTC) - timedelta(hours=10 - i)).isoformat()
            storage.insert_episode(
                {"session_id": "s", "directory": "/tmp", "raw_content": f"ep {i}", "timestamp": ts}
            )

        recent = storage.get_recent_episodes(limit=5)
        assert len(recent) == 5
        # Ascending timestamp order
        timestamps = [ep["timestamp"] for ep in recent]
        assert timestamps == sorted(timestamps)


class TestBatchWritesChunking:
    """batch_writes must not build a single unbounded SQL string.

    With MAX_BATCH_STATEMENTS=500 the implementation splits large batches
    into multiple transactions, each capped at that size.  All statements
    must still land.
    """

    def test_large_batch_all_land(self, storage):
        """Write 750 memories via batch_writes (>500); all must be readable."""
        import os

        if not os.environ.get("YADGAR_DB_URL"):
            pytest.skip("batch_writes requires server mode")

        # Pre-insert memories so we have valid IDs to UPDATE
        ids = [storage.insert_memory(_make_memory(content=f"batch-{i}")) for i in range(10)]

        # Build 750 UPDATE statements (heat updates) — exceeds the 500-statement cap
        stmts = [
            (
                "UPDATE type::record('memory', $id) SET tags = $tags",
                {"id": ids[i % 10], "tags": [f"batch-tag-{i}"]},
            )
            for i in range(750)
        ]
        # Must not raise (previously a single SQL blob could crash SurrealDB's serialiser)
        storage.batch_writes(stmts)

        # Spot-check: last update should have persisted
        mem = storage.get_memory(ids[9])
        assert mem is not None

    def test_empty_batch_is_noop(self, storage):
        storage.batch_writes([])  # must not raise


class TestBatchWritesByteChunking:
    """batch_writes must also split by serialised body size (MAX_BATCH_BYTES).

    Tests in this class use a monkeypatched _http mock so they run without a
    live SurrealDB server.  A "smart" mock rejects bodies > 1.2 * MAX_BATCH_BYTES
    with HTTPStatusError(413) to simulate the real failure condition.
    """

    def _make_storage_with_mock_http(self, monkeypatch):
        """Return a StorageEngine whose _http is replaced by a MagicMock."""
        from unittest.mock import MagicMock

        engine = StorageEngine.__new__(StorageEngine)
        # Minimal init — enough for batch_writes to work
        engine._db_url = "http://fake-surreal:8000"
        engine._embedding_dim = 384
        engine._db_path = ":memory:"

        mock_http = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status.return_value = None
        mock_http.post.return_value = mock_response
        engine._http = mock_http
        return engine, mock_http

    def test_batch_writes_chunks_by_bytes(self, monkeypatch):
        """100 statements each ~100 KB → ≥10 separate HTTP requests at 1 MB cap."""
        from yadgar.config import Settings

        monkeypatch.setattr(
            "yadgar.config.get_settings",
            lambda: Settings(MAX_BATCH_STATEMENTS=500, MAX_BATCH_BYTES=1_000_000),
        )

        engine, mock_http = self._make_storage_with_mock_http(monkeypatch)

        # Each statement has a ~100 KB content param
        big_content = "x" * 100_000  # 100 000 bytes in UTF-8
        stmts = [
            (
                "UPDATE type::record('memory', $id) SET content = $content",
                {"id": i, "content": big_content},
            )
            for i in range(100)
        ]
        engine.batch_writes(stmts)

        # 100 * 100 KB = 10 MB → expect at least 10 chunks at 1 MB cap
        assert mock_http.post.call_count >= 10

    def test_batch_writes_chunks_by_count(self, monkeypatch):
        """1500 tiny statements → ≥3 HTTP requests at MAX_BATCH_STATEMENTS=500."""
        from yadgar.config import Settings

        monkeypatch.setattr(
            "yadgar.config.get_settings",
            lambda: Settings(MAX_BATCH_STATEMENTS=500, MAX_BATCH_BYTES=1_000_000),
        )

        engine, mock_http = self._make_storage_with_mock_http(monkeypatch)

        stmts = [
            ("UPDATE type::record('memory', $id) SET heat = 0.5", {"id": i}) for i in range(1500)
        ]
        engine.batch_writes(stmts)

        assert mock_http.post.call_count >= 3

    def test_oversized_single_statement_is_attempted_alone(self, monkeypatch):
        """A single statement whose own size exceeds MAX_BATCH_BYTES is still attempted
        (not silently dropped) and emits a WARN log."""
        import logging

        from yadgar.config import Settings

        monkeypatch.setattr(
            "yadgar.config.get_settings",
            lambda: Settings(MAX_BATCH_STATEMENTS=500, MAX_BATCH_BYTES=100_000),
        )

        engine, mock_http = self._make_storage_with_mock_http(monkeypatch)

        # 2 MB statement — exceeds MAX_BATCH_BYTES (100 KB in this test)
        huge_content = "y" * 2_000_000
        stmts = [
            (
                "UPDATE type::record('memory', $id) SET content = $content",
                {"id": 1, "content": huge_content},
            )
        ]

        # pytest caplog attaches at root, but yadgar/log_config.py sets
        # `yadgar.propagate = False`, so records from yadgar.storage.* never
        # reach root → caplog stays empty regardless of `logger=` filter
        # (the `logger=` arg adjusts level on a logger but doesn't move the
        # capture handler). Attach our own list-handler at yadgar.storage so
        # propagation goes UP one level and lands in records[] before
        # propagate=False at yadgar stops the chain.
        records: list[logging.LogRecord] = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        list_handler = _ListHandler(level=logging.WARNING)
        target = logging.getLogger("yadgar.storage")
        prior_level = target.level
        target.setLevel(logging.WARNING)
        target.addHandler(list_handler)
        try:
            engine.batch_writes(stmts)
        finally:
            target.removeHandler(list_handler)
            target.setLevel(prior_level)

        # Exactly one HTTP request — not dropped
        assert mock_http.post.call_count == 1
        # A WARN was emitted by yadgar.storage or yadgar.storage.client
        assert any(
            r.levelno >= logging.WARNING and r.name.startswith("yadgar.storage") for r in records
        ), (
            f"no WARN from yadgar.storage* in {[(r.name, r.levelname, r.getMessage()) for r in records]}"
        )

    def test_batch_writes_no_413_when_framing_overhead_exceeds_estimate(self, monkeypatch):
        """batch_writes must not raise 413 when framing overhead (LET params, BEGIN/COMMIT,
        semicolons) makes the real HTTP body larger than _chunk_by_bytes estimated.

        Regression test for the underestimate bug: _chunk_by_bytes measured only
        JSON-serialised param values, ignoring the `LET $p{i}_{k} = ` prefix (~18 chars
        per param), `;\n` separators, and BEGIN/COMMIT TRANSACTION wrappers.  With many
        params per statement the real wire body could be 2-3x the estimate, causing 413.

        Strategy: use a tight limit mock that raises 413 on any body > limit and
        many-param statements where framing overhead is large relative to content.
        With the underestimating chunker, some chunks will exceed the limit → 413 raised.
        With the real-body-measuring fix, chunks are always small enough → no exception.
        """
        import httpx

        from yadgar.config import Settings

        # Tight byte cap: 1 KB.  Each statement has 8 params with ~20-byte values.
        # Per-stmt estimate: sql (~60 B) + 8 × ~22 B values ≈ 236 B.
        # Estimated chunk size: floor(1024/236) = 4 statements per chunk.
        # Actual body for 4 statements (4 × 8 = 32 LETs + BEGIN/COMMIT + separators):
        #   32 × ("LET $p{i}_key{k} = \"..20..\";\n" ≈ 38 chars each) ≈ 1216 B
        #   + BEGIN TRANSACTION;\n (19 B) + COMMIT TRANSACTION; (20 B)
        #   + 4 × sql ≈ 240 B  → total ≈ 1495 B >> 1024 → 413 under old code.
        limit = 1024

        monkeypatch.setattr(
            "yadgar.config.get_settings",
            lambda: Settings(MAX_BATCH_STATEMENTS=500, MAX_BATCH_BYTES=limit),
        )

        engine, _ = self._make_storage_with_mock_http(monkeypatch)

        post_calls: list[bytes] = []

        def smart_post(path, *, content, headers=None, **kwargs):
            post_calls.append(content)
            if len(content) > limit:
                request = httpx.Request("POST", "http://fake-surreal:8000/sql")
                response = httpx.Response(413, request=request)
                raise httpx.HTTPStatusError(
                    f"413: body={len(content)} > {limit}",
                    request=request,
                    response=response,
                )
            from unittest.mock import MagicMock

            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status.return_value = None
            return mock_resp

        engine._http.post.side_effect = smart_post

        # 20 statements, each with 8 params carrying ~20-byte content.
        # Framing overhead per statement: 8 × ~38 B (LET prefix) ≈ 304 B.
        # That alone exceeds the estimate for grouped chunks.
        val = "x" * 20
        stmts = [
            (
                "UPDATE type::record('memory', $id) SET "
                "f0=$f0, f1=$f1, f2=$f2, f3=$f3, f4=$f4, f5=$f5, f6=$f6, f7=$f7",
                {
                    "id": i,
                    "f0": val,
                    "f1": val,
                    "f2": val,
                    "f3": val,
                    "f4": val,
                    "f5": val,
                    "f6": val,
                    "f7": val,
                },
            )
            for i in range(20)
        ]

        # Must not raise — the fix measures real body and splits until each chunk fits.
        engine.batch_writes(stmts)

        # Sanity: multiple requests were made (chunking happened)
        assert len(post_calls) >= 2
        # Every request must fit within the limit
        for body in post_calls:
            assert len(body) <= limit, (
                f"A request body ({len(body)} B) exceeded the limit ({limit} B)"
            )


class TestActionLogPrune:
    """prune_processed_action_log removes old processed rows."""

    def _insert_action(self, storage, timestamp: str) -> int:
        aid = storage._next_id("action_log")
        storage._q(
            "CREATE type::record('action_log', $id) SET "
            "tool_name = $t, tool_input_summary = $s, "
            "directory = $d, session_id = $sid, "
            "timestamp = $ts, processed = true",
            {
                "id": aid,
                "t": "Bash",
                "s": "ls",
                "d": "/tmp",
                "sid": "s1",
                "ts": timestamp,
            },
        )
        return aid

    def test_prune_removes_old_processed_rows(self, storage):
        from datetime import UTC, datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        new_ts = datetime.now(UTC).isoformat()

        old_id = self._insert_action(storage, old_ts)
        new_id = self._insert_action(storage, new_ts)

        # Prune rows older than 7 days
        storage.prune_processed_action_log(older_than_days=7)

        # Old processed row gone
        old_rows = storage._q(f"SELECT id FROM action_log:{old_id}")
        assert old_rows == []

        # Recent row still present
        new_rows = storage._q(f"SELECT id FROM action_log:{new_id}")
        assert len(new_rows) == 1

    def test_prune_skips_unprocessed_rows(self, storage):
        """Unprocessed rows must not be pruned regardless of age."""
        from datetime import UTC, datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        aid = storage._next_id("action_log")
        storage._q(
            "CREATE type::record('action_log', $id) SET "
            "tool_name = $t, tool_input_summary = $s, "
            "directory = $d, session_id = $sid, "
            "timestamp = $ts, processed = false",
            {"id": aid, "t": "Bash", "s": "ls", "d": "/tmp", "sid": "s1", "ts": old_ts},
        )

        storage.prune_processed_action_log(older_than_days=7)

        rows = storage._q(f"SELECT id FROM action_log:{aid}")
        assert len(rows) == 1


# ── prune_old_rows generic helper ────────────────────────────────────────────


class TestPruneOldRows:
    """prune_old_rows should delete old rows and return the count."""

    def test_prune_narrative_entry(self, storage):
        """Old narrative entries are pruned; recent ones are kept."""
        from datetime import UTC, datetime, timedelta

        old_date = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        recent_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()

        # Insert one old and one recent narrative entry
        old_id = storage.insert_narrative_entry(
            {
                "directory_context": "/proj",
                "summary": "old narrative",
                "period_start": old_date,
                "period_end": old_date,
            }
        )
        recent_id = storage.insert_narrative_entry(
            {
                "directory_context": "/proj",
                "summary": "recent narrative",
                "period_start": recent_date,
                "period_end": recent_date,
            }
        )
        # Back-date old entry's created_at
        storage._q(
            "UPDATE type::record('narrative_entry', $id) SET created_at = $ts",
            {"id": old_id, "ts": old_date},
        )

        pruned = storage.prune_old_rows("narrative_entry", older_than_days=90)

        assert pruned >= 1
        old_rows = storage._q(f"SELECT id FROM narrative_entry:{old_id}")
        assert old_rows == [], "old narrative_entry should have been deleted"
        recent_rows = storage._q(f"SELECT id FROM narrative_entry:{recent_id}")
        assert len(recent_rows) == 1, "recent narrative_entry must survive"

    def test_prune_prospective_memory_only_inactive(self, storage):
        """prune_old_rows respects extra_where — only fired PMs are deleted."""
        from datetime import UTC, datetime, timedelta

        old_date = (datetime.now(UTC) - timedelta(days=40)).isoformat()

        # Active PM — must survive even if old
        active_id = storage.insert_prospective_memory(
            {
                "content": "check deploy",
                "trigger_condition": "deploy",
                "trigger_type": "keyword_match",
                "is_active": True,
            }
        )
        storage._q(
            "UPDATE type::record('prospective_memory', $id) SET created_at = $ts",
            {"id": active_id, "ts": old_date},
        )

        # Inactive (fired) PM — should be pruned
        inactive_id = storage.insert_prospective_memory(
            {
                "content": "old fired reminder",
                "trigger_condition": "done",
                "trigger_type": "keyword_match",
                "is_active": False,
            }
        )
        storage._q(
            "UPDATE type::record('prospective_memory', $id) SET created_at = $ts, is_active = false",
            {"id": inactive_id, "ts": old_date},
        )

        pruned = storage.prune_old_rows(
            "prospective_memory",
            older_than_days=30,
            extra_where="is_active = false",
        )

        assert pruned >= 1
        inactive_rows = storage._q(f"SELECT id FROM prospective_memory:{inactive_id}")
        assert inactive_rows == [], "fired prospective_memory should have been pruned"
        active_rows = storage._q(f"SELECT id FROM prospective_memory:{active_id}")
        assert len(active_rows) == 1, "active prospective_memory must survive"

    def test_prune_unknown_table_raises(self, storage):
        """prune_old_rows must refuse unknown tables to prevent injection."""
        import pytest

        with pytest.raises(ValueError, match="not in the allowed set"):
            storage.prune_old_rows("memory", older_than_days=7)

    def test_prune_zero_days_is_noop(self, storage):
        """older_than_days=0 disables the prune (returns 0)."""
        storage.insert_narrative_entry(
            {
                "directory_context": "/proj",
                "summary": "test",
                "period_start": "2020-01-01T00:00:00",
                "period_end": "2020-01-01T00:00:00",
            }
        )
        pruned = storage.prune_old_rows("narrative_entry", older_than_days=0)
        assert pruned == 0


class TestConsolidationWatermark:
    """v5.86 (OT-C4): persisted incremental similarity-linking watermarks."""

    def test_watermark_default_none(self, storage):
        assert storage.get_consolidation_watermark("similarity_linking") is None

    def test_watermark_roundtrip(self, storage):
        ts = "2026-06-27T12:00:00+00:00"
        storage.set_consolidation_watermark("similarity_linking", ts)
        assert storage.get_consolidation_watermark("similarity_linking") == ts

    def test_watermark_upsert_in_place(self, storage):
        storage.set_consolidation_watermark("similarity_linking", "2026-06-26T00:00:00+00:00")
        storage.set_consolidation_watermark("similarity_linking", "2026-06-27T00:00:00+00:00")
        assert (
            storage.get_consolidation_watermark("similarity_linking") == "2026-06-27T00:00:00+00:00"
        )

    def test_watermark_keys_independent(self, storage):
        storage.set_consolidation_watermark("similarity_linking", "2026-06-27T00:00:00+00:00")
        storage.set_consolidation_watermark("full_reconcile", "2026-06-20T00:00:00+00:00")
        assert (
            storage.get_consolidation_watermark("similarity_linking") == "2026-06-27T00:00:00+00:00"
        )
        assert storage.get_consolidation_watermark("full_reconcile") == "2026-06-20T00:00:00+00:00"

    def test_watermark_rejects_bad_key(self, storage):
        with pytest.raises(ValueError):
            storage.set_consolidation_watermark("bad key; DROP", "x")


class TestGetMemoriesWithEmbeddingsSince:
    """v5.86 (OT-C4): `since=` filters to memories created on/after a watermark."""

    def test_since_filters_old_memories(self, storage):
        import numpy as np

        vec = np.array([1.0] + [0.0] * 383, dtype=np.float32).tobytes()
        old_id = storage.insert_memory(
            _make_memory(
                content="old", embedding=vec, heat=1.0, created_at="2026-06-01T00:00:00+00:00"
            )
        )
        new_id = storage.insert_memory(
            _make_memory(
                content="new", embedding=vec, heat=1.0, created_at="2026-06-26T00:00:00+00:00"
            )
        )

        all_mems = storage.get_memories_with_embeddings()
        all_ids = {m["id"] for m in all_mems}
        assert {old_id, new_id} <= all_ids

        recent = storage.get_memories_with_embeddings(since="2026-06-20T00:00:00+00:00")
        recent_ids = {m["id"] for m in recent}
        assert new_id in recent_ids
        assert old_id not in recent_ids

    def test_since_none_returns_all(self, storage):
        import numpy as np

        vec = np.array([1.0] + [0.0] * 383, dtype=np.float32).tobytes()
        storage.insert_memory(
            _make_memory(
                content="m1", embedding=vec, heat=1.0, created_at="2026-06-01T00:00:00+00:00"
            )
        )
        assert len(storage.get_memories_with_embeddings(since=None)) == 1
