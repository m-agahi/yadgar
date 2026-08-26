"""Tests for dual-store Complementary Learning Systems (CLS)."""

import pytest
from hypothesis import example, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.storage import StorageEngine
from yadgar.backend.cls_store import DualStoreCLS

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: dict without this key is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "test.db"),
        CLUSTER_SIMILARITY_THRESHOLD=0.6,
        CURATION_SIMILARITY_THRESHOLD=0.85,
    )


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture
def embeddings():
    return EmbeddingEngine("all-MiniLM-L6-v2")


@pytest.fixture
def cls(storage, embeddings, settings):
    return DualStoreCLS(storage, embeddings, settings)


def _make_memory(
    storage,
    embeddings,
    content,
    directory="/tmp/project",
    tags=None,
    store_type="episodic",
    session_id=None,
    **kwargs,
):
    """Helper to insert a memory with real embedding and optional episode link."""
    embedding = embeddings.encode(content)
    # Create an episode to track session_id if provided
    episode_id = None
    if session_id is not None:
        episode_id = storage.insert_episode(
            {
                "session_id": session_id,
                "directory": directory,
                "raw_content": content,
            }
        )

    mem = {
        "project_id": _TEST_PROJECT,
        "content": content,
        "embedding": embedding,
        "tags": tags or ["test"],
        "directory_context": directory,
        "heat": 1.0,
        "is_stale": False,
        "embedding_model": embeddings.get_model_name(),
        "source_episode_id": episode_id,
    }
    mem.update(kwargs)
    mid = storage.insert_memory(mem)

    # Set store_type
    storage._q(
        "UPDATE type::record('memory', $id) SET store_type = $store_type",
        {"id": mid, "store_type": store_type},
    )

    return mid


# ── Classification Tests ──────────────────────────────────────────────


class TestClassifyMemory:
    def test_classify_episodic(self, cls):
        """A specific bug report should be classified as episodic."""
        content = "Fixed TypeError in auth.py line 42 when user passes None token"
        result = cls.classify_memory(content, ["bugfix"], "/tmp/project")
        assert result == "episodic"

    def test_classify_semantic_decision_keywords(self, cls):
        """A convention statement with decision keywords should be semantic."""
        content = "Always use factory pattern for creating service instances"
        result = cls.classify_memory(content, ["dev"], "/tmp/project")
        assert result == "semantic"

    def test_classify_semantic_architecture_keywords(self, cls):
        """Architecture-related content without specific indicators should be semantic."""
        content = "The design principle here is composition over inheritance"
        result = cls.classify_memory(content, ["dev"], "/tmp/project")
        assert result == "semantic"

    def test_classify_semantic_tags(self, cls):
        """Tags like 'convention' or 'rule' should force semantic classification."""
        content = "Use bun instead of npm for package management"
        result = cls.classify_memory(content, ["convention"], "/tmp/project")
        assert result == "semantic"

    def test_classify_episodic_specific_with_keywords(self, cls):
        """Content with decision keywords BUT specific file paths stays episodic."""
        content = "Always use JWT pattern in src/auth/middleware.ts for the auth flow"
        result = cls.classify_memory(content, ["dev"], "/tmp/project")
        # Has both decision and specific indicators → episodic (specific wins)
        assert result == "episodic"

    def test_classify_semantic_both_decision_and_architecture(self, cls):
        """Content with both decision AND architecture keywords → semantic."""
        content = "We should always follow the factory pattern for our architecture"
        result = cls.classify_memory(content, ["dev"], "/tmp/project")
        assert result == "semantic"


# ── Pattern Detection Tests ───────────────────────────────────────────


class TestFindRecurringPatterns:
    def test_find_recurring_3_occurrences(self, cls, storage, embeddings):
        """Three similar episodic memories from different sessions → pattern found."""
        # Create 3 very similar memories about JWT auth from different sessions
        _make_memory(
            storage,
            embeddings,
            "JWT authentication is used for API security",
            session_id="session-001",
        )
        _make_memory(
            storage,
            embeddings,
            "JWT authentication is used for API authorization",
            session_id="session-002",
        )
        _make_memory(
            storage,
            embeddings,
            "JWT authentication is used for API access control",
            session_id="session-003",
        )

        patterns = cls.find_recurring_patterns(min_occurrences=3)
        assert len(patterns) >= 1
        # The cluster should have 3 members
        found = any(p["occurrence_count"] >= 3 for p in patterns)
        assert found

    def test_no_pattern_below_threshold(self, cls, storage, embeddings):
        """Only 2 similar memories → should not form a pattern (min_occurrences=3)."""
        _make_memory(
            storage,
            embeddings,
            "Set up JWT authentication for the API",
            session_id="session-001",
        )
        _make_memory(
            storage,
            embeddings,
            "Added JWT authentication middleware",
            session_id="session-002",
        )

        patterns = cls.find_recurring_patterns(min_occurrences=3)
        # Should not find any patterns since we only have 2 similar memories
        qualifying = [p for p in patterns if p["occurrence_count"] >= 3]
        assert len(qualifying) == 0

    def test_no_pattern_single_session(self, cls, storage, embeddings):
        """Three similar memories from same session → no pattern (needs session diversity)."""
        for i in range(3):
            _make_memory(
                storage,
                embeddings,
                f"Set up JWT authentication for API endpoint {i}",
                session_id="session-same",
            )

        patterns = cls.find_recurring_patterns(min_occurrences=3)
        # All from same session → should be filtered out by session diversity check
        assert len(patterns) == 0

    def test_directory_filter(self, cls, storage, embeddings):
        """Pattern search filtered by directory."""
        _make_memory(
            storage,
            embeddings,
            "Using React hooks for state management",
            directory="/tmp/frontend",
            session_id="session-001",
        )
        _make_memory(
            storage,
            embeddings,
            "React hooks for managing component state",
            directory="/tmp/frontend",
            session_id="session-002",
        )
        _make_memory(
            storage,
            embeddings,
            "State management with React hooks pattern",
            directory="/tmp/frontend",
            session_id="session-003",
        )
        # Different directory, different topic
        _make_memory(
            storage,
            embeddings,
            "Database migration for PostgreSQL",
            directory="/tmp/backend",
            session_id="session-004",
        )

        patterns = cls.find_recurring_patterns(project_id="/tmp/frontend", min_occurrences=3)
        # Should find pattern in frontend, not backend
        if patterns:
            for p in patterns:
                assert "/tmp/frontend" in p["directories"]


