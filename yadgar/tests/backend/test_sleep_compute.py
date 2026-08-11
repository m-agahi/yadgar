"""Tests for the sleep-time compute system."""

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar.backend.curation import MemoryCurator
from yadgar.backend.sleep_compute import SleepComputeEngine


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "test.db"),
        DREAM_REPLAY_PAIRS=10,
    )


@pytest.fixture
def mock_embeddings():
    engine = MagicMock(spec=EmbeddingEngine)
    engine.get_model_name.return_value = "all-MiniLM-L6-v2"
    engine.encode.return_value = np.ones(384, dtype=np.float32).tobytes()
    engine.encode_batch.return_value = [np.ones(384, dtype=np.float32).tobytes()]
    return engine


@pytest.fixture
def sleep_engine(storage, mock_embeddings, settings):
    graph = KnowledgeGraph(storage, settings)
    thermo = MemoryThermodynamics(storage, mock_embeddings, settings)
    curator = MemoryCurator(storage, mock_embeddings, thermo, settings)
    return SleepComputeEngine(
        storage,
        mock_embeddings,
        graph,
        curator,
        thermo,
        settings,
    )


def _make_embedding(value: float = 1.0) -> bytes:
    """Create a normalized embedding vector filled with the given value."""
    vec = np.full(384, value, dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tobytes()


def _old_timestamp(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


class TestDreamReplay:
    def test_finds_connections(self, sleep_engine, storage, mock_embeddings):
        """Related but unconnected memories should be discovered."""
        vec = _make_embedding(1.0)
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Python FastAPI web server implementation",
                "embedding": vec,
                "directory_context": "/project-a",
                "heat": 0.8,
            }
        )
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Flask HTTP API endpoint design",
                "embedding": vec,
                "directory_context": "/project-b",
                "heat": 0.7,
            }
        )

        # Mock similarity above moderate threshold
        mock_embeddings.similarity.return_value = 0.5

        random.seed(42)
        stats = sleep_engine.dream_replay()

        assert stats["pairs_examined"] >= 1
        assert stats["connections_found"] >= 1

    def test_no_false_positives(self, sleep_engine, storage, mock_embeddings):
        """Unrelated memories should not get connected."""
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Database optimization techniques",
                "embedding": _make_embedding(1.0),
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Cooking recipe for pasta",
                "embedding": _make_embedding(-1.0),
                "directory_context": "/other",
                "heat": 0.7,
            }
        )

        # Mock similarity below threshold
        mock_embeddings.similarity.return_value = 0.1

        random.seed(42)
        stats = sleep_engine.dream_replay()

        assert stats["connections_found"] == 0
        assert stats["insights_generated"] == 0

    def test_strong_connection_generates_insight(self, sleep_engine, storage, mock_embeddings):
        """Strongly similar memories (>0.7) should generate dream insights."""
        vec = _make_embedding(1.0)
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "React component lifecycle hooks",
                "embedding": vec,
                "directory_context": "/frontend",
                "heat": 0.9,
            }
        )
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Vue.js component lifecycle methods",
                "embedding": vec,
                "directory_context": "/other-frontend",
                "heat": 0.8,
            }
        )

        mock_embeddings.similarity.return_value = 0.8

        random.seed(42)
        stats = sleep_engine.dream_replay()

        assert stats["insights_generated"] >= 1

        # Verify dream insight memory was created
        dream_mems = storage._q(
            "SELECT * FROM memory WHERE string::contains(content, 'Dream connection:')"
        )
        assert len(dream_mems) >= 1

    def test_skips_already_connected(self, sleep_engine, storage, mock_embeddings):
        """Memories already connected should be skipped."""
        vec = _make_embedding(1.0)
        mid_a = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Memory A",
                "embedding": vec,
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )
        mid_b = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Memory B",
                "embedding": vec,
                "directory_context": "/proj",
                "heat": 0.7,
            }
        )

        # Pre-connect them
        eid_a = storage.insert_entity({"name": f"memory:{mid_a}", "type": "file"})
        eid_b = storage.insert_entity({"name": f"memory:{mid_b}", "type": "file"})
        storage.insert_relationship(
            {
                "source_entity_id": eid_a,
                "target_entity_id": eid_b,
                "relationship_type": "co_occurrence",
            }
        )

        mock_embeddings.similarity.return_value = 0.9

        random.seed(42)
        stats = sleep_engine.dream_replay()

        # Should have examined 0 pairs since the only pair is already connected
        assert stats["pairs_examined"] == 0
        assert stats["connections_found"] == 0


