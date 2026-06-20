"""Tests for the causal discovery dispatch gate in _consolidation_cycle.

The PC algorithm dispatch is gated on `_events_since_last_discovery >= 50`.
These tests verify:
  1. Gate fires when a single cycle produces >= 50 memories.
  2. Gate fires via accumulation across two cycles (each < 50, total >= 50).
  3. Counter resets to 0 after discover_dag() is called, so a small subsequent
     cycle does NOT re-fire.
  4. No AttributeError or crash when `_causal_discovery` is None.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar.config import Settings
from yadgar.consolidation import ConsolidationScheduler
from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "causal_dispatch.db"))
    yield engine
    engine.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "causal_dispatch.db"),
        DECAY_FACTOR=0.95,
        COLD_THRESHOLD=0.05,
    )


@pytest.fixture()
def embeddings():
    eng = EmbeddingEngine()
    eng._unavailable = True  # skip model loading in tests
    return eng


def _make_scheduler(storage, embeddings, settings) -> ConsolidationScheduler:
    """Build a ConsolidationScheduler with all heavy sub-systems mocked out."""
    sched = ConsolidationScheduler(storage, embeddings, settings)

    # Replace the causal discovery instance with a mock so we can track calls
    mock_cd = MagicMock()
    mock_cd.discover_dag.return_value = {"metadata": {"directed_count": 3}}
    sched._causal_discovery = mock_cd

    # Stub every memory-producing phase so the cycle doesn't need real DB data
    # _process_action_log, cls.consolidation_cycle, curator.memify_cycle
    # Return zeros by default — tests override per-case below.
    sched._process_action_log = MagicMock(return_value={"processed": 0, "memories_created": 0})
    sched._cls.consolidation_cycle = MagicMock(
        return_value={"patterns_found": 0, "promoted": 0, "skipped_inconsistent": 0}
    )
    sched._curator.memify_cycle = MagicMock(
        return_value={"pruned": 0, "strengthened": 0, "reweighted": 0, "derived": 0}
    )

    return sched


def _run_cycle_with_counts(
    sched: ConsolidationScheduler,
    *,
    action_memories: int = 0,
    cls_promoted: int = 0,
    memify_derived: int = 0,
):
    """Configure stub return values then run one _consolidation_cycle."""
    sched._process_action_log.return_value = {
        "processed": action_memories,
        "memories_created": action_memories,
    }
    sched._cls.consolidation_cycle.return_value = {
        "patterns_found": cls_promoted,
        "promoted": cls_promoted,
        "skipped_inconsistent": 0,
    }
    sched._curator.memify_cycle.return_value = {
        "pruned": 0,
        "strengthened": 0,
        "reweighted": 0,
        "derived": memify_derived,
    }
    return sched._consolidation_cycle()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCausalDispatchGate:
    def test_dispatch_fires_when_single_cycle_produces_enough_memories(
        self, storage, embeddings, settings
    ):
        """One cycle with action_memories=40 + cls_promoted=10 + memify_derived=5 = 55 >= 50.

        discover_dag() must be called exactly once.
        """
        sched = _make_scheduler(storage, embeddings, settings)
        assert sched._causal_discovery.discover_dag.call_count == 0

        _run_cycle_with_counts(sched, action_memories=40, cls_promoted=10, memify_derived=5)

        assert sched._causal_discovery.discover_dag.call_count == 1

    def test_dispatch_fires_via_accumulation_across_two_cycles(self, storage, embeddings, settings):
        """Two cycles of 30 memories each (total 60) must trigger discover_dag once."""
        sched = _make_scheduler(storage, embeddings, settings)

        _run_cycle_with_counts(sched, action_memories=30)
        assert sched._causal_discovery.discover_dag.call_count == 0, (
            "First cycle (30 < 50) must NOT fire dispatch"
        )

        _run_cycle_with_counts(sched, action_memories=30)
        assert sched._causal_discovery.discover_dag.call_count == 1, (
            "Second cycle (cumulative 60 >= 50) must fire dispatch"
        )

    def test_dispatch_resets_after_discovery(self, storage, embeddings, settings):
        """After discover_dag fires, _events_since_last_discovery resets.

        A subsequent cycle with only 10 new memories must NOT re-fire.
        """
        sched = _make_scheduler(storage, embeddings, settings)

        # First cycle: 60 memories → fires
        _run_cycle_with_counts(sched, action_memories=60)
        assert sched._causal_discovery.discover_dag.call_count == 1
        assert sched._events_since_last_discovery == 0

        # Second cycle: 10 memories (< 50) → must NOT fire again
        _run_cycle_with_counts(sched, action_memories=10)
        assert sched._causal_discovery.discover_dag.call_count == 1

    def test_dispatch_skips_when_causal_discovery_disabled(self, storage, embeddings, settings):
        """When _causal_discovery is None the cycle must complete without error."""
        sched = _make_scheduler(storage, embeddings, settings)
        sched._causal_discovery = None  # simulate disabled

        # Should not raise
        stats = _run_cycle_with_counts(sched, action_memories=100)
        # Cycle ran, no causal_dag_edges key (discovery never ran)
        assert "causal_dag_edges" not in stats
