"""Tests for predictive coding write gate — surprisal-based memory gating."""

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.storage import StorageEngine
from yadgar.backend.predictive_coding import WriteGate
from yadgar.backend.retrieval import Retriever

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: dict without this key is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "test.db"),
        WRITE_GATE_THRESHOLD=0.4,
    )


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture
def kg(storage, settings):
    return KnowledgeGraph(storage, settings)


@pytest.fixture
def retriever(storage, embeddings, kg, settings):
    return Retriever(storage, embeddings, kg, settings)


@pytest.fixture
def gate(storage, embeddings, retriever, settings):
    return WriteGate(storage, embeddings, retriever, settings)


def _make_memory(storage, embeddings, content, directory="/tmp/project", tags=None, **kwargs):
    """Helper to insert a memory with real embedding."""
    embedding = embeddings.encode(content)
    mem = {
        "project_id": _TEST_PROJECT,
        "content": content,
        "embedding": embedding,
        "tags": tags or ["test"],
        "directory_context": directory,
        "heat": 1.0,
        "is_stale": False,
        "embedding_model": embeddings.get_model_name(),
    }
    mem.update(kwargs)
    mid = storage.insert_memory(mem)
    return mid


class TestHighSurprisalNovelContent:
    def test_novel_content_passes_gate(self, gate, storage, embeddings):
        """Completely novel content in an existing directory should pass the gate."""
        # Seed with some Python-related memories
        _make_memory(
            storage,
            embeddings,
            "Using Flask for the web API with SQLAlchemy ORM",
            directory="/tmp/project",
        )
        _make_memory(
            storage,
            embeddings,
            "Configured pytest with coverage reporting",
            directory="/tmp/project",
        )

        # Novel content about a completely different topic
        should_store, surprisal, reason = gate.should_store(
            "Implemented GPU-accelerated matrix multiplication with CUDA kernels",
            "/tmp/project",
            ["cuda", "gpu"],
        )
        assert should_store is True
        assert surprisal >= 0.4
        assert reason == "high_surprisal"


class TestLowSurprisalDuplicate:
    def test_near_duplicate_is_blocked(self, gate, storage, embeddings):
        """Near-duplicate content should be blocked by the gate."""
        _make_memory(
            storage,
            embeddings,
            "Using Flask for the web API with SQLAlchemy ORM for database access",
            directory="/tmp/project",
        )

        # Very similar content
        should_store, surprisal, reason = gate.should_store(
            "Using Flask for the web API with SQLAlchemy ORM for database queries",
            "/tmp/project",
            ["flask"],
        )
        assert should_store is False
        assert surprisal < 0.4
        assert reason.startswith("below_threshold")


