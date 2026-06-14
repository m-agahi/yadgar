"""C2 — Recall-frequency-modulated decay tests.

Tests verify:
1. Memory with access_count_since_decay=0 → heat reduced by decay factor (baseline regression).
2. Memory with access_count_since_decay=10, heat=0.5 → boosted to 1.0 (capped).
3. Memory with access_count_since_decay=1, heat=0.8 → small boost.
4. After decay, access_count_since_decay is reset to 0.
5. Protected memory: heat stays at 1.0 (decay skipped).
6. YADGAR_RECALL_BOOST=0.0 env → no boost, pure decay.
7. Entity decay — golden value: heat = init * DECAY_FACTOR^hours.
8. Entity cold-archival — goes_cold sets heat=0.0 and archived=True.
"""

from __future__ import annotations

import pytest

from yadgar.config import Settings
from yadgar.storage import StorageEngine

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_settings(**kwargs) -> Settings:
    base = dict(
        DECAY_FACTOR=0.9995,
        IMPORTANCE_DECAY_FACTOR=0.9999,
        COLD_THRESHOLD=0.0,
        ACTION_STREAM_COLD_THRESHOLD=0.0,
        RECALL_BOOST=0.05,
    )
    base.update(kwargs)
    return Settings(**base)


def _insert_memory(
    storage,
    heat: float,
    access_count_since_decay: int = 0,
    is_protected: bool = False,
    importance: float = 0.5,
    tags: list | None = None,
) -> int:
    """Insert a bare memory for decay testing."""
    now = storage._now_iso()
    mid = storage._next_id("memory")
    # Use a last_accessed 1 hour in the past to ensure hours_elapsed = 1.0
    from datetime import UTC, datetime, timedelta

    last = (datetime.now(UTC) - timedelta(hours=1.0)).isoformat()
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, tags = $tags, directory_context = $dir, "
        "created_at = $ts, last_accessed = $last, heat = $heat, "
        "is_stale = false, plasticity = 1.0, stability = 0.0, "
        "excitability = 1.0, store_type = $st, compression_level = 0, "
        "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
        "provenance_agent = $agent, vector_clock = $vc, is_protected = $prot, "
        "importance = $imp, emotional_valence = 0.0, confidence = 1.0, "
        "access_count = 0, useful_count = 0, "
        "access_count_since_decay = $acd",
        {
            "id": mid,
            "content": "test memory for decay",
            "tags": tags or [],
            "dir": "/tmp",
            "ts": now,
            "last": last,
            "heat": heat,
            "st": "episodic",
            "agent": "default",
            "vc": "{}",
            "prot": is_protected,
            "imp": importance,
            "acd": access_count_since_decay,
        },
    )
    return mid


def _run_decay(storage, settings: Settings) -> dict:
    """Instantiate ConsolidationScheduler-like context and run _apply_decay."""
    from yadgar.consolidation.heat_decay import _HeatDecayMixin
    from yadgar.embeddings import EmbeddingEngine
    from yadgar.thermodynamics import MemoryThermodynamics

    thermo = MemoryThermodynamics(storage, EmbeddingEngine(), settings)

    class _Runner(_HeatDecayMixin):
        def __init__(self):
            self._storage = storage
            self._settings = settings
            self._thermo = thermo

    runner = _Runner()
    stats: dict = {
        "memories_updated": 0,
        "memories_archived": 0,
        "entities_updated": 0,
    }
    runner._apply_decay(stats)
    return stats


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "decay_test.db"), embedding_dim=384)
    yield engine
    engine.close()


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_baseline_no_boost(storage):
    """T1: access_count_since_decay=0 → heat decays by ~DECAY_FACTOR^1 (no boost)."""
    settings = _make_settings(RECALL_BOOST=0.05)
    mid = _insert_memory(storage, heat=0.5, access_count_since_decay=0)

    _run_decay(storage, settings)

    mem = storage.get_memory(mid)
    # With hours=1, no modifiers (importance=0.5, valence=0, confidence=1.0):
    # effective_factor ≈ DECAY_FACTOR (0.9995); new_heat = 0.5 * 0.9995^1 + 0 * 0.05
    expected = 0.5 * (settings.DECAY_FACTOR**1)
    assert mem["heat"] == pytest.approx(expected, rel=1e-3)


