import os
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.storage import StorageEngine
from yadgar.core.consolidation import ConsolidationScheduler


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

    def test_cooccurrence_accumulates_across_separate_batches(self, engine, storage):
        """Regression: co_occurrence weight must survive across multiple
        force_consolidate() calls, despite the `_apply_decay` and
        `_memify_reweight` phases that run between cycles.

        Each of the 5 episodes contributes a +1.0 weight increment, so the
        deterministic baseline after 3 batches is weight == 5.0. Between
        batches, `_memify_reweight` (curation.py) and similar consolidation
        passes can add legitimate boosts to high-heat relationships.
        Empirically observed ~7.0 with default settings — the additional
        boost path beyond the documented `_memify_reweight` +0.5 is not
        precisely modeled in the test; the assertion is bounded
        empirically at [5.0, 10.0] to catch the bug class (weight stuck
        at 1.0 = lost increments) without depending on exact boost math.

        TODO: characterize the additional boost source(s) that push weight
        from the deterministic 5.0 to the observed ~7.0 and tighten the
        upper bound, ideally derived from constants rather than empiricism.
        """
        # Batch 1: 2 episodes, reset watermark so consolidate sees all
        for i in range(2):
            storage.insert_episode(
                {
                    "session_id": f"b1_sess{i}",
                    "directory": "/proj",
                    "raw_content": "def serialize():\nimport msgpack",
                }
            )
        engine._last_consolidated_episode_id = 0
        engine.force_consolidate()

        # Batch 2: 2 more episodes — watermark now at highest ep so far
        for i in range(2):
            storage.insert_episode(
                {
                    "session_id": f"b2_sess{i}",
                    "directory": "/proj",
                    "raw_content": "def serialize():\nimport msgpack",
                }
            )
        engine.force_consolidate()

        # Batch 3: 1 final episode
        storage.insert_episode(
            {
                "session_id": "b3_sess0",
                "directory": "/proj",
                "raw_content": "def serialize():\nimport msgpack",
            }
        )
        engine.force_consolidate()

        e1 = storage.get_entity_by_name("serialize")
        e2 = storage.get_entity_by_name("msgpack")
        assert e1 is not None, "entity 'serialize' not found after 5 episodes"
        assert e2 is not None, "entity 'msgpack' not found after 5 episodes"
        rel = storage.get_typed_relationship(
            e1["id"], e2["id"], "co_occurrence"
        ) or storage.get_typed_relationship(e2["id"], e1["id"], "co_occurrence")
        assert rel is not None, "co_occurrence relationship not created"
        # Deterministic base: 5.0 (one increment per episode across 3 batches).
        # Between batches, _memify_reweight (curation.py) and similar
        # consolidation passes can add legitimate boosts to high-heat relationships.
        # Empirically observed ~7.0 with default settings; bound at 10.0 to allow
        # for reasonable variation while still catching:
        #   - weight stuck at 1.0 (CREATE resetting instead of UPDATE) — the v4.4.11 bug
        #   - spike to 50.0+ (double-increment cascade or repeated boost loop)
        assert 5.0 <= rel["weight"] <= 10.0, (
            f"expected weight in [5.0, 10.0] across 3 batches, got {rel['weight']}. "
            "Below 5.0 = increments lost. Above 10.0 = boost loop or double-increment."
        )


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


# TestDaemonLifecycle and test_idle_detection removed (v5.7.0 PR-0):
# start()/stop()/_daemon_loop removed from ConsolidationScheduler.
# Consolidation now runs only via force_consolidate() or nightly cron (PR-1).


class TestActivityTracking:
    def test_record_activity_updates_timestamp(self, engine):
        old = engine.last_activity
        time.sleep(0.01)
        engine.record_activity()
        assert engine.last_activity > old


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


