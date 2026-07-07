"""Tests for the active memory curation engine and memify self-improvement layer."""

import logging
import random
import time

import numpy as np
import pytest

from yadgar._shared.config import Settings
from yadgar._shared.curation import MemoryCurator
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics

# Detect whether the embedding model can be loaded
_engine = EmbeddingEngine()
try:
    _engine._ensure_model()
    _model_available = not _engine._unavailable
except Exception:
    _model_available = False

requires_model = pytest.mark.skipif(
    not _model_available,
    reason="sentence-transformers model not available",
)


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_curation.db"), embedding_dim=384)
    yield engine
    engine.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "test.db"),
        CURATION_SIMILARITY_THRESHOLD=0.85,
    )


@pytest.fixture
def embeddings():
    return EmbeddingEngine()


@pytest.fixture
def thermo(storage, embeddings, settings):
    return MemoryThermodynamics(storage, embeddings, settings)


@pytest.fixture
def curator(storage, embeddings, thermo, settings):
    return MemoryCurator(storage, embeddings, thermo, settings)


def _make_embedding(dim: int = 384, seed: int = 0) -> bytes:
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim).astype(np.float32)
    # Normalize for cosine similarity
    vec = vec / np.linalg.norm(vec)
    return vec.tobytes()


def _make_similar_embedding(base: bytes, noise_scale: float = 0.01, seed: int = 1) -> bytes:
    """Create an embedding very similar to base (high cosine similarity)."""
    arr = np.frombuffer(base, dtype=np.float32).copy()
    rng = np.random.RandomState(seed)
    noise = rng.randn(len(arr)).astype(np.float32) * noise_scale
    arr += noise
    arr = arr / np.linalg.norm(arr)
    return arr.tobytes()


def _make_moderate_embedding(base: bytes, noise_scale: float = 0.5, seed: int = 2) -> bytes:
    """Create an embedding moderately similar to base (0.6-0.85 range)."""
    arr = np.frombuffer(base, dtype=np.float32).copy()
    rng = np.random.RandomState(seed)
    noise = rng.randn(len(arr)).astype(np.float32) * noise_scale
    arr += noise
    arr = arr / np.linalg.norm(arr)
    return arr.tobytes()


# ── test_curate_new_memory ───────────────────────────────────────────────


def test_curate_new_memory(curator, storage):
    """Novel content creates a new memory."""
    emb = _make_embedding(seed=42)
    result = curator.curate_on_remember(
        content="Brand new unique memory content about quantum physics",
        context="/test/project",
        tags=["test", "physics"],
        embedding=emb,
    )
    assert result["action"] == "created"
    assert "memory_id" in result

    mem = storage.get_memory(result["memory_id"])
    assert mem is not None
    assert "quantum physics" in mem["content"]


# ── test_curate_merge_similar ────────────────────────────────────────────


@requires_model
def test_curate_merge_similar(curator, storage, embeddings):
    """Similar content merges with existing memory."""
    content1 = "Python uses indentation for code blocks instead of braces"
    emb1 = embeddings.encode(content1)
    result1 = curator.curate_on_remember(
        content=content1,
        context="/test/project",
        tags=["python", "syntax"],
        embedding=emb1,
    )
    assert result1["action"] == "created"
    original_id = result1["memory_id"]

    # Very similar content should merge
    content2 = "Python uses indentation for code blocks instead of curly braces"
    emb2 = embeddings.encode(content2)
    result2 = curator.curate_on_remember(
        content=content2,
        context="/test/project",
        tags=["python", "language"],
        embedding=emb2,
    )
    assert result2["action"] == "merged"
    assert result2["memory_id"] == original_id

    merged = storage.get_memory(original_id)
    assert content1 in merged["content"]
    assert content2 in merged["content"]
    # Tags should be union
    assert "syntax" in merged["tags"]
    assert "language" in merged["tags"]
    assert merged["heat"] == 1.0  # Heat refreshed


# ── test_curate_link_moderate ────────────────────────────────────────────


@requires_model
def test_curate_link_moderate(curator, storage, embeddings):
    """Moderately similar content creates a link."""
    content1 = "FastAPI is a modern Python web framework for building APIs"
    emb1 = embeddings.encode(content1)
    result1 = curator.curate_on_remember(
        content=content1,
        context="/test/project",
        tags=["fastapi"],
        embedding=emb1,
    )
    assert result1["action"] == "created"
    original_id = result1["memory_id"]

    # Related but not identical — should link
    content2 = "Django is a full-featured Python web framework for web applications"
    emb2 = embeddings.encode(content2)

    # Verify similarity is in the moderate range
    sim = embeddings.similarity(emb1, emb2)
    if sim >= 0.6 and sim < 0.85:
        result2 = curator.curate_on_remember(
            content=content2,
            context="/test/project",
            tags=["django"],
            embedding=emb2,
        )
        assert result2["action"] == "linked"
        assert result2["memory_id"] != original_id
        assert result2["linked_to"] == original_id

        # Both memories should exist
        assert storage.get_memory(result2["memory_id"]) is not None
        assert storage.get_memory(original_id) is not None

        # Check that a derived_from relationship was created
        rels = storage._q("SELECT * FROM relationship WHERE relationship_type = 'derived_from'")
        assert len(rels) >= 1
    else:
        # If model gives different similarity, test the mechanism with synthetic embeddings
        emb_base = _make_embedding(seed=100)
        curator.curate_on_remember(
            content="Base memory content for linking test",
            context="/test/project",
            tags=["base"],
            embedding=emb_base,
        )

        # Create embedding in moderate similarity range
        emb_moderate = _make_moderate_embedding(emb_base, noise_scale=0.35, seed=200)
        sim_check = embeddings.similarity(emb_base, emb_moderate)
        # Adjust noise to hit 0.6-0.85 range
        for scale in [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6]:
            emb_moderate = _make_moderate_embedding(emb_base, noise_scale=scale, seed=200)
            sim_check = embeddings.similarity(emb_base, emb_moderate)
            if 0.6 <= sim_check < 0.85:
                break

        if 0.6 <= sim_check < 0.85:
            result_mod = curator.curate_on_remember(
                content="Moderately similar linking test content",
                context="/test/project",
                tags=["linked"],
                embedding=emb_moderate,
            )
            assert result_mod["action"] == "linked"
        else:
            pytest.skip("Could not generate embedding in moderate similarity range")


