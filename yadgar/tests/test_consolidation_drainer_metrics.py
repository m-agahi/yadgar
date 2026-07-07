"""Tests for dead-metric wiring: consolidation, drainer, curator, engram, astrocyte.

PR-E: v5.6.7 — verifies that previously declared-but-unwired Prometheus metrics
now produce observations after the relevant code paths run.

Isolation strategy: uses _sum.get() delta checks so tests accumulate safely
across the shared module-level yadgar.metrics registry.  Each test captures
a "before" snapshot and asserts the delta is positive after exercising the path.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.storage import StorageEngine

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "metrics_test.db"),
        DECAY_FACTOR=0.95,
        COLD_THRESHOLD=0.05,
        DAEMON_CHECK_INTERVAL=1,
    )


@pytest.fixture
def tmp_storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "metrics_storage.db"))
    yield engine
    engine.close()


@pytest.fixture
def stub_embeddings():
    e = EmbeddingEngine()
    e._unavailable = True  # skip real model loading
    return e


def _hist_sum(hist) -> float:
    """Return current _sum for an unlabeled histogram."""
    return hist._sum.get()


def _labeled_hist_sum(hist, **labels) -> float:
    """Return current _sum for a labeled histogram child (0.0 if not yet observed)."""
    key = tuple(labels[k] for k in hist._labelnames)
    child = hist._metrics.get(key)
    return child._sum.get() if child is not None else 0.0


def _labeled_counter_value(counter, **labels) -> float:
    """Return current _value for a labeled counter child (0.0 if not yet incremented)."""
    key = tuple(labels[k] for k in counter._labelnames)
    child = counter._metrics.get(key)
    return child._value.get() if child is not None else 0.0


def _counter_total(counter) -> float:
    """Sum _value across all labeled children of a counter."""
    return sum(c._value.get() for c in counter._metrics.values())


# ---------------------------------------------------------------------------
# 1. yadgar_consolidation_duration_seconds — full cycle observation
# ---------------------------------------------------------------------------


class TestConsolidationDurationMetric:
    def test_full_cycle_increments_sum(self, tmp_storage, stub_embeddings, tmp_settings):
        """After force_consolidate(), consolidation_duration_seconds{phase=full_cycle} sum > 0."""
        from yadgar._shared.metrics import yadgar_consolidation_duration_seconds
        from yadgar.core.consolidation import ConsolidationScheduler

        before = _labeled_hist_sum(yadgar_consolidation_duration_seconds, phase="full_cycle")

        scheduler = ConsolidationScheduler(tmp_storage, stub_embeddings, tmp_settings)
        scheduler.force_consolidate()

        after = _labeled_hist_sum(yadgar_consolidation_duration_seconds, phase="full_cycle")

        assert after > before, (
            f"yadgar_consolidation_duration_seconds{{phase=full_cycle}} sum did not increase "
            f"(before={before}, after={after})"
        )


# ---------------------------------------------------------------------------
# 2. yadgar_drain_stage_ms{stage="insert"} — drainer stage timing
# ---------------------------------------------------------------------------


class TestDrainStageMsMetric:
    def test_insert_stage_observed_after_drain(self, tmp_path):
        """After a drain cycle with one item, insert stage sum increases."""
        from yadgar._shared.metrics import yadgar_drain_stage_ms
        from yadgar.core.file_queue import FileQueue, QueueDrainer

        before = _labeled_hist_sum(yadgar_drain_stage_ms, stage="insert")

        q = FileQueue(base_dir=tmp_path)
        # _internal=True: approved carve-out — bypasses branch-context pre-validation
        # so the item reaches _apply_inner (which is patched) and the stage metric fires.
        q.enqueue("memorize", {"content": "x", "context": "y", "tags": [], "_internal": True})

        with patch.object(QueueDrainer, "_apply_inner"):
            drainer = QueueDrainer(queue=q, storage_factory=MagicMock(), drain_interval=999)
            drainer.drain_now()

        after = _labeled_hist_sum(yadgar_drain_stage_ms, stage="insert")

        assert after > before, (
            f"yadgar_drain_stage_ms{{stage=insert}} sum did not increase "
            f"(before={before}, after={after})"
        )

    def test_archive_stage_observed_after_drain(self, tmp_path):
        """After a successful drain, archive stage sum increases."""
        from yadgar._shared.metrics import yadgar_drain_stage_ms
        from yadgar.core.file_queue import FileQueue, QueueDrainer

        before = _labeled_hist_sum(yadgar_drain_stage_ms, stage="archive")

        q = FileQueue(base_dir=tmp_path)
        # _internal=True: approved carve-out — bypasses branch-context pre-validation
        # so the item reaches _apply_inner (which is patched) and the stage metric fires.
        q.enqueue("memorize", {"content": "x", "context": "y", "tags": [], "_internal": True})

        with patch.object(QueueDrainer, "_apply_inner"):
            drainer = QueueDrainer(queue=q, storage_factory=MagicMock(), drain_interval=999)
            drainer.drain_now()

        after = _labeled_hist_sum(yadgar_drain_stage_ms, stage="archive")

        assert after > before, (
            f"yadgar_drain_stage_ms{{stage=archive}} sum did not increase "
            f"(before={before}, after={after})"
        )


# ---------------------------------------------------------------------------
# 3. yadgar_curator_duration_ms + yadgar_curator_merge_outcome_total
# ---------------------------------------------------------------------------


class TestCuratorMetrics:
    @pytest.fixture
    def curator(self, tmp_storage, stub_embeddings, tmp_settings):
        from yadgar._shared.curation import MemoryCurator
        from yadgar._shared.thermodynamics import MemoryThermodynamics

        thermo = MemoryThermodynamics(tmp_storage, stub_embeddings, tmp_settings)
        return MemoryCurator(tmp_storage, stub_embeddings, thermo, tmp_settings)

    def test_duration_increments_on_curate(self, curator):
        """curate_on_remember() increments yadgar_curator_duration_ms sum."""
        from yadgar._shared.metrics import yadgar_curator_duration_ms

        before = _hist_sum(yadgar_curator_duration_ms)

        dummy_embedding = b"\x00" * (4 * 384)
        curator.curate_on_remember(
            content="metrics test memory",
            context="/test",
            tags=["test"],
            embedding=dummy_embedding,
        )

        after = _hist_sum(yadgar_curator_duration_ms)
        assert after > before, (
            f"yadgar_curator_duration_ms sum did not increase (before={before}, after={after})"
        )

    def test_outcome_counter_increments_on_curate(self, curator):
        """curate_on_remember() increments yadgar_curator_merge_outcome for some outcome."""
        from yadgar._shared.metrics import yadgar_curator_merge_outcome

        before_total = _counter_total(yadgar_curator_merge_outcome)

        dummy_embedding = b"\x00" * (4 * 384)
        curator.curate_on_remember(
            content="outcome counter test memory",
            context="/test",
            tags=["test"],
            embedding=dummy_embedding,
        )

        after_total = _counter_total(yadgar_curator_merge_outcome)

        assert after_total > before_total, (
            f"yadgar_curator_merge_outcome_total was not incremented "
            f"(before={before_total}, after={after_total})"
        )


# ---------------------------------------------------------------------------
# 4. yadgar_engram_allocate_duration_ms
# ---------------------------------------------------------------------------


class TestEngramAllocateMetric:
    def test_allocate_increments_duration(self, tmp_storage, tmp_settings):
        """EngramAllocator.allocate() increments yadgar_engram_allocate_duration_ms."""
        from yadgar._shared.engram import EngramAllocator
        from yadgar._shared.metrics import yadgar_engram_allocate_duration_ms

        before = _hist_sum(yadgar_engram_allocate_duration_ms)

        allocator = EngramAllocator(tmp_storage, tmp_settings)
        mid = tmp_storage.insert_memory(
            {
                "content": "engram test",
                "directory_context": "/test",
                "heat": 0.5,
            }
        )
        allocator.allocate(mid)

        after = _hist_sum(yadgar_engram_allocate_duration_ms)
        assert after > before, (
            f"yadgar_engram_allocate_duration_ms sum did not increase "
            f"(before={before}, after={after})"
        )


# ---------------------------------------------------------------------------
# 5. yadgar_astrocyte_assign_duration_ms
# ---------------------------------------------------------------------------


class TestAstrocyteAssignMetric:
    def test_assign_memory_increments_duration(self, tmp_storage, stub_embeddings, tmp_settings):
        """AstrocytePool.assign_memory() increments yadgar_astrocyte_assign_duration_ms."""
        from yadgar._shared.astrocyte_pool import AstrocytePool
        from yadgar._shared.knowledge_graph import KnowledgeGraph
        from yadgar._shared.metrics import yadgar_astrocyte_assign_duration_ms
        from yadgar._shared.thermodynamics import MemoryThermodynamics

        before = _hist_sum(yadgar_astrocyte_assign_duration_ms)

        graph = KnowledgeGraph(tmp_storage, tmp_settings)
        thermo = MemoryThermodynamics(tmp_storage, stub_embeddings, tmp_settings)
        pool = AstrocytePool(tmp_storage, stub_embeddings, graph, thermo, tmp_settings)
        pool.init_processes()

        memory = {"id": 1, "content": "astrocyte test memory", "tags": []}
        pool.assign_memory(memory)

        after = _hist_sum(yadgar_astrocyte_assign_duration_ms)
        assert after > before, (
            f"yadgar_astrocyte_assign_duration_ms sum did not increase "
            f"(before={before}, after={after})"
        )


# ---------------------------------------------------------------------------
# 6. yadgar_action_batch_size
# ---------------------------------------------------------------------------


class TestActionBatchSizeMetric:
    def test_batch_size_observed_after_empty_action_log(
        self, tmp_storage, stub_embeddings, tmp_settings
    ):
        """_process_action_log() observes yadgar_action_batch_size even on empty batch."""
        from yadgar._shared.metrics import yadgar_action_batch_size
        from yadgar.core.consolidation import ConsolidationScheduler

        # Sum increases even for zero-observation (bucket count increments, sum += 0 → use _sum != track via count)
        # Use a custom check: after running, the +Inf bucket count increments.
        # Simplest: count from samples.
        def _count_from_samples(h) -> float:
            for s in h._child_samples():
                if s.name == "_count":
                    return s.value
            return 0.0

        before = _count_from_samples(yadgar_action_batch_size)

        scheduler = ConsolidationScheduler(tmp_storage, stub_embeddings, tmp_settings)
        scheduler._process_action_log()

        after = _count_from_samples(yadgar_action_batch_size)
        assert after > before, (
            f"yadgar_action_batch_size count did not increase (before={before}, after={after})"
        )

    def test_batch_size_value_matches_row_count(self, tmp_storage, stub_embeddings, tmp_settings):
        """yadgar_action_batch_size sum increases by the number of rows returned."""
        from yadgar._shared.metrics import yadgar_action_batch_size
        from yadgar.core.consolidation import ConsolidationScheduler

        scheduler = ConsolidationScheduler(tmp_storage, stub_embeddings, tmp_settings)

        fake_rows = [
            {
                "id": i,
                "tool_name": "memorize",
                "tool_input_summary": "x",
                "directory": "/p",
                "timestamp": "2026-01-01T00:00:00",
            }
            for i in range(5)
        ]

        before_sum = _hist_sum(yadgar_action_batch_size)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(tmp_storage, "get_unprocessed_actions", return_value=fake_rows)
            )
            stack.enter_context(patch.object(tmp_storage, "mark_actions_processed"))
            stack.enter_context(patch.object(tmp_storage, "prune_processed_action_log"))
            stack.enter_context(
                patch.object(stub_embeddings, "encode", return_value=b"\x00" * (4 * 384))
            )
            stack.enter_context(patch.object(tmp_storage, "insert_memory", return_value=1))
            scheduler._process_action_log()

        after_sum = _hist_sum(yadgar_action_batch_size)
        assert after_sum - before_sum == pytest.approx(5.0), (
            f"Expected batch size observation of 5, got delta {after_sum - before_sum}"
        )
