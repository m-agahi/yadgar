"""TDD tests for v5.15.0 CPU burst detection infrastructure.

Red-first. Tests must fail before D1/D4 implementation.

Coverage:
  D1 — Phase duration warn threshold:
    - test_phase_duration_warn_emits_critical_log (above threshold → CRITICAL)
    - test_phase_duration_under_threshold_no_warn  (below threshold → no CRITICAL)
  D4 — Static caller audit:
    - test_no_unexpected_sleep_cycle_callers (run_sleep_cycle usage confined to expected set)
    - test_no_unexpected_force_consolidate_callers (force_consolidate callers audited)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# D1: Phase duration warn threshold
# ---------------------------------------------------------------------------


class TestPhaseDurationWarn:
    """PHASE_DURATION_WARN_MS config → CRITICAL log when a phase exceeds threshold."""

    def _make_orchestrator(self):
        """Build a minimal _OrchestratorMixin instance with stubs."""
        from yadgar.backend.consolidation.orchestrator import _OrchestratorMixin

        class _Stub(_OrchestratorMixin):
            def __init__(self):
                self._last_sleep_cycle = None
                # Stub all phase methods used in _consolidation_cycle
                self._apply_decay = MagicMock()
                self._process_new_episodes = MagicMock()
                self._prune_old_episodes_safe = MagicMock()
                self._merge_duplicates = MagicMock()
                self._link_similar_memories = MagicMock()
                self._graph = MagicMock()
                self._curator = MagicMock(
                    memify_cycle=MagicMock(
                        return_value={"pruned": 0, "strengthened": 0, "reweighted": 0, "derived": 0}
                    )
                )
                self._cls = MagicMock(
                    consolidation_cycle=MagicMock(
                        return_value={"patterns_found": 0, "promoted": 0, "skipped_inconsistent": 0}
                    )
                )
                self._process_action_log = MagicMock(
                    return_value={"processed": 0, "memories_created": 0}
                )
                self._run_causal_discovery_phase = MagicMock()
                self._run_retention_tasks = MagicMock()
                self._run_post_cycle_tasks = MagicMock()
                self._sleep_engine = MagicMock()

        return _Stub()

    def test_phase_duration_warn_emits_critical_log(self, caplog, monkeypatch):
        """When apply_decay takes longer than PHASE_DURATION_WARN_MS, CRITICAL is emitted."""
        import yadgar.backend.consolidation.orchestrator as _orch_mod

        # Set threshold very low so any real execution exceeds it
        monkeypatch.setattr(_orch_mod, "PHASE_DURATION_WARN_MS", 1, raising=False)

        # Override settings to expose the constant if it's loaded from there
        try:
            from yadgar._shared.config import get_settings

            settings = get_settings()
            monkeypatch.setattr(settings, "PHASE_DURATION_WARN_MS", 1, raising=False)
        except Exception:
            pass

        orchestrator = self._make_orchestrator()

        # Make apply_decay sleep for 5ms to guarantee > 1ms threshold
        def slow_apply_decay(stats):
            time.sleep(0.005)  # 5ms

        orchestrator._apply_decay.side_effect = slow_apply_decay

        with caplog.at_level(logging.CRITICAL, logger="yadgar.consolidation"):
            orchestrator._consolidation_cycle()

        # Verify CRITICAL log was emitted for the slow phase
        critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        phase_warn_records = [
            r
            for r in critical_records
            if "apply_decay" in r.message or "duration" in r.message.lower()
        ]
        assert phase_warn_records, (
            f"Expected CRITICAL log for slow apply_decay phase, got: {[r.message for r in caplog.records]}"
        )

    def test_phase_duration_under_threshold_no_warn(self, caplog, monkeypatch):
        """When all phases complete within threshold, no CRITICAL duration log is emitted."""
        import yadgar.backend.consolidation.orchestrator as _orch_mod

        # Set very high threshold so nothing triggers it
        monkeypatch.setattr(_orch_mod, "PHASE_DURATION_WARN_MS", 60_000, raising=False)

        try:
            from yadgar._shared.config import get_settings

            settings = get_settings()
            monkeypatch.setattr(settings, "PHASE_DURATION_WARN_MS", 60_000, raising=False)
        except Exception:
            pass

        orchestrator = self._make_orchestrator()

        with caplog.at_level(logging.CRITICAL, logger="yadgar.consolidation"):
            orchestrator._consolidation_cycle()

        # No CRITICAL log mentioning "duration" should appear
        critical_duration_records = [
            r
            for r in caplog.records
            if r.levelno == logging.CRITICAL and "duration" in r.message.lower()
        ]
        assert not critical_duration_records, (
            f"Unexpected CRITICAL duration log: {[r.message for r in critical_duration_records]}"
        )


# ---------------------------------------------------------------------------
# D4: Static caller audit — run_sleep_cycle and force_consolidate
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent  # yadgar/
_YADGAR_SRC = _REPO_ROOT / "yadgar"

# Expected callers/definers of run_sleep_cycle — ONLY these files may reference it outside tests.
# Add to this set only with explicit design review.
_EXPECTED_SLEEP_CYCLE_CALLERS = frozenset(
    {
        "core/sleep_compute/__init__.py",  # SleepEngine.run_sleep_cycle() definition
        "core/consolidation/orchestrator.py",  # _maybe_sleep_cycle — calls self._sleep_engine.run_sleep_cycle()
        "core/server/tools/admin_other.py",  # consolidate_now(mode='full') explicit MCP tool
        # R3 Car 1: consolidation compute relocated core → backend; these are the
        # relocated legitimate callers (same call sites, new home).
        "backend/consolidation/__init__.py",  # R3 Car 1 relocation
        "backend/consolidation/orchestrator.py",  # R3 Car 1 relocation
        "backend/sleep_compute/__init__.py",  # R3 Car 1 relocation
    }
)

# Expected callers/definers of force_consolidate — ONLY these files may reference it.
_EXPECTED_FORCE_CONSOLIDATE_CALLERS = frozenset(
    {
        "core/consolidation/__init__.py",  # ConsolidationScheduler.force_consolidate() definition
        "core/consolidation/orchestrator.py",  # docstring reference (not a call)
        "core/server/tools/admin_other.py",  # consolidate_now MCP tool
        "core/scripts/nightly_cycle.py",  # nightly cron script
        "core/server/http.py",  # startup/shutdown lifecycle call
        # R3 Car 1: consolidation compute relocated core → backend; these are the
        # relocated legitimate callers (same call sites, new home).
        "backend/consolidation/__init__.py",  # R3 Car 1 relocation
        "backend/consolidation/orchestrator.py",  # R3 Car 1 relocation
        "backend/consolidation/service.py",  # R3 Car 1 relocation
    }
)


def _grep_callers(pattern: str) -> set[str]:
    """Return set of relative paths (from yadgar/) that reference pattern, excluding tests."""
    import re

    results = set()
    for py_file in _YADGAR_SRC.rglob("*.py"):
        # Skip test files and __pycache__
        rel = py_file.relative_to(_YADGAR_SRC)
        rel_str = str(rel)
        if "test_" in rel_str or "__pycache__" in rel_str or "tests/" in rel_str:
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(pattern, text):
            results.add(rel_str)
    return results


class TestAutomaticTriggerAudit:
    """D4: Ensure no unexpected callers of run_sleep_cycle or force_consolidate exist."""

    def test_no_unexpected_sleep_cycle_callers(self):
        """Only admin_other.py and orchestrator.py may reference run_sleep_cycle."""
        actual = _grep_callers(r"\brun_sleep_cycle\b")
        unexpected = actual - _EXPECTED_SLEEP_CYCLE_CALLERS
        assert not unexpected, (
            "Unexpected run_sleep_cycle callers found (add to _EXPECTED_SLEEP_CYCLE_CALLERS "
            "with explicit review):\n  " + "\n  ".join(sorted(unexpected))
        )

    def test_no_unexpected_force_consolidate_callers(self):
        """Only admin_other.py and nightly_cycle.py may reference force_consolidate."""
        actual = _grep_callers(r"\bforce_consolidate\b")
        unexpected = actual - _EXPECTED_FORCE_CONSOLIDATE_CALLERS
        assert not unexpected, (
            "Unexpected force_consolidate callers found (add to _EXPECTED_FORCE_CONSOLIDATE_CALLERS "
            "with explicit review):\n  " + "\n  ".join(sorted(unexpected))
        )