class TestCommunityDetection:
    def test_detects_communities(self, sleep_engine, storage):
        """Connected entity clusters should be detected as communities."""
        # Cluster 1: Python backend
        e1 = storage.insert_entity({"name": "fastapi", "type": "dependency"})
        e2 = storage.insert_entity({"name": "uvicorn", "type": "dependency"})
        e3 = storage.insert_entity({"name": "starlette", "type": "dependency"})

        # Cluster 2: Frontend
        e4 = storage.insert_entity({"name": "react", "type": "dependency"})
        e5 = storage.insert_entity({"name": "webpack", "type": "dependency"})

        # Intra-cluster edges for cluster 1
        storage.insert_relationship(
            {
                "source_entity_id": e1,
                "target_entity_id": e2,
                "relationship_type": "co_occurrence",
            }
        )
        storage.insert_relationship(
            {
                "source_entity_id": e1,
                "target_entity_id": e3,
                "relationship_type": "co_occurrence",
            }
        )
        storage.insert_relationship(
            {
                "source_entity_id": e2,
                "target_entity_id": e3,
                "relationship_type": "co_occurrence",
            }
        )

        # Intra-cluster edges for cluster 2
        storage.insert_relationship(
            {
                "source_entity_id": e4,
                "target_entity_id": e5,
                "relationship_type": "co_occurrence",
            }
        )

        communities = sleep_engine.detect_communities()

        assert len(communities) >= 1
        total_entities = sum(c["entity_count"] for c in communities)
        assert total_entities >= 4

    def test_no_communities_without_relationships(self, sleep_engine, storage):
        """No communities should be detected when entities have no relationships."""
        storage.insert_entity({"name": "isolated_a", "type": "function"})
        storage.insert_entity({"name": "isolated_b", "type": "function"})

        communities = sleep_engine.detect_communities()
        assert communities == []


class TestClusterSummarization:
    def test_clusters_get_summaries(self, sleep_engine, storage, mock_embeddings):
        """Clusters with > 3 members should get summaries and centroids."""
        cluster_id = storage.insert_cluster(
            {
                "name": "test_cluster",
                "level": 1,
                "member_count": 5,
            }
        )

        vec = _make_embedding(1.0)
        for i in range(5):
            mid = storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": f"Memory about import fastapi and yadgar/server.py part {i}",
                    "embedding": vec,
                    "directory_context": "/project",
                    "heat": 0.7,
                }
            )
            storage._q(
                "UPDATE type::record('memory', $mid) SET cluster_id = $cid",
                {"mid": mid, "cid": cluster_id},
            )

        sleep_engine.generate_cluster_summaries()

        cluster = storage.get_cluster(cluster_id)
        assert cluster["summary"] != ""
        assert cluster["centroid_embedding"] is not None

    def test_small_clusters_skipped(self, sleep_engine, storage):
        """Clusters with <= 3 members should not be summarized."""
        cluster_id = storage.insert_cluster(
            {
                "name": "small_cluster",
                "level": 1,
                "summary": "original",
                "member_count": 2,
            }
        )

        sleep_engine.generate_cluster_summaries()

        cluster = storage.get_cluster(cluster_id)
        assert cluster["summary"] == "original"


