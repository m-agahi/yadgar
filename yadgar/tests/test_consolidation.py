import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from yadgar.config import Settings
from yadgar.consolidation import ConsolidationScheduler
from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_consolidation.db"))
    yield engine
    engine.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "test.db"),
        IDLE_THRESHOLD_SECONDS=1,
        DECAY_FACTOR=0.95,
        COLD_THRESHOLD=0.05,
        DAEMON_CHECK_INTERVAL=1,
    )


@pytest.fixture
def embeddings():
    engine = EmbeddingEngine()
    engine._unavailable = True  # don't load real model in tests
    return engine


@pytest.fixture
def engine(storage, embeddings, settings):
    return ConsolidationScheduler(storage, embeddings, settings)


def _hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


class TestDecayApplication:
    def test_memory_heat_decreases(self, engine, storage):
        mid = storage.insert_memory(
            {
                "content": "decay test",
                "directory_context": "/proj",
                "heat": 1.0,
                "last_accessed": _hours_ago(24),
            }
        )
        engine.force_consolidate()
        mem = storage.get_memory(mid)
        # Enhanced decay: default confidence=1.0 slows decay slightly
        # effective_factor = 1.0 - (1.0 - 0.95) / (1.0 + 1.0 * 0.1)
        effective_factor = 1.0 - (1.0 - 0.95) / 1.1
        expected = 1.0 * (effective_factor**24)
        assert mem["heat"] == pytest.approx(expected, abs=1e-4)

    def test_entity_heat_decreases(self, engine, storage):
        storage.insert_entity(
            {
                "name": "test_func",
                "type": "function",
                "heat": 1.0,
                "last_accessed": _hours_ago(10),
            }
        )
        engine.force_consolidate()
        ent = storage.get_entity_by_name("test_func")
        expected = 1.0 * (0.95**10)
        assert ent["heat"] == pytest.approx(expected, abs=1e-4)

    def test_recent_memory_barely_decays(self, engine, storage):
        mid = storage.insert_memory(
            {
                "content": "fresh memory",
                "directory_context": "/proj",
                "heat": 1.0,
                "last_accessed": _hours_ago(0.01),
            }
        )
        engine.force_consolidate()
        mem = storage.get_memory(mid)
        assert mem["heat"] > 0.99


class TestColdArchival:
    def test_memory_archived_below_threshold(self, engine, storage):
        # 0.1 * 0.95^60 ≈ 0.0046, well below 0.05
        mid = storage.insert_memory(
            {
                "content": "old memory",
                "directory_context": "/proj",
                "heat": 0.1,
                "last_accessed": _hours_ago(60),
            }
        )
        engine.force_consolidate()
        mem = storage.get_memory(mid)
        assert mem["heat"] == 0.0

    def test_entity_archived_below_threshold(self, engine, storage):
        storage.insert_entity(
            {
                "name": "old_func",
                "type": "function",
                "heat": 0.1,
                "last_accessed": _hours_ago(60),
            }
        )
        engine.force_consolidate()
        ent = storage.get_entity_by_name("old_func")
        assert ent["heat"] == 0.0
        assert ent["archived"] is True

    def test_hot_memory_not_archived(self, engine, storage):
        mid = storage.insert_memory(
            {
                "content": "hot memory",
                "directory_context": "/proj",
                "heat": 0.9,
                "last_accessed": _hours_ago(1),
            }
        )
        engine.force_consolidate()
        mem = storage.get_memory(mid)
        assert mem["heat"] > 0.05

    def test_action_stream_archived_at_higher_threshold(self, storage, embeddings, tmp_path):
        """Action stream memories use ACTION_STREAM_COLD_THRESHOLD, not global COLD_THRESHOLD.

        A memory at heat=0.08 should be archived when ACTION_STREAM_COLD_THRESHOLD=0.1
        but a normal memory at the same heat should survive when COLD_THRESHOLD=0.02.
        """
        settings = Settings(
            DB_PATH=str(tmp_path / "test_pertype.db"),
            IDLE_THRESHOLD_SECONDS=1,
            DECAY_FACTOR=0.95,
            COLD_THRESHOLD=0.02,
            ACTION_STREAM_COLD_THRESHOLD=0.1,
            DAEMON_CHECK_INTERVAL=1,
        )
        engine = ConsolidationScheduler(storage, embeddings, settings)

        action_mid = storage.insert_memory(
            {
                "content": "Session activity [Bash(3)]: 3 tool calls",
                "directory_context": "/proj",
                "tags": ["_action_stream", "_auto"],
                "heat": 0.08,
                "last_accessed": _hours_ago(0),
            }
        )
        normal_mid = storage.insert_memory(
            {
                "content": "important architectural decision",
                "directory_context": "/proj",
                "heat": 0.08,
                "last_accessed": _hours_ago(0),
            }
        )

        engine.force_consolidate()

        assert storage.get_memory(action_mid)["heat"] == 0.0, (
            "action stream memory above ACTION_STREAM_COLD_THRESHOLD should be archived"
        )
        assert storage.get_memory(normal_mid)["heat"] > 0.0, (
            "normal memory above COLD_THRESHOLD should NOT be archived"
        )

    def test_global_cold_threshold_archives_normal_memory(self, storage, embeddings, tmp_path):
        """Normal memories are archived when heat drops below COLD_THRESHOLD=0.02."""
        settings = Settings(
            DB_PATH=str(tmp_path / "test_global_cold.db"),
            IDLE_THRESHOLD_SECONDS=1,
            DECAY_FACTOR=0.95,
            COLD_THRESHOLD=0.02,
            ACTION_STREAM_COLD_THRESHOLD=0.1,
            DAEMON_CHECK_INTERVAL=1,
        )
        engine = ConsolidationScheduler(storage, embeddings, settings)

        mid = storage.insert_memory(
            {
                "content": "rarely accessed fact",
                "directory_context": "/proj",
                "heat": 0.01,
                "last_accessed": _hours_ago(0),
            }
        )

        engine.force_consolidate()

        assert storage.get_memory(mid)["heat"] == 0.0, (
            "memory below COLD_THRESHOLD should be archived to heat=0.0"
        )