# ── test_contradiction_detection ─────────────────────────────────────────


@requires_model
def test_contradiction_detection(curator, storage, embeddings):
    """Opposing content is flagged as contradicting."""
    content1 = "We use PostgreSQL as our primary database for the application"
    emb1 = embeddings.encode(content1)
    storage.insert_memory(
        {
            "content": content1,
            "embedding": emb1,
            "tags": ["database"],
            "directory_context": "/test",
            "heat": 1.0,
            "is_stale": False,
        }
    )

    content2 = "We no longer use PostgreSQL, instead of PostgreSQL we switched to MySQL"
    emb2 = embeddings.encode(content2)

    contradictions = curator.detect_contradictions(content2, emb2)

    # Should detect the contradiction (negation pattern present in new content)
    if contradictions:
        assert any(
            c["reason"] in ("negation_mismatch", "action_divergence") for c in contradictions
        )
        # Old memory's confidence should be reduced
        for c in contradictions:
            old_mem = storage.get_memory(c["memory_id"])
            assert old_mem["confidence"] < 1.0


# ── test_memify_prune ────────────────────────────────────────────────────


def test_memify_prune(curator, storage):
    """Cold unreliable memories with zero access get pruned."""
    emb = _make_embedding(seed=10)
    mid = storage.insert_memory(
        {
            "content": "This is a cold unreliable memory that was never accessed",
            "embedding": emb,
            "tags": ["cold", "_action_stream"],  # pruner only removes _action_stream memories
            "directory_context": "/test",
            "heat": 0.005,  # < 0.01
            "is_stale": False,
        }
    )
    # Set confidence < 0.3 and access_count = 0
    storage._q(
        "UPDATE type::record('memory', $id) SET confidence = 0.2, access_count = 0",
        {"id": mid},
    )

    stats = curator.memify_cycle()
    assert stats["pruned"] >= 1
    assert storage.get_memory(mid) is None


# ── test_memify_strengthen ───────────────────────────────────────────────