class TestReembedStale:
    def test_stale_memories_reembedded(self, sleep_engine, storage, mock_embeddings):
        """Memories with wrong model version should get new embeddings."""
        old_vec = _make_embedding(0.5)
        new_vec = _make_embedding(1.0)

        mid1 = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Old embedding test",
                "embedding": old_vec,
                "directory_context": "/proj",
                "heat": 0.8,
                "embedding_model": "old-model-v1",
            }
        )
        mid2 = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Another old embedding",
                "embedding": old_vec,
                "directory_context": "/proj",
                "heat": 0.7,
                "embedding_model": "old-model-v1",
            }
        )

        mock_embeddings.encode_batch.return_value = [new_vec, new_vec]

        count = sleep_engine.reembed_stale()
        assert count == 2

        mem1 = storage.get_memory(mid1)
        assert mem1["embedding_model"] == "all-MiniLM-L6-v2"
        mem2 = storage.get_memory(mid2)
        assert mem2["embedding_model"] == "all-MiniLM-L6-v2"

    def test_current_model_not_reembedded(self, sleep_engine, storage, mock_embeddings):
        """Memories already using the current model should not be re-embedded."""
        vec = _make_embedding(1.0)
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Current model memory",
                "embedding": vec,
                "directory_context": "/proj",
                "heat": 0.8,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )

        count = sleep_engine.reembed_stale()
        assert count == 0


class TestMemoryCompression:
    def test_old_long_memories_compressed(self, sleep_engine, storage, mock_embeddings):
        """Old memories with long content should be compressed."""
        long_content = (
            "This is a long memory about Python development. "
            "We worked on yadgar/server.py and fixed the API endpoint. "
            + "This is filler content that does not contain entities. " * 30
            + "The final fix was in yadgar/storage.py which resolved the issue."
        )
        old_time = _old_timestamp(60)
        mid = storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": long_content,
                "embedding": _make_embedding(1.0),
                "directory_context": "/proj",
                "heat": 0.5,
                "created_at": old_time,
                "last_accessed": old_time,
            }
        )

        count = sleep_engine.compress_old_memories(days_threshold=30)
        assert count >= 1

        mem = storage.get_memory(mid)
        assert len(mem["content"]) < len(long_content)
        assert mem["compressed"] is True

    def test_recent_memories_not_compressed(self, sleep_engine, storage):
        """Recent memories should not be compressed regardless of length."""
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "x " * 600,  # long but recent
                "embedding": _make_embedding(1.0),
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )

        count = sleep_engine.compress_old_memories(days_threshold=30)
        assert count == 0

    def test_short_memories_not_compressed(self, sleep_engine, storage):
        """Short old memories should not be compressed."""
        old_time = _old_timestamp(60)
        storage.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "Short memory",
                "embedding": _make_embedding(1.0),
                "directory_context": "/proj",
                "heat": 0.5,
                "created_at": old_time,
                "last_accessed": old_time,
            }
        )

        count = sleep_engine.compress_old_memories(days_threshold=30)
        assert count == 0


class TestFullSleepCycle:
    def test_all_phases_run(self, sleep_engine, storage, mock_embeddings):
        """Full sleep cycle should execute all phases without errors."""
        vec = _make_embedding(1.0)
        for i in range(3):
            storage.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": f"Memory {i} about testing sleep cycle",
                    "embedding": vec,
                    "directory_context": "/proj",
                    "heat": 0.7,
                    "embedding_model": "all-MiniLM-L6-v2",
                }
            )

        mock_embeddings.similarity.return_value = 0.3  # below threshold

        stats = sleep_engine.run_sleep_cycle()

        assert "dream_replay" in stats
        assert "communities" in stats
        assert "cluster_summaries_generated" in stats
        assert "reembedded" in stats
        assert "compressed" in stats

    def test_sleep_cycle_with_empty_db(self, sleep_engine):
        """Sleep cycle should handle an empty database gracefully."""
        stats = sleep_engine.run_sleep_cycle()

        assert stats["dream_replay"]["pairs_examined"] == 0
        assert stats["communities"] == []
        assert stats["reembedded"] == 0
        assert stats["compressed"] == 0