class TestEntityExtraction:
    def test_extracts_file_paths(self, engine, storage):
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "Edited yadgar/server.py and tests/test_server.py",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()
        assert storage.get_entity_by_name("yadgar/server.py") is not None
        assert storage.get_entity_by_name("tests/test_server.py") is not None

    def test_extracts_function_defs(self, engine, storage):
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "def process_items():\n    pass\nclass DataProcessor:\n    pass",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()
        assert storage.get_entity_by_name("process_items") is not None
        assert storage.get_entity_by_name("DataProcessor") is not None

    def test_extracts_imports(self, engine, storage):
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "import os\nfrom pathlib import Path\n",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()
        assert storage.get_entity_by_name("os") is not None
        assert storage.get_entity_by_name("pathlib") is not None

    def test_extracts_errors(self, engine, storage):
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "Traceback (most recent call last)\nValueError: invalid input",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()
        assert storage.get_entity_by_name("Traceback") is not None
        assert storage.get_entity_by_name("ValueError") is not None

    def test_extracts_js_require(self, engine, storage):
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "const express = require('express')",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()
        assert storage.get_entity_by_name("express") is not None

    def test_reinforces_existing_entity(self, engine, storage):
        storage.insert_entity(
            {
                "name": "my_func",
                "type": "function",
                "heat": 0.5,
                "last_accessed": _hours_ago(10),
            }
        )
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "def my_func():\n    pass",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()
        ent = storage.get_entity_by_name("my_func")
        # Heat was decayed from 0.5 then reinforced by +0.1
        # Decay alone: 0.5 * 0.95^10 ≈ 0.299, after reinforce ≈ 0.399
        # The exact value depends on ordering, but it should be > decayed value
        decayed_only = 0.5 * (0.95**10)
        assert ent["heat"] > decayed_only


class TestRelationshipBuilding:
    def test_cooccurring_entities_get_relationship(self, engine, storage):
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "def handler():\n    pass\nimport flask",
            }
        )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()

        e1 = storage.get_entity_by_name("handler")
        e2 = storage.get_entity_by_name("flask")
        assert e1 is not None
        assert e2 is not None
        rel = storage.get_relationship_between(e1["id"], e2["id"])
        assert rel is not None
        assert rel["relationship_type"] == "co_occurrence"

    def test_repeated_cooccurrence_increases_weight(self, engine, storage):
        for i in range(3):
            storage.insert_episode(
                {
                    "session_id": f"sess{i}",
                    "directory": "/proj",
                    "raw_content": "def parse():\nimport json",
                }
            )

        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()

        e1 = storage.get_entity_by_name("parse")
        e2 = storage.get_entity_by_name("json")
        # Query the co_occurrence rel specifically — once its weight hits
        # CAUSAL_THRESHOLD the graph also derives a `caused_by` rel between the
        # same pair, so the type-agnostic get_relationship_between is ambiguous.
        # co_occurrence is symmetric; the stored direction depends on extraction
        # order, so check both.
        rel = storage.get_typed_relationship(
            e1["id"], e2["id"], "co_occurrence"
        ) or storage.get_typed_relationship(e2["id"], e1["id"], "co_occurrence")
        assert rel is not None
        # First episode creates at weight 1.0, next two reinforce by +1.0 each
        assert rel["weight"] == pytest.approx(3.0)


