"""Tests for scripts/check_complexity.py — I13 pre-commit hook.

TDD: all tests written BEFORE implementation.

Coverage required by task spec:
  - Function over LOC hard → block (exit 1)
  - Function over LOC soft, no noqa → warn (exit 0, stderr message)
  - Function over LOC soft, with noqa → pass silently (exit 0, no message)
  - File over 1000 LOC → block (exit 1)
  - Test file → LOC + params caps skipped, cyclo + nesting still enforced
  - Class methods > 30 → soft warn (exit 0)
  - Inheritance depth > 3 → hard block (exit 1)
  - Baseline ratchet: pre-existing violation at current level → pass
  - Baseline ratchet: worse than baseline → block

Additional coverage:
  - Nesting > 4 hard → block
  - Params > 8 hard (non-test) → block
  - Cyclomatic > 15 → block (cyclo covered by ruff, but hook also checks)
  - New file not in baseline → full caps apply
  - --update-baseline writes baseline and exits 0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import hook from scripts/ via path injection (no package __init__)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_complexity import (  # noqa: E402
    ViolationSeverity,
    analyze_staged_file,
    check_files,
    load_baseline,
    update_baseline,
)

# ---------------------------------------------------------------------------
# Helpers to build synthetic Python source
# ---------------------------------------------------------------------------


def _fn_with_loc(n_body_lines: int, name: str = "my_func") -> str:
    """Generate a function with exactly n_body_lines lines in body (plus def line)."""
    body = "\n".join(f"    x_{i} = {i}" for i in range(n_body_lines))
    return f"def {name}():\n{body}\n"


def _fn_with_params(n_params: int, name: str = "my_func") -> str:
    """Generate a function with n_params positional params."""
    params = ", ".join(f"p{i}" for i in range(n_params))
    return f"def {name}({params}):\n    pass\n"


def _fn_with_nesting(depth: int, name: str = "my_func") -> str:
    """Generate function with nested ifs reaching `depth` nesting levels."""
    indent = "    "
    lines = [f"def {name}(x):"]
    for i in range(depth):
        lines.append(f"{indent * (i + 1)}if x:")
    lines.append(f"{indent * (depth + 1)}pass")
    return "\n".join(lines) + "\n"


def _fn_with_cyclo(n_branches: int, name: str = "my_func") -> str:
    """Generate a function with n_branches extra branches (cyclo = 1 + n_branches)."""
    branches = "\n".join(f"    if x == {i}:\n        pass" for i in range(n_branches))
    return f"def {name}(x):\n{branches}\n    return x\n"


def _class_with_methods(n_methods: int, class_name: str = "MyClass") -> str:
    methods = "\n".join(f"    def method_{i}(self):\n        pass" for i in range(n_methods))
    return f"class {class_name}:\n{methods}\n"


def _class_with_depth(depth: int, class_name: str = "MyClass") -> str:
    """Generate a chain of classes with inheritance depth `depth`."""
    lines = ["class Base:\n    pass\n"]
    prev = "Base"
    for i in range(1, depth):
        name = f"Sub{i}"
        lines.append(f"class {name}({prev}):\n    pass\n")
        prev = name
    final = f"class {class_name}({prev}):\n    pass\n"
    lines.append(final)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_py(tmp_path):
    """Return a factory to write Python source to a temp file."""

    def _write(source: str, filename: str = "test_target.py") -> Path:
        p = tmp_path / filename
        p.write_text(source, encoding="utf-8")
        return p

    return _write


@pytest.fixture()
def empty_baseline(tmp_path) -> Path:
    """Empty baseline file."""
    p = tmp_path / ".complexity-baseline.json"
    p.write_text("{}", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# analyze_staged_file — unit tests for per-file analysis
# ---------------------------------------------------------------------------


class TestFunctionLOCHard:
    def test_over_150_loc_hard_violation(self, tmp_py):
        # Function with 152 body lines → loc = 153 (def + 152) > 150 hard cap
        src = _fn_with_loc(152)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        hard = [
            v for v in result.violations if v.severity == ViolationSeverity.HARD and "loc" in v.cap
        ]
        assert hard, f"Expected hard LOC violation, got {result.violations}"

    def test_exactly_150_loc_no_hard_violation(self, tmp_py):
        # Function exactly at hard cap (150 LOC) → no HARD violation (soft still fires)
        src = _fn_with_loc(149)  # def + 149 = 150 LOC
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        loc_hard = [
            v for v in result.violations if "loc" in v.cap and v.severity == ViolationSeverity.HARD
        ]
        assert not loc_hard, f"Exactly 150 LOC should not hard-violate: {loc_hard}"


class TestFunctionLOCSoft:
    def test_over_80_soft_no_noqa_warns(self, tmp_py):
        # Function with 83 body lines → loc = 84 > 80 soft, no noqa
        src = _fn_with_loc(83)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        soft = [
            v for v in result.violations if v.severity == ViolationSeverity.SOFT and "loc" in v.cap
        ]
        assert soft, f"Expected soft LOC violation, got {result.violations}"

    def test_over_80_soft_with_noqa_passes_silently(self, tmp_py):
        # noqa on def line suppresses soft violation (suppressed=True, not in warnings)
        body = "\n".join(f"    x_{i} = {i}" for i in range(83))
        src = f"def my_func():  # noqa: C901 - cohesive: single flow\n{body}\n"
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        # Suppressed violations should not appear in warnings
        assert not result.warnings, f"noqa should suppress soft LOC warning, got {result.warnings}"
        assert result.exit_code == 0

    def test_noqa_does_not_suppress_hard_violation(self, tmp_py):
        # noqa cannot suppress hard violations
        body = "\n".join(f"    x_{i} = {i}" for i in range(152))
        src = f"def my_func():  # noqa: C901 - cohesive: single flow\n{body}\n"
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        hard = [
            v for v in result.violations if v.severity == ViolationSeverity.HARD and "loc" in v.cap
        ]
        assert hard, "noqa must NOT suppress hard LOC violation"


class TestFileLOC:
    def test_file_over_1000_loc_hard(self, tmp_py):
        # File with 1001 lines → hard block
        lines = ["x = 1"] * 1001
        src = "\n".join(lines) + "\n"
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        file_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and v.cap == "file_loc"
        ]
        assert file_hard, f"Expected file LOC hard violation, got {result.violations}"

    def test_file_exactly_1000_loc_no_hard(self, tmp_py):
        lines = ["x = 1"] * 1000
        src = "\n".join(lines) + "\n"
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        file_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and v.cap == "file_loc"
        ]
        assert not file_hard, "1000 lines should not hard-violate"


class TestTestFileExemptions:
    def test_test_file_loc_exempt(self, tmp_py):
        # Test file: function over 150 LOC → no LOC violation
        src = _fn_with_loc(160)
        f = tmp_py(src, "test_target.py")
        result = analyze_staged_file(str(f), is_test=True)
        loc_violations = [v for v in result.violations if "loc" in v.cap and "file" not in v.cap]
        assert not loc_violations, f"Test file should be LOC-exempt: {loc_violations}"

    def test_test_file_params_exempt(self, tmp_py):
        # Test file: function with 10 params → no params violation
        src = _fn_with_params(10)
        f = tmp_py(src, "test_target.py")
        result = analyze_staged_file(str(f), is_test=True)
        param_violations = [v for v in result.violations if "params" in v.cap]
        assert not param_violations, f"Test file should be params-exempt: {param_violations}"

    def test_test_file_nesting_still_enforced(self, tmp_py):
        # Test file: function with nesting depth 5 → HARD violation
        src = _fn_with_nesting(5)
        f = tmp_py(src, "test_target.py")
        result = analyze_staged_file(str(f), is_test=True)
        nesting_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "nesting" in v.cap
        ]
        assert nesting_hard, f"Test file nesting should still be enforced: {result.violations}"

    def test_test_file_cyclo_still_enforced(self, tmp_py):
        # Test file: function with cyclo > 15 → HARD violation
        src = _fn_with_cyclo(16)  # cyclo = 1 + 16 = 17 > 15
        f = tmp_py(src, "test_target.py")
        result = analyze_staged_file(str(f), is_test=True)
        cyclo_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "cyclo" in v.cap
        ]
        assert cyclo_hard, f"Test file cyclo should still be enforced: {result.violations}"


class TestNesting:
    def test_nesting_over_4_hard_block(self, tmp_py):
        src = _fn_with_nesting(5)  # depth 5 > 4 hard
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        nesting_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "nesting" in v.cap
        ]
        assert nesting_hard, f"Nesting > 4 should be hard: {result.violations}"

    def test_nesting_exactly_4_no_violation(self, tmp_py):
        src = _fn_with_nesting(4)  # depth 4 = cap, no violation
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        nesting_hard = [v for v in result.violations if "nesting" in v.cap]
        assert not nesting_hard, f"Nesting == 4 should not violate: {result.violations}"


class TestParams:
    def test_params_over_8_hard_nontest(self, tmp_py):
        src = _fn_with_params(9)  # 9 > 8 hard
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        param_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "params" in v.cap
        ]
        assert param_hard, f"Params > 8 should be hard: {result.violations}"

    def test_params_exactly_8_no_hard(self, tmp_py):
        src = _fn_with_params(8)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        param_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "params" in v.cap
        ]
        assert not param_hard, f"Params == 8 should not hard-violate: {result.violations}"

    def test_params_6_to_8_soft(self, tmp_py):
        src = _fn_with_params(6)  # 6 > 5 soft
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        param_soft = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.SOFT and "params" in v.cap
        ]
        assert param_soft, f"Params 6 should be soft: {result.violations}"


class TestClassViolations:
    def test_class_methods_over_30_soft(self, tmp_py):
        src = _class_with_methods(31)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        methods_soft = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.SOFT and "methods" in v.cap
        ]
        assert methods_soft, f"31 methods should be soft-warn: {result.violations}"

    def test_class_methods_30_no_violation(self, tmp_py):
        src = _class_with_methods(30)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        methods_v = [v for v in result.violations if "methods" in v.cap]
        assert not methods_v, f"30 methods should not violate: {result.violations}"

    def test_class_inheritance_depth_over_3_hard(self, tmp_py):
        # depth 4: Base → Sub1 → Sub2 → Sub3 → MyClass
        src = _class_with_depth(4)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        depth_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "inheritance" in v.cap
        ]
        assert depth_hard, f"Inheritance depth 4 should be hard: {result.violations}"

    def test_class_inheritance_depth_3_no_hard(self, tmp_py):
        # depth 3: Base → Sub1 → Sub2 → MyClass
        src = _class_with_depth(3)
        f = tmp_py(src, "target.py")
        result = analyze_staged_file(str(f), is_test=False)
        depth_hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and "inheritance" in v.cap
        ]
        assert not depth_hard, f"Inheritance depth 3 should not hard-violate: {result.violations}"


# ---------------------------------------------------------------------------
# Baseline ratchet tests
# ---------------------------------------------------------------------------


class TestBaselineRatchet:
    def _make_baseline(self, tmp_path, entries: dict) -> Path:
        p = tmp_path / ".complexity-baseline.json"
        p.write_text(json.dumps(entries), encoding="utf-8")
        return p

    def test_preexisting_at_same_level_passes(self, tmp_py, tmp_path):
        # Function with loc=155 (hard). Baseline records loc=155. Should pass.
        src = _fn_with_loc(154)  # def + 154 body = 155
        f = tmp_py(src, "target.py")
        # Key includes lineno: function starts at line 1
        fn_key = f"{f}::my_func@1"
        baseline_entry = {"loc": 155, "cyclo": 1, "params": 0, "nesting": 0}
        baseline_path = self._make_baseline(tmp_path, {fn_key: baseline_entry})

        result = analyze_staged_file(
            str(f), is_test=False, baseline=load_baseline(str(baseline_path))
        )
        # Violation should be downgraded to ALLOWED (pre-existing)
        blocked = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and not v.pre_existing
        ]
        assert not blocked, f"Pre-existing violation should not block: {result.violations}"

    def test_worsened_beyond_baseline_blocks(self, tmp_py, tmp_path):
        # Function with loc=200 (hard). Baseline records loc=155. Worsened → block.
        src = _fn_with_loc(199)  # def + 199 = 200
        f = tmp_py(src, "target.py")
        fn_key = f"{f}::my_func@1"
        baseline_entry = {"loc": 155, "cyclo": 1, "params": 0, "nesting": 0}
        baseline_path = self._make_baseline(tmp_path, {fn_key: baseline_entry})

        result = analyze_staged_file(
            str(f), is_test=False, baseline=load_baseline(str(baseline_path))
        )
        blocked = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and not v.pre_existing
        ]
        assert blocked, f"Worsened violation should block: {result.violations}"

    def test_new_file_not_in_baseline_full_caps(self, tmp_py, tmp_path):
        # New file not in baseline → full caps apply
        src = _fn_with_loc(154)  # 155 > 150 hard
        f = tmp_py(src, "target.py")
        baseline_path = self._make_baseline(tmp_path, {})  # empty baseline

        result = analyze_staged_file(
            str(f), is_test=False, baseline=load_baseline(str(baseline_path))
        )
        hard = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and not v.pre_existing
        ]
        assert hard, f"New file should have full caps applied: {result.violations}"

    def test_improvement_below_baseline_passes(self, tmp_py, tmp_path):
        # Function improves from loc=200 to loc=155 — still hard but better than baseline.
        # Should pass (pre-existing at current level).
        src = _fn_with_loc(154)  # 155
        f = tmp_py(src, "target.py")
        fn_key = f"{f}::my_func@1"
        baseline_entry = {"loc": 200, "cyclo": 1, "params": 0, "nesting": 0}
        baseline_path = self._make_baseline(tmp_path, {fn_key: baseline_entry})

        result = analyze_staged_file(
            str(f), is_test=False, baseline=load_baseline(str(baseline_path))
        )
        blocked = [
            v
            for v in result.violations
            if v.severity == ViolationSeverity.HARD and not v.pre_existing
        ]
        assert not blocked, f"Improvement from baseline should not block: {result.violations}"


# ---------------------------------------------------------------------------
# check_files integration — exit code semantics
# ---------------------------------------------------------------------------


class TestCheckFiles:
    def test_hard_violation_blocks(self, tmp_py, empty_baseline):
        src = _fn_with_loc(154)  # hard
        f = tmp_py(src, "hard_target.py")
        result = check_files([str(f)], baseline_path=str(empty_baseline))
        assert result.exit_code == 1, "Hard violation must exit 1"

    def test_soft_only_warns_exit_0(self, tmp_py, empty_baseline):
        src = _fn_with_loc(83)  # soft only (84 LOC, no noqa)
        f = tmp_py(src, "soft_target.py")
        result = check_files([str(f)], baseline_path=str(empty_baseline))
        assert result.exit_code == 0, "Soft-only violation must exit 0"
        assert result.warnings, "Soft violation must produce warnings"

    def test_clean_file_silent_exit_0(self, tmp_py, empty_baseline):
        src = "def f():\n    pass\n"
        f = tmp_py(src, "clean.py")
        result = check_files([str(f)], baseline_path=str(empty_baseline))
        assert result.exit_code == 0
        assert not result.warnings
        assert not result.errors

    def test_noqa_soft_suppressed(self, tmp_py, empty_baseline):
        body = "\n".join(f"    x_{i} = {i}" for i in range(83))
        src = f"def my_func():  # noqa: C901 – cohesive: single validation flow\n{body}\n"
        f = tmp_py(src, "noqa_target.py")
        result = check_files([str(f)], baseline_path=str(empty_baseline))
        assert result.exit_code == 0
        # Should be no warnings for this file's LOC soft violation
        loc_warnings = [w for w in result.warnings if "loc" in w.lower()]
        assert not loc_warnings, f"noqa should silence soft loc warning: {result.warnings}"


# ---------------------------------------------------------------------------
# update_baseline
# ---------------------------------------------------------------------------


class TestUpdateBaseline:
    def test_update_baseline_writes_file(self, tmp_py, tmp_path):
        src = _fn_with_loc(154)  # hard violation
        f = tmp_py(src, "target.py")
        baseline_path = tmp_path / ".complexity-baseline.json"
        update_baseline([str(f)], str(baseline_path))
        assert baseline_path.exists()
        data = json.loads(baseline_path.read_text())
        # Should have at least one entry
        assert len(data) > 0

    def test_update_baseline_records_metrics(self, tmp_py, tmp_path):
        body = "\n".join(f"    x_{i} = {i}" for i in range(154))
        src = f"def my_func():\n{body}\n"
        f = tmp_py(src, "target.py")
        baseline_path = tmp_path / ".complexity-baseline.json"
        update_baseline([str(f)], str(baseline_path))
        data = json.loads(baseline_path.read_text())
        # Key includes lineno @1 (function starts at line 1)
        fn_key = f"{f}::my_func@1"
        assert fn_key in data, f"Expected {fn_key} in baseline, got keys: {list(data.keys())[:5]}"
        entry = data[fn_key]
        assert "loc" in entry
        assert entry["loc"] == 155  # def line + 154 body lines