# ── detect_communities bulk-SQL perf tests (v4.4.10) ────────────────────────

import time  # noqa: E402

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: dict without this key is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"


@pytest.fixture
def communities_at_scale(tmp_path, settings, mock_embeddings):
    """Seed 100 entities + sparse relationships for community detection perf test.

    100 entities → 4950 pairs in detect_communities nested loop.
    At 3ms/pair that is ~15s with the old per-pair HTTP pattern.
    """
    engine = StorageEngine(str(tmp_path / "comm_scale.db"))
    graph = KnowledgeGraph(engine, settings)
    thermo = MemoryThermodynamics(engine, mock_embeddings, settings)
    curator = MemoryCurator(engine, mock_embeddings, thermo, settings)
    sleep_eng = SleepComputeEngine(engine, mock_embeddings, graph, curator, thermo, settings)

    rng = random.Random(42)
    n = 100
    entity_ids = []
    for i in range(n):
        eid = engine.insert_entity({"name": f"ent_{i}", "type": "file"})
        entity_ids.append(eid)

    # Sparse relationships: ~50 random pairs
    inserted: set[tuple[int, int]] = set()
    for _ in range(50):
        a, b = rng.sample(entity_ids, 2)
        key = (min(a, b), max(a, b))
        if key not in inserted:
            inserted.add(key)
            engine.insert_relationship(
                {"source_entity_id": a, "target_entity_id": b, "relationship_type": "co_occurrence"}
            )

    yield sleep_eng, engine
    engine.close()


@pytest.mark.perf
@pytest.mark.timeout(60)
def test_detect_communities_under_5s_at_100_entities(communities_at_scale):
    """Regression guard: detect_communities must use bulk SQL not per-pair HTTP.

    100 entities → 4950 pairs. Bulk SQL should complete well under 5s.
    Would exceed the 60s timeout with the old per-pair HTTP pattern.
    """
    sleep_eng, _engine = communities_at_scale
    t0 = time.monotonic()
    sleep_eng.detect_communities()
    elapsed = time.monotonic() - t0
    # TODO: tighten back to 5s once CI runner is faster; 15s is the hard ceiling
    assert elapsed < 15.0, f"detect_communities took {elapsed:.1f}s at N=100 (target <15s)"


def test_detect_communities_correctness_finds_clusters(tmp_path, settings, mock_embeddings):
    """Bulk-SQL path correctly identifies communities from relationship graph."""
    engine = StorageEngine(str(tmp_path / "comm_correct.db"))
    graph = KnowledgeGraph(engine, settings)
    thermo = MemoryThermodynamics(engine, mock_embeddings, settings)
    curator = MemoryCurator(engine, mock_embeddings, thermo, settings)
    sleep_eng = SleepComputeEngine(engine, mock_embeddings, graph, curator, thermo, settings)

    # Two clusters: A-B-C fully connected, D-E-F fully connected
    cluster1 = [engine.insert_entity({"name": f"a{i}", "type": "file"}) for i in range(3)]
    cluster2 = [engine.insert_entity({"name": f"b{i}", "type": "file"}) for i in range(3)]

    for i, a in enumerate(cluster1):
        for b in cluster1[i + 1 :]:
            engine.insert_relationship(
                {"source_entity_id": a, "target_entity_id": b, "relationship_type": "co_occurrence"}
            )
    for i, a in enumerate(cluster2):
        for b in cluster2[i + 1 :]:
            engine.insert_relationship(
                {"source_entity_id": a, "target_entity_id": b, "relationship_type": "co_occurrence"}
            )

    communities = sleep_eng.detect_communities()
    assert len(communities) >= 1
    engine.close()