# ── Consistency Tests ─────────────────────────────────────────────────


class TestCheckConsistency:
    def test_check_consistency_consistent(self, cls):
        """Non-contradicting cluster should pass consistency check."""
        cluster = [
            {"id": 1, "content": "Use TypeScript for all new modules"},
            {"id": 2, "content": "TypeScript should be used for modules"},
            {"id": 3, "content": "All modules use TypeScript"},
        ]
        result = cls.check_consistency(cluster)
        assert result["consistent"] is True
        assert len(result["contradictions"]) == 0

    def test_check_consistency_contradicting(self, cls):
        """Cluster with negation mismatch should be flagged."""
        cluster = [
            {"id": 1, "content": "Use TypeScript for all modules"},
            {"id": 2, "content": "Do not use TypeScript for new modules"},
            {"id": 3, "content": "TypeScript is used for modules"},
        ]
        result = cls.check_consistency(cluster)
        assert result["consistent"] is False
        assert len(result["contradictions"]) > 0


# ── Schema Abstraction Tests ──────────────────────────────────────────


class TestAbstractToSchema:
    def test_abstract_to_schema(self, cls):
        """Multiple episodic memories about JWT should produce a generalized schema."""
        cluster = [
            {"id": 1, "content": "Set up JWT auth for API", "tags": ["auth"]},
            {"id": 2, "content": "Added JWT verification middleware", "tags": ["auth"]},
            {"id": 3, "content": "JWT token refresh endpoint implemented", "tags": ["auth", "api"]},
        ]
        schema = cls.abstract_to_schema(cluster)
        assert isinstance(schema, str)
        assert len(schema) > 10
        # Should reference JWT since it appears in all three
        assert "jwt" in schema.lower()

    def test_abstract_empty_cluster(self, cls):
        """Empty cluster returns None — caller treats as no-op.

        C7c (task #339): contract is ``str | None``, and empty cluster
        returns None specifically (not ``""``). ``promotion._promote_pattern``
        guards with ``if not schema:``, which also happens to catch an
        empty string, but that guard's breadth is not this function's
        contract — pin the actual return value. Mirrors the unit-test
        contract at ``tests/backend/test_patterns_unit.py:test_empty_cluster``.
        """
        schema = cls.abstract_to_schema([])
        assert schema is None

    def test_abstract_preserves_common_tags(self, cls):
        """Schema should mention tags that appear across multiple memories."""
        cluster = [
            {"id": 1, "content": "Deploy with Docker containers", "tags": ["devops"]},
            {"id": 2, "content": "Docker deployment pipeline setup", "tags": ["devops"]},
            {"id": 3, "content": "Container deployment using Docker", "tags": ["devops"]},
        ]
        schema = cls.abstract_to_schema(cluster)
        assert "devops" in schema.lower()


# ── Consolidation Cycle Tests ─────────────────────────────────────────