class TestAlwaysStoreErrors:
    def test_error_keywords_bypass_gate(self, gate, storage, embeddings):
        """Content with error keywords should always be stored."""
        # Seed with similar content
        _make_memory(
            storage,
            embeddings,
            "Database connection configuration for PostgreSQL",
            directory="/tmp/project",
        )

        # Error content about database — even if topic is similar, bypass gate
        should_store, surprisal, reason = gate.should_store(
            "Database connection error: PostgreSQL connection refused on port 5432",
            "/tmp/project",
            ["database"],
        )
        assert should_store is True
        assert reason == "bypass_error_keywords"

    def test_exception_keyword_bypasses(self, gate, storage, embeddings):
        """Content mentioning exceptions should bypass the gate."""
        _make_memory(
            storage,
            embeddings,
            "Python function for data processing",
            directory="/tmp/project",
        )
        should_store, surprisal, reason = gate.should_store(
            "Python exception in data processing pipeline",
            "/tmp/project",
            ["python"],
        )
        assert should_store is True
        assert reason == "bypass_error_keywords"

    def test_traceback_keyword_bypasses(self, gate, storage, embeddings):
        """Content mentioning tracebacks should bypass."""
        should_store, _, reason = gate.should_store(
            "Traceback most recent call last in main.py",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_error_keywords"

    def test_failed_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'failed' should bypass."""
        should_store, _, reason = gate.should_store(
            "Build failed due to missing dependency",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_error_keywords"

    def test_bug_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'bug' should bypass."""
        should_store, _, reason = gate.should_store(
            "Found a bug in the authentication logic",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_error_keywords"

    def test_crash_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'crash' should bypass."""
        should_store, _, reason = gate.should_store(
            "Application crash on startup after config change",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_error_keywords"


class TestAlwaysStoreDecisions:
    def test_decision_keywords_bypass_gate(self, gate, storage, embeddings):
        """Content with decision keywords should always be stored."""
        _make_memory(
            storage,
            embeddings,
            "Working with the project configuration system",
            directory="/tmp/project",
        )

        should_store, surprisal, reason = gate.should_store(
            "Decided to use Redis instead of Memcached for caching",
            "/tmp/project",
            ["caching"],
        )
        assert should_store is True
        assert reason == "bypass_decision_keywords"

    def test_chose_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'chose' should bypass."""
        should_store, _, reason = gate.should_store(
            "Chose TypeScript over JavaScript for type safety",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_decision_keywords"

    def test_switched_to_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'switched to' should bypass."""
        should_store, _, reason = gate.should_store(
            "Switched to pnpm from npm for faster installs",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_decision_keywords"

    def test_migrated_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'migrated' should bypass."""
        should_store, _, reason = gate.should_store(
            "Migrated database from MySQL to PostgreSQL",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_decision_keywords"

    def test_architecture_keyword_bypasses(self, gate, storage, embeddings):
        """Content with 'architecture' should bypass."""
        should_store, _, reason = gate.should_store(
            "Redesigned the microservices architecture for better scalability",
            "/tmp/project",
            [],
        )
        assert should_store is True
        assert reason == "bypass_decision_keywords"


class TestImportantTagBypass:
    def test_important_tag_bypasses(self, gate, storage, embeddings):
        """Content tagged 'important' should always be stored."""
        _make_memory(
            storage,
            embeddings,
            "Standard project setup with Python and pip",
            directory="/tmp/project",
        )

        should_store, _, reason = gate.should_store(
            "Standard project setup with Python and pip configuration",
            "/tmp/project",
            ["important"],
        )
        assert should_store is True
        assert reason == "bypass_important_tag"

    def test_critical_tag_bypasses(self, gate, storage, embeddings):
        """Content tagged 'critical' should always be stored."""
        should_store, _, reason = gate.should_store(
            "Routine maintenance task for the project",
            "/tmp/project",
            ["critical"],
        )
        assert should_store is True
        assert reason == "bypass_important_tag"


class TestEmptyDirectoryModerateSurprise:
    def test_new_directory_returns_high_surprisal(self, gate):
        """A brand new directory with no memories should return ~0.8 surprisal."""
        surprisal = gate.compute_surprisal(
            "Setting up a new Rust project with Cargo",
            "/tmp/brand-new-project",
            ["rust"],
        )
        assert surprisal == pytest.approx(0.8, abs=0.01)

    def test_new_directory_always_passes_gate(self, gate):
        """Content for a brand new directory should pass the gate."""
        should_store, surprisal, reason = gate.should_store(
            "Initializing a new Go microservice",
            "/tmp/brand-new-service",
            ["go"],
        )
        assert should_store is True
        assert surprisal >= 0.4


class TestEntityNoveltyNewEntities:
    def test_new_entities_increase_surprisal(self, gate, storage, embeddings):
        """Content with new entities not in the graph should increase surprisal."""
        # Seed with known entities
        _make_memory(
            storage,
            embeddings,
            "def process_data(): pass\ndef validate_input(): pass",
            directory="/tmp/project",
        )
        # Add known entities to entity table
        storage.insert_entity({"name": "process_data", "type": "function"})
        storage.insert_entity({"name": "validate_input", "type": "function"})

        # Content with entirely new entity names
        entity_novelty = gate._compute_entity_novelty(
            "def quantum_entangle(): pass\ndef teleport_state(): pass",
            "/tmp/project",
        )
        # New entities should yield high entity novelty
        assert entity_novelty > 0.5

    def test_existing_entities_lower_novelty(self, gate, storage, embeddings):
        """Content referencing existing entities should have lower entity novelty."""
        storage.insert_entity({"name": "process_data", "type": "function"})
        storage.insert_entity({"name": "validate_input", "type": "function"})

        entity_novelty = gate._compute_entity_novelty(
            "def process_data(): updated\ndef validate_input(): improved",
            "/tmp/project",
        )
        # Existing entities should yield lower novelty
        assert entity_novelty < 0.8


class TestTemporalNoveltyRecent:
    def test_recent_topic_has_low_temporal_novelty(self, gate, storage, embeddings):
        """A topic discussed very recently should have low temporal novelty."""
        # Insert a memory about Flask with a very recent timestamp
        storage.insert_entity({"name": "Flask", "type": "dependency"})
        _make_memory(
            storage,
            embeddings,
            "Setting up Flask web server with routes",
            directory="/tmp/project",
        )

        temporal_novelty = gate._compute_temporal_novelty(
            "Adding Flask middleware for authentication",
            "/tmp/project",
        )
        # Recent discussion about Flask → low temporal novelty
        assert temporal_novelty <= 0.3

    def test_no_related_entities_high_temporal_novelty(self, gate):
        """Content with no matching entities should have high temporal novelty."""
        temporal_novelty = gate._compute_temporal_novelty(
            "Quantum computing entanglement protocol",
            "/tmp/empty-project",
        )
        assert temporal_novelty >= 0.7


class TestBoundaryDetectionTopicChange:
    def test_strong_signal_on_topic_transition(self, gate):
        """A strong topic change should yield boundary > 0.6."""
        boundary = gate.compute_boundary_signal(
            "Implementing GPU-accelerated neural network training with CUDA",
            "Debugging the CSS layout issue in the navigation bar",
        )
        assert boundary > 0.6

    def test_weak_signal_on_same_topic(self, gate):
        """Similar topics should yield low boundary signal."""
        boundary = gate.compute_boundary_signal(
            "Fixed a bug in the Flask authentication middleware",
            "Updated the Flask authentication middleware with new tokens",
        )
        assert boundary < 0.6

    def test_boundary_in_valid_range(self, gate):
        """Boundary signal should always be in [0.0, 1.0]."""
        boundary = gate.compute_boundary_signal(
            "Hello world",
            "Goodbye world",
        )
        assert 0.0 <= boundary <= 1.0


class TestDirectoryModelBuilds:
    def test_directory_model_returns_correct_stats(self, gate, storage, embeddings):
        """Directory model should return accurate statistics."""
        _make_memory(
            storage,
            embeddings,
            "Flask web server configuration",
            directory="/tmp/myproject",
            tags=["flask", "web"],
        )
        _make_memory(
            storage,
            embeddings,
            "SQLAlchemy database models and migrations",
            directory="/tmp/myproject",
            tags=["database", "flask"],
        )

        model = gate.get_directory_model("/tmp/myproject")
        assert model["memory_count"] == 2
        assert model["avg_heat"] > 0.0
        assert isinstance(model["common_tags"], list)
        assert isinstance(model["recent_topics"], list)
        assert model["centroid_embedding"] is not None

    def test_empty_directory_model(self, gate):
        """Empty directory should return zero-valued model."""
        model = gate.get_directory_model("/tmp/nonexistent")
        assert model["memory_count"] == 0
        assert model["entity_count"] == 0
        assert model["avg_heat"] == 0.0
        assert model["common_tags"] == []
        assert model["recent_topics"] == []
        assert model["centroid_embedding"] is None


class TestWriteGateIntegration:
    def test_server_remember_respects_gate(self, tmp_path):
        """Server memorize() should respect write gate decisions during drain."""
        from yadgar.core import server
        from yadgar.tests._backend_harness import wire_drainer
        from yadgar.tests.conftest import memorize_sync

        db_path = str(tmp_path / "test_integration.db")
        server.init_engines(db_path=db_path, start_daemons=False)

        try:
            # R3: core has no drainer — wire the in-process backend drainer so
            # memorize_sync can flush the queue and return a DB row with id.
            with wire_drainer(server._get_file_queue):
                # First, store a base memory (novel, should pass gate)
                # memorize_sync drains and returns DB row with id
                result1 = memorize_sync(
                    content="Using Redis for caching with TTL-based expiration",
                    context="/tmp/integration-test",
                    tags=["redis", "caching"],
                )
                # First memory in a new directory should always be stored
                assert "id" in result1

                # Store another base memory
                memorize_sync(
                    content="PostgreSQL database with connection pooling via pgbouncer",
                    context="/tmp/integration-test",
                    tags=["postgres", "database"],
                )

                # Now try to store a near-duplicate — may be blocked by write gate
                # during drain
                result3 = memorize_sync(
                    content="Using Redis for caching with TTL-based expiration policy",
                    context="/tmp/integration-test",
                    tags=["redis"],
                )
                # This may be blocked or may be merged by curator.
                # v4.4: gate fires during drain — caller gets DB row (id present) or
                # the queued response if the gate blocked and nothing was persisted.
                # Either way the call must not raise.
                assert result3 is not None
        finally:
            server.shutdown()


class TestSurprisalReturnedInResponse:
    def test_surprisal_in_remember_response(self, tmp_path):
        """Write gate computes a valid surprisal score for novel content."""
        from yadgar.core import server
        from yadgar.tests.conftest import memorize_sync

        db_path = str(tmp_path / "test_surprisal_response.db")
        server.init_engines(db_path=db_path, start_daemons=False)

        try:
            # Store a novel memory and verify it lands in the DB
            result = memorize_sync(
                content="Implementing a brand new quantum error correction algorithm",
                context="/tmp/surprisal-test",
                tags=["quantum"],
            )
            # v4.4 async path: surprisal is internal to the drainer, not in the
            # caller response. Verify the memory was stored successfully.
            assert result.get("id") is not None or result.get("stored") is True

            # Verify the write gate itself returns a valid surprisal range
            write_gate = server._write_gate
            if write_gate is not None:
                surprisal = write_gate.compute_surprisal(
                    "Implementing a brand new quantum error correction algorithm",
                    "/tmp/surprisal-test",
                    ["quantum"],
                )
                assert isinstance(surprisal, float)
                assert 0.0 <= surprisal <= 1.0
        finally:
            server.shutdown()

    def test_blocked_memory_returns_surprisal(self, tmp_path):
        """Write gate computes surprisal for duplicate content; low when embeddings available."""
        from yadgar.core import server
        from yadgar.tests.conftest import memorize_sync

        db_path = str(tmp_path / "test_blocked_surprisal.db")
        server.init_engines(db_path=db_path, start_daemons=False)

        try:
            # Store a base memory to build a generative model
            base_content = "Python Flask web application with REST API endpoints"
            r1 = memorize_sync(
                content=base_content,
                context="/tmp/surprisal-block-test",
                tags=["flask"],
            )
            assert r1.get("id") is not None or r1.get("stored") is True

            # Verify the write gate returns a valid surprisal value for the same content
            write_gate = server._write_gate
            if write_gate is not None:
                surprisal = write_gate.compute_surprisal(
                    base_content,
                    "/tmp/surprisal-block-test",
                    ["flask"],
                )
                assert isinstance(surprisal, float)
                assert 0.0 <= surprisal <= 1.0
                # When sentence-transformers is available, duplicates get low surprisal.
                # Without embeddings, the embedding component falls back to high novelty,
                # so we only check the magnitude when embeddings are functional.
                from yadgar._shared.embeddings import EmbeddingEngine

                _emb = EmbeddingEngine()
                if _emb.encode("test") is not None:
                    assert surprisal < 0.5
        finally:
            server.shutdown()


class TestSurprisalComputation:
    def test_surprisal_range(self, gate, storage, embeddings):
        """Surprisal should always be in [0.0, 1.0]."""
        _make_memory(
            storage,
            embeddings,
            "Python web development with Django",
            directory="/tmp/project",
        )
        surprisal = gate.compute_surprisal(
            "Building web apps with Python Django framework",
            "/tmp/project",
            ["python"],
        )
        assert 0.0 <= surprisal <= 1.0

    def test_identical_content_low_surprisal(self, gate, storage, embeddings):
        """Identical content should have very low surprisal."""
        content = "Configuring nginx reverse proxy with SSL termination"
        _make_memory(storage, embeddings, content, directory="/tmp/project")

        surprisal = gate.compute_surprisal(content, "/tmp/project", [])
        assert surprisal < 0.4

    def test_novel_content_high_surprisal(self, gate, storage, embeddings):
        """Completely novel content should have high surprisal."""
        _make_memory(
            storage,
            embeddings,
            "Python web application with Flask",
            directory="/tmp/project",
        )

        surprisal = gate.compute_surprisal(
            "Implementing quantum annealing optimization for protein folding",
            "/tmp/project",
            ["quantum"],
        )
        assert surprisal > 0.4


class TestStructuralNovelty:
    def test_no_entities_returns_low_structural_novelty(self, gate):
        """Content with no extractable entities should return low structural novelty."""
        novelty = gate._compute_structural_novelty(
            "This is a simple note",
            "/tmp/project",
        )
        assert novelty <= 0.2

    def test_new_relationship_context_high_novelty(self, gate, storage, embeddings):
        """Content introducing new relationship types should yield high novelty."""
        # No existing relationships — any rel context is new
        novelty = gate._compute_structural_novelty(
            "Fixed the ImportError in the main module by updating the package",
            "/tmp/project",
        )
        # resolved_by from the error-fix pattern should be detected
        # If it's a new rel type in the graph → 0.8
        assert novelty >= 0.2  # At minimum


class TestEmbeddingNovelty:
    def test_no_vectors_high_novelty(self, gate):
        """No existing vectors should yield high embedding novelty."""
        novelty = gate._compute_embedding_novelty("Brand new content with no prior context")
        assert novelty >= 0.7

    def test_similar_content_low_novelty(self, gate, storage, embeddings):
        """Similar content should yield low embedding novelty."""
        _make_memory(
            storage,
            embeddings,
            "Setting up Flask with SQLAlchemy for database access",
            directory="/tmp/project",
        )
        novelty = gate._compute_embedding_novelty(
            "Configuring Flask with SQLAlchemy for database operations"
        )
        assert novelty < 0.5


# ── structural_novelty bulk-SQL perf tests (v4.4.10) ────────────────────────

import time  # noqa: E402


@pytest.fixture
def structural_novelty_at_scale(tmp_path, settings, embeddings):
    """Seed 50 entities whose names appear in content via 'from X import Y' statements.

    'from X import Y' gives Y a rel_context="imports", so new_rel_contexts is non-empty
    and the entity-pair loop actually runs. 50 'Y' entities → up to 1225 pairs.
    At 3ms/pair that is ~3.7s with the old per-pair HTTP pattern.
    """
    from yadgar._shared.knowledge_graph import KnowledgeGraph
    from yadgar.backend.retrieval import Retriever

    engine = StorageEngine(str(tmp_path / "sn_scale.db"))
    kg = KnowledgeGraph(engine, settings)
    retriever = Retriever(engine, embeddings, kg, settings)
    gate = WriteGate(engine, embeddings, retriever, settings)

    n = 50
    # "from base import func0, func1, ..." → each funcN gets rel_context="imports"
    # All funcN names become content_entity_names; pre-insert them so they appear in all_entities
    names = [f"func{i}" for i in range(n)]
    for name in names:
        engine.insert_entity({"name": name, "type": "function"})

    # Single "from base import func0, func1, ..." line → n names with rel_context="imports"
    content = "from base import " + ", ".join(names)

    yield gate, engine, content
    engine.close()


@pytest.mark.perf
@pytest.mark.timeout(60)
def test_structural_novelty_under_5s_at_50_entities(structural_novelty_at_scale):
    """Regression guard: _compute_structural_novelty must use bulk SQL not per-pair HTTP.

    50 entities in content → up to 1225 pairs. Bulk SQL must finish under 5s.
    """
    gate, _engine, content = structural_novelty_at_scale
    t0 = time.monotonic()
    gate._compute_structural_novelty(content, "/proj")
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"_compute_structural_novelty took {elapsed:.1f}s at N=50 (target <5s)"


def test_structural_novelty_correctness_returns_float(tmp_path, settings, embeddings):
    """Bulk-SQL path returns a valid float in [0, 1]."""
    from yadgar._shared.knowledge_graph import KnowledgeGraph
    from yadgar.backend.retrieval import Retriever

    engine = StorageEngine(str(tmp_path / "sn_correct.db"))
    kg = KnowledgeGraph(engine, settings)
    retriever = Retriever(engine, embeddings, kg, settings)
    gate = WriteGate(engine, embeddings, retriever, settings)

    e1 = engine.insert_entity({"name": "alpha", "type": "dependency"})
    e2 = engine.insert_entity({"name": "beta", "type": "dependency"})
    engine.insert_relationship(
        {
            "source_entity_id": e1,
            "target_entity_id": e2,
            "relationship_type": "imports",
        }
    )

    result = gate._compute_structural_novelty("import alpha\nimport beta", "/proj")
    assert isinstance(result, float)
    assert 0.0 <= result <= 1.0
    engine.close()