class TestDuplicateMerge:
    def test_near_identical_memories_merged(self, storage, settings):
        # Use a mock embeddings engine that reports high similarity
        mock_emb = MagicMock(spec=EmbeddingEngine)
        mock_emb.similarity.return_value = 0.98

        vec_a = np.ones(384, dtype=np.float32).tobytes()
        vec_b = np.ones(384, dtype=np.float32).tobytes()

        id_a = storage.insert_memory(
            {
                "content": "how to configure the database",
                "embedding": vec_a,
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )
        id_b = storage.insert_memory(
            {
                "content": "how to configure the database connection",
                "embedding": vec_b,
                "directory_context": "/proj",
                "heat": 0.5,
            }
        )

        engine = ConsolidationScheduler(storage, mock_emb, settings)
        engine.force_consolidate()

        # Higher-heat memory survives
        assert storage.get_memory(id_a) is not None
        assert storage.get_memory(id_b) is None

    def test_dissimilar_memories_kept(self, storage, settings):
        # Note: the mock is vestigial after the numpy rewrite — the new path computes
        # cosine directly from embeddings. vec_a and vec_b must be genuinely dissimilar
        # vectors so cosine similarity is well below 0.95.
        mock_emb = MagicMock(spec=EmbeddingEngine)
        mock_emb.similarity.return_value = 0.4

        # vec_a = all-ones; vec_b = [1.0, 0.0, 0.0, ...] — cosine ≈ 1/sqrt(384) ≈ 0.051
        vec_a = np.ones(384, dtype=np.float32).tobytes()
        unit_b = np.zeros(384, dtype=np.float32)
        unit_b[0] = 1.0
        vec_b = unit_b.tobytes()

        id_a = storage.insert_memory(
            {
                "content": "database configuration",
                "embedding": vec_a,
                "directory_context": "/proj",
                "heat": 0.8,
            }
        )
        id_b = storage.insert_memory(
            {
                "content": "hiking trail map",
                "embedding": vec_b,
                "directory_context": "/proj",
                "heat": 0.5,
            }
        )

        engine = ConsolidationScheduler(storage, mock_emb, settings)
        engine.force_consolidate()

        assert storage.get_memory(id_a) is not None
        assert storage.get_memory(id_b) is not None


class TestDaemonLifecycle:
    def test_starts_and_stops(self, engine):
        engine.start()
        assert engine.is_running is True
        assert engine._thread is not None
        assert engine._thread.is_alive()

        engine.stop()
        assert engine.is_running is False
        assert engine._thread is None

    def test_double_start_is_noop(self, engine):
        engine.start()
        thread1 = engine._thread
        engine.start()
        assert engine._thread is thread1
        engine.stop()

    def test_stop_without_start(self, engine):
        engine.stop()  # should not raise
        assert engine.is_running is False


class TestActivityTracking:
    def test_record_activity_updates_timestamp(self, engine):
        old = engine.last_activity
        time.sleep(0.01)
        engine.record_activity()
        assert engine.last_activity > old

    def test_idle_detection(self, engine, storage):
        # Set last_activity far in the past so the daemon considers us idle
        engine.last_activity = datetime.now(UTC) - timedelta(seconds=600)
        # Insert an episode so consolidation has something to do
        storage.insert_episode(
            {
                "session_id": "sess1",
                "directory": "/proj",
                "raw_content": "def idle_test(): pass",
            }
        )
        engine.start()
        engine._last_consolidated_episode_id = 0
        # Poll until the daemon loop fires and extracts the entity (up to 2 s)
        for _ in range(20):
            if storage.get_entity_by_name("idle_test") is not None:
                break
            time.sleep(0.1)
        engine.stop()

        # Entity should have been extracted during idle consolidation
        assert storage.get_entity_by_name("idle_test") is not None


class TestConsolidationLog:
    def test_log_entry_created(self, engine, storage):
        engine.force_consolidate()
        stats = storage.get_memory_stats()
        assert stats["last_consolidation"] is not None

    def test_force_consolidate_returns_stats(self, engine):
        result = engine.force_consolidate()
        assert "memories_added" in result
        assert "memories_updated" in result
        assert "memories_archived" in result
        assert "memories_deleted" in result


# ── process_episodes bulk-SQL perf tests (v4.4.10) ──────────────────────────


@pytest.fixture
def process_episodes_at_scale(tmp_path, settings):
    """Build one episode that extracts 50 entities via import statements.

    50 entities → 1225 pairs. At 3ms/pair that's ~3.7s with the old per-pair
    HTTP pattern; bulk SQL should finish well under 5s.
    Uses import statements so _extract_entities() actually finds them.
    """
    engine = StorageEngine(str(tmp_path / "pe_scale.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    sched = ConsolidationScheduler(engine, emb, settings)

    n = 50
    # "import mod_0" ... "import mod_49" — each matches _IMPORT_RE
    lines = [f"import mod_{i}" for i in range(n)]
    raw = "\n".join(lines)
    engine.insert_episode({"session_id": "perf_test", "directory": "/proj", "raw_content": raw})

    yield sched, engine
    engine.close()


@pytest.mark.timeout(60)
def test_process_episodes_under_10s_at_50_entities(process_episodes_at_scale):
    """Regression guard for the O(N²) per-pair HTTP bug fixed in v4.4.10.

    50 import statements → 50 entities → 1225 pairs. At 3ms/pair that is ~3.7s
    broken (before batch writes, >60s with the old per-pair pattern).
    Bulk SQL + batched writes must finish under 10s total.
    """
    sched, _storage = process_episodes_at_scale
    sched._last_consolidated_episode_id = 0
    t0 = time.monotonic()
    sched._process_new_episodes({"episodes_processed": 0})
    elapsed = time.monotonic() - t0
    assert elapsed < 10.0, f"_process_new_episodes took {elapsed:.1f}s at N=50 (target <10s)"


def test_process_episodes_correctness_relationships_created(tmp_path, settings):
    """Bulk-SQL path creates co_occurrence relationships for co-occurring entities."""
    engine = StorageEngine(str(tmp_path / "pe_correct.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    sched = ConsolidationScheduler(engine, emb, settings)

    engine.insert_episode(
        {
            "session_id": "s1",
            "directory": "/proj",
            "raw_content": "def alpha():\nimport beta",
        }
    )
    sched._last_consolidated_episode_id = 0
    sched._process_new_episodes({"episodes_processed": 0})

    e_alpha = engine.get_entity_by_name("alpha")
    e_beta = engine.get_entity_by_name("beta")
    assert e_alpha is not None
    assert e_beta is not None
    rel = engine.get_relationship_between(e_alpha["id"], e_beta["id"])
    assert rel is not None
    assert rel["relationship_type"] == "co_occurrence"
    engine.close()


# ── _merge_duplicates numpy-vectorised perf tests (v4.4.10) ─────────────────


def _make_merge_duplicates_storage(tmp_path, n: int, high_sim_pairs: int = 5):
    """Create a StorageEngine with n memories.

    The first `high_sim_pairs * 2` memories are near-identical pairs (sim ≈ 1.0)
    so the merge path actually has work to do. The remainder are orthogonal-ish
    vectors so they stay below the 0.95 threshold.
    """
    rng = np.random.default_rng(42)
    engine = StorageEngine(str(tmp_path / "merge_scale.db"))

    for i in range(n):
        if i < high_sim_pairs * 2 and i % 2 == 1:
            # Make this embedding nearly identical to the previous one (cosine ≈ 1.0)
            # by copying and adding tiny noise
            base = rng.standard_normal(384).astype(np.float32)
            base /= np.linalg.norm(base)
            noise = rng.standard_normal(384).astype(np.float32) * 1e-4
            vec = base + noise
        else:
            vec = rng.standard_normal(384).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        engine.insert_memory(
            {
                "content": f"memory content {i}",
                "embedding": vec.tobytes(),
                "directory_context": "/proj",
                "heat": float(rng.uniform(0.1, 1.0)),
            }
        )

    return engine


@pytest.mark.timeout(60)
def test_merge_duplicates_under_5s_at_500_memories_with_embeddings(tmp_path, settings):
    """Regression guard for the O(N²) Python-loop cosine sim in _merge_duplicates.

    500 memories → 124 750 pairs. The legacy path calls EmbeddingEngine.similarity()
    per pair (~125k calls, each doing numpy frombuffer + dot product).
    The vectorised numpy path must finish well under 5s.
    """
    storage = _make_merge_duplicates_storage(tmp_path, n=500, high_sim_pairs=5)
    emb = EmbeddingEngine()
    emb._unavailable = True
    sched = ConsolidationScheduler(storage, emb, settings)

    t0 = time.monotonic()
    stats: dict = {"memories_deleted": 0}
    sched._merge_duplicates(stats)
    elapsed = time.monotonic() - t0
    storage.close()

    assert elapsed < 5.0, f"_merge_duplicates took {elapsed:.2f}s at N=500 (target <5s)"


def test_merge_duplicates_correctness_near_dup_pairs_deleted(tmp_path, settings):
    """Vectorised _merge_duplicates deletes the lower-heat duplicate from each near-dup pair.

    Inserts 3 near-identical pairs (cosine ≈ 1.0, noise ≈ 1e-5) and 4 dissimilar
    singletons. Asserts that:
    - exactly 3 memories are deleted (one per pair — the lower-heat one)
    - the surviving members are the higher-heat ones from each pair
    - the 4 singletons all survive
    """
    rng = np.random.default_rng(7)
    storage = StorageEngine(str(tmp_path / "merge_correct.db"))

    # Build 3 near-dup pairs with known heat ordering
    # pair_i_A has heat in [0.6, 0.9], pair_i_B has heat in [0.1, 0.4] → A always wins
    survivor_contents = set()
    victim_contents = set()
    singleton_contents = set()

    for i in range(3):
        base = rng.standard_normal(384).astype(np.float32)
        base /= np.linalg.norm(base)
        noise = rng.standard_normal(384).astype(np.float32) * 1e-5
        vec_b = base + noise
        vec_b /= np.linalg.norm(vec_b)

        heat_a = float(rng.uniform(0.6, 0.9))
        heat_b = float(rng.uniform(0.1, 0.4))
        assert heat_a > heat_b  # sanity: A is hotter

        content_a = f"dup pair {i} A"
        content_b = f"dup pair {i} B"
        storage.insert_memory(
            {
                "content": content_a,
                "embedding": base.tobytes(),
                "directory_context": "/proj",
                "heat": heat_a,
            }
        )
        storage.insert_memory(
            {
                "content": content_b,
                "embedding": vec_b.tobytes(),
                "directory_context": "/proj",
                "heat": heat_b,
            }
        )

        survivor_contents.add(content_a)
        victim_contents.add(content_b)

    for i in range(4):
        v = rng.standard_normal(384).astype(np.float32)
        v /= np.linalg.norm(v)
        content = f"singleton {i}"
        storage.insert_memory(
            {
                "content": content,
                "embedding": v.tobytes(),
                "directory_context": "/proj",
                "heat": 0.5,
            }
        )
        singleton_contents.add(content)

    emb = EmbeddingEngine()
    emb._unavailable = True
    sched = ConsolidationScheduler(storage, emb, settings)

    stats: dict = {"memories_deleted": 0}
    sched._merge_duplicates(stats)

    remaining = {m["content"] for m in storage.get_all_memories_with_embeddings()}
    storage.close()

    assert stats["memories_deleted"] == 3, f"expected 3 deletions, got {stats['memories_deleted']}"
    assert survivor_contents.issubset(remaining), (
        f"high-heat survivors missing: {survivor_contents - remaining}"
    )
    assert victim_contents.isdisjoint(remaining), (
        f"low-heat victims still present: {victim_contents & remaining}"
    )
    assert singleton_contents.issubset(remaining), (
        f"singletons wrongly deleted: {singleton_contents - remaining}"
    )


class TestSimilarityLinkDegreeCap:
    def test_link_similar_memories_respects_degree_cap(self, storage, tmp_path):
        """memory_similarity_link must not exceed MAX_SIMILARITY_LINKS_PER_MEMORY
        per memory, even when every pair is above the similarity threshold."""
        settings = Settings(
            DB_PATH=str(tmp_path / "degree.db"),
            MAX_SIMILARITY_LINKS_PER_MEMORY=3,
            SIMILARITY_LINK_THRESHOLD=0.5,
        )
        emb = EmbeddingEngine()
        emb._unavailable = True
        sched = ConsolidationScheduler(storage, emb, settings)

        # 8 memories with identical embeddings → cosine 1.0 between all pairs.
        # The memory.embedding HNSW field is fixed at 384 dimensions.
        vec = np.array([1.0] + [0.0] * 383, dtype=np.float32).tobytes()
        for i in range(8):
            storage.insert_memory(
                {
                    "content": f"identical-embedding memory {i}",
                    "embedding": vec,
                    "directory_context": "/proj",
                    "heat": 1.0,
                }
            )

        sched._link_similar_memories({})

        degree: dict[int, int] = {}
        for link in storage.get_all_memory_similarity_links():
            degree[link["source_memory_id"]] = degree.get(link["source_memory_id"], 0) + 1
            degree[link["target_memory_id"]] = degree.get(link["target_memory_id"], 0) + 1

        assert degree, "expected some similarity links to be created"
        assert max(degree.values()) <= 3, f"degree cap violated: {degree}"


# ── SIMILARITY_MATRIX_MAX_CANDIDATES cap tests ──────────────────────────────


class TestSimilarityMatrixCandidateCap:
    """_link_similar_memories and _merge_duplicates must not build an N×N matrix
    for the full table when N > SIMILARITY_MATRIX_MAX_CANDIDATES."""

    def test_link_similar_memories_respects_candidate_cap(self, tmp_path):
        """When N memories > cap, get_memories_with_embeddings is called with limit=cap."""
        from unittest.mock import patch

        import numpy as np

        cap = 5
        settings = Settings(
            DB_PATH=str(tmp_path / "cap_link.db"),
            SIMILARITY_MATRIX_MAX_CANDIDATES=cap,
            SIMILARITY_LINK_THRESHOLD=0.5,
        )
        storage = StorageEngine(str(tmp_path / "cap_link.db"))
        emb = EmbeddingEngine()
        emb._unavailable = True
        sched = ConsolidationScheduler(storage, emb, settings)

        # Insert cap+5 memories with real embeddings
        for i in range(cap + 5):
            vec = np.random.default_rng(i).standard_normal(384).astype(np.float32)
            vec /= np.linalg.norm(vec)
            storage.insert_memory(
                {
                    "content": f"cap test memory {i}",
                    "embedding": vec.tobytes(),
                    "directory_context": "/proj",
                    "heat": 1.0,
                }
            )

        # Spy: track the limit argument passed to get_memories_with_embeddings
        original = storage.get_memories_with_embeddings
        called_with_limit = []

        def spy(*args, **kwargs):
            called_with_limit.append(kwargs.get("limit", args[0] if args else None))
            return original(*args, **kwargs)

        with patch.object(storage, "get_memories_with_embeddings", side_effect=spy):
            sched._link_similar_memories({})

        storage.close()

        assert called_with_limit, "get_memories_with_embeddings was not called"
        assert called_with_limit[0] == cap, f"expected limit={cap}, got {called_with_limit[0]}"

    def test_merge_duplicates_respects_candidate_cap(self, tmp_path):
        """When N memories > cap, _merge_duplicates uses at most cap candidates."""
        from unittest.mock import patch

        import numpy as np

        cap = 5
        settings = Settings(
            DB_PATH=str(tmp_path / "cap_merge.db"),
            SIMILARITY_MATRIX_MAX_CANDIDATES=cap,
        )
        storage = StorageEngine(str(tmp_path / "cap_merge.db"))
        emb = EmbeddingEngine()
        emb._unavailable = True
        sched = ConsolidationScheduler(storage, emb, settings)

        for i in range(cap + 5):
            vec = np.random.default_rng(i + 100).standard_normal(384).astype(np.float32)
            vec /= np.linalg.norm(vec)
            storage.insert_memory(
                {
                    "content": f"merge cap test memory {i}",
                    "embedding": vec.tobytes(),
                    "directory_context": "/proj",
                    "heat": 1.0,
                }
            )

        original = storage.get_memories_with_embeddings
        called_with_limit = []

        def spy(*args, **kwargs):
            called_with_limit.append(kwargs.get("limit", args[0] if args else None))
            return original(*args, **kwargs)

        with patch.object(storage, "get_memories_with_embeddings", side_effect=spy):
            sched._merge_duplicates({"memories_deleted": 0})

        storage.close()

        assert called_with_limit, "get_memories_with_embeddings was not called"
        assert called_with_limit[0] == cap, f"expected limit={cap}, got {called_with_limit[0]}"


# ── Consolidation cooldown tests (v4.8 fix #4) ──────────────────────────────


class TestConsolidationCooldown:
    """_daemon_loop must not re-fire idle consolidation until cooldown expires.

    force_consolidate() always runs regardless of cooldown.
    The daily 18:30 UTC cycle is not under test here (time-sensitive).
    """

    def _make_scheduler(self, tmp_path, cooldown: int, check_interval: float = 0.01):
        """Build a ConsolidationScheduler with minimal settings for cooldown tests."""
        settings = Settings(
            DB_PATH=str(tmp_path / "cooldown_test.db"),
            IDLE_THRESHOLD_SECONDS=1,
            DAEMON_CHECK_INTERVAL=check_interval,
            CONSOLIDATION_COOLDOWN_SECONDS=cooldown,
        )
        storage = StorageEngine(str(tmp_path / "cooldown_test.db"))
        emb = EmbeddingEngine()
        emb._unavailable = True
        sched = ConsolidationScheduler(storage, emb, settings)
        return sched, storage

    def test_cooldown_blocks_immediate_refire(self, tmp_path):
        """Idle-triggered cycle must not re-fire while cooldown has not expired.

        Setup:
        - last_activity = epoch (idle_seconds >> IDLE_THRESHOLD_SECONDS)
        - get_episodes_since returns 1 episode (so threshold gate passes)
        - CONSOLIDATION_COOLDOWN_SECONDS = 1800

        Run the daemon for two iterations. Assert _consolidation_cycle called
        exactly once: the cooldown blocks the second iteration.
        """
        from unittest.mock import patch

        sched, storage = self._make_scheduler(tmp_path, cooldown=1800, check_interval=0.01)
        # Simulate deep idle
        sched.last_activity = datetime.fromtimestamp(0, UTC)

        call_count = []

        def fake_cycle():
            call_count.append(1)
            return {}

        # Stub get_episodes_since so the new_episodes gate always passes
        storage.insert_episode(
            {"session_id": "s1", "directory": "/proj", "raw_content": "test content"}
        )

        with patch.object(sched, "_consolidation_cycle", side_effect=fake_cycle):
            sched.start()
            # Give daemon enough time for at least 2 check-interval wake-ups
            time.sleep(0.15)
            sched.stop()

        assert len(call_count) == 1, (
            f"expected exactly 1 cycle call with cooldown active, got {len(call_count)}"
        )

    def test_cooldown_expires(self, tmp_path):
        """Idle cycle fires when _last_cycle_completed_at is older than cooldown."""
        from unittest.mock import patch

        sched, storage = self._make_scheduler(tmp_path, cooldown=60, check_interval=0.01)
        # Simulate deep idle
        sched.last_activity = datetime.fromtimestamp(0, UTC)
        # Simulate last cycle completed 31 minutes ago (> 60s cooldown)
        sched._last_cycle_completed_at = datetime.now(UTC) - timedelta(minutes=31)

        call_count = []

        def fake_cycle():
            call_count.append(1)
            return {}

        storage.insert_episode(
            {"session_id": "s2", "directory": "/proj", "raw_content": "test content 2"}
        )

        with patch.object(sched, "_consolidation_cycle", side_effect=fake_cycle):
            sched.start()
            time.sleep(0.15)
            sched.stop()

        assert len(call_count) >= 1, (
            f"expected at least 1 cycle after cooldown expired, got {len(call_count)}"
        )

    def test_force_consolidate_ignores_cooldown(self, tmp_path):
        """force_consolidate() must run even when cooldown has not expired.

        An explicit user/MCP request beats throttling.
        """
        from unittest.mock import patch

        sched, _storage = self._make_scheduler(tmp_path, cooldown=1800, check_interval=30)
        # Mark cooldown as just started (now)
        sched._last_cycle_completed_at = datetime.now(UTC)

        call_count = []

        def fake_cycle():
            call_count.append(1)
            return {}

        with patch.object(sched, "_consolidation_cycle", side_effect=fake_cycle):
            sched.force_consolidate()

        assert len(call_count) == 1, (
            f"force_consolidate() must ignore cooldown; got {len(call_count)} calls"
        )