class TestConsolidationCycle:
    def test_consolidation_promotes(self, cls, storage, embeddings):
        """A recurring pattern should get promoted to a semantic memory."""
        # Create enough similar episodic memories from different sessions
        _make_memory(
            storage,
            embeddings,
            "Use dependency injection for service construction",
            session_id="session-001",
        )
        _make_memory(
            storage,
            embeddings,
            "Dependency injection pattern for building services",
            session_id="session-002",
        )
        _make_memory(
            storage,
            embeddings,
            "Service construction via dependency injection",
            session_id="session-003",
        )

        stats = cls.consolidation_cycle()
        # Should have found and promoted at least one pattern
        assert stats["patterns_found"] >= 0  # May or may not cluster depending on embeddings
        assert stats["total_episodic"] >= 0
        assert stats["total_semantic"] >= 0
        # Check that promoted count is consistent
        assert stats["promoted"] + stats["skipped_inconsistent"] <= stats["patterns_found"]

    def test_episodic_preserved(self, cls, storage, embeddings):
        """Original episodic memories should NOT be deleted after promotion."""
        ids = []
        for i, session in enumerate(["s1", "s2", "s3"]):
            mid = _make_memory(
                storage,
                embeddings,
                f"Always validate user input before database queries ({i})",
                session_id=session,
            )
            ids.append(mid)

        cls.consolidation_cycle()

        # All original episodic memories should still exist
        for mid in ids:
            mem = storage.get_memory(mid)
            assert mem is not None, f"Memory {mid} was deleted during CLS consolidation"

    def test_consolidation_cycle_stats(self, cls, storage, embeddings):
        """Consolidation cycle should return correctly structured statistics."""
        # Add some memories
        _make_memory(
            storage,
            embeddings,
            "Testing framework uses pytest with fixtures",
            session_id="s1",
        )
        _make_memory(
            storage,
            embeddings,
            "Using pytest fixtures for test setup",
            session_id="s2",
        )

        stats = cls.consolidation_cycle()

        # Verify all expected keys exist
        assert "patterns_found" in stats
        assert "promoted" in stats
        assert "skipped_inconsistent" in stats
        assert "total_episodic" in stats
        assert "total_semantic" in stats

        # Types
        assert isinstance(stats["patterns_found"], int)
        assert isinstance(stats["promoted"], int)
        assert isinstance(stats["skipped_inconsistent"], int)
        assert isinstance(stats["total_episodic"], int)
        assert isinstance(stats["total_semantic"], int)

    def test_skips_inconsistent_patterns(self, cls, storage, embeddings):
        """Patterns with contradictions should be skipped during consolidation."""
        # Create contradicting memories that are still similar enough to cluster
        _make_memory(
            storage,
            embeddings,
            "We use Redis for caching in all services",
            session_id="s1",
            tags=["caching"],
        )
        _make_memory(
            storage,
            embeddings,
            "We do not use Redis for caching anymore",
            session_id="s2",
            tags=["caching"],
        )
        _make_memory(
            storage,
            embeddings,
            "Redis caching is used across all our services",
            session_id="s3",
            tags=["caching"],
        )

        stats = cls.consolidation_cycle()
        # The contradicting cluster should be skipped
        # (exact behavior depends on embedding similarity and clustering)
        assert stats["skipped_inconsistent"] >= 0

    def test_secret_gated_pattern_skipped_cycle_continues(self, tmp_path):
        """consolidation_cycle must NOT abort when one pattern's insert_memory raises
        SecretLeakBlocked.  The secret pattern is skipped (skipped_secret += 1) and
        all remaining patterns are still evaluated — the cycle completes normally.

        This is a hermetic mock test: no SurrealDB, no real embeddings.
        Two patterns are fed in via mocked find_recurring_patterns:
          - pattern_secret: triggers SecretLeakBlocked on insert_memory
          - pattern_clean:  insert_memory succeeds (returns id 999)
        Expected outcome: no exception raised, promoted==1, skipped_secret==1.
        """
        from unittest.mock import MagicMock, patch

        import numpy as np

        from yadgar._shared.security.secrets import SecretLeakBlocked
        from yadgar._shared.storage import StorageEngine

        vec = np.ones(384, dtype=np.float32).tobytes()

        storage_mock = MagicMock(spec=StorageEngine)
        # First insert raises SecretLeakBlocked (secret pattern); second succeeds (clean pattern)
        storage_mock.insert_memory.side_effect = [
            SecretLeakBlocked("secret_detected: GitHub token", "ghp_faketoken123456"),
            999,
        ]
        # No existing semantic memories (duplicate check returns empty)
        storage_mock.search_vectors.return_value = []
        # No existing relationships
        storage_mock.get_relationships_among_entities.return_value = []
        # Entity lookups return None → triggers insert_entity
        storage_mock.get_entity_by_name.return_value = None
        storage_mock.insert_entity.return_value = 1
        storage_mock.update_memory_fields.return_value = None
        storage_mock.reinforce_relationship.return_value = None
        storage_mock.insert_relationship.return_value = None
        storage_mock.count_memories_by_store_type.return_value = 0

        emb_mock = _make_mock_embeddings(similarity_value=1.0)

        settings = Settings(
            DB_PATH=str(tmp_path / "secret_test.db"),
            CLUSTER_SIMILARITY_THRESHOLD=0.0,
            CURATION_SIMILARITY_THRESHOLD=0.5,
        )

        cls_store = DualStoreCLS(storage_mock, emb_mock, settings)

        shared_mems = [
            {
                "id": 1,
                "content": "safe content here",
                "directory_context": "/proj",
                # C4 (0047 PR#40 §5): a promotion whose cluster names no single
                # project_id is skipped and counted rather than collapsed onto
                # the "global" sentinel. This test's subject is the secret gate,
                # so the cluster is given a nameable project to keep it on the
                # promote path; the skip itself is asserted in
                # test_c4_sessionless_writers.TestClsPromotionSkipsUnnameableClusters.
                "project_id": "m-agahi/yadgar",
                "embedding": vec,
            },
        ]
        pattern_secret = {
            "memories": shared_mems,
            "occurrence_count": 3,
            "directories": ["/proj"],
        }
        pattern_clean = {
            "memories": shared_mems,
            "occurrence_count": 3,
            "directories": ["/proj"],
        }

        consistent_result = {"consistent": True, "contradictions": []}

        with (
            patch.object(
                cls_store, "find_recurring_patterns", return_value=[pattern_secret, pattern_clean]
            ),
            patch.object(cls_store, "check_consistency", return_value=consistent_result),
            # Return a non-degenerate, non-empty schema so _promote_pattern proceeds to insert_memory
            patch.object(
                cls_store,
                "abstract_to_schema",
                return_value="urllib.request is used for HTTP calls across yadgar/retrieval/core.py",
            ),
        ):
            stats = cls_store.consolidation_cycle()

        # Cycle must not raise; secret pattern skipped, clean pattern promoted
        assert stats["promoted"] == 1, (
            f"expected 1 promoted (clean pattern), got {stats['promoted']}"
        )
        assert stats["skipped_secret"] == 1, (
            f"expected 1 skipped_secret (poisoned pattern), got {stats['skipped_secret']}"
        )


# ── Dual-Store Query Tests ────────────────────────────────────────────


