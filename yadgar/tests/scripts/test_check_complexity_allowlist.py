"""Tests for scripts/check_complexity_allowlist.py — I30 invariant.

TDD: tests written before/alongside implementation.

Coverage:
  (a) GATE: HARD violation in allowlist → passes
  (a) GATE: HARD violation NOT in allowlist → fails
  (b) RATIONALE: allowlist entry with rationale >= 40 chars → passes
  (b) RATIONALE: allowlist entry with short/empty rationale → fails
  (c) STALE: allowlist entry matching a real HARD violation → passes
  (c) STALE: allowlist entry for a function no longer HARD → stale error
  (d) DRIFT: recorded metric matches current → passes
  (d) DRIFT: current metric > recorded by > 20% → drift error
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_complexity_allowlist import (  # noqa: E402
    MIN_RATIONALE_LEN,
    check_drift,
    check_gate,
    check_rationale,
    check_stale,
)
from complexity_config import AllowlistEntry, build_allowlist_index  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LONG_RATIONALE = "pre-existing; scheduled for v5.55 wave N refactor"  # 51 chars
_SHORT_RATIONALE = "too short"  # 9 chars < 40


def _entry(
    path: str = "yadgar/foo.py",
    function: str = "my_func",
    metric: str = "cyclomatic",
    value: int = 20,
    rationale: str = _LONG_RATIONALE,
    lineno: int = 1,
) -> AllowlistEntry:
    return AllowlistEntry(
        path=path,
        function=function,
        lineno=lineno,
        metrics={metric: value},
        rationale=rationale,
        added="2026-06-13",
        added_by="test",
    )


def _violation(
    path: str = "yadgar/foo.py",
    function: str = "my_func",
    metric: str = "cyclomatic",
    actual: int = 20,
    limit: int = 15,
    lineno: int = 1,
) -> dict:
    return {
        "path": path,
        "function": function,
        "lineno": lineno,
        "metric": metric,
        "actual": actual,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# (a) GATE tests
# ---------------------------------------------------------------------------


class TestGate:
    def test_hard_violation_in_allowlist_passes(self):
        """HARD violation covered by allowlist → no gate error."""
        v = _violation()
        entry = _entry()
        idx = build_allowlist_index([entry])
        errors = check_gate([v], idx)
        assert not errors, f"Expected no gate errors: {errors}"

    def test_hard_violation_not_in_allowlist_fails(self):
        """HARD violation not in allowlist → gate error."""
        v = _violation()
        errors = check_gate([v], {})  # empty allowlist
        assert errors, "Expected gate error for unallowlisted HARD violation"
        assert "GATE" in errors[0]

    def test_multiple_metrics_each_checked(self):
        """Each metric must be independently allowlisted."""
        # Entry covers cyclomatic but not fn_loc
        entry = _entry(metric="cyclomatic", value=20)
        idx = build_allowlist_index([entry])

        violations = [
            _violation(metric="cyclomatic", actual=20),
            _violation(metric="fn_loc", actual=200),  # not allowlisted
        ]
        errors = check_gate(violations, idx)
        assert len(errors) == 1, f"Expected 1 gate error for fn_loc: {errors}"
        assert "fn_loc" in errors[0]

    def test_different_function_not_allowlisted(self):
        """Allowlist entry for one function doesn't cover another."""
        entry = _entry(function="func_a")
        idx = build_allowlist_index([entry])
        v = _violation(function="func_b")  # different function
        errors = check_gate([v], idx)
        assert errors, "Different function should not be covered by allowlist"

    def test_different_path_not_allowlisted(self):
        """Allowlist entry for one file doesn't cover another."""
        entry = _entry(path="yadgar/a.py")
        idx = build_allowlist_index([entry])
        v = _violation(path="yadgar/b.py")
        errors = check_gate([v], idx)
        assert errors, "Different path should not be covered"

    def test_no_violations_no_errors(self):
        """No HARD violations → no gate errors."""
        errors = check_gate([], {})
        assert not errors


# ---------------------------------------------------------------------------
# (b) RATIONALE tests
# ---------------------------------------------------------------------------


