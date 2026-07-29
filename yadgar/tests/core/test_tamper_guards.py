"""Meta-tests for the e2e tamper-protection guards (task #52).

Each test SIMULATES a tampering scenario and asserts that the relevant guard
FIRES (returns/raises a violation).  All tests are hermetic — no SurrealDB, no
network, no filesystem mutation beyond tmp_path.

Guards under test:
  Layer 1 — check_green_floor()    in scripts/check_contract_coverage.py
  Layer 2 — check_green_integrity() in scripts/check_contract_coverage.py
  Layer 3 — lint_file() / lint_dir() in scripts/check_e2e_assertions.py
  Layer 4 — check_diff()            in scripts/check_test_weakening.py

Non-e2e (runs under CI `-m 'not e2e'`).
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------


def _load(script_name: str):  # type: ignore[return]
    """Load a scripts/ module by filename and return it."""
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ccc = _load("check_contract_coverage.py")
cea = _load("check_e2e_assertions.py")
ctw = _load("check_test_weakening.py")


# ---------------------------------------------------------------------------
# Fixtures — minimal contract text snippets
# ---------------------------------------------------------------------------

# A valid one-✅ contract body that passes all existing rules.
_ONE_GREEN = (
    "**1 ✅ · 0 ⏳ · 0 ❌.**\n"
    "**0 `[r]` · 0 `[u]` · 0 none.**\n"
    "- BC-E1 row. ✅ "
    "`tests/e2e/test_vacuum_backup_safety.py::TestBCE1_RowCountsPreserved`\n"
)

# A contract with TWO green rows (for floor tests).
_TWO_GREEN = (
    "**2 ✅ · 0 ⏳ · 0 ❌.**\n"
    "**0 `[r]` · 0 `[u]` · 0 none.**\n"
    "- BC-E1 row. ✅ "
    "`tests/e2e/test_vacuum_backup_safety.py::TestBCE1_RowCountsPreserved`\n"
    "- BC-E2 row. ✅ "
    "`tests/e2e/test_vacuum_backup_safety.py::TestBCE2_VacuumAtomicity`\n"
)


# ---------------------------------------------------------------------------
# LAYER 1 — ✅-count floor
# ---------------------------------------------------------------------------


class TestLayer1GreenFloor:
    """Layer 1: guard fires when ✅ count drops below the floor."""

    def test_floor_fires_when_count_below_floor(self) -> None:
        """Simulate: contract has 1 ✅ but floor is 2 → guard must fire."""
        errors = ccc.check_green_floor(_ONE_GREEN, floor=2)
        assert errors, "Layer 1 guard must fire when green count < floor"
        assert any("layer 1" in e and "floor" in e for e in errors), errors

    def test_floor_passes_when_count_equals_floor(self) -> None:
        """Exact match: 1 ✅, floor 1 → passes."""
        errors = ccc.check_green_floor(_ONE_GREEN, floor=1)
        assert not errors, f"Layer 1 must pass at exact floor: {errors}"

    def test_floor_passes_when_count_above_floor(self) -> None:
        """2 ✅, floor 1 → passes (floor is a minimum, not an exact value)."""
        errors = ccc.check_green_floor(_TWO_GREEN, floor=1)
        assert not errors, f"Layer 1 must pass when count exceeds floor: {errors}"

    def test_floor_fires_at_zero_green(self) -> None:
        """All ⏳: zero ✅, floor > 0 → must fire."""
        no_green = (
            "**0 ✅ · 1 ⏳ · 0 ❌.**\n**0 `[r]` · 1 `[u]` · 0 none.**\n- BC-X1 row. ⏳[u] P1\n"
        )
        errors = ccc.check_green_floor(no_green, floor=1)
        assert errors, "Layer 1 must fire when all rows are ⏳ and floor > 0"


# ---------------------------------------------------------------------------
# LAYER 2 — ✅ ↔ decorator integrity
# ---------------------------------------------------------------------------


class TestLayer2GreenIntegrity:
    """Layer 2: guard fires when a ✅-cited node carries skip/skipif/xfail."""

    def test_xfail_on_mapped_test_fires(self, tmp_path: Path) -> None:
        """Simulate: test file has xfail on the mapped node → guard fires."""
        fake_file = tmp_path / "test_fake.py"
        fake_file.write_text(
            textwrap.dedent("""\
                import pytest

                @pytest.mark.xfail(strict=True, reason="broken")
                def test_should_be_green():
                    assert 1 == 1
            """)
        )
        # Contract cites fake_file::test_should_be_green as ✅.
        rel = str(fake_file.relative_to(tmp_path))
        contract_text = (
            "**1 ✅ · 0 ⏳ · 0 ❌.**\n"
            "**0 `[r]` · 0 `[u]` · 0 none.**\n"
            f"- BC-X1 row. ✅ `{rel}::test_should_be_green`\n"
        )
        errors = ccc.check_green_integrity(contract_text, repo_root=tmp_path)
        assert errors, "Layer 2 must fire when ✅-mapped node has xfail"
        assert any("layer 2" in e and "test_should_be_green" in e for e in errors), errors

    def test_skip_on_mapped_test_fires(self, tmp_path: Path) -> None:
        """Simulate: mapped node has @pytest.mark.skip → guard fires."""
        fake_file = tmp_path / "test_skipped.py"
        fake_file.write_text(
            textwrap.dedent("""\
                import pytest

                @pytest.mark.skip(reason="not yet")
                def test_was_green():
                    assert True
            """)
        )
        rel = str(fake_file.relative_to(tmp_path))
        contract_text = (
            "**1 ✅ · 0 ⏳ · 0 ❌.**\n"
            "**0 `[r]` · 0 `[u]` · 0 none.**\n"
            f"- BC-X2 row. ✅ `{rel}::test_was_green`\n"
        )
        errors = ccc.check_green_integrity(contract_text, repo_root=tmp_path)
        assert errors, "Layer 2 must fire when ✅-mapped node has skip"
        assert any("layer 2" in e for e in errors), errors

    def test_xfail_on_class_fires(self, tmp_path: Path) -> None:
        """Simulate: the *class* carrying the method has xfail → guard fires."""
        fake_file = tmp_path / "test_cls.py"
        fake_file.write_text(
            textwrap.dedent("""\
                import pytest

                @pytest.mark.xfail(strict=True, reason="class-level xfail")
                class TestMyClass:
                    def test_method(self):
                        assert True
            """)
        )
        rel = str(fake_file.relative_to(tmp_path))
        contract_text = (
            "**1 ✅ · 0 ⏳ · 0 ❌.**\n"
            "**0 `[r]` · 0 `[u]` · 0 none.**\n"
            f"- BC-X3 row. ✅ `{rel}::TestMyClass`\n"
        )
        errors = ccc.check_green_integrity(contract_text, repo_root=tmp_path)
        assert errors, "Layer 2 must fire when ✅-mapped class has xfail"

    def test_clean_test_passes(self, tmp_path: Path) -> None:
        """A ✅-mapped test with no skip/xfail decorators must not fire."""
        fake_file = tmp_path / "test_clean.py"
        fake_file.write_text(
            textwrap.dedent("""\
                def test_real_passing():
                    assert 2 + 2 == 4
            """)
        )
        rel = str(fake_file.relative_to(tmp_path))
        contract_text = (
            "**1 ✅ · 0 ⏳ · 0 ❌.**\n"
            "**0 `[r]` · 0 `[u]` · 0 none.**\n"
            f"- BC-X4 row. ✅ `{rel}::test_real_passing`\n"
        )
        errors = ccc.check_green_integrity(contract_text, repo_root=tmp_path)
        assert not errors, f"Layer 2 must not fire on a clean test: {errors}"

    def test_pending_row_with_xfail_not_flagged(self, tmp_path: Path) -> None:
        """A ⏳ row citing an xfail-decorated test is fine — layer 2 only guards ✅."""
        fake_file = tmp_path / "test_pending.py"
        fake_file.write_text(
            textwrap.dedent("""\
                import pytest

                @pytest.mark.xfail(strict=True, reason="known broken")
                def test_known_broken():
                    assert False
            """)
        )
        rel = str(fake_file.relative_to(tmp_path))
        contract_text = (
            "**0 ✅ · 1 ⏳ · 0 ❌.**\n"
            "**0 `[r]` · 1 `[u]` · 0 none.**\n"
            f"- BC-X5 row. ⏳[u] `{rel}::test_known_broken`\n"
        )
        errors = ccc.check_green_integrity(contract_text, repo_root=tmp_path)
        assert not errors, f"Layer 2 must only guard ✅ rows, not ⏳: {errors}"


# ---------------------------------------------------------------------------
# LAYER 3 — e2e assertion-presence lint
# ---------------------------------------------------------------------------


class TestLayer3AssertionPresence:
    """Layer 3: AST lint fires on gutted test bodies, passes on real assertions."""

    def test_gutted_test_fires(self, tmp_path: Path) -> None:
        """Simulate: test_* function with only `pass` → guard fires."""
        fake = tmp_path / "test_gutted.py"
        fake.write_text(
            textwrap.dedent("""\
                def test_empty():
                    pass
            """)
        )
        violations = cea.lint_file(fake)
        assert violations, "Layer 3 must fire on a test with no assertions"
        assert any("test_empty" in v for v in violations), violations

    def test_assert_stmt_passes(self, tmp_path: Path) -> None:
        """A test with a plain assert statement passes."""
        fake = tmp_path / "test_assert.py"
        fake.write_text(
            textwrap.dedent("""\
                def test_has_assert():
                    assert 1 + 1 == 2
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"Layer 3 must pass on test with assert: {violations}"

    def test_pytest_raises_passes(self, tmp_path: Path) -> None:
        """A test using `with pytest.raises(...)` passes."""
        fake = tmp_path / "test_raises.py"
        fake.write_text(
            textwrap.dedent("""\
                import pytest

                def test_has_raises():
                    with pytest.raises(ValueError):
                        raise ValueError("expected")
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"Layer 3 must pass on pytest.raises: {violations}"

    def test_pytest_warns_passes(self, tmp_path: Path) -> None:
        """A test using `with pytest.warns(...)` passes."""
        fake = tmp_path / "test_warns.py"
        fake.write_text(
            textwrap.dedent("""\
                import pytest
                import warnings

                def test_has_warns():
                    with pytest.warns(UserWarning):
                        warnings.warn("hi", UserWarning)
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"Layer 3 must pass on pytest.warns: {violations}"

    def test_self_assert_method_passes(self, tmp_path: Path) -> None:
        """A test method using self.assertEqual passes (call name starts with 'assert')."""
        fake = tmp_path / "test_self.py"
        fake.write_text(
            textwrap.dedent("""\
                class TestSomething:
                    def test_method(self):
                        self.assertEqual(1, 1)
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"Layer 3 must pass on self.assert* call: {violations}"

    def test_escape_comment_suppresses_violation(self, tmp_path: Path) -> None:
        """A test with `# tamper-lint: no-assert <reason>` is exempt."""
        fake = tmp_path / "test_escape.py"
        fake.write_text(
            textwrap.dedent("""\
                def test_side_effect_only():
                    # tamper-lint: no-assert assertion is via side-effect
                    some_call_that_raises_on_fail()
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"Escape comment must suppress violation: {violations}"

    def test_skip_decorated_test_exempt(self, tmp_path: Path) -> None:
        """A test decorated with @pytest.mark.skip is exempt from assertion check."""
        fake = tmp_path / "test_skip_exempt.py"
        fake.write_text(
            textwrap.dedent("""\
                import pytest

                @pytest.mark.skip(reason="deferred")
                def test_not_yet_implemented():
                    raise NotImplementedError
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"skip-decorated test must be exempt: {violations}"

    def test_xfail_decorated_test_exempt(self, tmp_path: Path) -> None:
        """A test decorated with @pytest.mark.xfail is exempt from assertion check."""
        fake = tmp_path / "test_xfail_exempt.py"
        fake.write_text(
            textwrap.dedent("""\
                import pytest

                @pytest.mark.xfail(strict=True, reason="known broken")
                def test_known_broken():
                    pass
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"xfail-decorated test must be exempt: {violations}"

    def test_non_test_function_not_flagged(self, tmp_path: Path) -> None:
        """Helper functions not named test_* are not subject to the check."""
        fake = tmp_path / "test_helper.py"
        fake.write_text(
            textwrap.dedent("""\
                def _helper():
                    pass

                def test_calls_helper():
                    result = _helper()
                    assert result is None
            """)
        )
        violations = cea.lint_file(fake)
        assert not violations, f"Non-test helpers must not be flagged: {violations}"


# ---------------------------------------------------------------------------
# LAYER 4 — pre-commit diff guard
# ---------------------------------------------------------------------------


class TestLayer4DiffGuard:
    """Layer 4: diff guard fires on assert removal or ✅ count decrease."""

    def _make_diff(self, removed_lines: list[str], added_lines: list[str]) -> str:
        """Build a minimal unified-diff string for an e2e test file."""
        lines = ["diff --git a/yadgar/tests/e2e/test_fake.py b/yadgar/tests/e2e/test_fake.py"]
        lines.extend(f"-{line}" for line in removed_lines)
        lines.extend(f"+{line}" for line in added_lines)
        return "\n".join(lines)

    def test_net_removal_of_asserts_fires(self) -> None:
        """Removing more asserts than added → guard fires."""
        diff = self._make_diff(
            removed_lines=["    assert x == 1", "    assert y > 0"],
            added_lines=["    pass"],
        )
        errors = ctw.check_diff(diff, head_green=5, staged_green=5)
        assert errors, "Layer 4 must fire on net assert removal"
        assert any("layer 4" in e and "assert" in e for e in errors), errors

    def test_net_addition_of_asserts_passes(self) -> None:
        """Adding more asserts than removed → no error."""
        diff = self._make_diff(
            removed_lines=["    assert x == 1"],
            added_lines=["    assert x == 1", "    assert y > 0"],
        )
        errors = ctw.check_diff(diff, head_green=5, staged_green=5)
        assert not errors, f"Layer 4 must pass on net assert addition: {errors}"

    def test_green_count_drop_fires(self) -> None:
        """✅ count decreasing from 5 to 4 → guard fires."""
        errors = ctw.check_diff("", head_green=5, staged_green=4)
        assert errors, "Layer 4 must fire when ✅ count drops"
        assert any("layer 4" in e and "✅" in e for e in errors), errors

    def test_green_count_stable_passes(self) -> None:
        """✅ count unchanged → no error."""
        errors = ctw.check_diff("", head_green=5, staged_green=5)
        assert not errors, f"Layer 4 must pass when ✅ count is stable: {errors}"

    def test_green_count_increase_passes(self) -> None:
        """✅ count growing → no error (tests are being added)."""
        errors = ctw.check_diff("", head_green=5, staged_green=6)
        assert not errors, f"Layer 4 must pass when ✅ count grows: {errors}"

    def test_both_violations_fire_together(self) -> None:
        """Both assert removal AND ✅ drop → two separate violations reported."""
        diff = self._make_diff(
            removed_lines=["    assert x == 1", "    assert y > 0"],
            added_lines=[],
        )
        errors = ctw.check_diff(diff, head_green=5, staged_green=4)
        assert len(errors) >= 2, f"Both violations must be reported: {errors}"

    def test_non_e2e_file_asserts_ignored(self) -> None:
        """Assert removal in a non-e2e file does not trigger the guard."""
        diff = (
            "diff --git a/yadgar/server/tools/memorize.py b/yadgar/server/tools/memorize.py\n"
            "-    assert x == 1\n"
            "+    pass\n"
        )
        errors = ctw.check_diff(diff, head_green=5, staged_green=5)
        assert not errors, f"Non-e2e assert removal must not fire: {errors}"

    def test_missing_head_green_skips_green_check(self) -> None:
        """If HEAD contract is unreadable (None), green-count check is skipped."""
        errors = ctw.check_diff("", head_green=None, staged_green=4)
        assert not errors, f"Should skip green check when head_green is None: {errors}"


# ---------------------------------------------------------------------------
# Integration: real tree is clean
# ---------------------------------------------------------------------------


def test_real_contract_passes_all_layers() -> None:
    """The shipped BEHAVIOR_CONTRACT.md passes both layer-1 and layer-2 checks."""
    text = (_REPO_ROOT / "docs" / "contracts" / "BEHAVIOR_CONTRACT.md").read_text(encoding="utf-8")
    errors = ccc.check(text)
    assert not errors, f"Real contract must pass all guards: {errors}"


def test_real_e2e_dir_passes_layer3() -> None:
    """All current e2e tests have at least one real assertion."""
    violations = cea.lint_dir()
    assert not violations, f"Real e2e tests must all have assertions: {violations}"


# ---------------------------------------------------------------------------
# Scan-scope widening (gate-blindness class, 2026-07-29)
#
# The layer-3 lint pinned its scan root to yadgar/tests/e2e/ while six *e2e*
# modules live outside it — scan-scope < artifact-scope, so those modules were
# never assertion-linted.  Layer 4 carried the SAME pin independently in its own
# regex.  These tests hold both scopes open and mechanically lock them together.
# ---------------------------------------------------------------------------


class TestScanScope:
    """The layer-3 scan set covers every *e2e* module, not just tests/e2e/."""

    def test_scan_paths_includes_out_of_root_e2e_modules(self) -> None:
        """*e2e* modules outside yadgar/tests/e2e/ are in the scan set."""
        scanned = {p.relative_to(_REPO_ROOT).as_posix() for p in cea.scan_paths()}
        # Discovered independently of the script, so the test cannot inherit
        # the script's own scoping bug.
        expected = {
            p.relative_to(_REPO_ROOT).as_posix()
            for p in (_REPO_ROOT / "yadgar" / "tests").rglob("*e2e*.py")
        }
        assert expected, "fixture guard: repo must contain *e2e* test modules"
        missing = expected - scanned
        assert not missing, f"e2e-shaped modules outside the scan set: {sorted(missing)}"

    def test_scan_paths_still_includes_the_e2e_dir(self) -> None:
        """Widening must not drop the original yadgar/tests/e2e/ scan root."""
        scanned = set(cea.scan_paths())
        e2e_dir_files = set((_REPO_ROOT / "yadgar" / "tests" / "e2e").rglob("*.py"))
        assert e2e_dir_files, "fixture guard: yadgar/tests/e2e/ must contain modules"
        assert e2e_dir_files <= scanned, "widening dropped files from the original scan root"

    def test_scan_paths_excludes_non_e2e_tests(self) -> None:
        """A plain unit-test module is NOT pulled into the e2e assertion lint."""
        scanned = {p.relative_to(_REPO_ROOT).as_posix() for p in cea.scan_paths()}
        assert "yadgar/tests/core/test_tamper_guards.py" not in scanned

    def test_real_scan_scope_passes_layer3(self) -> None:
        """Every e2e-shaped module in the repo has assertions (CI enforcement hook).

        This is what gives the layer-3 lint CI presence: tests/core/ runs in CI,
        so a violation anywhere in the widened scan set fails a PR even though
        the pre-commit hook is not what caught it.
        """
        violations = cea.lint_scope()
        assert not violations, f"Widened e2e scan set must be clean: {violations}"


class TestLayer3Layer4ScopeLockstep:
    """Layer 4's internal path regex must track layer 3's scan set exactly.

    The two scopes are declared independently (one is a path glob, the other a
    regex over `diff --git` lines).  Drift between them re-creates the original
    defect silently, so assert the agreement mechanically rather than by comment.
    """

    def test_every_scanned_path_matches_layer4_regex(self) -> None:
        unmatched = [
            p.relative_to(_REPO_ROOT).as_posix()
            for p in cea.scan_paths()
            if not ctw._E2E_PATH_RE.search(p.relative_to(_REPO_ROOT).as_posix())
        ]
        assert not unmatched, f"layer 4 regex does not cover layer 3 scan paths: {unmatched}"

    def test_layer4_ignores_non_e2e_test_modules(self) -> None:
        assert not ctw._E2E_PATH_RE.search("yadgar/tests/core/test_tamper_guards.py")

    def test_layer4_fires_on_out_of_root_e2e_module(self) -> None:
        """An assert removal in an out-of-root *e2e* module is now caught."""
        diff = (
            "diff --git a/yadgar/tests/core/test_backend_traceparent_e2e.py"
            " b/yadgar/tests/core/test_backend_traceparent_e2e.py\n"
            "-    assert resp.status_code == 200\n"
            "+    pass\n"
        )
        errors = ctw.check_diff(diff, head_green=5, staged_green=5)
        assert errors, "layer 4 must fire on assert removal outside yadgar/tests/e2e/"
        assert any("assert" in e for e in errors), errors