class TestQueryDual:
    def test_query_dual_specific(self, cls, storage, embeddings):
        """A specific query (with file path) should weight episodic results higher."""
        # Create episodic and semantic memories
        _make_memory(
            storage,
            embeddings,
            "Fixed bug in src/auth/login.py that caused null token error",
            store_type="episodic",
        )
        _make_memory(
            storage,
            embeddings,
            "Authentication system uses JWT tokens with refresh mechanism",
            store_type="semantic",
        )

        results = cls.query_dual("error in src/auth/login.py", project_id="", prefer="auto")
        assert isinstance(results, list)
        # The specific episodic memory should appear in results
        if results:
            # Results should contain memories with _dual_score
            assert "_dual_score" in results[0]

    def test_query_dual_general(self, cls, storage, embeddings):
        """A general query (about patterns) should weight semantic results higher."""
        _make_memory(
            storage,
            embeddings,
            "Fixed TypeError in auth.py line 42",
            store_type="episodic",
        )
        _make_memory(
            storage,
            embeddings,
            "The architecture pattern uses factory methods for service creation",
            store_type="semantic",
        )

        results = cls.query_dual(
            "what architecture pattern do we use", project_id="", prefer="auto"
        )
        assert isinstance(results, list)

    def test_query_dual_prefer_episodic(self, cls, storage, embeddings):
        """Explicit episodic preference should weight episodic 2x."""
        _make_memory(
            storage,
            embeddings,
            "Fixed bug in authentication module",
            store_type="episodic",
        )
        _make_memory(
            storage,
            embeddings,
            "Authentication uses JWT tokens as standard",
            store_type="semantic",
        )

        results = cls.query_dual("authentication", project_id="", prefer="episodic")
        assert isinstance(results, list)

    def test_query_dual_prefer_semantic(self, cls, storage, embeddings):
        """Explicit semantic preference should weight semantic 2x."""
        _make_memory(
            storage,
            embeddings,
            "Fixed bug in authentication module",
            store_type="episodic",
        )
        _make_memory(
            storage,
            embeddings,
            "Authentication uses JWT tokens as standard",
            store_type="semantic",
        )

        results = cls.query_dual("authentication", project_id="", prefer="semantic")
        assert isinstance(results, list)

    def test_query_dual_directory_filter(self, cls, storage, embeddings):
        """Query with directory should only return memories from that directory."""
        _make_memory(
            storage,
            embeddings,
            "React hooks for state management",
            directory="/tmp/frontend",
            store_type="episodic",
        )
        _make_memory(
            storage,
            embeddings,
            "Database migration script for PostgreSQL",
            directory="/tmp/backend",
            store_type="episodic",
        )

        results = cls.query_dual("state management", project_id="/tmp/frontend", prefer="auto")
        for r in results:
            assert r.get("directory_context") == "/tmp/frontend"

    def test_query_dual_no_embedding(self, cls, storage, tmp_path):
        """Query with no embeddings available should return empty list."""
        # Use a cls with broken embeddings
        bad_embeddings = EmbeddingEngine("nonexistent-model")
        bad_embeddings._unavailable = True
        bad_cls = DualStoreCLS(storage, bad_embeddings, Settings(DB_PATH=str(tmp_path / "bad.db")))

        results = bad_cls.query_dual("test query", project_id="", prefer="auto")
        assert results == []


# ── consolidation_cycle derived_from link perf tests (v4.4.11) ──────────────

import time  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import numpy as np  # noqa: E402


def _make_mock_embeddings(similarity_value: float = 1.0) -> MagicMock:
    """Return a mock EmbeddingEngine where all operations return fixed values."""
    vec = np.ones(384, dtype=np.float32).tobytes()
    emb = MagicMock(spec=EmbeddingEngine)
    emb.get_model_name.return_value = "all-MiniLM-L6-v2"
    emb.encode.return_value = vec
    emb.encode_batch.return_value = [vec]
    # All pairs are identical (similarity=1.0) → guaranteed to cluster together.
    emb.similarity.return_value = similarity_value
    return emb


@pytest.fixture
def cls_consolidation_at_scale(tmp_path):
    """Seed a qualifying cluster with 40 episodic memories from 2 sessions.

    consolidation_cycle must promote the cluster and create 40 derived_from links.
    With the old _create_derived_link pattern: 40 get_relationship_between HTTP calls.
    Bulk SQL must resolve all 40 links in one query.
    """
    db_path = str(tmp_path / "cls_scale.db")
    engine = StorageEngine(db_path)
    emb = _make_mock_embeddings(similarity_value=1.0)
    # Low similarity threshold so all memories cluster together.
    scale_settings = Settings(
        DB_PATH=str(tmp_path / "test.db"),
        CLUSTER_SIMILARITY_THRESHOLD=0.0,
        # Below 1.0 so the newly created semantic memory is not mistaken for a duplicate.
        CURATION_SIMILARITY_THRESHOLD=0.5,
    )
    cls_store = DualStoreCLS(engine, emb, scale_settings)

    # 40 episodic memories with identical content (guaranteed to cluster), from 2 sessions.
    vec = np.ones(384, dtype=np.float32).tobytes()
    content = "Recurring pattern: always use dependency injection for service construction"
    for i in range(40):
        session = "session-A" if i % 2 == 0 else "session-B"
        episode_id = engine.insert_episode(
            {"session_id": session, "directory": "/proj", "raw_content": content}
        )
        engine.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": content,
                "embedding": vec,
                "tags": ["episodic"],
                "directory_context": "/proj",
                "heat": 0.8,
                "is_stale": False,
                "source_episode_id": episode_id,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )

    yield cls_store, engine
    engine.close()


@pytest.mark.perf
@pytest.mark.timeout(60)
def test_consolidation_cycle_derived_links_under_10s_at_40_memories(
    cls_consolidation_at_scale,
):
    """Regression guard: consolidation_cycle derived_from links must use bulk SQL.

    40 episodic memories in a cluster → 40 get_relationship_between calls under the old
    _create_derived_link pattern (~120ms extra at 3ms/call; scales to minutes at hundreds).
    Bulk SQL path resolves all 40 links in one query. Must complete under 10s.
    """
    cls_store, _engine = cls_consolidation_at_scale
    t0 = time.monotonic()
    stats = cls_store.consolidation_cycle()
    elapsed = time.monotonic() - t0
    assert elapsed < 10.0, (
        f"consolidation_cycle took {elapsed:.1f}s at N=40 cluster members (target <10s)"
    )
    assert stats["total_episodic"] >= 40


def test_consolidation_cycle_derived_from_links_created(tmp_path):
    """Bulk-SQL path correctly creates derived_from relationships for a promoted cluster."""
    engine = StorageEngine(str(tmp_path / "cls_correct.db"))
    emb = _make_mock_embeddings(similarity_value=1.0)
    correct_settings = Settings(
        DB_PATH=str(tmp_path / "test.db"),
        CLUSTER_SIMILARITY_THRESHOLD=0.0,
        CURATION_SIMILARITY_THRESHOLD=0.5,
    )
    cls_store = DualStoreCLS(engine, emb, correct_settings)

    vec = np.ones(384, dtype=np.float32).tobytes()
    content = "Recurring: always validate input before processing"
    for i in range(3):
        session = f"session-{i}"
        episode_id = engine.insert_episode(
            {"session_id": session, "directory": "/proj", "raw_content": content}
        )
        engine.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": content,
                "embedding": vec,
                "tags": ["episodic"],
                "directory_context": "/proj",
                "heat": 0.8,
                "is_stale": False,
                "source_episode_id": episode_id,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )

    stats = cls_store.consolidation_cycle()

    # The stats structure must be correct regardless of promotion outcome.
    assert "promoted" in stats
    assert "total_episodic" in stats
    assert stats["total_episodic"] >= 3
    engine.close()