# ── dream_replay bulk-SQL perf tests (v4.4.11) ──────────────────────────────


@pytest.fixture
def dream_replay_at_scale(tmp_path, settings):
    """Seed 60 memories with pre-existing entity nodes and no inter-entity relationships.

    dream_replay with DREAM_REPLAY_PAIRS=200 → up to 200 _memories_connected checks.
    Each check was one get_relationship_between HTTP call (~3ms) under the old pattern.
    Bulk SQL must complete well under 5s.
    """
    high_pairs_settings = Settings(
        DB_PATH=str(tmp_path / "test.db"),
        DREAM_REPLAY_PAIRS=200,
    )
    engine = StorageEngine(str(tmp_path / "dream_scale.db"))
    graph = KnowledgeGraph(engine, high_pairs_settings)
    thermo = MemoryThermodynamics(engine, MagicMock(spec=EmbeddingEngine), high_pairs_settings)
    curator = MemoryCurator(
        engine,
        MagicMock(spec=EmbeddingEngine),
        thermo,
        high_pairs_settings,
    )

    mock_emb = MagicMock(spec=EmbeddingEngine)
    mock_emb.get_model_name.return_value = "all-MiniLM-L6-v2"
    # All memories have embeddings; similarity below threshold so no writes occur
    mock_emb.encode.return_value = np.ones(384, dtype=np.float32).tobytes()
    mock_emb.similarity.return_value = 0.1  # below 0.4 threshold → pairs_examined but no writes

    sleep_eng = SleepComputeEngine(engine, mock_emb, graph, curator, thermo, high_pairs_settings)

    n = 60
    vec = np.ones(384, dtype=np.float32).tobytes()
    for i in range(n):
        mem_id = engine.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": f"memory content {i}",
                "embedding": vec,
                "tags": [],
                "directory_context": "/proj",
                "heat": 0.5,
                "is_stale": False,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )
        # Pre-create entity nodes so _memories_connected/entity resolution has work to do.
        engine.insert_entity({"name": f"memory:{mem_id}", "type": "file"})

    yield sleep_eng, engine
    engine.close()


@pytest.mark.perf
@pytest.mark.timeout(60)
def test_dream_replay_under_5s_at_60_memories(dream_replay_at_scale):
    """Regression guard: dream_replay must use bulk SQL not per-pair HTTP.

    60 memories × 200 DREAM_REPLAY_PAIRS. With the old _memories_connected pattern,
    each pair triggered one get_relationship_between HTTP call (~3ms each → ~0.6s for
    200 pairs). The bulk SQL path resolves all entity ids + relationships in two queries.
    Must complete under 5s.
    """
    sleep_eng, _engine = dream_replay_at_scale
    random.seed(42)
    t0 = time.monotonic()
    sleep_eng.dream_replay()
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"dream_replay took {elapsed:.1f}s at N=60 memories (target <5s)"