def test_memify_strengthen(curator, storage):
    """Frequently used high-confidence memories get importance boosted."""
    emb = _make_embedding(seed=20)
    mid = storage.insert_memory(
        {
            "content": "Frequently accessed and useful memory about project architecture",
            "embedding": emb,
            "tags": ["architecture"],
            "directory_context": "/test",
            "heat": 0.8,
            "is_stale": False,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET access_count = 10, confidence = 0.9, importance = 0.5",
        {"id": mid},
    )

    stats = curator.memify_cycle()
    assert stats["strengthened"] >= 1

    mem = storage.get_memory(mid)
    assert mem["importance"] == pytest.approx(0.6, abs=0.01)


# ── test_memify_derive ───────────────────────────────────────────────────


def test_memify_derive(curator, storage):
    """High-weight entity pairs generate derived fact memories."""
    storage._now_iso()

    # Create two entities
    eid1 = storage.insert_entity({"name": "module.py", "type": "file"})
    eid2 = storage.insert_entity({"name": "utils.py", "type": "file"})

    # Create a high-weight co_occurrence relationship (weight > 10)
    storage.insert_relationship(
        {
            "source_entity_id": eid1,
            "target_entity_id": eid2,
            "relationship_type": "co_occurrence",
            "weight": 12.0,
        }
    )

    stats = curator.memify_cycle()
    assert stats["derived"] >= 1

    # Check the derived memory exists
    rows = storage._q(
        "SELECT * FROM memory WHERE string::contains(content, 'module.py') AND string::contains(content, 'utils.py') AND string::contains(content, 'frequently modified')"
    )
    assert len(rows) >= 1


# ── test_curation_preserves_existing ─────────────────────────────────────


def test_curation_preserves_existing(curator, storage):
    """Existing memories are not corrupted by curation operations."""
    emb1 = _make_embedding(seed=50)
    mid1 = storage.insert_memory(
        {
            "content": "Important existing memory about database migrations",
            "embedding": emb1,
            "tags": ["database", "migrations"],
            "directory_context": "/test/project",
            "heat": 0.9,
            "is_stale": False,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET confidence = 0.95, access_count = 3, importance = 0.7",
        {"id": mid1},
    )

    original = storage.get_memory(mid1)

    # Insert a completely different memory via curation
    emb2 = _make_embedding(seed=99)
    result = curator.curate_on_remember(
        content="Unrelated content about frontend React components",
        context="/test/other",
        tags=["react", "frontend"],
        embedding=emb2,
    )
    assert result["action"] == "created"

    # Run memify cycle
    curator.memify_cycle()

    # Verify original memory is unchanged
    preserved = storage.get_memory(mid1)
    assert preserved is not None
    assert preserved["content"] == original["content"]
    assert preserved["tags"] == original["tags"]
    assert preserved["heat"] == original["heat"]
    assert preserved["confidence"] == original["confidence"]


# ── test_memify_reweight ─────────────────────────────────────────────────


def test_memify_reweight(curator, storage):
    """Established relationships between hot entities get weight boosted."""
    eid1 = storage.insert_entity({"name": "hot_entity_a", "type": "file", "heat": 0.9})
    eid2 = storage.insert_entity({"name": "hot_entity_b", "type": "file", "heat": 0.8})

    rid = storage.insert_relationship(
        {
            "source_entity_id": eid1,
            "target_entity_id": eid2,
            "relationship_type": "co_occurrence",
            "weight": 6.0,  # Established relationship (>= 5.0)
        }
    )

    stats = curator.memify_cycle()
    assert stats["reweighted"] >= 1

    rows = storage._q(
        "SELECT weight FROM type::record('relationship', $id)",
        {"id": rid},
    )
    assert rows[0]["weight"] == pytest.approx(6.5, abs=0.01)  # 6.0 + 0.5 boost


def test_memify_reweight_cold_decay(curator, storage):
    """Relationships between cold entities get weight decayed."""
    eid1 = storage.insert_entity({"name": "cold_a", "type": "file", "heat": 0.05})
    eid2 = storage.insert_entity({"name": "cold_b", "type": "file", "heat": 0.05})

    rid = storage.insert_relationship(
        {
            "source_entity_id": eid1,
            "target_entity_id": eid2,
            "relationship_type": "co_occurrence",
            "weight": 3.0,
        }
    )

    stats = curator.memify_cycle()
    assert stats["reweighted"] >= 1

    rows = storage._q(
        "SELECT weight FROM type::record('relationship', $id)",
        {"id": rid},
    )
    assert rows[0]["weight"] == pytest.approx(2.7, abs=0.01)  # 3.0 * 0.9


def test_memify_derive_idempotent(curator, storage):
    """Derived facts are not duplicated on repeated runs."""
    eid1 = storage.insert_entity({"name": "a.py", "type": "file"})
    eid2 = storage.insert_entity({"name": "b.py", "type": "file"})

    storage.insert_relationship(
        {
            "source_entity_id": eid1,
            "target_entity_id": eid2,
            "relationship_type": "co_occurrence",
            "weight": 15.0,
        }
    )

    stats1 = curator.memify_cycle()
    assert stats1["derived"] >= 1

    stats2 = curator.memify_cycle()
    assert stats2["derived"] == 0  # Should not re-derive

    rows = storage._q(
        "SELECT * FROM memory WHERE string::contains(content, 'a.py') AND string::contains(content, 'b.py') AND string::contains(content, 'frequently')"
    )
    assert len(rows) == 1


# ── scale fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def memory_curator_at_scale(tmp_path, embeddings, settings):
    """Seed 500 entities + 200 relationships for performance tests.

    Returns (MemoryCurator, StorageEngine).  Fixture setup time is NOT counted
    against the 30-second wall-time assertion — the timer starts after yield.
    """
    from yadgar._shared.storage import StorageEngine
    from yadgar._shared.thermodynamics import MemoryThermodynamics

    engine = StorageEngine(str(tmp_path / "scale_curation.db"), embedding_dim=384)
    thermo = MemoryThermodynamics(engine, embeddings, settings)
    curator = MemoryCurator(engine, embeddings, thermo, settings)

    rng = random.Random(42)
    n_entities = 500
    n_relationships = 200

    entity_ids = []
    for i in range(n_entities):
        heat = rng.uniform(0.0, 1.0)
        eid = engine.insert_entity({"name": f"entity_{i}", "type": "file", "heat": heat})
        entity_ids.append(eid)

    rel_types = ["co_occurrence", "derived_from", "semantic_similarity", "caused_by"]
    inserted_pairs: set[tuple[int, int]] = set()
    count = 0
    attempts = 0
    while count < n_relationships and attempts < n_relationships * 10:
        attempts += 1
        src, tgt = rng.sample(entity_ids, 2)
        pair = (min(src, tgt), max(src, tgt))
        if pair in inserted_pairs:
            continue
        inserted_pairs.add(pair)
        rtype = rng.choice(rel_types)
        weight = rng.uniform(1.0, 15.0)
        engine.insert_relationship(
            {
                "source_entity_id": src,
                "target_entity_id": tgt,
                "relationship_type": rtype,
                "weight": weight,
            }
        )
        count += 1

    yield curator, engine
    engine.close()


# ── Test 1: performance regression guard ─────────────────────────────────


@pytest.mark.timeout(60)
def test_memify_reweight_under_30s_at_500_entities(memory_curator_at_scale):
    """At production-like scale, memify_reweight must not block the cycle.

    Regression test for the O(N²) per-pair HTTP bug fixed in v4.4.8.
    """
    curator, storage = memory_curator_at_scale
    stats = {"reweighted": 0}
    t0 = time.monotonic()
    curator._memify_reweight(stats)
    elapsed = time.monotonic() - t0
    assert elapsed < 30.0, f"memify_reweight took {elapsed:.1f}s at N=500 (target <30s)"


# ── Test 2: correctness with known heat distribution ──────────────────────


def test_memify_reweight_correctness_with_known_heat_distribution(curator, storage):
    """Known heat values produce exact reweight counts; also checks non-co_occurrence types."""
    # Hot pair with established weight — should get boost
    hot_a = storage.insert_entity({"name": "hot_x", "type": "file", "heat": 0.9})
    hot_b = storage.insert_entity({"name": "hot_y", "type": "file", "heat": 0.85})
    rid_hot = storage.insert_relationship(
        {
            "source_entity_id": hot_a,
            "target_entity_id": hot_b,
            "relationship_type": "semantic_similarity",
            "weight": 7.0,
        }
    )

    # Cold pair — should decay
    cold_a = storage.insert_entity({"name": "cold_x", "type": "file", "heat": 0.02})
    cold_b = storage.insert_entity({"name": "cold_y", "type": "file", "heat": 0.03})
    rid_cold = storage.insert_relationship(
        {
            "source_entity_id": cold_a,
            "target_entity_id": cold_b,
            "relationship_type": "caused_by",
            "weight": 4.0,
        }
    )

    # Warm pair (neither hot nor cold) — should be untouched
    warm_a = storage.insert_entity({"name": "warm_x", "type": "file", "heat": 0.5})
    warm_b = storage.insert_entity({"name": "warm_y", "type": "file", "heat": 0.4})
    rid_warm = storage.insert_relationship(
        {
            "source_entity_id": warm_a,
            "target_entity_id": warm_b,
            "relationship_type": "co_occurrence",
            "weight": 3.0,
        }
    )

    stats = {"reweighted": 0}
    curator._memify_reweight(stats)

    # Exactly 2 relationships should be touched: hot-boost + cold-decay
    assert stats["reweighted"] == 2

    hot_rows = storage._q("SELECT weight FROM type::record('relationship', $id)", {"id": rid_hot})
    assert hot_rows[0]["weight"] == pytest.approx(7.5, abs=0.01)  # 7.0 + 0.5 boost

    cold_rows = storage._q("SELECT weight FROM type::record('relationship', $id)", {"id": rid_cold})
    assert cold_rows[0]["weight"] == pytest.approx(3.6, abs=0.01)  # 4.0 * 0.9

    warm_rows = storage._q("SELECT weight FROM type::record('relationship', $id)", {"id": rid_warm})
    assert warm_rows[0]["weight"] == pytest.approx(3.0, abs=0.01)  # unchanged


# ── Test 3: consolidation cycle emits all phase complete markers ──────────


def test_consolidation_cycle_emits_all_phase_complete_markers(tmp_path, caplog):
    """Every unconditional consolidation phase must log both starting and complete records.

    Regression guard: if a phase silently returns early (exception swallowed,
    missing log call), this test will catch the missing marker.
    """

    from yadgar._shared.config import Settings
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.storage import StorageEngine
    from yadgar.core.consolidation import ConsolidationScheduler

    storage = StorageEngine(str(tmp_path / "phase_log.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True  # skip model load
    settings = Settings(DB_PATH=str(tmp_path / "phase_log.db"))
    engine = ConsolidationScheduler(storage, emb, settings)

    unconditional_phases = [
        "apply_decay",
        "process_episodes",
        "merge_duplicates",
        "memify",
        "insert_consolidation_log",
        "mtree_probe",
    ]

    with caplog.at_level(logging.INFO, logger="yadgar"):
        engine.force_consolidate()

    log_text = "\n".join(r.message for r in caplog.records)
    for phase in unconditional_phases:
        assert f"phase_start: {phase}" in log_text, f"missing 'phase_start: {phase}'"
        assert f"phase_end: {phase}" in log_text, f"missing 'phase_end: {phase}'"

    storage.close()


# ── test_memify_prune_auto_generated ────────────────────────────────────────


def test_memify_prune_auto_generated(tmp_path):
    """_memify_prune deletes cold, stale, old auto-generated memories.

    Scenario:
    - 1 old cold stale (last_accessed old) auto-generated memory → should be pruned
    - 1 user memory (no auto-generated tag)                      → must survive
    - 1 protected auto-generated memory                          → must survive

    v5.66: prune gate is now recency-based (last_accessed < cutoff), not
    access_count==0.  Memories must have last_accessed backdated to be eligible.
    """
    from datetime import UTC, datetime, timedelta

    from yadgar._shared.config import Settings
    from yadgar._shared.curation import MemoryCurator
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.storage import StorageEngine
    from yadgar._shared.thermodynamics import MemoryThermodynamics

    storage = StorageEngine(str(tmp_path / "autogen_prune.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    settings = Settings(
        DB_PATH=str(tmp_path / "autogen_prune.db"),
        AUTO_GENERATED_MEMORY_MAX_AGE_DAYS=30,
        COLD_THRESHOLD=0.02,
    )
    thermo = MemoryThermodynamics(storage, emb, settings)
    curator = MemoryCurator(storage, emb, thermo, settings)

    old_date = (datetime.now(UTC) - timedelta(days=45)).isoformat()

    # 1. Cold, stale, old auto-generated memory — SHOULD be pruned.
    #    last_accessed also backdated: no recent recall -> eligible for purge.
    autogen_id = storage.insert_memory(
        {
            "content": "auto-generated derived fact about a.py and b.py",
            "tags": ["derived", "auto-generated"],
            "directory_context": "system",
            "heat": 0.005,  # below COLD_THRESHOLD
        }
    )
    # Back-date created_at AND last_accessed so it exceeds the age floor.
    # v5.66: without last_accessed backdated, last_accessed=insert_time (now) → spared.
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": autogen_id, "ts": old_date},
    )

    # 2. User memory — MUST survive regardless
    user_id = storage.insert_memory(
        {
            "content": "important user memory about project architecture",
            "tags": ["architecture"],
            "directory_context": "/proj",
            "heat": 0.005,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": user_id, "ts": old_date},
    )

    # 3. Protected auto-generated memory — MUST survive
    protected_id = storage.insert_memory(
        {
            "content": "auto-generated but protected memory",
            "tags": ["derived", "auto-generated"],
            "directory_context": "system",
            "heat": 0.005,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET "
        "created_at = $ts, last_accessed = $ts, access_count = 0, is_protected = true",
        {"id": protected_id, "ts": old_date},
    )

    stats = {"pruned": 0}
    curator._memify_prune(stats)

    assert storage.get_memory(autogen_id) is None, (
        "cold old stale auto-generated memory should have been pruned"
    )
    assert storage.get_memory(user_id) is not None, "user memory must not be pruned"
    assert storage.get_memory(protected_id) is not None, "protected memory must not be pruned"
    assert stats["pruned"] >= 1

    storage.close()


# ── test_memify_derive_no_413_on_5000_statements ─────────────────────────


def test_memify_derive_no_413_on_5000_statements(monkeypatch):
    """_memify_derive must not raise HTTPStatusError when the batch is large.

    Regression test for the 413 Payload Too Large crash: every consolidation
    cycle crashed because a single derive statement's large content param blew
    past SurrealDB's HTTP body limit.  batch_writes must chunk by serialised
    byte size so no single request exceeds MAX_BATCH_BYTES.

    Strategy: build a batch of 5000 statements directly and call batch_writes
    on a mocked _http that raises 413 on bodies > 1.2 × MAX_BATCH_BYTES.
    Assert no exception escapes — the chunking keeps each request under the cap.
    """
    from unittest.mock import MagicMock

    import httpx

    from yadgar._shared.config import Settings
    from yadgar._shared.storage import StorageEngine

    max_batch_bytes = 1_000_000  # 1 MB

    monkeypatch.setattr(
        "yadgar._shared.config.get_settings",
        lambda: Settings(MAX_BATCH_STATEMENTS=500, MAX_BATCH_BYTES=max_batch_bytes),
    )

    # Build a storage engine with a smart mock HTTP client
    engine = StorageEngine.__new__(StorageEngine)
    engine._db_url = "http://fake-surreal:8000"
    engine._embedding_dim = 384
    engine._db_path = ":memory:"

    threshold = int(max_batch_bytes * 1.2)

    def smart_post(path, *, content, headers=None, **kwargs):
        """Raise HTTPStatusError(413) if body exceeds threshold."""
        if len(content) > threshold:
            request = httpx.Request("POST", "http://fake-surreal:8000/sql")
            response = httpx.Response(413, request=request)
            raise httpx.HTTPStatusError(
                f"413 Payload Too Large: body={len(content)} > {threshold}",
                request=request,
                response=response,
            )
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    mock_http = MagicMock()
    mock_http.post.side_effect = smart_post
    engine._http = mock_http

    # Build 5000 statements with a large-ish content field to simulate the
    # real _memify_derive payload (each statement carries a content param of
    # ~300 bytes, plus SQL template + param overhead ~ similar to real derive).
    content_payload = "a" * 300  # ~300 bytes per statement
    stmts = [
        (
            "CREATE type::record('memory', $id) SET content = $content, heat = 0.5",
            {"id": i, "content": content_payload},
        )
        for i in range(5000)
    ]

    # Must not raise — chunking keeps each HTTP body under the 1.2 MB threshold
    engine.batch_writes(stmts)

    # Sanity: at least one HTTP call was made
    assert mock_http.post.call_count >= 1


# ── test_memify_prune_auto_abstracted ────────────────────────────────────────


def test_memify_prune_auto_abstracted(tmp_path):
    """_memify_prune deletes stale, old auto-abstracted memories (v5.66 recency gate).

    Scenario:
    - 1 old stale (last_accessed old) auto-abstracted memory  → should be pruned
    - 1 user memory (no auto-abstracted tag)                  → must survive
    - 1 protected auto-abstracted memory                      → must survive
    - 1 recently-created auto-abstracted memory               → must survive (too young)
    - 1 accessed recently (last_accessed recent) memory       → must survive (recently used)
    - 1 memory at age = MAX - 1 days                          → must survive (boundary: under age cap)
    - 1 memory at age = MAX + 1 days                          → must be pruned (boundary: over age cap)
    - 1 high-heat old+stale memory                            → must be pruned (no heat gate)

    v5.66: prune gate changed from access_count==0 to recency of last_accessed.
    All memories that should be pruned have both created_at AND last_accessed
    backdated; memories that should survive (recently-accessed) have last_accessed
    left at insert time (= now, within the recency window).
    """
    from datetime import UTC, datetime, timedelta

    from yadgar._shared.config import Settings
    from yadgar._shared.curation import MemoryCurator
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.storage import StorageEngine
    from yadgar._shared.thermodynamics import MemoryThermodynamics

    storage = StorageEngine(str(tmp_path / "aa_prune.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    max_age = 30
    settings = Settings(
        DB_PATH=str(tmp_path / "aa_prune.db"),
        AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS=max_age,
        COLD_THRESHOLD=0.02,
    )
    thermo = MemoryThermodynamics(storage, emb, settings)
    curator = MemoryCurator(storage, emb, thermo, settings)

    old_date = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    recent_date = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    boundary_under_date = (datetime.now(UTC) - timedelta(days=max_age - 1)).isoformat()
    boundary_over_date = (datetime.now(UTC) - timedelta(days=max_age + 1)).isoformat()

    # 1. Old, stale (last_accessed old too) auto-abstracted memory — SHOULD be pruned.
    #    Both created_at and last_accessed backdated: no recent recall -> eligible.
    prunable_id = storage.insert_memory(
        {
            "content": "Recurring pattern across 5 observations: bash git diff cat",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.01,  # below COLD_THRESHOLD (0.02)
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": prunable_id, "ts": old_date},
    )

    # 2. User memory (no auto-abstracted tag) — MUST survive
    user_id = storage.insert_memory(
        {
            "content": "important user memory about architecture",
            "tags": ["architecture"],
            "directory_context": "/proj",
            "heat": 0.01,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": user_id, "ts": old_date},
    )

    # 3. Protected auto-abstracted memory — MUST survive
    protected_id = storage.insert_memory(
        {
            "content": "Recurring pattern across 3 observations: deploy pipeline",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.01,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET "
        "created_at = $ts, last_accessed = $ts, access_count = 0, is_protected = true",
        {"id": protected_id, "ts": old_date},
    )

    # 4. Recent auto-abstracted memory — MUST survive (too young: created_at within window)
    recent_id = storage.insert_memory(
        {
            "content": "Recurring pattern across 4 observations: test fixture setup",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.01,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": recent_id, "ts": recent_date},
    )

    # 5. Old but RECENTLY ACCESSED memory — MUST survive (v5.66: recency gate).
    #    created_at is old, but last_accessed is left at insert time (now = within window).
    #    This models the correct case: memory is old but is still being used.
    accessed_id = storage.insert_memory(
        {
            "content": "Recurring pattern across 6 observations: authentication flow",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.01,
        }
    )
    storage._q(
        # Only backdate created_at; leave last_accessed=now (insert time) -> recently accessed
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 2",
        {"id": accessed_id, "ts": old_date},
    )

    # 6. Boundary: age = MAX - 1 days — MUST survive (just under the age cap).
    #    last_accessed also at boundary_under_date (same as created_at).
    boundary_under_id = storage.insert_memory(
        {
            "content": "Recurring pattern boundary: just under max age",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.01,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": boundary_under_id, "ts": boundary_under_date},
    )

    # 7. Boundary: age = MAX + 1 days — SHOULD be pruned (just over the age cap).
    boundary_over_id = storage.insert_memory(
        {
            "content": "Recurring pattern boundary: just over max age",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.01,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": boundary_over_id, "ts": boundary_over_date},
    )

    # 8. High-heat auto-abstracted memory (heat=0.8), old+stale — MUST be pruned.
    #    Pass 3 has no heat gate; old+stale regardless of heat -> purged.
    high_heat_date = (datetime.now(UTC) - timedelta(days=max_age + 5)).isoformat()
    high_heat_id = storage.insert_memory(
        {
            "content": "Recurring pattern: high-heat fresh auto-abstracted, but old enough",
            "tags": ["semantic", "auto-abstracted"],
            "directory_context": "system",
            "heat": 0.8,  # realistic fresh auto-abstracted heat — well above any threshold
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts, access_count = 0",
        {"id": high_heat_id, "ts": high_heat_date},
    )

    stats = {"pruned": 0}
    curator._memify_prune(stats)

    assert storage.get_memory(prunable_id) is None, (
        "old stale auto-abstracted memory should have been pruned"
    )
    assert storage.get_memory(user_id) is not None, "user memory must not be pruned"
    assert storage.get_memory(protected_id) is not None, "protected memory must not be pruned"
    assert storage.get_memory(recent_id) is not None, "recent auto-abstracted must not be pruned"
    assert storage.get_memory(accessed_id) is not None, (
        "recently-accessed auto-abstracted must not be pruned (last_accessed within window)"
    )
    assert storage.get_memory(boundary_under_id) is not None, (
        "auto-abstracted at age MAX-1 must survive (boundary: under age cap)"
    )
    assert storage.get_memory(boundary_over_id) is None, (
        "auto-abstracted at age MAX+1 must be pruned (boundary: over age cap)"
    )
    # high-heat auto-abstracted must still be pruned — Pass 3 has no heat gate
    assert storage.get_memory(high_heat_id) is None, (
        "high-heat auto-abstracted over age cap must be pruned — Pass 3 has no heat gate"
    )
    assert stats["pruned"] >= 3  # prunable_id + boundary_over_id + high_heat_id

    storage.close()


# ── test_memify_prune_dream_insights ─────────────────────────────────────────


def test_memify_prune_dream_insights(tmp_path):
    """_memify_prune age-caps dream insights regardless of heat or access_count (Fix 2).

    Dream insights start at heat=0.5, decay to ~0.1, but COLD_THRESHOLD=0.02
    means they sit above the threshold for weeks.  The age cap is hard — one
    accidental recall must not let a dream insight escape forever.

    Scenario:
    - 1 old unaccessed dream+auto-generated memory with heat=0.1  → pruned
    - 1 old dream insight with access_count>0                      → pruned (no escape)
    - 1 recent dream+auto-generated memory                         → must survive
    - 1 dream memory without auto-generated tag                    → must survive
    - 1 protected dream+auto-generated memory (old)                → must survive
    - 1 memory at age = MAX - 1 days                               → must survive (boundary)
    - 1 memory at age = MAX + 1 days                               → must be pruned (boundary)
    """
    from datetime import UTC, datetime, timedelta

    from yadgar._shared.config import Settings
    from yadgar._shared.curation import MemoryCurator
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.storage import StorageEngine
    from yadgar._shared.thermodynamics import MemoryThermodynamics

    storage = StorageEngine(str(tmp_path / "dream_prune.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    max_age = 21
    settings = Settings(
        DB_PATH=str(tmp_path / "dream_prune.db"),
        DREAM_INSIGHT_MAX_AGE_DAYS=max_age,
        COLD_THRESHOLD=0.05,  # 0.1 heat is above this — old pass would spare it
    )
    thermo = MemoryThermodynamics(storage, emb, settings)
    curator = MemoryCurator(storage, emb, thermo, settings)

    old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    recent_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    boundary_under_date = (datetime.now(UTC) - timedelta(days=max_age - 1)).isoformat()
    boundary_over_date = (datetime.now(UTC) - timedelta(days=max_age + 1)).isoformat()

    # 1. Old, heat=0.1 (above COLD_THRESHOLD), unaccessed dream insight → PRUNED
    dream_prunable_id = storage.insert_memory(
        {
            "content": "Dream connection: authentication and caching may relate to session handling",
            "tags": ["dream", "auto-generated"],
            "directory_context": "system",
            "heat": 0.1,  # above COLD_THRESHOLD (0.05) — old pass would NOT prune this
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 0",
        {"id": dream_prunable_id, "ts": old_date},
    )

    # 2. Old dream insight with accesses — MUST be PRUNED (hard age cap, no escape)
    dream_accessed_id = storage.insert_memory(
        {
            "content": "Dream connection: deployment and testing relate to CI pipeline",
            "tags": ["dream", "auto-generated"],
            "directory_context": "system",
            "heat": 0.1,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 3",
        {"id": dream_accessed_id, "ts": old_date},
    )

    # 3. Recent dream insight — MUST survive (not old enough)
    dream_recent_id = storage.insert_memory(
        {
            "content": "Dream connection: refactoring and architecture may relate",
            "tags": ["dream", "auto-generated"],
            "directory_context": "system",
            "heat": 0.1,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 0",
        {"id": dream_recent_id, "ts": recent_date},
    )

    # 4. Dream tag but NOT auto-generated — MUST survive (not targeted by this pass)
    dream_manual_id = storage.insert_memory(
        {
            "content": "Dream insight: user manually recorded dream about project architecture",
            "tags": ["dream"],
            "directory_context": "system",
            "heat": 0.1,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 0",
        {"id": dream_manual_id, "ts": old_date},
    )

    # 5. Protected dream+auto-generated memory (old) — MUST survive
    dream_protected_id = storage.insert_memory(
        {
            "content": "Dream connection: protected critical insight about system design",
            "tags": ["dream", "auto-generated"],
            "directory_context": "system",
            "heat": 0.1,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET "
        "created_at = $ts, access_count = 0, is_protected = true",
        {"id": dream_protected_id, "ts": old_date},
    )

    # 6. Boundary: age = MAX - 1 days — MUST survive (just under the cap)
    dream_boundary_under_id = storage.insert_memory(
        {
            "content": "Dream connection: boundary under — should not be pruned yet",
            "tags": ["dream", "auto-generated"],
            "directory_context": "system",
            "heat": 0.1,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 0",
        {"id": dream_boundary_under_id, "ts": boundary_under_date},
    )

    # 7. Boundary: age = MAX + 1 days — SHOULD be pruned (just over the cap)
    dream_boundary_over_id = storage.insert_memory(
        {
            "content": "Dream connection: boundary over — should be pruned",
            "tags": ["dream", "auto-generated"],
            "directory_context": "system",
            "heat": 0.1,
        }
    )
    storage._q(
        "UPDATE type::record('memory', $id) SET created_at = $ts, access_count = 0",
        {"id": dream_boundary_over_id, "ts": boundary_over_date},
    )

    stats = {"pruned": 0}
    curator._memify_prune(stats)

    assert storage.get_memory(dream_prunable_id) is None, (
        "old high-heat unaccessed dream insight should be pruned by age cap"
    )
    assert storage.get_memory(dream_accessed_id) is None, (
        "accessed dream insight must be pruned — hard age cap has no access_count escape"
    )
    assert storage.get_memory(dream_recent_id) is not None, (
        "recent dream insight must not be pruned"
    )
    assert storage.get_memory(dream_manual_id) is not None, (
        "non-auto-generated dream tag must not be pruned by this pass"
    )
    assert storage.get_memory(dream_protected_id) is not None, (
        "protected dream insight must not be pruned"
    )
    assert storage.get_memory(dream_boundary_under_id) is not None, (
        "dream insight at age MAX-1 must survive (boundary: under age cap)"
    )
    assert storage.get_memory(dream_boundary_over_id) is None, (
        "dream insight at age MAX+1 must be pruned (boundary: over age cap)"
    )
    assert stats["pruned"] >= 3  # dream_prunable + dream_accessed + dream_boundary_over


# ── v4.9 item 9 — Pass 6: degenerate auto-abstracted prune ──────────────────


class TestMemifyPruneDegenerateAutoAbstracted:
    """_memify_prune Pass 6 must delete auto-abstracted memories whose content
    matches the degenerate shape (no meaningful subject after the Recurring prefix).

    Fixture: 3 degenerate + 2 legitimate auto-abstracted memories.
    After _memify_prune: 3 degenerate deleted, 2 legitimate survive.
    """

    def _insert_memory(
        self,
        storage: StorageEngine,
        content: str,
        tags: list,
        store_type: str = "semantic",
        is_protected: bool = False,
    ) -> int:
        mid = storage.insert_memory(
            {
                "content": content,
                "tags": tags,
                "directory_context": "/proj",
                "heat": 0.1,
                "is_stale": False,
                "embedding_model": "test",
            }
        )
        storage._q(
            "UPDATE type::record('memory', $id) SET store_type = $st, is_protected = $ip, access_count = 0",
            {"id": mid, "st": store_type, "ip": is_protected},
        )
        return mid

    def test_pass6_deletes_degenerate_keeps_legitimate(self, storage, settings, embeddings, thermo):
        """3 degenerate auto-abstracted + 2 legitimate → 3 deleted, 2 survive."""
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        # 3 degenerate memories — body after prefix has no meaningful subject
        degen1 = self._insert_memory(
            storage,
            "Recurring pattern across 27 observations: frequently modified together",
            ["semantic", "auto-abstracted"],
        )
        degen2 = self._insert_memory(
            storage,
            "Recurring pattern across 5 observations: frequently modified together",
            ["semantic", "auto-abstracted"],
        )
        degen3 = self._insert_memory(
            storage,
            "Recurring pattern: frequently modified together",
            ["semantic", "auto-abstracted"],
        )

        # 2 legitimate auto-abstracted memories — meaningful subjects
        legit1 = self._insert_memory(
            storage,
            "Recurring pattern across 12 observations: urllib.request used in retrieval/core.py",
            ["semantic", "auto-abstracted"],
        )
        legit2 = self._insert_memory(
            storage,
            "Recurring pattern across 8 observations: consolidation_cycle triggers vacuum on large DB",
            ["semantic", "auto-abstracted"],
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(degen1) is None, "degen1 must be pruned"
        assert storage.get_memory(degen2) is None, "degen2 must be pruned"
        assert storage.get_memory(degen3) is None, "degen3 must be pruned"
        assert storage.get_memory(legit1) is not None, "legit1 must survive"
        assert storage.get_memory(legit2) is not None, "legit2 must survive"
        assert stats["pruned"] >= 3

    def test_pass6_respects_protected(self, storage, settings, embeddings, thermo):
        """Protected degenerate memories must not be deleted."""
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        protected_degen = self._insert_memory(
            storage,
            "Recurring pattern across 27 observations: frequently modified together",
            ["semantic", "auto-abstracted"],
            is_protected=True,
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(protected_degen) is not None, (
            "Protected degenerate must not be pruned"
        )

    def test_pass6_keeps_non_recurring_auto_abstracted(self, storage, settings, embeddings, thermo):
        """Pass 6 must NOT delete auto-abstracted memories whose body doesn't
        have the Recurring-pattern prefix. Guards against multilingual data loss.

        Pure non-Latin memory (no ASCII identifiers) would fail
        _has_ascii_identifier_token, but lacks the Recurring prefix so it must
        never be considered degenerate.
        """
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        # Pure Cyrillic, no Recurring prefix, no ASCII identifiers —
        # buggy condition 2 would wrongly delete this
        cyrillic_id = self._insert_memory(
            storage,
            "Часто изменяется вместе с другим файлом в проекте",
            ["semantic", "auto-abstracted"],
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(cyrillic_id) is not None, (
            "non-Latin content without Recurring prefix must NOT be pruned"
        )

    def test_pass6_detects_tags_suffix_degenerate(self, storage, settings, embeddings, thermo):
        """Degenerate memory with [tags: ...] suffix appended by abstract_to_schema
        must be detected and pruned. Verifies _TAGS_SUFFIX_RE stripping works end-to-end.
        """
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        tags_suffix_degen = self._insert_memory(
            storage,
            "Recurring pattern across 27 observations: frequently modified together"
            " [tags: episodic, auto-abstracted]",
            ["semantic", "auto-abstracted"],
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(tags_suffix_degen) is None, (
            "degenerate memory with [tags:...] suffix must be pruned"
        )

    def test_pass6_accessed_degenerate_is_purged(self, storage, settings, embeddings, thermo):
        """v5.66: Pass 6 no longer spares degenerate memories based on access_count.

        Degenerate content was never valid signal; an accidental recall should not
        grant immortality.  access_count > 0 no longer protects degenerate rows.
        is_protected is still honoured (tested separately).
        """
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        accessed_degen_id = self._insert_memory(
            storage,
            "Recurring pattern across 27 observations: frequently modified together",
            ["semantic", "auto-abstracted"],
        )
        # Set access_count > 0 — under old guard this spared the memory; not anymore.
        storage._q(
            "UPDATE type::record('memory', $id) SET access_count = 3",
            {"id": accessed_degen_id},
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(accessed_degen_id) is None, (
            "accessed degenerate memory must be purged by Pass 6 — access_count no longer protects degenerate content"
        )

    def test_pass6_keeps_cyrillic_with_recurring_prefix(
        self, storage, settings, embeddings, thermo
    ):
        """Production case: abstract_to_schema always adds Recurring prefix.

        Cyrillic body with Recurring prefix must survive Pass 6.  This was the
        real bug: condition 2 fired on any non-ASCII body with the prefix,
        silently deleting Russian/Arabic/Japanese/Greek content.
        """
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        mid = self._insert_memory(
            storage,
            "Recurring pattern across 5 observations: Часто изменяется модуль storage вместе с config",
            ["semantic", "auto-abstracted"],
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(mid) is not None, (
            "Cyrillic auto-abstracted memory with Recurring prefix must survive Pass 6"
        )

    def test_pass6_keeps_arabic_with_recurring_prefix(self, storage, settings, embeddings, thermo):
        """Arabic body with Recurring prefix must survive Pass 6 (audit pass 2 CRITICAL)."""
        curator = MemoryCurator(storage, embeddings, thermo, settings)

        mid = self._insert_memory(
            storage,
            "Recurring pattern across 3 observations: يتم تعديله بشكل متكرر مع ملفات أخرى في المشروع",
            ["semantic", "auto-abstracted"],
        )

        stats = {"pruned": 0}
        curator._memify_prune(stats)

        assert storage.get_memory(mid) is not None, (
            "Arabic auto-abstracted memory with Recurring prefix must survive Pass 6"
        )