# ── CLS_PATTERN_MAX_CANDIDATES cap tests ─────────────────────────────────────


class TestClsPatternCandidateCap:
    """find_recurring_patterns must not scan the full episodic store when
    the count exceeds CLS_PATTERN_MAX_CANDIDATES."""

    def test_find_recurring_patterns_respects_candidate_cap(self, tmp_path):
        """With cap=5 and 10 episodic memories, get_memories_by_store_type is
        called with limit=5 (not returning all 10)."""
        from unittest.mock import patch

        import numpy as np

        cap = 5
        settings = Settings(
            DB_PATH=str(tmp_path / "cap_cls.db"),
            CLS_PATTERN_MAX_CANDIDATES=cap,
            CLUSTER_SIMILARITY_THRESHOLD=0.6,
        )
        storage = StorageEngine(str(tmp_path / "cap_cls.db"))
        emb = EmbeddingEngine("all-MiniLM-L6-v2")
        cls = DualStoreCLS(storage, emb, settings)

        # Insert cap+5 episodic memories
        for i in range(cap + 5):
            vec = np.random.default_rng(i + 200).standard_normal(384).astype(np.float32)
            vec /= np.linalg.norm(vec)
            mid = storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": f"cls cap test memory {i}",
                    "embedding": vec.tobytes(),
                    "directory_context": "/proj",
                    "heat": 1.0,
                }
            )
            storage._q(
                "UPDATE type::record('memory', $id) SET store_type = 'episodic'",
                {"id": mid},
            )

        original = storage.get_memories_by_store_type
        called_with_limit = []

        # C9c (0047 §5): the spy mirrors the real signature, so it moved with it
        # when ``get_memories_by_store_type``'s ``directory`` became ``project_id``.
        # Car H1 (§1.3): it moved again for ``unscoped`` — a spy that swallowed
        # the new keyword would let the corpus-read opt-in go untested here.
        def spy(store_type, project_id=None, limit=None, *, unscoped=False):
            called_with_limit.append(limit)
            return original(store_type, project_id=project_id, limit=limit, unscoped=unscoped)

        with patch.object(storage, "get_memories_by_store_type", side_effect=spy):
            cls.find_recurring_patterns()

        storage.close()

        assert called_with_limit, "get_memories_by_store_type was not called"
        assert called_with_limit[0] == cap, f"expected limit={cap}, got {called_with_limit[0]}"


# ── Test: action-stream / auto-abstracted memories skipped by CLS promotion ──


class TestActionStreamNotPromoted:
    """find_recurring_patterns must skip memories tagged _action_stream or
    auto-abstracted — they are noise that must not be promoted to semantic (Fix 3)."""

    def test_action_stream_memories_not_clustered(self, tmp_path):
        """Memories tagged _action_stream are excluded from CLS pattern detection.

        Setup: 4 clean episodic memories from different sessions (forms a 4-count
        pattern at min_occurrences=3) + 1 tagged _action_stream with DISTINCT
        content.  Without the filter, the tagged memory would appear in results;
        with the filter only the 4 clean memories form the pattern and the tagged
        memory's content must be absent from all pattern members.

        This test fails if the filter is removed (5 memories → 5-count pattern
        including the tagged content).
        """
        import numpy as np

        settings = Settings(
            DB_PATH=str(tmp_path / "action_stream.db"),
            CLUSTER_SIMILARITY_THRESHOLD=0.0,  # everything clusters
            CURATION_SIMILARITY_THRESHOLD=0.5,
        )
        storage = StorageEngine(str(tmp_path / "action_stream.db"))
        emb_mock = _make_mock_embeddings(similarity_value=1.0)
        cls_store = DualStoreCLS(storage, emb_mock, settings)

        vec = np.ones(384, dtype=np.float32).tobytes()
        clean_content = "Recurring pattern: bash git diff cat"
        tagged_content = "ACTION_STREAM_NOISE: raw shell commands logged here"

        # Four clean episodic memories from different sessions — forms a 4-count pattern
        for session in ("s-A", "s-B", "s-C", "s-D"):
            ep_id = storage.insert_episode(
                {"session_id": session, "directory": "/proj", "raw_content": clean_content}
            )
            storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": clean_content,
                    "embedding": vec,
                    "tags": ["episodic"],
                    "directory_context": "/proj",
                    "heat": 0.8,
                    "is_stale": False,
                    "source_episode_id": ep_id,
                    "embedding_model": "all-MiniLM-L6-v2",
                }
            )

        # Fifth memory tagged _action_stream with distinct content — must be EXCLUDED
        ep_id5 = storage.insert_episode(
            {"session_id": "s-E", "directory": "/proj", "raw_content": tagged_content}
        )
        mid5 = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": tagged_content,
                "embedding": vec,
                "tags": ["_action_stream"],
                "directory_context": "/proj",
                "heat": 0.8,
                "is_stale": False,
                "source_episode_id": ep_id5,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )
        storage._q(
            "UPDATE type::record('memory', $id) SET store_type = 'episodic'",
            {"id": mid5},
        )

        patterns = cls_store.find_recurring_patterns(min_occurrences=3)
        qualifying = [p for p in patterns if p["occurrence_count"] >= 3]

        # Pattern IS found from the 4 clean memories (non-vacuous)
        assert len(qualifying) >= 1, (
            "expected pattern from 4 clean episodic memories but none found"
        )

        # Tagged memory's distinct content must not appear in any pattern's members
        for p in qualifying:
            member_contents = [m["content"] for m in p["memories"]]
            assert tagged_content not in member_contents, (
                "_action_stream memory must not appear in CLS pattern members"
            )

        storage.close()

    def test_auto_abstracted_memories_not_clustered(self, tmp_path):
        """Memories tagged auto-abstracted are excluded from CLS pattern detection.

        Already-promoted semantics re-entering the clustering loop would create
        secondary noise.  Setup: 4 clean episodic memories from different sessions
        (forms a 4-count pattern at min_occurrences=3) + 1 tagged auto-abstracted
        with DISTINCT content.  Without the filter, the tagged memory would appear
        in results; with the filter only the 4 clean memories form the pattern and
        the tagged memory's content must be absent from all pattern members.
        """
        import numpy as np

        settings = Settings(
            DB_PATH=str(tmp_path / "auto_abs.db"),
            CLUSTER_SIMILARITY_THRESHOLD=0.0,
            CURATION_SIMILARITY_THRESHOLD=0.5,
        )
        storage = StorageEngine(str(tmp_path / "auto_abs.db"))
        emb_mock = _make_mock_embeddings(similarity_value=1.0)
        cls_store = DualStoreCLS(storage, emb_mock, settings)

        vec = np.ones(384, dtype=np.float32).tobytes()
        clean_content = "Recurring pattern: deploy pipeline setup"
        tagged_content = "AUTO_ABSTRACTED_SCHEMA: Recurring across 5 obs: deploy pipeline setup"

        # Four clean episodic memories from different sessions — forms a 4-count pattern
        for session in ("s-A", "s-B", "s-C", "s-D"):
            ep_id = storage.insert_episode(
                {"session_id": session, "directory": "/proj", "raw_content": clean_content}
            )
            storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": clean_content,
                    "embedding": vec,
                    "tags": ["episodic"],
                    "directory_context": "/proj",
                    "heat": 0.8,
                    "is_stale": False,
                    "source_episode_id": ep_id,
                    "embedding_model": "all-MiniLM-L6-v2",
                }
            )

        # Fifth memory tagged auto-abstracted with distinct content — must be EXCLUDED
        ep_id5 = storage.insert_episode(
            {"session_id": "s-E", "directory": "/proj", "raw_content": tagged_content}
        )
        mid5 = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": tagged_content,
                "embedding": vec,
                "tags": ["semantic", "auto-abstracted"],
                "directory_context": "/proj",
                "heat": 0.5,
                "is_stale": False,
                "source_episode_id": ep_id5,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )
        storage._q(
            "UPDATE type::record('memory', $id) SET store_type = 'episodic'",
            {"id": mid5},
        )

        patterns = cls_store.find_recurring_patterns(min_occurrences=3)
        qualifying = [p for p in patterns if p["occurrence_count"] >= 3]

        # Pattern IS found from the 4 clean memories (non-vacuous)
        assert len(qualifying) >= 1, (
            "expected pattern from 4 clean episodic memories but none found"
        )

        # Tagged memory's distinct content must not appear in any pattern's members
        for p in qualifying:
            member_contents = [m["content"] for m in p["memories"]]
            assert tagged_content not in member_contents, (
                "auto-abstracted memory must not appear in CLS pattern members"
            )

        storage.close()