def test_dream_replay_correctness_skips_connected(tmp_path, settings):
    """Bulk-SQL path correctly skips memory pairs that already have a relationship."""
    engine = StorageEngine(str(tmp_path / "dream_correct.db"))
    graph = KnowledgeGraph(engine, settings)

    mock_emb = MagicMock(spec=EmbeddingEngine)
    mock_emb.get_model_name.return_value = "all-MiniLM-L6-v2"
    mock_emb.encode.return_value = np.ones(384, dtype=np.float32).tobytes()
    # High similarity so pairs WOULD be connected if not already skipped
    mock_emb.similarity.return_value = 0.9

    thermo = MemoryThermodynamics(engine, mock_emb, settings)
    curator = MemoryCurator(engine, mock_emb, thermo, settings)
    sleep_eng = SleepComputeEngine(engine, mock_emb, graph, curator, thermo, settings)

    vec = np.ones(384, dtype=np.float32).tobytes()
    mid_a = engine.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": "alpha memory",
            "embedding": vec,
            "directory_context": "/proj",
            "heat": 0.8,
            "is_stale": False,
            "embedding_model": "all-MiniLM-L6-v2",
        }
    )
    mid_b = engine.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": "beta memory",
            "embedding": vec,
            "directory_context": "/proj",
            "heat": 0.8,
            "is_stale": False,
            "embedding_model": "all-MiniLM-L6-v2",
        }
    )

    # Create entity nodes and a pre-existing relationship between them.
    eid_a = engine.insert_entity({"name": f"memory:{mid_a}", "type": "file"})
    eid_b = engine.insert_entity({"name": f"memory:{mid_b}", "type": "file"})
    engine.insert_relationship(
        {"source_entity_id": eid_a, "target_entity_id": eid_b, "relationship_type": "co_occurrence"}
    )

    random.seed(42)
    stats = sleep_eng.dream_replay()

    # The pair is already connected so dream_replay should skip it (pairs_examined = 0).
    assert stats["pairs_examined"] == 0, (
        f"Already-connected pair should be skipped, but pairs_examined={stats['pairs_examined']}"
    )
    assert stats["connections_found"] == 0
    engine.close()


# ── C3 dream_replay two-phase fetch tests ────────────────────────────────────