def test_recall_boost_caps_at_1(storage):
    """T2: access_count_since_decay=20, heat=0.8 → decayed+boost > 1.0 → capped at 1.0."""
    settings = _make_settings(RECALL_BOOST=0.05)
    # 0.8 * 0.9995^1 ≈ 0.7996 + 20 * 0.05 = 1.7996 → capped at 1.0
    mid = _insert_memory(storage, heat=0.8, access_count_since_decay=20)

    _run_decay(storage, settings)

    mem = storage.get_memory(mid)
    assert mem["heat"] == pytest.approx(1.0, rel=1e-6)


def test_small_boost(storage):
    """T3: access_count_since_decay=1, heat=0.8 → small boost applied."""
    settings = _make_settings(RECALL_BOOST=0.05)
    mid = _insert_memory(storage, heat=0.8, access_count_since_decay=1)

    _run_decay(storage, settings)

    mem = storage.get_memory(mid)
    # decayed = 0.8 * 0.9995^1 ≈ 0.7996; boost = 1 * 0.05 = 0.05; total ≈ 0.8496
    expected_decayed = 0.8 * (settings.DECAY_FACTOR**1)
    expected = min(expected_decayed + 0.05, 1.0)
    assert mem["heat"] == pytest.approx(expected, rel=1e-3)
    assert mem["heat"] > 0.8  # boosted above initial heat


def test_access_count_since_decay_reset(storage):
    """T4: After decay cycle, access_count_since_decay is reset to 0."""
    settings = _make_settings(RECALL_BOOST=0.05)
    mid = _insert_memory(storage, heat=0.5, access_count_since_decay=5)

    _run_decay(storage, settings)

    mem = storage.get_memory(mid)
    assert mem.get("access_count_since_decay", 0) == 0


def test_protected_memory_not_decayed(storage):
    """T5: Protected memory heat stays at 1.0."""
    settings = _make_settings(RECALL_BOOST=0.05)
    mid = _insert_memory(storage, heat=1.0, access_count_since_decay=0, is_protected=True)

    _run_decay(storage, settings)

    mem = storage.get_memory(mid)
    assert mem["heat"] == pytest.approx(1.0, rel=1e-6)


def test_zero_recall_boost_env(storage):
    """T6: YADGAR_RECALL_BOOST=0.0 → no boost, pure decay (back-compat)."""
    settings = _make_settings(RECALL_BOOST=0.0)
    mid = _insert_memory(storage, heat=0.5, access_count_since_decay=10)

    _run_decay(storage, settings)

    mem = storage.get_memory(mid)
    # boost = 10 * 0.0 = 0; pure decay only
    expected = 0.5 * (settings.DECAY_FACTOR**1)
    assert mem["heat"] == pytest.approx(expected, rel=1e-3)


def test_entity_decay_golden_value(storage):
    """T7 (characterization): Entity heat = init * DECAY_FACTOR^hours (1h elapsed)."""
    from datetime import UTC, datetime, timedelta

    settings = _make_settings(DECAY_FACTOR=0.9995, COLD_THRESHOLD=0.0)
    # Insert entity with last_accessed 1 hour ago
    last = (datetime.now(UTC) - timedelta(hours=1.0)).isoformat()
    storage.insert_entity(
        {"name": "test_entity_decay", "type": "function", "heat": 0.8, "last_accessed": last}
    )

    _run_decay(storage, settings)

    ent = storage.get_entity_by_name("test_entity_decay")
    expected = 0.8 * (0.9995**1)
    assert ent["heat"] == pytest.approx(expected, rel=1e-3)
    assert not ent.get("archived", False)


def test_entity_cold_archival(storage):
    """T8 (characterization): Entity goes_cold → heat=0.0, archived=True."""
    from datetime import UTC, datetime, timedelta

    # COLD_THRESHOLD=0.5: any heat < 0.5 after decay triggers archival
    # heat=0.4, hours=1: new_heat = 0.4 * 0.9995^1 ≈ 0.3998 < 0.5 → archived
    settings = _make_settings(DECAY_FACTOR=0.9995, COLD_THRESHOLD=0.5)
    last = (datetime.now(UTC) - timedelta(hours=1.0)).isoformat()
    storage.insert_entity(
        {"name": "cold_entity", "type": "function", "heat": 0.4, "last_accessed": last}
    )

    _run_decay(storage, settings)

    ent = storage.get_entity_by_name("cold_entity")
    assert ent["heat"] == pytest.approx(0.0)
    assert ent.get("archived") is True