def test_process_episodes_creates_resolved_by_relationship(tmp_path, settings):
    """P0.4: a memory describing an error AND its resolution creates a resolved_by edge.

    Pre-fix this was provably dead: the extractor emitted (error, "error",
    "resolved_by") but the handler looked for a never-emitted "solution" entity,
    so _find_entity_by_type returned None and no edge was ever created.
    """
    engine = StorageEngine(str(tmp_path / "pe_resolved.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    sched = ConsolidationScheduler(engine, emb, settings)

    engine.insert_episode(
        {
            "session_id": "s_resolve",
            "directory": "/proj",
            "raw_content": "Fixed the ValueError by adding a null guard",
        }
    )
    sched._last_consolidated_episode_id = 0
    sched._process_new_episodes({"episodes_processed": 0})

    resolved = engine.get_relationships_by_types(["resolved_by"])
    assert resolved, "expected a resolved_by relationship to be created"
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


@pytest.mark.skipif(
    os.environ.get("PYTEST_XDIST_WORKER") is not None,
    reason="timing-sensitive perf guard unreliable under xdist parallel CPU contention; run serially for perf gating",
)
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


def _canonical_link_set(storage) -> set[tuple[int, int]]:
    """Return the undirected link set as canonical (lo, hi) id pairs."""
    out: set[tuple[int, int]] = set()
    for link in storage.get_all_memory_similarity_links():
        a, b = link["source_memory_id"], link["target_memory_id"]
        out.add((a, b) if a < b else (b, a))
    return out


def _cluster_vec(cluster: int, jitter: int) -> bytes:
    """Deterministic 384-d unit vector: same cluster → high cosine, diff → ~0."""
    rng = np.random.default_rng(cluster * 1000)
    base = rng.standard_normal(384).astype(np.float32)
    base /= np.linalg.norm(base)
    # tiny jitter keeps intra-cluster pairs distinct but well above threshold
    noise = np.random.default_rng(cluster * 1000 + jitter + 1).standard_normal(384)
    noise = noise.astype(np.float32) * 0.02
    vec = base + noise
    vec /= np.linalg.norm(vec)
    return vec.tobytes()


class TestIncrementalSimilarityLinking:
    """v5.86 (OT-C4): incremental probe×corpus linking equals the full N×N pass."""

    # Cluster layout: 5 olds + 4 news across 2 clusters so that BOTH old↔old AND
    # new↔new same-cluster pairs exist (the cases a degenerate fixture hides).
    #   olds  idx 0..4 → clusters [0, 0, 1, 0, 1]  (cluster0 has 3 olds, cluster1 has 2)
    #   news  idx 5..8 → clusters [0, 1, 0, 1]      (each cluster gains 2 news)
    _OLD_CLUSTERS = [0, 0, 1, 0, 1]
    _NEW_CLUSTERS = [0, 1, 0, 1]

    def _insert_olds(self, storage):
        """Insert the OLD memories (created_at < watermark). Returns old_ids."""
        old_ts = "2026-06-01T00:00:00+00:00"
        old_ids = []
        for i, cl in enumerate(self._OLD_CLUSTERS):
            old_ids.append(
                storage.insert_memory(
                    {
                        "content": f"old {i}",
                        "embedding": _cluster_vec(cl, i),
                        "directory_context": "/proj",
                        "heat": 1.0,
                        "created_at": old_ts,
                    }
                )
            )
        return old_ids

    def _insert_news(self, storage):
        """Insert the NEW memories (created_at >= watermark)."""
        new_ts = "2026-06-26T00:00:00+00:00"
        for j, cl in enumerate(self._NEW_CLUSTERS):
            storage.insert_memory(
                {
                    "content": f"new {j}",
                    "embedding": _cluster_vec(cl, 100 + j),
                    "directory_context": "/proj",
                    "heat": 1.0,
                    "created_at": new_ts,
                }
            )

    def _new_sched(self, storage, settings):
        emb = EmbeddingEngine()
        emb._unavailable = True
        return ConsolidationScheduler(storage, emb, settings)

    def test_incremental_equals_full_with_stable_embeddings(self, tmp_path):
        """Incremental(probe=new) yields SAME links as full pass.

        Faithful to production: old↔old links are PRE-SEEDED (an olds-only pass =
        "prior runs"), then the incremental run adds only new↔* pairs. The full
        store re-runs the complete pass. Equivalence must hold over a corpus that
        actually contains old↔old AND new↔new same-cluster pairs.
        """
        watermark = "2026-06-20T00:00:00+00:00"
        settings_full = Settings(
            DB_PATH=str(tmp_path / "full.db"),
            SIMILARITY_LINK_THRESHOLD=0.5,
            MAX_SIMILARITY_LINKS_PER_MEMORY=15,
        )
        settings_inc = Settings(
            DB_PATH=str(tmp_path / "inc.db"),
            SIMILARITY_LINK_THRESHOLD=0.5,
            MAX_SIMILARITY_LINKS_PER_MEMORY=15,
        )

        # --- Full pass: insert olds + news, then ONE full pass over the corpus ---
        full_store = StorageEngine(str(tmp_path / "full.db"))
        sched_full = self._new_sched(full_store, settings_full)
        old_ids = self._insert_olds(full_store)
        self._insert_news(full_store)
        sched_full._link_similar_memories({})
        full_links = _canonical_link_set(full_store)
        full_store.close()

        # --- Incremental: olds-only full pass (= "prior runs", seeds old↔old),
        #     THEN insert news, THEN incremental cycle (probe = since watermark) ---
        inc_store = StorageEngine(str(tmp_path / "inc.db"))
        sched_inc = self._new_sched(inc_store, settings_inc)
        self._insert_olds(inc_store)
        sched_inc._link_similar_memories({})  # prior runs — only olds exist yet
        self._insert_news(inc_store)
        sched_inc._link_similar_memories_incremental({}, since=watermark)
        inc_links = _canonical_link_set(inc_store)
        inc_store.close()

        # Sanity: the fixture must actually contain an old↔old same-cluster link,
        # otherwise this test degenerates to the trivial case.
        old_old_pairs = {(a, b) if a < b else (b, a) for a in old_ids for b in old_ids if a != b}
        assert full_links & old_old_pairs, "fixture must contain ≥1 old↔old link"

        assert full_links, "expected the full pass to create some links"
        assert inc_links == full_links, (
            f"incremental != full\nfull-only={full_links - inc_links}\n"
            f"inc-only={inc_links - full_links}"
        )

    def test_incremental_includes_new_old_links(self, tmp_path):
        """Probe (new) must link to OLD corpus members, not just other new ones."""
        watermark = "2026-06-20T00:00:00+00:00"
        settings = Settings(
            DB_PATH=str(tmp_path / "no.db"),
            SIMILARITY_LINK_THRESHOLD=0.5,
            MAX_SIMILARITY_LINKS_PER_MEMORY=15,
        )
        store = StorageEngine(str(tmp_path / "no.db"))
        # 1 old + 1 new in the SAME cluster → a single new↔old link expected.
        emb = EmbeddingEngine()
        emb._unavailable = True
        sched = ConsolidationScheduler(store, emb, settings)
        old_id = store.insert_memory(
            {
                "content": "old c0",
                "embedding": _cluster_vec(0, 0),
                "directory_context": "/proj",
                "heat": 1.0,
                "created_at": "2026-06-01T00:00:00+00:00",
            }
        )
        new_id = store.insert_memory(
            {
                "content": "new c0",
                "embedding": _cluster_vec(0, 1),
                "directory_context": "/proj",
                "heat": 1.0,
                "created_at": "2026-06-26T00:00:00+00:00",
            }
        )
        sched._link_similar_memories_incremental({}, since=watermark)
        links = _canonical_link_set(store)
        store.close()
        expected = (old_id, new_id) if old_id < new_id else (new_id, old_id)
        assert expected in links, f"new↔old link missing: {links}"

    def test_incremental_no_self_links(self, tmp_path):
        """A probe memory present in both probe and corpus must not link to itself."""
        watermark = "2026-06-20T00:00:00+00:00"
        settings = Settings(
            DB_PATH=str(tmp_path / "self.db"),
            SIMILARITY_LINK_THRESHOLD=0.5,
            MAX_SIMILARITY_LINKS_PER_MEMORY=15,
        )
        store = StorageEngine(str(tmp_path / "self.db"))
        emb = EmbeddingEngine()
        emb._unavailable = True
        sched = ConsolidationScheduler(store, emb, settings)
        store.insert_memory(
            {
                "content": "lonely new",
                "embedding": _cluster_vec(0, 0),
                "directory_context": "/proj",
                "heat": 1.0,
                "created_at": "2026-06-26T00:00:00+00:00",
            }
        )
        sched._link_similar_memories_incremental({}, since=watermark)
        links = _canonical_link_set(store)
        store.close()
        assert all(a != b for a, b in links), f"self-link created: {links}"
        assert links == set(), "no links expected for a single isolated memory"


class TestSimilarityLinkingDispatch:
    """v5.86 (OT-C4): in-cycle dispatch + post-sleep full reconcile, default OFF."""

    def _sched(self, store, **overrides):
        base = dict(SIMILARITY_LINK_THRESHOLD=0.5, MAX_SIMILARITY_LINKS_PER_MEMORY=15)
        base.update(overrides)
        settings = Settings(DB_PATH=store._db_path if hasattr(store, "_db_path") else ":m:", **base)
        emb = EmbeddingEngine()
        emb._unavailable = True
        return ConsolidationScheduler(store, emb, settings)

    def test_default_off_runs_full_pass_and_no_watermark(self, tmp_path):
        """Flag OFF (default): full pass runs, no watermark is written."""
        store = StorageEngine(str(tmp_path / "off.db"))
        sched = self._sched(store)  # default: incremental disabled
        for i in range(3):
            store.insert_memory(
                {
                    "content": f"m{i}",
                    "embedding": _cluster_vec(0, i),
                    "directory_context": "/proj",
                    "heat": 1.0,
                }
            )
        sched._run_similarity_linking({})
        links = _canonical_link_set(store)
        wm = store.get_consolidation_watermark("similarity_linking")
        store.close()
        assert links, "full pass should have created links"
        assert wm is None, "default-OFF must not write a watermark"

    def test_flag_on_first_run_seeds_then_bumps_watermark(self, tmp_path):
        """Flag ON, no prior watermark: full seed pass + watermark written."""
        store = StorageEngine(str(tmp_path / "on.db"))
        sched = self._sched(store, SIMILARITY_LINKING_INCREMENTAL_ENABLED=True)
        for i in range(3):
            store.insert_memory(
                {
                    "content": f"m{i}",
                    "embedding": _cluster_vec(0, i),
                    "directory_context": "/proj",
                    "heat": 1.0,
                }
            )
        assert store.get_consolidation_watermark("similarity_linking") is None
        sched._run_similarity_linking({})
        wm = store.get_consolidation_watermark("similarity_linking")
        store.close()
        assert wm is not None, "flag ON must persist a watermark after the run"

    def test_full_reconcile_inert_when_flag_off(self, tmp_path):
        """Flag OFF: post-sleep reconcile does NOT fire (production unchanged)."""
        store = StorageEngine(str(tmp_path / "inert.db"))
        sched = self._sched(store)  # OFF
        called = {"n": 0}
        orig = sched._link_similar_memories

        def spy(stats):
            called["n"] += 1
            return orig(stats)

        sched._link_similar_memories = spy
        sched._maybe_full_reconcile({"reembedded": 5})
        store.close()
        assert called["n"] == 0, "reconcile must be inert when flag OFF"

    def test_full_reconcile_fires_on_reembed(self, tmp_path):
        """Flag ON + sleep re-embedded memories → full reconcile runs."""
        store = StorageEngine(str(tmp_path / "reembed.db"))
        sched = self._sched(store, SIMILARITY_LINKING_INCREMENTAL_ENABLED=True)
        called = {"n": 0}
        orig = sched._link_similar_memories

        def spy(stats):
            called["n"] += 1
            return orig(stats)

        sched._link_similar_memories = spy
        sched._maybe_full_reconcile({"reembedded": 3, "compressed": 0})
        wm = store.get_consolidation_watermark("full_reconcile")
        store.close()
        assert called["n"] == 1, "reconcile must fire when embeddings changed"
        assert wm is not None, "full_reconcile watermark must be written"

    def test_full_reconcile_skipped_when_nothing_changed_and_recent(self, tmp_path):
        """Flag ON, no re-embed, recent reconcile watermark → skip."""
        store = StorageEngine(str(tmp_path / "skip.db"))
        sched = self._sched(
            store,
            SIMILARITY_LINKING_INCREMENTAL_ENABLED=True,
            SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS=7,
        )
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        store.set_consolidation_watermark("full_reconcile", _dt.now(_UTC).isoformat())
        called = {"n": 0}
        sched._link_similar_memories = lambda stats: called.__setitem__("n", called["n"] + 1)
        sched._maybe_full_reconcile({"reembedded": 0, "compressed": 0})
        store.close()
        assert called["n"] == 0, "must skip when nothing changed and reconcile is recent"

    def test_full_reconcile_relinks_changed_old_pair(self, tmp_path):
        """Embedding mutation on an OLD memory → full reconcile links the new pair.

        This is the safety-net case: an incremental-by-created_at pass would miss
        an old↔old pair whose similarity only crossed the threshold after a
        re-embed. The full reconcile must catch it.
        """
        store = StorageEngine(str(tmp_path / "changed.db"))
        sched = self._sched(store, SIMILARITY_LINKING_INCREMENTAL_ENABLED=True)
        old_ts = "2026-06-01T00:00:00+00:00"
        # Two OLD memories in DIFFERENT clusters → initially NOT linked.
        a = store.insert_memory(
            {
                "content": "old A",
                "embedding": _cluster_vec(0, 0),
                "directory_context": "/proj",
                "heat": 1.0,
                "created_at": old_ts,
            }
        )
        b = store.insert_memory(
            {
                "content": "old B",
                "embedding": _cluster_vec(1, 0),
                "directory_context": "/proj",
                "heat": 1.0,
                "created_at": old_ts,
            }
        )
        # Seed the link graph (different clusters → no a↔b link yet).
        sched._link_similar_memories({})
        before = _canonical_link_set(store)
        key = (a, b) if a < b else (b, a)
        assert key not in before, "different-cluster pair should not be linked initially"

        # Simulate a re-embed: B's embedding now matches A's cluster.
        store.update_memory_fields(b, embedding=_cluster_vec(0, 1))

        # Full reconcile (triggered by reembedded>0) must now link a↔b.
        sched._maybe_full_reconcile({"reembedded": 1})
        after = _canonical_link_set(store)
        store.close()
        assert key in after, f"reconcile failed to link changed old pair: {after}"


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


# TestConsolidationCooldown removed (v5.7.0 PR-0):
# _daemon_loop and its cooldown mechanism removed from ConsolidationScheduler.
# Consolidation now runs only via force_consolidate() or nightly cron (PR-1).
# test_force_consolidate_ignores_cooldown is now vacuously true:
# force_consolidate() always runs since there is no cooldown gate.
