"""Hermetic unit tests for domain-aware heat decay (#40).

These tests mock storage and thermodynamics — no SurrealDB required.
They guard four invariants:
  1. Domain RATE: decisions (1.5x) retains more heat than errors (0.7x) over same elapsed time.
  2. Single-decay invariant: consolidate_domain does NOT change heat (regression guard for
     the historical double-decay bug).
  3. MAX tie-break: a memory in {decisions 1.5, dependencies 1.2} uses 1.5.
  4. SUMMARY: consolidate_domain returns a truthy `summary` key.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from yadgar._shared.astrocyte_pool import DOMAIN_DEFINITIONS, AstrocytePool
from yadgar.core.consolidation.heat_decay import _HeatDecayMixin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mem(mid: int, heat: float, last_accessed: str) -> dict:
    """Minimal memory dict as returned by _rows_to_dicts (id already int)."""
    return {
        "id": mid,
        "heat": heat,
        "last_accessed": last_accessed,
        "last_decay_at": None,
        "is_protected": False,
        "access_count_since_decay": 0,
        "tags": [],
        "importance": 0.5,
        "emotional_valence": 0.0,
        "confidence": 0.5,
    }


def _make_proc(name: str, memory_ids: list[int]) -> dict:
    """Minimal astrocyte_process dict."""
    return {
        "id": 1,
        "name": name,
        "domain": name,
        "memory_ids": list(memory_ids),
        "entity_ids": [],
        "heat": 1.0,
    }


# ---------------------------------------------------------------------------
# Test 1: Domain RATE — decisions (1.5) retains more heat than errors (0.7)
# ---------------------------------------------------------------------------


class TestDomainDecayRate:
    """Verify domain multiplier routes through heat_decay._decay_memories correctly.

    Memory in 'decisions' (mult=1.5) → adjusted_hours = hours/1.5 → fewer effective
    decay hours → higher residual heat than 'errors' (mult=0.7).
    """

    def test_decisions_retain_more_heat_than_errors(self):
        """decisions (1.5x multiplier) should end with higher heat than errors (0.7x)."""
        from datetime import UTC, datetime, timedelta

        # Both memories accessed 24 hours ago, same starting heat
        last_accessed = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        mem_decisions = _make_mem(1, 0.9, last_accessed)
        mem_errors = _make_mem(2, 0.9, last_accessed)

        # Mock storage: decisions memory in decisions domain, errors in errors domain
        mock_storage = MagicMock()
        mock_storage.get_all_memories_for_decay_scalar.return_value = [mem_decisions, mem_errors]
        mock_storage.get_astrocyte_processes.return_value = [
            _make_proc("decisions", [1]),
            _make_proc("errors", [2]),
        ]
        mock_storage.batch_writes = MagicMock()
        mock_storage.get_all_entities_for_decay.return_value = []

        # Mock thermo: compute_decay = heat * decay_factor ** hours (simplified)
        def compute_decay(mem: dict, hours: float) -> float:
            return mem["heat"] * (0.95**hours)

        mock_thermo = MagicMock()
        mock_thermo.compute_decay.side_effect = compute_decay

        mock_settings = MagicMock()
        mock_settings.COLD_THRESHOLD = 0.01
        mock_settings.ACTION_STREAM_COLD_THRESHOLD = 0.01
        mock_settings.RECALL_BOOST = 0.0
        mock_settings.ASTROCYTE_POOL_ENABLED = True

        # Build a minimal _HeatDecayMixin instance
        class _Scheduler(_HeatDecayMixin):
            pass

        sched = _Scheduler.__new__(_Scheduler)
        sched._storage = mock_storage
        sched._thermo = mock_thermo
        sched._settings = mock_settings

        from datetime import UTC, datetime

        stats = {"memories_archived": 0, "memories_updated": 0}
        now = datetime.now(UTC)
        batch = sched._decay_memories(stats, now)

        # Parse heat from the batch writes
        heat_by_id: dict[int, float] = {}
        for _sql, params in batch:
            heat_by_id[params["id"]] = params["heat"]

        # decisions (1.5x): adjusted_hours = 24/1.5 = 16 → heat = 0.9 * 0.95^16 ≈ 0.39
        # errors   (0.7x): adjusted_hours = 24/0.7 = 34 → heat = 0.9 * 0.95^34 ≈ 0.16
        assert 1 in heat_by_id, "decisions memory must appear in decay batch"
        assert 2 in heat_by_id, "errors memory must appear in decay batch"
        assert heat_by_id[1] > heat_by_id[2], (
            f"decisions heat {heat_by_id[1]:.4f} must exceed errors heat {heat_by_id[2]:.4f} "
            "(decisions mult=1.5 preserves more, errors mult=0.7 decays faster)"
        )

    def test_decisions_multiplier_is_1_5(self):
        assert DOMAIN_DEFINITIONS["decisions"]["decay_multiplier"] == 1.5

    def test_errors_multiplier_is_0_7(self):
        assert DOMAIN_DEFINITIONS["errors"]["decay_multiplier"] == 0.7


# ---------------------------------------------------------------------------
# Test 2: Single-decay invariant — consolidate_domain does NOT write heat
# ---------------------------------------------------------------------------


class TestSingleDecayInvariant:
    """After ONE _apply_decay pass, calling consolidate_domain must NOT change heat.

    This is the regression guard for the historical double-decay bug (#40):
    old code called BOTH _apply_decay AND consolidate_domain with their own
    update_memory_heat writes, compounding decay for multi-domain memories.
    """

    def _make_pool_with_one_memory(self) -> tuple[AstrocytePool, MagicMock, int]:
        """Return (pool, mock_storage, memory_id) with one memory in 'decisions' domain."""
        from datetime import UTC, datetime, timedelta

        last_accessed = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        mid = 42
        mem = _make_mem(mid, 0.8, last_accessed)

        # Decisions process already holds this memory
        proc_record = {
            "id": 10,
            "name": "decisions",
            "domain": "decisions",
            "memory_ids": [mid],
            "entity_ids": [],
            "heat": 1.0,
        }

        mock_storage = MagicMock()
        mock_storage.get_memory.return_value = mem
        mock_storage.get_astrocyte_processes.return_value = [proc_record]
        mock_storage.get_entity_by_name.return_value = None
        mock_storage.update_astrocyte_process = MagicMock()

        mock_thermo = MagicMock()
        mock_thermo.compute_decay.return_value = 0.75

        mock_graph = MagicMock()
        mock_graph.extract_entities_typed.return_value = []

        mock_embeddings = MagicMock()
        mock_settings = MagicMock()
        mock_settings.COLD_THRESHOLD = 0.01
        mock_settings.NUM_ASTROCYTE_PROCESSES = 4

        # Patch init_processes so it doesn't hit storage
        with patch.object(AstrocytePool, "init_processes", return_value=None):
            pool = AstrocytePool(
                mock_storage, mock_embeddings, mock_graph, mock_thermo, mock_settings
            )

        # Manually prime _processes
        pool._processes = {"decisions": proc_record}

        return pool, mock_storage, mid

    def test_consolidate_domain_never_calls_update_memory_heat(self):
        """consolidate_domain must NOT call storage.update_memory_heat — ever."""
        pool, mock_storage, mid = self._make_pool_with_one_memory()

        pool.consolidate_domain("decisions")

        mock_storage.update_memory_heat.assert_not_called()

    def test_heat_unchanged_after_consolidate_domain(self):
        """Memory heat must be identical before and after consolidate_domain."""
        pool, mock_storage, mid = self._make_pool_with_one_memory()

        mem_before = mock_storage.get_memory(mid)
        heat_before = mem_before["heat"]

        pool.consolidate_domain("decisions")

        # After consolidation the mock returns the same object — heat untouched
        heat_after = mock_storage.get_memory(mid)["heat"]
        assert heat_after == heat_before, (
            "consolidate_domain must not alter heat "
            "(heat_decay._decay_memories is the single decay authority)"
        )


# ---------------------------------------------------------------------------
# Test 3: MAX tie-break — memory in {decisions 1.5, dependencies 1.2} uses 1.5
# ---------------------------------------------------------------------------


class TestMaxTieBreak:
    """When a memory belongs to multiple domains, the highest multiplier wins (MAX)."""

    def test_max_multiplier_chosen_for_multi_domain_memory(self):
        """Memory in decisions (1.5) + dependencies (1.2) must get 1.5 mult."""
        from datetime import UTC, datetime, timedelta

        last_accessed = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        mid = 99
        mem = _make_mem(mid, 0.9, last_accessed)

        mock_storage = MagicMock()
        mock_storage.get_all_memories_for_decay_scalar.return_value = [mem]
        mock_storage.get_astrocyte_processes.return_value = [
            # Same memory in both domains — MAX tie-break should pick 1.5
            _make_proc("decisions", [mid]),
            _make_proc("dependencies", [mid]),
        ]
        mock_storage.batch_writes = MagicMock()
        mock_storage.get_all_entities_for_decay.return_value = []

        actual_hours_seen: list[float] = []

        def compute_decay(mem_dict: dict, hours: float) -> float:
            actual_hours_seen.append(hours)
            return mem_dict["heat"] * (0.95**hours)

        mock_thermo = MagicMock()
        mock_thermo.compute_decay.side_effect = compute_decay

        mock_settings = MagicMock()
        mock_settings.COLD_THRESHOLD = 0.01
        mock_settings.ACTION_STREAM_COLD_THRESHOLD = 0.01
        mock_settings.RECALL_BOOST = 0.0
        mock_settings.ASTROCYTE_POOL_ENABLED = True

        class _Scheduler(_HeatDecayMixin):
            pass

        sched = _Scheduler.__new__(_Scheduler)
        sched._storage = mock_storage
        sched._thermo = mock_thermo
        sched._settings = mock_settings

        from datetime import UTC, datetime

        stats = {"memories_archived": 0, "memories_updated": 0}
        sched._decay_memories(stats, datetime.now(UTC))

        assert len(actual_hours_seen) == 1, "memory must be decayed exactly once"
        expected_hours = 24.0 / 1.5  # MAX(1.5, 1.2) = 1.5
        assert abs(actual_hours_seen[0] - expected_hours) < 0.1, (
            f"expected adjusted_hours ≈ {expected_hours:.2f} (24/1.5), "
            f"got {actual_hours_seen[0]:.2f} — MAX tie-break failed"
        )

    def test_decisions_1_5_beats_dependencies_1_2(self):
        """Sanity: decisions multiplier (1.5) > dependencies (1.2)."""
        assert (
            DOMAIN_DEFINITIONS["decisions"]["decay_multiplier"]
            > DOMAIN_DEFINITIONS["dependencies"]["decay_multiplier"]
        )


# ---------------------------------------------------------------------------
# Test 4: SUMMARY — consolidate_domain returns a truthy `summary`
# ---------------------------------------------------------------------------


class TestConsolidateDomainSummary:
    """consolidate_domain must return a dict with a truthy `summary` key."""

    def _make_pool_empty(self) -> AstrocytePool:
        """Pool with no assigned memories — minimal mock."""
        proc_record = {
            "id": 1,
            "name": "code-patterns",
            "domain": "code-patterns",
            "memory_ids": [],
            "entity_ids": [],
            "heat": 1.0,
        }

        mock_storage = MagicMock()
        mock_storage.get_memory.return_value = None
        mock_storage.update_astrocyte_process = MagicMock()

        mock_thermo = MagicMock()
        mock_graph = MagicMock()
        mock_graph.extract_entities_typed.return_value = []
        mock_embeddings = MagicMock()
        mock_settings = MagicMock()
        mock_settings.COLD_THRESHOLD = 0.01
        mock_settings.NUM_ASTROCYTE_PROCESSES = 4

        with patch.object(AstrocytePool, "init_processes", return_value=None):
            pool = AstrocytePool(
                mock_storage, mock_embeddings, mock_graph, mock_thermo, mock_settings
            )
        pool._processes = {"code-patterns": proc_record}
        return pool

    def test_summary_key_present_and_truthy(self):
        pool = self._make_pool_empty()
        result = pool.consolidate_domain("code-patterns")
        assert isinstance(result, dict), "consolidate_domain must return a dict"
        assert "summary" in result, "consolidate_domain must include 'summary' key"
        assert result["summary"], "summary must be truthy (non-empty string)"

    def test_summary_contains_domain_name(self):
        pool = self._make_pool_empty()
        result = pool.consolidate_domain("code-patterns")
        assert "code-patterns" in result["summary"], "summary must identify the domain processed"

    def test_unknown_domain_returns_error_not_summary(self):
        pool = self._make_pool_empty()
        result = pool.consolidate_domain("nonexistent-domain")
        assert "error" in result
        assert "summary" not in result