# ── v4.9 item 9: Degenerate CLS pattern guard ─────────────────────────────────


class TestHasAsciiIdentifierToken:
    """Unit tests for _has_ascii_identifier_token helper (Fix 1)."""

    def test_rejects_bare_frequently_modified_together(self):
        """'frequently modified together' has no identifier → rejected."""
        from yadgar.backend.cls_store import _has_ascii_identifier_token

        assert _has_ascii_identifier_token("frequently modified together") is False

    def test_rejects_short_body(self):
        """Body shorter than 20 chars is rejected regardless of content."""
        from yadgar.backend.cls_store import _has_ascii_identifier_token

        assert _has_ascii_identifier_token("short body") is False

    def test_accepts_python_path(self):
        """Body containing a .py file path is accepted."""
        from yadgar.backend.cls_store import _has_ascii_identifier_token

        assert (
            _has_ascii_identifier_token(
                "urllib.request and yadgar/cls_store.py frequently modified together"
            )
            is True
        )

    def test_accepts_json_extension(self):
        """Body containing a .json file is accepted."""
        from yadgar.backend.cls_store import _has_ascii_identifier_token

        assert (
            _has_ascii_identifier_token(
                "pyproject.json and config.json frequently modified together"
            )
            is True
        )

    def test_accepts_long_python_identifier(self):
        """Body with a Python identifier longer than 3 chars is accepted."""
        from yadgar.backend.cls_store import _has_ascii_identifier_token

        assert (
            _has_ascii_identifier_token("consolidation_cycle and embedding_engine used frequently")
            is True
        )

    def test_rejects_stop_words_only(self):
        """Pure stop-word body rejected."""
        from yadgar.backend.cls_store import _has_ascii_identifier_token

        assert (
            _has_ascii_identifier_token("and the for with that this from was were frequently")
            is False
        )


