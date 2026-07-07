"""Tests for yadgar/consolidation/cold_retention.py — cold-memory retention DRY-RUN pass.

TDD: tests authored BEFORE implementation was written.
Safety critical: verifies DELETE NOTHING by default.

Coverage:
  - Candidate identification gate (heat, age, access_count, is_protected, _anchor)
  - DRY-RUN default: delete_memory never called
  - Report-only mode logging + metric
  - Gated delete (ENABLED=True, DRY_RUN=False): delete_memory called for candidates only
  - Stats dict shape
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from yadgar.core.consolidation.cold_retention import (
    _cold_memory_retention_report,
    _is_candidate,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers — mirrors test_prune_passes_module.py house style
# ---------------------------------------------------------------------------


def _settings(
    cold_threshold: float = 0.02,
    retention_days: int = 90,
    purge_enabled: bool = False,
    dry_run: bool = True,
):
    s = MagicMock()
    s.COLD_THRESHOLD = cold_threshold
    s.COLD_MEMORY_RETENTION_DAYS = retention_days
    s.COLD_MEMORY_PURGE_ENABLED = purge_enabled
    s.COLD_MEMORY_PURGE_DRY_RUN = dry_run
    return s


def _mem(
    mid: int,
    *,
    heat: float = 0.0,
    access_count: int = 0,
    is_protected: bool = False,
    created_days_ago: int = 100,
    tags: list | None = None,
    content: str = "user memory content",
):
    """Minimal memory dict. Defaults represent a canonical candidate."""
    created = (datetime.now(UTC) - timedelta(days=created_days_ago)).isoformat()
    return {
        "id": mid,
        "heat": heat,
        "access_count": access_count,
        "is_protected": is_protected,
        "created_at": created,
        "tags": tags if tags is not None else [],
        "content": content,
    }


def _storage(*memories):
    s = MagicMock()
    s.get_memories_by_heat.return_value = list(memories)
    return s


# ---------------------------------------------------------------------------
# _is_candidate unit tests
# ---------------------------------------------------------------------------


class TestIsCandidate:
    """Unit-test the gate predicate in isolation."""

    def _cutoff(self, days: int = 90) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def test_canonical_candidate_accepted(self):
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is True

    def test_warm_heat_rejected(self):
        mem = _mem(1, heat=0.05, access_count=0, created_days_ago=100)
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_heat_exactly_at_threshold_rejected(self):
        # Gate is strict less-than — at threshold is NOT cold enough
        mem = _mem(1, heat=0.02, access_count=0, created_days_ago=100)
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_too_recent_rejected(self):
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=30)
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_accessed_rejected(self):
        mem = _mem(1, heat=0.0, access_count=1, created_days_ago=100)
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_protected_rejected(self):
        mem = _mem(1, heat=0.0, access_count=0, is_protected=True, created_days_ago=100)
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_anchored_rejected(self):
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100, tags=["_anchor"])
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_anchor_in_mixed_tags_rejected(self):
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100, tags=["user", "_anchor"])
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_tags_as_json_string_parsed(self):
        """Tags may arrive as JSON string from SurrealDB — must still gate on _anchor."""
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        mem["tags"] = json.dumps(["_anchor"])
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is False

    def test_untagged_user_memory_candidate(self):
        """Untagged user memories (the immortal class) are candidates."""
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100, tags=[])
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is True

    def test_tagged_non_anchor_candidate(self):
        """Non-_anchor system tags do not protect (only is_protected / _anchor do)."""
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100, tags=["feedback"])
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is True

    def test_none_heat_treated_as_zero(self):
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        mem["heat"] = None
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is True

    def test_none_access_count_treated_as_zero(self):
        mem = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        mem["access_count"] = None
        assert _is_candidate(mem, self._cutoff(90), cold_threshold=0.02) is True


# ---------------------------------------------------------------------------
# _cold_memory_retention_report — default safe behaviour (no deletes)
# ---------------------------------------------------------------------------


class TestDefaultSafeMode:
    """With defaults (PURGE_ENABLED=False, DRY_RUN=True) nothing is deleted."""

    def test_no_candidates_returns_zero_stats(self):
        mem = _mem(1, heat=0.5)  # warm — not a candidate
        storage = _storage(mem)
        result = _cold_memory_retention_report(storage, _settings())
        assert result == {"candidates": 0, "deleted": 0}
        storage.delete_memory.assert_not_called()

    def test_candidates_identified_correctly(self):
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        c2 = _mem(2, heat=0.01, access_count=0, created_days_ago=200)
        storage = _storage(c1, c2)
        result = _cold_memory_retention_report(storage, _settings())
        assert result["candidates"] == 2
        assert result["deleted"] == 0

    def test_delete_memory_never_called_default(self):
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        storage = _storage(c1)
        _cold_memory_retention_report(storage, _settings())
        storage.delete_memory.assert_not_called()

    def test_warm_memory_not_counted_as_candidate(self):
        warm = _mem(1, heat=0.5, access_count=0, created_days_ago=100)
        storage = _storage(warm)
        result = _cold_memory_retention_report(storage, _settings())
        assert result["candidates"] == 0

    def test_recent_cold_memory_not_candidate(self):
        recent = _mem(1, heat=0.0, access_count=0, created_days_ago=30)
        storage = _storage(recent)
        result = _cold_memory_retention_report(storage, _settings(retention_days=90))
        assert result["candidates"] == 0

    def test_protected_cold_old_memory_not_candidate(self):
        protected = _mem(1, heat=0.0, access_count=0, created_days_ago=100, is_protected=True)
        storage = _storage(protected)
        result = _cold_memory_retention_report(storage, _settings())
        assert result["candidates"] == 0

    def test_anchored_cold_old_memory_not_candidate(self):
        anchored = _mem(1, heat=0.0, access_count=0, created_days_ago=100, tags=["_anchor"])
        storage = _storage(anchored)
        result = _cold_memory_retention_report(storage, _settings())
        assert result["candidates"] == 0

    def test_accessed_cold_old_memory_not_candidate(self):
        accessed = _mem(1, heat=0.0, access_count=2, created_days_ago=100)
        storage = _storage(accessed)
        result = _cold_memory_retention_report(storage, _settings())
        assert result["candidates"] == 0

    def test_purge_enabled_but_dry_run_true_still_no_delete(self):
        """PURGE_ENABLED=True with DRY_RUN=True still must not delete."""
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        storage = _storage(c1)
        result = _cold_memory_retention_report(storage, _settings(purge_enabled=True, dry_run=True))
        assert result["candidates"] == 1
        assert result["deleted"] == 0
        storage.delete_memory.assert_not_called()

    def test_purge_disabled_dry_run_false_still_no_delete(self):
        """DRY_RUN=False alone is not enough — PURGE_ENABLED must also be True."""
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        storage = _storage(c1)
        result = _cold_memory_retention_report(
            storage, _settings(purge_enabled=False, dry_run=False)
        )
        assert result["candidates"] == 1
        assert result["deleted"] == 0
        storage.delete_memory.assert_not_called()


# ---------------------------------------------------------------------------
# _cold_memory_retention_report — mixed candidate set
# ---------------------------------------------------------------------------


class TestCandidateFiltering:
    """Correct set of candidates identified from a mixed memory list."""

    def test_only_candidates_counted(self):
        candidate = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        warm = _mem(2, heat=0.5, access_count=0, created_days_ago=100)
        recent = _mem(3, heat=0.0, access_count=0, created_days_ago=10)
        protected = _mem(4, heat=0.0, access_count=0, created_days_ago=100, is_protected=True)
        anchored = _mem(5, heat=0.0, access_count=0, created_days_ago=100, tags=["_anchor"])
        accessed = _mem(6, heat=0.0, access_count=1, created_days_ago=100)
        storage = _storage(candidate, warm, recent, protected, anchored, accessed)
        result = _cold_memory_retention_report(storage, _settings())
        # Only candidate #1 qualifies
        assert result["candidates"] == 1
        assert result["deleted"] == 0


# ---------------------------------------------------------------------------
# Metric emission
# ---------------------------------------------------------------------------


class TestMetricEmission:
    def test_gauge_set_with_candidate_count(self):
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        c2 = _mem(2, heat=0.0, access_count=0, created_days_ago=200)
        storage = _storage(c1, c2)
        with patch(
            "yadgar.core.consolidation.cold_retention._emit_cold_purge_candidates_metric"
        ) as mock_emit:
            _cold_memory_retention_report(storage, _settings())
            mock_emit.assert_called_once_with(2)

    def test_gauge_set_to_zero_when_no_candidates(self):
        warm = _mem(1, heat=0.5)
        storage = _storage(warm)
        with patch(
            "yadgar.core.consolidation.cold_retention._emit_cold_purge_candidates_metric"
        ) as mock_emit:
            _cold_memory_retention_report(storage, _settings())
            mock_emit.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# Gated delete — BOTH gates armed
# ---------------------------------------------------------------------------


class TestGatedDelete:
    """When PURGE_ENABLED=True AND DRY_RUN=False, delete_memory IS called."""

    def test_candidates_deleted_when_both_gates_armed(self):
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        c2 = _mem(2, heat=0.0, access_count=0, created_days_ago=200)
        storage = _storage(c1, c2)
        result = _cold_memory_retention_report(
            storage, _settings(purge_enabled=True, dry_run=False)
        )
        assert result["candidates"] == 2
        assert result["deleted"] == 2
        storage.delete_memory.assert_any_call(1)
        storage.delete_memory.assert_any_call(2)
        assert storage.delete_memory.call_count == 2

    def test_only_candidates_deleted_not_non_candidates(self):
        """Non-candidates must NOT be passed to delete_memory even in purge mode."""
        candidate = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        protected = _mem(2, heat=0.0, access_count=0, created_days_ago=100, is_protected=True)
        warm = _mem(3, heat=0.5, access_count=0, created_days_ago=100)
        storage = _storage(candidate, protected, warm)
        result = _cold_memory_retention_report(
            storage, _settings(purge_enabled=True, dry_run=False)
        )
        assert result["candidates"] == 1
        assert result["deleted"] == 1
        storage.delete_memory.assert_called_once_with(1)

    def test_no_candidates_no_deletes(self):
        warm = _mem(1, heat=0.5)
        storage = _storage(warm)
        result = _cold_memory_retention_report(
            storage, _settings(purge_enabled=True, dry_run=False)
        )
        assert result == {"candidates": 0, "deleted": 0}
        storage.delete_memory.assert_not_called()


# ---------------------------------------------------------------------------
# Stats dict contract
# ---------------------------------------------------------------------------


class TestStatsContract:
    def test_stats_keys_always_present(self):
        storage = _storage()
        result = _cold_memory_retention_report(storage, _settings())
        assert "candidates" in result
        assert "deleted" in result

    def test_deleted_always_zero_in_default_mode(self):
        c1 = _mem(1, heat=0.0, access_count=0, created_days_ago=100)
        storage = _storage(c1)
        result = _cold_memory_retention_report(storage, _settings())
        assert result["deleted"] == 0

    def test_storage_queried_with_min_heat_zero(self):
        """Must use min_heat=0.0 to catch heat=0 immortals."""
        storage = _storage()
        _cold_memory_retention_report(storage, _settings())
        storage.get_memories_by_heat.assert_called_once()
        call_kwargs = storage.get_memories_by_heat.call_args
        # Handles both positional and keyword call styles
        args, kwargs = call_kwargs
        min_heat = args[0] if args else kwargs.get("min_heat")
        assert min_heat == 0.0