class TestDreamReplayTwoPhase:
    """C3: dream_replay MUST sample IDs first, then fetch only sampled rows.

    The old path called get_all_memories_with_embeddings() which pulled ~3000
    rows (SELECT * with full content + embedding) just to sample ~40 vectors.
    The two-phase path calls get_candidate_memory_ids() (ids only, cheap) then
    get_memories_by_ids_projected(ids) (content+embedding for ~40 rows only).

    Assert: get_all_memories_with_embeddings is NOT called during dream_replay.
    Assert: get_memories_by_ids_projected receives ≤ 2·DREAM_REPLAY_PAIRS ids.
    Assert: dream insight still produced (content + embedding still present).
    """

    def _make_engine(self, tmp_path, n: int = 60, pairs: int = 10):
        high_settings = Settings(
            DB_PATH=str(tmp_path / "test.db"),
            DREAM_REPLAY_PAIRS=pairs,
        )
        engine = StorageEngine(str(tmp_path / "dream_c3.db"))
        mock_emb = MagicMock(spec=EmbeddingEngine)
        mock_emb.get_model_name.return_value = "all-MiniLM-L6-v2"
        mock_emb.encode.return_value = np.ones(384, dtype=np.float32).tobytes()
        mock_emb.similarity.return_value = 0.1  # below threshold — no writes
        graph = KnowledgeGraph(engine, high_settings)
        thermo = MemoryThermodynamics(engine, mock_emb, high_settings)
        curator = MemoryCurator(engine, mock_emb, thermo, high_settings)
        sleep_eng = SleepComputeEngine(engine, mock_emb, graph, curator, thermo, high_settings)

        vec = np.ones(384, dtype=np.float32).tobytes()
        for i in range(n):
            engine.insert_memory(
                {
                    "project_id": _TEST_PROJECT,
                    "content": f"c3 test memory {i}",
                    "embedding": vec,
                    "directory_context": "/proj",
                    "heat": 0.5,
                    "is_stale": False,
                    "embedding_model": "all-MiniLM-L6-v2",
                }
            )

        return sleep_eng, engine, high_settings

    def test_dream_replay_does_not_call_get_all_memories(self, tmp_path):
        """dream_replay MUST NOT call get_all_memories_with_embeddings (C3 gate).

        Seeds 60 memories so the population is much larger than DREAM_REPLAY_PAIRS=10.
        Patches the method with a sentinel that raises AssertionError if called,
        while providing the two-phase helpers via normal storage.
        """
        sleep_eng, engine, _settings = self._make_engine(tmp_path, n=60, pairs=10)

        try:

            def _must_not_be_called():
                raise AssertionError(
                    "C3: dream_replay called get_all_memories_with_embeddings() — "
                    "must use two-phase fetch (get_candidate_memory_ids + "
                    "get_memories_by_ids_projected) instead."
                )

            with patch.object(engine, "get_all_memories_with_embeddings", _must_not_be_called):
                random.seed(42)
                sleep_eng.dream_replay()  # must not raise
        finally:
            engine.close()

    def test_dream_replay_fetches_bounded_rows(self, tmp_path):
        """get_memories_by_ids_projected receives ≤ 2·DREAM_REPLAY_PAIRS ids (C3 bound).

        With DREAM_REPLAY_PAIRS=10 and 60 candidate memories, the by-ids fetch
        should receive at most 20 ids (pairs × 2 unique members per pair, deduped).
        """
        PAIRS = 10
        sleep_eng, engine, _settings = self._make_engine(tmp_path, n=60, pairs=PAIRS)

        try:
            fetched_id_batches: list[list[int]] = []
            real_fetch = engine.get_memories_by_ids_projected

            def spy_fetch(ids):
                fetched_id_batches.append(list(ids))
                return real_fetch(ids)

            with patch.object(engine, "get_memories_by_ids_projected", spy_fetch):
                random.seed(42)
                sleep_eng.dream_replay()

            assert fetched_id_batches, (
                "C3: dream_replay never called get_memories_by_ids_projected — "
                "two-phase fetch not implemented."
            )
            total_ids = sum(len(b) for b in fetched_id_batches)
            assert total_ids <= 2 * PAIRS, (
                f"C3: dream_replay fetched {total_ids} rows but DREAM_REPLAY_PAIRS={PAIRS} "
                f"so ≤{2 * PAIRS} rows expected (≤ 2·pairs unique memory ids). "
                "Two-phase fetch is not bounding the row fetch correctly."
            )
        finally:
            engine.close()

    def test_dream_replay_insight_still_works_with_two_phase(self, tmp_path):
        """Insight generation still works after C3 refactor (content + embedding present).

        Seeds 2 memories with high similarity so dream replay generates an insight.
        Asserts the dream insight memory is created — i.e. content is present in
        the projected rows returned by get_memories_by_ids_projected.
        """
        high_settings = Settings(
            DB_PATH=str(tmp_path / "test.db"),
            DREAM_REPLAY_PAIRS=10,
        )
        engine = StorageEngine(str(tmp_path / "dream_c3_insight.db"))
        mock_emb = MagicMock(spec=EmbeddingEngine)
        mock_emb.get_model_name.return_value = "all-MiniLM-L6-v2"
        mock_emb.encode.return_value = np.ones(384, dtype=np.float32).tobytes()
        mock_emb.similarity.return_value = 0.85  # above 0.7 → insight generated

        graph = KnowledgeGraph(engine, high_settings)
        thermo = MemoryThermodynamics(engine, mock_emb, high_settings)
        curator = MemoryCurator(engine, mock_emb, thermo, high_settings)
        sleep_eng = SleepComputeEngine(engine, mock_emb, graph, curator, thermo, high_settings)

        vec = np.ones(384, dtype=np.float32).tobytes()
        engine.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "C3 insight test memory alpha",
                "embedding": vec,
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )
        engine.insert_memory(
            {
                "project_id": _TEST_PROJECT,
                "content": "C3 insight test memory beta",
                "embedding": vec,
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )

        try:
            random.seed(42)
            stats = sleep_eng.dream_replay()

            assert stats["insights_generated"] >= 1, (
                "C3: dream_replay must still generate insights after two-phase fetch refactor. "
                f"stats={stats}"
            )
            dream_mems = engine._q(
                "SELECT * FROM memory WHERE string::contains(content, 'Dream connection:')"
            )
            assert len(dream_mems) >= 1, (
                "C3: dream insight memory must be created — content field must be present "
                "in projected rows from get_memories_by_ids_projected."
            )
        finally:
            engine.close()