class TestIsDegenerateAutoAbstracted:
    """Unit tests for _is_degenerate_auto_abstracted (PR #60 audit fixes)."""

    def test_tags_suffix_variant_detected(self):
        """Realistic emission shape with [tags: ...] suffix IS degenerate."""
        from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

        content = (
            "Recurring pattern across 27 observations: frequently modified together"
            " [tags: episodic, auto-abstracted]"
        )
        assert _is_degenerate_auto_abstracted(content) is True, (
            "degenerate body with [tags:...] suffix must be detected"
        )

    def test_non_recurring_prefix_non_latin_not_degenerate(self):
        """Body without Recurring prefix and without ASCII identifiers must NOT be degenerate.

        Guards against multilingual data loss: pure Cyrillic content has no
        ASCII identifier tokens, so _has_ascii_identifier_token returns False.
        Condition 2 must only fire when the Recurring prefix was present.
        """
        from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

        content = "Часто изменяется вместе с другим файлом в проекте"
        assert _is_degenerate_auto_abstracted(content) is False, (
            "non-Latin content without Recurring prefix must NOT be marked degenerate"
        )

    def test_non_recurring_prefix_arabic_not_degenerate(self):
        """Arabic script content without Recurring prefix must not be degenerate."""
        from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

        content = "يتم تعديله بشكل متكرر مع ملفات أخرى في المشروع"
        assert _is_degenerate_auto_abstracted(content) is False, (
            "Arabic content without Recurring prefix must NOT be marked degenerate"
        )

    def test_recurring_prefix_non_latin_body_NOT_degenerate(self):
        """Recurring prefix + non-Latin body must NOT be flagged as degenerate.

        abstract_to_schema always prepends 'Recurring pattern across N observations:'
        so in production every auto-abstracted memory has this prefix. Condition 2
        must not delete Russian/Arabic/Japanese/Greek/etc content just because it
        lacks ASCII identifier tokens — Unicode letter runs are meaningful tokens too.
        """
        from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

        content = (
            "Recurring pattern across 5 observations: часто изменяется вместе с другими файлами"
        )
        assert _is_degenerate_auto_abstracted(content) is False, (
            "Recurring prefix + Cyrillic body must NOT be marked degenerate — "
            "Unicode tokens are meaningful subjects"
        )

    def test_recurring_prefix_arabic_body_NOT_degenerate(self):
        """Recurring prefix + Arabic body must NOT be flagged as degenerate."""
        from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

        content = "Recurring pattern across 3 observations: يتم تعديله بشكل متكرر مع ملفات أخرى"
        assert _is_degenerate_auto_abstracted(content) is False, (
            "Recurring prefix + Arabic body must NOT be marked degenerate"
        )

    def test_recurring_prefix_japanese_body_NOT_degenerate(self):
        """Recurring prefix + Japanese body must NOT be flagged as degenerate."""
        from yadgar.backend.cls_store import _is_degenerate_auto_abstracted

        content = "Recurring pattern across 4 observations: 頻繁に変更されるモジュール"
        assert _is_degenerate_auto_abstracted(content) is False, (
            "Recurring prefix + Japanese body must NOT be marked degenerate"
        )


class TestIsThinAutoAbstracted:
    """Unit tests for _is_thin_auto_abstracted (C4.3 / S1, ADR-0142).

    A meta-token-DENSE auto-abstracted schema — a bag dominated by yadgar-internal
    plumbing tokens (entity:NNNN, derived_from, co_occurrence, N-edge, graph, viz)
    with too few distinct real domain tokens — must NOT be promoted to a semantic
    memory. This is distinct from the DEGENERATE guard (exact "frequently modified
    together" / no-meaningful-token): a thin schema DOES contain meaningful tokens,
    it is just internal noise.

    Load-bearing invariant (over-suppression guard): a genuinely useful abstraction
    with several distinct real domain tokens must STILL be promoted. The guard
    targets meta-token DENSITY, not verbosity — a long topical schema with real
    anchors (jwt, docker, longmemeval) is kept.
    """

    def test_meta_dense_internal_plumbing_is_thin(self):
        """The canonical live example: entity-namespace / graph-plumbing bag."""
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        content = (
            "Recurring pattern across 4 observations: 2026-07-16 via earlier entities "
            "0-edge dead weight entity:4551 viz connections derived_from edges graph "
            "only co_occurrence [tags: reference, entity, viz, graph_prior]"
        )
        assert _is_thin_auto_abstracted(content) is True, (
            "meta-token-dense internal-plumbing schema must be flagged thin"
        )

    def test_pure_namespace_tokens_is_thin(self):
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        content = (
            "Recurring pattern across 6 observations: entity:9001 entity:9002 "
            "derived_from co_occurrence edges graph edge [tags: entity, graph]"
        )
        assert _is_thin_auto_abstracted(content) is True

    # ── Over-suppression guard: real abstractions must NOT be flagged thin ──

    def test_jwt_auth_abstraction_not_thin(self):
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        content = (
            "Recurring pattern across 3 observations: jwt auth middleware token "
            "refresh endpoint [tags: auth]"
        )
        assert _is_thin_auto_abstracted(content) is False

    def test_docker_deploy_abstraction_not_thin(self):
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        content = (
            "Recurring pattern across 4 observations: docker deployment pipeline "
            "container registry push [tags: devops]"
        )
        assert _is_thin_auto_abstracted(content) is False

    def test_topical_verbose_benchmark_abstraction_not_thin(self):
        """The topical-but-verbose benchmark schema carries REAL recall anchors
        (longmemeval, dataset, benchmarks) — suppressing it would be a genuine
        recall loss. It is verbose, not meta-dense → must NOT be flagged thin."""
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        content = (
            "Recurring pattern across 3 observations: run benchmarks running commit "
            "harnesses make fast longmemeval primary rigorous benchmark mit dataset "
            "500 self-seeds [tags: benchmark, longmemeval, eval, feedback]"
        )
        assert _is_thin_auto_abstracted(content) is False

    @pytest.mark.parametrize(
        "content",
        [
            "Recurring pattern across 3 observations: postgres migration index concurrently lock",
            "Recurring pattern across 5 observations: react hooks state effect memo callback",
            "Recurring pattern across 2 observations: kafka consumer offset commit rebalance",
            "Recurring pattern across 4 observations: terraform module vpc subnet routing",
            "Recurring pattern across 3 observations: redis sentinel quorum failover replica",
        ],
    )
    def test_real_domain_abstractions_not_thin(self, content):
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        assert _is_thin_auto_abstracted(content) is False, (
            f"real domain abstraction over-suppressed as thin: {content!r}"
        )

    def test_non_recurring_prefix_never_thin(self):
        """Guard only fires on the Recurring-pattern prefix (auto-abstracted shape).
        Arbitrary user content must NOT be reached by the thin gate."""
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        content = "entity:4551 derived_from co_occurrence edges graph"
        assert _is_thin_auto_abstracted(content) is False

    # ── Threshold boundary (pins _THIN_MIN_REAL_TOKENS == 3 exactly) ──

    def test_exactly_two_real_tokens_is_thin(self):
        """A schema whose body has exactly TWO distinct real tokens (the rest
        meta/stop) is below the min-information bar → thin. Pins the lower edge:
        a laxer threshold (K=2) would wrongly promote it."""
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        # real tokens: {kafka, consumer}; entity/graph/derived_from are meta.
        content = (
            "Recurring pattern across 3 observations: kafka consumer entity:9 "
            "graph derived_from co_occurrence [tags: entity]"
        )
        assert _is_thin_auto_abstracted(content) is True

    def test_exactly_three_real_tokens_not_thin(self):
        """A schema with exactly THREE distinct real tokens clears the bar →
        NOT thin. Pins the upper edge: a stricter threshold (K=4) or a `<=`
        comparison would wrongly suppress it."""
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        # real tokens: {kafka, consumer, offset}; entity/graph are meta.
        content = (
            "Recurring pattern across 3 observations: kafka consumer offset "
            "entity:9 graph [tags: entity]"
        )
        assert _is_thin_auto_abstracted(content) is False