class TestRationale:
    def test_long_rationale_passes(self):
        entry = _entry(rationale=_LONG_RATIONALE)
        errors = check_rationale([entry])
        assert not errors, f"Long rationale should pass: {errors}"

    def test_short_rationale_fails(self):
        entry = _entry(rationale=_SHORT_RATIONALE)
        errors = check_rationale([entry])
        assert errors, "Short rationale should fail"
        assert "RATIONALE" in errors[0]

    def test_empty_rationale_fails(self):
        entry = _entry(rationale="")
        errors = check_rationale([entry])
        assert errors, "Empty rationale should fail"

    def test_whitespace_only_rationale_fails(self):
        entry = _entry(rationale="   ")
        errors = check_rationale([entry])
        assert errors, "Whitespace-only rationale should fail"

    def test_exactly_min_len_passes(self):
        rationale = "x" * MIN_RATIONALE_LEN
        entry = _entry(rationale=rationale)
        errors = check_rationale([entry])
        assert not errors, f"Exactly {MIN_RATIONALE_LEN} chars should pass: {errors}"

    def test_one_below_min_len_fails(self):
        rationale = "x" * (MIN_RATIONALE_LEN - 1)
        entry = _entry(rationale=rationale)
        errors = check_rationale([entry])
        assert errors, f"{MIN_RATIONALE_LEN - 1} chars should fail"

    def test_multiple_entries_all_checked(self):
        entries = [
            _entry(function="f1", rationale=_LONG_RATIONALE),
            _entry(function="f2", rationale=_SHORT_RATIONALE),
        ]
        errors = check_rationale(entries)
        assert len(errors) == 1, "Only the short-rationale entry should fail"
        assert "f2" in errors[0]


# ---------------------------------------------------------------------------
# (c) STALE tests
# ---------------------------------------------------------------------------


class TestStale:
    def test_entry_matching_real_violation_passes(self):
        entry = _entry()
        v = _violation()
        errors = check_stale([entry], [v])
        assert not errors, f"Matching entry should not be stale: {errors}"

    def test_entry_no_longer_hard_is_stale(self):
        """Allowlist entry with no matching current HARD violation → stale."""
        entry = _entry(metric="cyclomatic", value=20)
        # No current violations
        errors = check_stale([entry], [])
        assert errors, "Entry with no matching violation should be stale"
        assert "STALE" in errors[0]

    def test_entry_metric_under_cap_is_stale(self):
        """Allowlist has cyclomatic but current violations don't include it."""
        entry = _entry(metric="cyclomatic", value=20)
        # Different metric violation for same function
        v = _violation(metric="fn_loc", actual=200)
        errors = check_stale([entry], [v])
        assert errors, "Entry with no matching metric violation should be stale"

    def test_multiple_metrics_each_checked(self):
        """Entry with multiple metrics: each metric checked independently."""
        entry = AllowlistEntry(
            path="yadgar/foo.py",
            function="my_func",
            lineno=1,
            metrics={"cyclomatic": 20, "fn_loc": 200},
            rationale=_LONG_RATIONALE,
            added="2026-06-13",
            added_by="test",
        )
        # Only cyclomatic is still HARD; fn_loc was fixed
        violations = [_violation(metric="cyclomatic", actual=20)]
        errors = check_stale([entry], violations)
        # fn_loc metric should be stale
        assert errors, "The fn_loc metric should be stale"
        assert "fn_loc" in errors[0]

    def test_empty_allowlist_no_stale(self):
        errors = check_stale([], [_violation()])
        assert not errors


# ---------------------------------------------------------------------------
# (d) DRIFT tests
# ---------------------------------------------------------------------------


class TestDrift:
    def test_matching_metric_no_drift(self):
        entry = _entry(metric="cyclomatic", value=20)
        v = _violation(metric="cyclomatic", actual=20)
        errors = check_drift([entry], [v])
        assert not errors, f"Matching value should not drift: {errors}"

    def test_improvement_below_recorded_no_error(self):
        """Current value < recorded → improvement, no drift error."""
        entry = _entry(metric="cyclomatic", value=25)
        v = _violation(metric="cyclomatic", actual=20)  # improved
        errors = check_drift([entry], [v])
        assert not errors, "Improvement should not trigger drift"

    def test_growth_within_tolerance_no_error(self):
        """Growth <= 20% → within tolerance, no error."""
        entry = _entry(metric="cyclomatic", value=20)
        # 20% of 20 = 4, so 24 is exactly at tolerance
        v = _violation(metric="cyclomatic", actual=24)
        errors = check_drift([entry], [v])
        assert not errors, "Growth within tolerance should not trigger drift"

    def test_growth_beyond_tolerance_triggers_drift(self):
        """Growth > 20% → drift error."""
        entry = _entry(metric="cyclomatic", value=20)
        v = _violation(metric="cyclomatic", actual=25)  # 25% growth > 20%
        errors = check_drift([entry], [v])
        assert errors, "Growth > 20% should trigger drift"
        assert "DRIFT" in errors[0]

    def test_large_growth_triggers_drift(self):
        entry = _entry(metric="fn_loc", value=150)
        v = _violation(metric="fn_loc", actual=300)  # doubled
        errors = check_drift([entry], [v])
        assert errors, "Doubling should trigger drift"

    def test_no_current_violation_no_drift_error(self):
        """If there's no matching current violation, that's caught by stale check, not drift."""
        entry = _entry(metric="cyclomatic", value=20)
        errors = check_drift([entry], [])  # no violations
        assert not errors, "Missing violation should be caught by stale check, not drift"
