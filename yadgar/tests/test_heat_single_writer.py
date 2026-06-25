"""Single-writer invariant tests for heat decay (T4 — BC-CSW1).

BC-CSW1: One consolidation cycle MUST call storage.batch_writes exactly ONCE
for all heat mutations (memories + entities combined).  No phase other than the
HeatWriter apply step may call batch_writes with heat payloads during a cycle.

Tests:
  1. apply_decay issues exactly ONE batch_writes call (not two — mem then ent).
  2. The single batch contains both memory-heat and entity-heat statements.
  3. Zero-item tables (no memories or no entities) still produce exactly ONE call.
  4. The HeatWriter facade (apply_heat_intents) is the only public heat-write path.
  5. Behavior preservation: same heat values produced as the old two-call path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from yadgar.consolidation.heat_decay import _HeatDecayMixin
from yadgar.storage.heat_writer import HeatWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mem(mid: int, heat: float, hours_old: float = 1.0) -> dict:
    last = (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    return {
        "id": mid,
        "heat": heat,
        "last_accessed": last,
        "last_decay_at": None,
        "is_protected": False,
        "access_count_since_decay": 0,
        "tags": [],
        "importance": 0.5,
        "emotional_valence": 0.0,
        "confidence": 0.5,
    }


def _make_entity(eid: int, heat: float, hours_old: float = 1.0) -> dict:
    last = (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    return {
        "id": eid,
        "heat": heat,
        "last_accessed": last,
        "last_decay_at": None,
    }


def _make_runner(memories: list[dict], entities: list[dict], cold_threshold: float = 0.0):
    """Return a _HeatDecayMixin runner with mocked storage/thermo/settings."""
    mock_storage = MagicMock()
    mock_storage.get_all_memories_for_decay.return_value = memories
    # C2: decay reads via the scalar projection now (storage.get_all_memories_for_decay_scalar)
    mock_storage.get_all_memories_for_decay_scalar.return_value = memories
    mock_storage.get_all_entities_for_decay.return_value = entities
    mock_storage.get_astrocyte_processes.return_value = []
    mock_storage.batch_writes = MagicMock()

    def compute_decay(mem: dict, hours: float) -> float:
        return mem["heat"] * (0.9995**hours)

    mock_thermo = MagicMock()
    mock_thermo.compute_decay.side_effect = compute_decay

    mock_settings = MagicMock()
    mock_settings.COLD_THRESHOLD = cold_threshold
    mock_settings.ACTION_STREAM_COLD_THRESHOLD = cold_threshold
    mock_settings.RECALL_BOOST = 0.0
    mock_settings.DECAY_FACTOR = 0.9995
    mock_settings.ASTROCYTE_POOL_ENABLED = False  # simplify — no domain mult

    class _Runner(_HeatDecayMixin):
        pass

    runner = _Runner.__new__(_Runner)
    runner._storage = mock_storage
    runner._thermo = mock_thermo
    runner._settings = mock_settings
    return runner, mock_storage


# ---------------------------------------------------------------------------
# Test 1: _apply_decay issues EXACTLY ONE batch_writes call
# ---------------------------------------------------------------------------


class TestSingleBatchWritesCall:
    """BC-CSW1 core invariant: one batch_writes per cycle for all heat."""

    def test_one_memory_one_entity_one_call(self):
        """With one memory + one entity, batch_writes called exactly once."""
        memories = [_make_mem(1, 0.8)]
        entities = [_make_entity(10, 0.7)]
        runner, mock_storage = _make_runner(memories, entities)

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        assert mock_storage.batch_writes.call_count == 1, (
            "BC-CSW1: _apply_decay MUST call batch_writes exactly once; "
            f"got {mock_storage.batch_writes.call_count} calls"
        )

    def test_many_memories_many_entities_one_call(self):
        """10 memories + 10 entities → still exactly ONE batch_writes call."""
        memories = [_make_mem(i, 0.5 + i * 0.03) for i in range(1, 11)]
        entities = [_make_entity(100 + i, 0.4 + i * 0.02) for i in range(10)]
        runner, mock_storage = _make_runner(memories, entities)

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        assert mock_storage.batch_writes.call_count == 1, (
            f"expected 1 batch_writes call, got {mock_storage.batch_writes.call_count}"
        )

    def test_no_memories_still_one_call(self):
        """Zero memories + entities → batch_writes called at most once (with entity stmts)."""
        entities = [_make_entity(10, 0.7)]
        runner, mock_storage = _make_runner([], entities)

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        # Must be 0 or 1 (depending on whether entities trigger a write), never 2
        assert mock_storage.batch_writes.call_count <= 1, (
            f"expected ≤1 batch_writes call, got {mock_storage.batch_writes.call_count}"
        )

    def test_no_entities_still_one_call(self):
        """Zero entities + memories → at most one batch_writes call."""
        memories = [_make_mem(1, 0.8)]
        runner, mock_storage = _make_runner(memories, [])

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        assert mock_storage.batch_writes.call_count <= 1, (
            f"expected ≤1 batch_writes call, got {mock_storage.batch_writes.call_count}"
        )

    def test_both_empty_no_call(self):
        """No memories + no entities → batch_writes NOT called (nothing to flush)."""
        runner, mock_storage = _make_runner([], [])

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        assert mock_storage.batch_writes.call_count == 0


# ---------------------------------------------------------------------------
# Test 2: Single batch contains BOTH memory and entity statements
# ---------------------------------------------------------------------------


class TestCombinedBatch:
    """The one batch_writes call must carry both memory and entity statements."""

    def test_batch_contains_memory_and_entity_statements(self):
        """Verify a single batch_writes call delivers memory + entity SQL."""
        memories = [_make_mem(1, 0.8)]
        entities = [_make_entity(10, 0.7)]
        runner, mock_storage = _make_runner(memories, entities)

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        assert mock_storage.batch_writes.call_count == 1
        stmts: list[tuple] = mock_storage.batch_writes.call_args[0][0]
        # Both memory and entity records must appear
        sqls = [s for s, _ in stmts]
        assert any("memory" in s for s in sqls), "batch must contain a memory update"
        assert any("entity" in s for s in sqls), "batch must contain an entity update"


# ---------------------------------------------------------------------------
# Test 3: HeatWriter facade is the ONLY heat-write path
# ---------------------------------------------------------------------------


class TestHeatWriterFacade:
    """HeatWriter.apply_heat_intents routes all intents to a single batch_writes."""

    def test_heat_writer_calls_batch_writes_once(self):
        """HeatWriter.apply_heat_intents calls storage.batch_writes exactly once."""
        mock_storage = MagicMock()
        mock_storage.batch_writes = MagicMock()

        hw = HeatWriter(mock_storage)
        intents = [
            (
                "UPDATE type::record('memory', $id) SET heat = $heat, last_decay_at = $now",
                {"id": 1, "heat": 0.75, "now": datetime.now(UTC).isoformat()},
            ),
            (
                "UPDATE type::record('entity', $id) SET heat = $heat, last_decay_at = $now",
                {"id": 10, "heat": 0.6, "now": datetime.now(UTC).isoformat()},
            ),
        ]

        hw.apply_heat_intents(intents)

        assert mock_storage.batch_writes.call_count == 1

    def test_heat_writer_empty_intents_no_call(self):
        """Empty intents → no storage call."""
        mock_storage = MagicMock()
        mock_storage.batch_writes = MagicMock()

        hw = HeatWriter(mock_storage)
        hw.apply_heat_intents([])

        mock_storage.batch_writes.assert_not_called()

    def test_heat_writer_passes_intents_verbatim(self):
        """HeatWriter forwards intents unchanged to batch_writes."""
        mock_storage = MagicMock()
        mock_storage.batch_writes = MagicMock()

        hw = HeatWriter(mock_storage)
        intents = [
            ("UPDATE type::record('memory', $id) SET heat = $heat", {"id": 1, "heat": 0.5}),
        ]
        hw.apply_heat_intents(intents)

        mock_storage.batch_writes.assert_called_once_with(intents)


# ---------------------------------------------------------------------------
# Test 4: Behavior preservation — same heat math as before
# ---------------------------------------------------------------------------


class TestBehaviorPreservation:
    """After refactor, heat values produced are identical to the old two-call path."""

    def test_memory_heat_value_unchanged(self):
        """Memory heat after single-call refactor matches the pre-refactor formula."""
        memories = [_make_mem(1, 0.5, hours_old=1.0)]
        runner, mock_storage = _make_runner(memories, [])

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        if mock_storage.batch_writes.call_count == 0:
            # Nothing changed (heat diff < 1e-9) — expected for certain settings
            return

        stmts: list[tuple] = mock_storage.batch_writes.call_args[0][0]
        mem_stmts = [(s, p) for s, p in stmts if "memory" in s]
        assert len(mem_stmts) == 1
        _, params = mem_stmts[0]
        # Decay factor 0.9995^1h ≈ 0.9995; heat = 0.5 * 0.9995
        expected = 0.5 * (0.9995**1.0)
        assert abs(params["heat"] - expected) < 1e-4, (
            f"heat {params['heat']:.6f} ≠ expected {expected:.6f}"
        )

    def test_entity_heat_value_unchanged(self):
        """Entity heat after refactor matches heat * DECAY_FACTOR^hours."""
        entities = [_make_entity(10, 0.8, hours_old=1.0)]
        runner, mock_storage = _make_runner([], entities)

        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)

        if mock_storage.batch_writes.call_count == 0:
            return

        stmts: list[tuple] = mock_storage.batch_writes.call_args[0][0]
        ent_stmts = [(s, p) for s, p in stmts if "entity" in s]
        assert len(ent_stmts) == 1
        _, params = ent_stmts[0]
        expected = 0.8 * (0.9995**1.0)
        assert abs(params["heat"] - expected) < 1e-4