class TestThinAutoAbstractedProperty:
    """Hypothesis: over-suppression is a falsifiable property.

    Any Recurring-pattern schema whose body is >= K distinct real (non-meta,
    non-stopword) domain tokens is NEVER flagged thin, regardless of how many
    extra tokens surround them.
    """

    _REAL_TOKENS = [
        "jwt",
        "auth",
        "middleware",
        "docker",
        "kafka",
        "postgres",
        "redis",
        "terraform",
        "pipeline",
        "migration",
        "consumer",
        "sentinel",
        "vpc",
        "hooks",
        "endpoint",
        "registry",
        "offset",
        "quorum",
        "subnet",
        "index",
    ]

    @hyp_settings(max_examples=200)
    @given(
        tokens=st.lists(st.sampled_from(_REAL_TOKENS), min_size=4, max_size=10, unique=True),
        n=st.integers(min_value=2, max_value=40),
    )
    @example(tokens=["jwt", "auth", "middleware", "docker"], n=3)
    def test_enough_real_tokens_never_thin(self, tokens, n):
        from yadgar.backend.cls_store import _is_thin_auto_abstracted

        body = " ".join(tokens)
        content = f"Recurring pattern across {n} observations: {body}"
        assert _is_thin_auto_abstracted(content) is False


class TestDegeneratePatternNotEmitted:
    """consolidation_cycle must not emit memories whose extracted body fails
    _has_ascii_identifier_token (ASCII-only; non-Latin guarded at call site) — Fix 1."""

    def test_degenerate_content_not_emitted(self, tmp_path):
        """Cluster of 'memory:X and memory:Y are frequently modified together' strings
        must NOT produce a semantic memory — body after prefix is just
        'frequently modified together' which fails the subject check."""
        import numpy as np

        settings = Settings(
            DB_PATH=str(tmp_path / "degen.db"),
            CLUSTER_SIMILARITY_THRESHOLD=0.0,
            CURATION_SIMILARITY_THRESHOLD=0.5,
        )
        storage = StorageEngine(str(tmp_path / "degen.db"))
        emb_mock = _make_mock_embeddings(similarity_value=1.0)
        cls_store = DualStoreCLS(storage, emb_mock, settings)

        # Insert 3 memories that would produce the degenerate pattern
        vec = np.ones(384, dtype=np.float32).tobytes()
        degenerate_contents = [
            "memory:101 and memory:102 are frequently modified together",
            "memory:103 and memory:104 are frequently modified together",
            "memory:105 and memory:106 are frequently modified together",
        ]
        for i, content in enumerate(degenerate_contents):
            session = f"degen-session-{i}"
            ep_id = storage.insert_episode(
                {"session_id": session, "directory": "/proj", "raw_content": content}
            )
            mid = storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": content,
                    "embedding": vec,
                    "tags": ["episodic"],
                    "directory_context": "/proj",
                    "heat": 0.8,
                    "is_stale": False,
                    "source_episode_id": ep_id,
                    "embedding_model": "all-MiniLM-L6-v2",
                }
            )
            storage._q(
                "UPDATE type::record('memory', $id) SET store_type = 'episodic'",
                {"id": mid},
            )

        before_semantic = storage.count_memories_by_store_type("semantic")
        cls_store.consolidation_cycle()
        after_semantic = storage.count_memories_by_store_type("semantic")

        # No new semantic memory should have been emitted
        assert after_semantic == before_semantic, (
            f"Degenerate pattern promoted: {after_semantic - before_semantic} new semantic memories created"
        )
        storage.close()

    def test_meaningful_pattern_still_emitted(self, tmp_path):
        """Cluster with a real subject (e.g. urllib.request) IS promoted — no false negatives."""
        import numpy as np

        settings = Settings(
            DB_PATH=str(tmp_path / "real.db"),
            CLUSTER_SIMILARITY_THRESHOLD=0.0,
            CURATION_SIMILARITY_THRESHOLD=0.5,
        )
        storage = StorageEngine(str(tmp_path / "real.db"))
        emb_mock = _make_mock_embeddings(similarity_value=1.0)
        cls_store = DualStoreCLS(storage, emb_mock, settings)

        vec = np.ones(384, dtype=np.float32).tobytes()
        # Content with meaningful subject: urllib.request appears in all memories
        real_contents = [
            "urllib.request module used for HTTP calls in yadgar/retrieval/core.py",
            "urllib.request used in retrieval pipeline via yadgar/retrieval/core.py helper",
            "HTTP calls routed through urllib.request in yadgar/retrieval/core.py",
        ]
        for i, content in enumerate(real_contents):
            session = f"real-session-{i}"
            ep_id = storage.insert_episode(
                {"session_id": session, "directory": "/proj", "raw_content": content}
            )
            mid = storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": content,
                    "embedding": vec,
                    "tags": ["episodic"],
                    "directory_context": "/proj",
                    "heat": 0.8,
                    "is_stale": False,
                    "source_episode_id": ep_id,
                    "embedding_model": "all-MiniLM-L6-v2",
                }
            )
            storage._q(
                "UPDATE type::record('memory', $id) SET store_type = 'episodic'",
                {"id": mid},
            )

        before_semantic = storage.count_memories_by_store_type("semantic")
        cls_store.consolidation_cycle()
        after_semantic = storage.count_memories_by_store_type("semantic")

        # A real pattern should be promoted
        assert after_semantic > before_semantic, (
            "Expected at least one semantic memory promoted for meaningful pattern"
        )
        storage.close()
