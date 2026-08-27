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

import ast
import importlib.util
import json
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


# ---------------------------------------------------------------------------
# LAYER 4 — branch-diff mode (gate-blindness class, 2026-07-29)
#
# The guard sourced its entire input from `git diff --cached`.  A CI checkout
# has an empty index, so `diff_text` was always "" there: the hook executed,
# printed "test-weakening guard OK." and exited 0 regardless of what the PR
# contained.  Correct trigger, correct scope, assertion structurally incapable
# of firing where it matters.  Fixed by porting check_backend_bump's ADR-0080
# merge-base contract: one pure check_diff() fed from the same inputs in both
# modes, so local and CI return the same verdict for the same repo state.
# ---------------------------------------------------------------------------


class _FakeGit:
    """Scriptable `git` stand-in: maps an argv tuple to canned stdout."""

    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: list[str]) -> str:
        key = tuple(args)
        self.calls.append(key)
        return self.responses.get(key, "")


_BRANCH_DIFF_WEAKENED = (
    "diff --git a/yadgar/tests/core/test_consolidation_embedded_e2e.py"
    " b/yadgar/tests/core/test_consolidation_embedded_e2e.py\n"
    "-    assert stored == 3\n"
    "+    pass\n"
)

_CONTRACT_PATH = "docs/contracts/BEHAVIOR_CONTRACT.md"


def _responses(
    *,
    merge_base: str = "abc123",
    branch_diff: str = "",
    staged_diff: str = "",
    base_green: str = "**5 ✅",
    head_green: str = "**5 ✅",
    index_green: str | None = None,
) -> dict[tuple[str, ...], str]:
    out = {
        ("merge-base", "origin/master", "HEAD"): merge_base,
        ("diff", merge_base, "HEAD"): branch_diff,
        ("diff", "--cached"): staged_diff,
        ("show", f"{merge_base}:{_CONTRACT_PATH}"): base_green,
        ("show", f"HEAD:{_CONTRACT_PATH}"): head_green,
        ("show", f":{_CONTRACT_PATH}"): index_green if index_green is not None else "",
    }
    return out


class TestLayer4BranchDiffMode:
    """Layer 4 must see committed-but-not-staged weakening — the CI condition."""

    def test_committed_weakening_is_caught_in_branch_mode(self) -> None:
        """THE fix: an assert removal committed on the branch, nothing staged."""
        git = _FakeGit(_responses(branch_diff=_BRANCH_DIFF_WEAKENED, staged_diff=""))
        diff_text, base_green, after_green = ctw.collect_inputs("origin/master", git)
        errors = ctw.check_diff(diff_text, base_green, after_green)
        assert errors, "branch-diff mode must catch a committed assert removal"
        assert any("assert" in e for e in errors), errors

    def test_same_state_is_invisible_to_staged_only_mode(self) -> None:
        """The contrast that proves the old guard was inert: nothing staged → nothing seen."""
        staged_only_diff = ""  # exactly what `git diff --cached` returns in CI
        errors = ctw.check_diff(staged_only_diff, 5, 5)
        assert not errors, "staged-only mode is blind to committed weakening (the defect)"

    def test_staged_changes_still_counted(self) -> None:
        """Pre-commit still sees the about-to-exist commit, not just branch history."""
        git = _FakeGit(_responses(branch_diff="", staged_diff=_BRANCH_DIFF_WEAKENED))
        diff_text, base_green, after_green = ctw.collect_inputs("origin/master", git)
        errors = ctw.check_diff(diff_text, base_green, after_green)
        assert errors, "staged weakening must still fire (no regression on the old path)"

    def test_branch_and_staged_diffs_are_unioned(self) -> None:
        git = _FakeGit(_responses(branch_diff="BRANCH_MARKER\n", staged_diff="STAGED_MARKER\n"))
        diff_text, _, _ = ctw.collect_inputs("origin/master", git)
        assert "BRANCH_MARKER" in diff_text
        assert "STAGED_MARKER" in diff_text

    def test_green_count_baseline_is_merge_base_not_head(self) -> None:
        """✅ regression across the whole branch is caught, not just this commit."""
        git = _FakeGit(
            _responses(base_green="**9 ✅", head_green="**7 ✅"),
        )
        diff_text, base_green, after_green = ctw.collect_inputs("origin/master", git)
        assert base_green == 9
        assert after_green == 7
        errors = ctw.check_diff(diff_text, base_green, after_green)
        assert any("✅" in e for e in errors), errors

    def test_index_green_wins_over_head_when_staged(self) -> None:
        """When the contract is staged, the index copy is the about-to-exist state."""
        git = _FakeGit(_responses(base_green="**9 ✅", head_green="**9 ✅", index_green="**6 ✅"))
        _, base_green, after_green = ctw.collect_inputs("origin/master", git)
        assert (base_green, after_green) == (9, 6)

    def test_unreachable_base_falls_back_to_staged_only(self) -> None:
        """No origin/master (fresh clone, no remote) → degrade, never raise."""
        git = _FakeGit(
            {
                ("merge-base", "origin/master", "HEAD"): "",  # unreachable
                ("diff", "--cached"): _BRANCH_DIFF_WEAKENED,
                ("show", f"HEAD:{_CONTRACT_PATH}"): "**5 ✅",
                ("show", f":{_CONTRACT_PATH}"): "**5 ✅",
            }
        )
        diff_text, base_green, after_green = ctw.collect_inputs("origin/master", git)
        assert "assert stored == 3" in diff_text, "fallback must still read the staged diff"
        assert (base_green, after_green) == (5, 5)
        errors = ctw.check_diff(diff_text, base_green, after_green)
        assert errors, "fallback still catches staged weakening"

    def test_unreachable_base_does_not_diff_against_a_literal_ref(self) -> None:
        """Fallback must not ask git to diff against the empty string / a bogus ref."""
        git = _FakeGit({("merge-base", "origin/master", "HEAD"): ""})
        ctw.collect_inputs("origin/master", git)
        assert ("diff", "", "HEAD") not in git.calls, git.calls

    def test_missing_contract_at_base_skips_green_check(self) -> None:
        """Contract absent at the merge-base (new file) → green check is skipped, not crashed."""
        git = _FakeGit(_responses(base_green="", head_green="**5 ✅"))
        diff_text, base_green, after_green = ctw.collect_inputs("origin/master", git)
        assert base_green is None
        assert not ctw.check_diff(diff_text, base_green, after_green)

    def test_ci_mode_uses_branch_diff(self, monkeypatch) -> None:
        """`--ci --base <ref>` runs the same collector — same inputs, same verdict."""
        git = _FakeGit(_responses(branch_diff=_BRANCH_DIFF_WEAKENED))
        monkeypatch.setattr(ctw, "_git", git)
        assert ctw.main(["--ci", "--base", "origin/master"]) == 1

    def test_ci_mode_requires_base(self) -> None:
        assert ctw.main(["--ci"]) == 1, "--ci without --base must error, not silently pass"


class TestLayer4PerFileDelta:
    """Assert removals are netted PER FILE, not summed across the whole diff.

    Discovered empirically while executing the branch-diff mutation test: with a
    single global sum, removing an assert from one e2e module was masked by five
    asserts ADDED to a different e2e module earlier on the same branch, and the
    guard stayed green.  Global-net was tolerable while the window was one staged
    commit (commits are narrow, so offsetting was rare); over a whole branch it
    collapses the guard's sensitivity to "the branch's total e2e assert count went
    down" — far weaker than the per-commit behaviour it replaced.  A removal in
    test A is not compensated by an addition in test B: different tests.
    """

    _MULTI_FILE_OFFSETTING = (
        "diff --git a/yadgar/tests/core/test_code_graph_e2e.py"
        " b/yadgar/tests/core/test_code_graph_e2e.py\n"
        + "+    assert ok\n"
        * 5
        + "diff --git a/yadgar/tests/core/test_consolidation_embedded_e2e.py"
        " b/yadgar/tests/core/test_consolidation_embedded_e2e.py\n"
        '-    assert rows, f"memory:{mid} not found"\n'
        "+    pass\n"
    )

    def test_removal_is_not_masked_by_additions_in_another_file(self) -> None:
        """THE regression shape: -1 in file B, +5 in file A → must still fire."""
        errors = ctw.check_diff(self._MULTI_FILE_OFFSETTING, 5, 5)
        assert errors, "a removal in one e2e module must not be offset by another module"

    def test_violation_names_the_offending_file(self) -> None:
        """Per-file semantics make the file the unit of violation — report it."""
        errors = ctw.check_diff(self._MULTI_FILE_OFFSETTING, 5, 5)
        joined = " ".join(errors)
        assert "test_consolidation_embedded_e2e.py" in joined, errors
        assert "test_code_graph_e2e.py" not in joined, (
            "the file that ADDED asserts must not be blamed",
            errors,
        )

    def test_net_positive_within_one_file_still_passes(self) -> None:
        """Tightening the scope must not degrade into 'any removed assert line, ever'."""
        diff = (
            "diff --git a/yadgar/tests/e2e/test_fake.py b/yadgar/tests/e2e/test_fake.py\n"
            "-    assert x == 1\n"
            "+    assert x == 1\n"
            "+    assert y > 0\n"
        )
        assert not ctw.check_diff(diff, 5, 5), "a refactor that nets +1 in one file is fine"

    def test_multiple_offending_files_all_reported(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/e2e/test_a.py b/yadgar/tests/e2e/test_a.py\n"
            "-    assert a\n"
            "diff --git a/yadgar/tests/e2e/test_b.py b/yadgar/tests/e2e/test_b.py\n"
            "-    assert b\n"
        )
        joined = " ".join(ctw.check_diff(diff, 5, 5))
        assert "test_a.py" in joined and "test_b.py" in joined


class TestLayer4CiModeRequiresRealBase:
    """In --ci mode an unresolvable merge-base is a HARD ERROR, not a fail-open.

    The fail-open is correct for pre-commit (a fresh clone with no remote is a
    legitimate state). In CI it is the defect class this whole plan exists to
    remove: if `git merge-base origin/master HEAD` cannot resolve — shallow
    checkout, unfetched ref, dubious-ownership refusal — the branch diff collapses
    to empty and the step prints "OK" exit 0, indistinguishable from a genuine
    pass. Nothing in the CI log would reveal that the guard never engaged.

    Passing --ci --base <ref> IS the caller asserting that base ref exists.
    """

    def test_ci_mode_hard_fails_when_base_unresolvable(self, monkeypatch) -> None:
        monkeypatch.setattr(ctw, "_git", _FakeGit({}))  # merge-base returns ""
        assert ctw.main(["--ci", "--base", "origin/master"]) == 1

    def test_precommit_mode_still_fails_open_when_base_unresolvable(self, monkeypatch) -> None:
        """Fresh clone / no remote must NOT block a local commit."""
        monkeypatch.setattr(ctw, "_git", _FakeGit({}))
        assert ctw.main([]) == 0

    def test_resolve_merge_base_returns_empty_when_git_fails(self) -> None:
        assert ctw.resolve_merge_base("origin/master", _FakeGit({})) == ""

    def test_resolve_merge_base_returns_the_sha(self) -> None:
        git = _FakeGit({("merge-base", "origin/master", "HEAD"): "abc123\n"})
        assert ctw.resolve_merge_base("origin/master", git) == "abc123"


# ---------------------------------------------------------------------------
# Layer 4 — per-entry allowlist (replaces the ALLOW_TEST_WEAKEN blanket bypass)
# ---------------------------------------------------------------------------
#
# `ALLOW_TEST_WEAKEN=1` was a blanket, invisible, whole-run bypass: one env var
# silenced EVERY file in the diff, left no trace in the diff a reviewer reads,
# and was used three times on the ADR-0215 train.  It is replaced by a per-file
# allowlist that records each sanctioned deletion with an exact allowed delta
# and a written reason, so the sanction is reviewable where review happens.


def _diff_for(path: str, removed: int = 0, added: int = 0) -> str:
    """Build a minimal diff whose net assert delta for *path* is added-removed."""
    return (
        f"diff --git a/{path} b/{path}\n"
        + "-    assert gone\n" * removed
        + "+    assert kept\n" * added
    )


_ALLOWED_FILE = "yadgar/tests/e2e/test_scope_filter_e2e.py"
_REASON = (
    "Deleted by Car 1 (7bf28dda) per ADR-0215 — asserted branch+directory scope "
    "composition, and the branch axis no longer exists."
)


class TestLayer4Allowlist:
    """Per-entry allowlist: exact delta, no grandfathering, stale entries surfaced."""

    def test_allowlisted_file_within_its_delta_passes(self) -> None:
        """The sanctioned deletion itself: measured -12, allowed -12 → clean."""
        allowlist = {_ALLOWED_FILE: {"allowed_delta": -12, "rationale": _REASON}}
        errors = ctw.check_diff(_diff_for(_ALLOWED_FILE, removed=12), 5, 5, allowlist)
        assert not errors, errors

    def test_allowlisted_file_exceeding_its_delta_fails(self) -> None:
        """THE anti-grandfather property: -13 against an allowed -12 must FAIL.

        An entry that absorbs any future weakening of the same file is the same
        hole in a nicer coat — the allowlist would license the file forever.
        """
        allowlist = {_ALLOWED_FILE: {"allowed_delta": -12, "rationale": _REASON}}
        errors = ctw.check_diff(_diff_for(_ALLOWED_FILE, removed=13), 5, 5, allowlist)
        assert errors, "a delta worse than the allowlisted one must not be absorbed"
        assert any("-13" in e and "-12" in e for e in errors), errors

    def test_non_allowlisted_file_still_fails(self) -> None:
        """A file with no entry fails exactly as it did before the allowlist."""
        errors = ctw.check_diff(_diff_for("yadgar/tests/e2e/test_other.py", removed=1), 5, 5, {})
        assert errors, "an ungoverned removal must still fire"
        assert any("test_other.py" in e for e in errors), errors

    def test_absent_allowlist_keeps_todays_strict_behaviour(self) -> None:
        """Default (no allowlist passed) == the pre-allowlist contract."""
        errors = ctw.check_diff(_diff_for(_ALLOWED_FILE, removed=12), 5, 5)
        assert errors, "check_diff with no allowlist must stay strict"

    def test_stale_entry_when_delta_improved_is_surfaced(self) -> None:
        """Measured -11 against a recorded -12: the entry over-grants — say so."""
        allowlist = {_ALLOWED_FILE: {"allowed_delta": -12, "rationale": _REASON}}
        warnings = ctw.stale_allowlist_entries(_diff_for(_ALLOWED_FILE, removed=11), allowlist)
        assert any("STALE" in w and _ALLOWED_FILE in w for w in warnings), warnings

    def test_stale_entry_when_file_left_the_diff_is_surfaced(self) -> None:
        """The post-merge shape: the file is no longer in the diff at all."""
        allowlist = {_ALLOWED_FILE: {"allowed_delta": -12, "rationale": _REASON}}
        warnings = ctw.stale_allowlist_entries("", allowlist)
        assert any("STALE" in w and _ALLOWED_FILE in w for w in warnings), warnings

    def test_stale_entry_does_not_fail_the_run(self) -> None:
        """Stale is a WARNING here, unlike the sibling guards — the base ref moves."""
        allowlist = {_ALLOWED_FILE: {"allowed_delta": -12, "rationale": _REASON}}
        assert not ctw.check_diff("", 5, 5, allowlist), "stale must not be a hard error"

    def test_short_rationale_is_malformed(self) -> None:
        allowlist = {_ALLOWED_FILE: {"allowed_delta": -12, "rationale": "too short"}}
        errors = ctw.check_diff(_diff_for(_ALLOWED_FILE, removed=12), 5, 5, allowlist)
        assert any("MALFORMED" in e for e in errors), errors

    def test_non_negative_allowed_delta_is_malformed(self) -> None:
        """An entry only ever sanctions a REMOVAL; 0/+N is a meaningless entry."""
        allowlist = {_ALLOWED_FILE: {"allowed_delta": 0, "rationale": _REASON}}
        errors = ctw.check_diff(_diff_for(_ALLOWED_FILE, removed=12), 5, 5, allowlist)
        assert any("MALFORMED" in e for e in errors), errors

    def test_error_message_points_at_the_allowlist_not_an_env_var(self) -> None:
        errors = ctw.check_diff(_diff_for("yadgar/tests/e2e/test_other.py", removed=1), 5, 5, {})
        joined = " ".join(errors)
        assert ".test-weakening-allowlist.json" in joined, errors
        assert "ALLOW_TEST_WEAKEN" not in joined, "the removed env var must not be advertised"

    def test_green_regression_message_points_at_the_allowlist(self) -> None:
        joined = " ".join(ctw.check_diff("", head_green=5, staged_green=4))
        assert "ALLOW_TEST_WEAKEN" not in joined, joined

    def test_shipped_allowlist_is_wellformed(self) -> None:
        """The real file on disk parses and every present entry satisfies the schema.

        Well-formed, not non-empty: 0047 C15a emptied the shipped allowlist on
        purpose (its own ``_stale_policy`` — both prior entries' files left the
        merge-base diff for good once the ADR-0215 train merged, so the entries
        could never stop warning and were removed). "Empty" is now a legitimate
        governed state — "no entries currently sanctioned" — not a guard defect,
        so asserting non-emptiness would make this test fail on correct,
        intentional cleanup forever. Routed through ``check_diff`` (rather than
        re-implementing the per-field checks here) so this test exercises the
        exact validation path production runs, not a parallel copy of it that
        could drift. ``test_corrupted_allowlist_copy_is_rejected`` below proves
        this rewrite still rejects a malformed file — the property the test's
        name promises.
        """
        allowlist = ctw.load_allowlist(_REPO_ROOT / ctw._ALLOWLIST_NAME)
        errors = ctw.check_diff("", head_green=None, staged_green=None, allowlist=allowlist)
        assert not errors, errors

    def test_corrupted_allowlist_copy_is_rejected(self, tmp_path: Path) -> None:
        """Deliberately corrupt a scratch copy and confirm well-formedness still fires.

        Proves the rewrite above didn't quietly stop checking anything: an
        allowlist entry with a non-negative ``allowed_delta`` *and* a too-short
        ``rationale`` must still surface MALFORMED, exactly as it did before an
        empty allowlist was accepted as legitimate.
        """
        corrupted = tmp_path / "corrupted-allowlist.json"
        corrupted.write_text(
            json.dumps(
                {
                    "_comment": "scratch copy, deliberately malformed for this test",
                    "yadgar/tests/e2e/test_bad.py": {
                        "allowed_delta": 3,
                        "rationale": "too short",
                    },
                }
            )
        )
        allowlist = ctw.load_allowlist(corrupted)
        assert allowlist, "fixture guard: the malformed entry must survive the underscore-strip"
        errors = ctw.check_diff("", head_green=None, staged_green=None, allowlist=allowlist)
        assert errors, "a malformed entry must still be rejected"
        assert any("MALFORMED" in e for e in errors), errors


class TestLayer4EnvBypassIsGone:
    """The regression that matters: ALLOW_TEST_WEAKEN must do NOTHING.

    Shape matters here.  Asserting `main() == 0` with the var set proves nothing
    once the real branch diff is allowlisted — it would return 0 either way.  The
    only test that can fail if the bypass came back drives main() over a
    WEAKENED, NON-allowlisted diff with the var set and demands exit 1.
    """

    def test_env_var_does_not_bypass_a_weakened_diff(self, monkeypatch) -> None:
        monkeypatch.setattr(ctw, "_git", _FakeGit(_responses(branch_diff=_BRANCH_DIFF_WEAKENED)))
        monkeypatch.setattr(ctw, "_ALLOWLIST_NAME", ".nonexistent-allowlist.json")
        monkeypatch.setenv("ALLOW_TEST_WEAKEN", "1")
        assert ctw.main(["--ci", "--base", "origin/master"]) == 1, (
            "ALLOW_TEST_WEAKEN must no longer bypass anything"
        )

    def test_env_var_does_not_bypass_in_precommit_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(ctw, "_git", _FakeGit(_responses(branch_diff=_BRANCH_DIFF_WEAKENED)))
        monkeypatch.setattr(ctw, "_ALLOWLIST_NAME", ".nonexistent-allowlist.json")
        monkeypatch.setenv("ALLOW_TEST_WEAKEN", "1")
        assert ctw.main([]) == 1

    def test_the_bypass_constant_is_deleted(self) -> None:
        assert not hasattr(ctw, "_ALLOW_ENV"), "_ALLOW_ENV must be gone, not merely unused"

    def test_the_script_never_mentions_the_env_var(self) -> None:
        src = (_REPO_ROOT / "scripts" / "check_test_weakening.py").read_text(encoding="utf-8")
        assert "ALLOW_TEST_WEAKEN" not in src, "no docstring/message may advertise the bypass"

    def test_no_workflow_passes_the_env_var(self) -> None:
        """Both workflow sets — they diverge, so both must be checked."""
        for rel in (".github/workflows/ci-pr.yml", ".forgejo/workflows/ci-pr.yaml"):
            text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "ALLOW_TEST_WEAKEN" not in text, f"{rel} still carries the bypass"


# ---------------------------------------------------------------------------
# Layer 4 — the NON-e2e arm (task 379)
#
# ADR-0430 names scripts/check_test_weakening.py as the mechanism enforcing
# "no test may be weakened to reach green".  Its scan scope was
# `_E2E_PATH_RE` — `yadgar/tests/e2e/**` plus modules with `e2e` in the name —
# so it could only ever fail on an e2e file.  Measured on PR #68: 29 test files
# changed, 4,224 lines, ZERO of them e2e.  The guard passed unconditionally
# while three assertions were relaxed, and `.test-weakening-allowlist.json` was
# never consulted because nothing in the diff was in scope to consult it for.
#
# Correct trigger, correct wiring, scope structurally incapable of covering the
# files that actually change — the same "gate that checks nothing" shape as the
# `git diff --cached` defect above, one layer over.
#
# The scan is now every `yadgar/tests/**/*.py`.  `_E2E_PATH_RE` stays, but only
# as layer 3's lockstep declaration (layer 3 still scans e2e alone).
#
# Three weakening SHAPES are detected, chosen because they are the shapes that
# actually occurred and because they are the shapes a net-assert-count rule is
# blind to (relaxing `assert x == ""` to `assert not x` removes one assert line
# and adds one — net zero).  Measured over the whole bug-bag-2 train diff
# (39 test files, 5,644 inserted lines) these three rules produce exactly ONE
# violation: car K's real relaxation in test_cls_store.py.  Zero false
# positives on the other 38 files.
# ---------------------------------------------------------------------------


# The REAL diff car K landed (`git diff origin/master...HEAD --
# yadgar/tests/backend/test_cls_store.py`, train/bug-bag-2-2026-08-23), reduced
# to the lines the guard reads.  This is the motivating case: if the widened
# guard cannot flag THIS, it is not done.
_CAR_K_REAL_DIFF = (
    "diff --git a/yadgar/tests/backend/test_cls_store.py"
    " b/yadgar/tests/backend/test_cls_store.py\n"
    "--- a/yadgar/tests/backend/test_cls_store.py\n"
    "+++ b/yadgar/tests/backend/test_cls_store.py\n"
    "@@ -281,9 +281,15 @@ class TestAbstractToSchema:\n"
    '         assert "jwt" in schema.lower()\n'
    " \n"
    "     def test_abstract_empty_cluster(self, cls):\n"
    '-        """Empty cluster should return empty string."""\n'
    '+        """Empty cluster returns falsy (None) — caller treats as no-op.\n'
    "+\n"
    "+        C7c (task #339): contract is None; ``promotion._promote_pattern``\n"
    "+        guards with ``if not schema:`` so empty string and None both signal\n"
    "+        skip.\n"
    '+        """\n'
    "         schema = cls.abstract_to_schema([])\n"
    '-        assert schema == ""\n'
    "+        assert not schema\n"
)

_CLS_STORE = "yadgar/tests/backend/test_cls_store.py"


class TestLayer4CoversNonE2ETestFiles:
    """The scope defect itself: a non-e2e test file must be in the scan set."""

    def test_scan_scope_matches_a_non_e2e_test_module(self) -> None:
        assert ctw._TEST_PATH_RE.search(_CLS_STORE), (
            "layer 4 must scan every yadgar/tests/**/*.py — scoping it to e2e is "
            "what let PR #68's 29 changed test files pass unexamined"
        )

    def test_scan_scope_still_matches_every_e2e_path(self) -> None:
        """Widening must not drop the files the old scope did cover."""
        for path in ("yadgar/tests/e2e/test_x.py", "yadgar/tests/core/test_y_e2e.py"):
            assert ctw._TEST_PATH_RE.search(path), path

    def test_non_test_sources_are_still_out_of_scope(self) -> None:
        """Production code removing an `assert` is not test weakening."""
        diff = (
            "diff --git a/yadgar/core/storage.py b/yadgar/core/storage.py\n"
            "-        assert rows\n"
            "+        pass\n"
        )
        assert not ctw.check_diff(diff, 5, 5), "only yadgar/tests/ is in scope"

    def test_e2e_arm_is_unchanged(self) -> None:
        """The pre-existing e2e behaviour must survive the widening verbatim."""
        diff = _diff_for("yadgar/tests/e2e/test_other.py", removed=1)
        assert ctw.check_diff(diff, 5, 5, {}), "the e2e net-assert rule must still fire"


class TestLayer4WeakeningShapes:
    """The three shapes a net-assert-count rule cannot see."""

    def test_car_k_real_diff_is_flagged(self) -> None:
        """THE motivating case — the real relaxation this task was filed for.

        Net assert delta is ZERO (one `assert` line removed, one added), so the
        pre-existing rule is structurally blind to it. The exact-value rule is
        what sees it.
        """
        errors = ctw.check_diff(_CAR_K_REAL_DIFF, 5, 5, {})
        assert errors, "the widened guard must flag car K's real relaxation"
        joined = " ".join(errors)
        assert _CLS_STORE in joined, errors
        assert "exact-value" in joined, errors

    def test_car_k_real_diff_has_zero_net_assert_delta(self) -> None:
        """Proves WHY the old rule could not have caught it, rather than asserting it."""
        metrics = ctw._per_file_metrics(_CAR_K_REAL_DIFF)
        assert metrics[_CLS_STORE].asserts == 0, (
            "fixture guard: if this diff had a negative net assert delta the "
            "exact-value rule would not be the thing catching it"
        )

    def test_equality_relaxed_to_truthiness_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            "-        assert count == 3\n"
            "+        assert count\n"
        )
        assert ctw.check_diff(diff, 5, 5, {}), "`== N` relaxed to truthiness is a weakening"

    def test_assertequal_relaxed_to_assertin_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            "-        self.assertEqual(body, expected)\n"
            "+        self.assertIn(expected, body)\n"
        )
        assert ctw.check_diff(diff, 5, 5, {}), "assertEqual -> assertIn is a weakening"

    def test_exact_value_swapped_for_another_exact_value_passes(self) -> None:
        """A CONTRACT change is not a weakening — `== ""` to `is None` stays exact.

        This is the discriminator that keeps the rule usable: car K's sibling
        edit in test_patterns_unit.py made exactly this swap on the same
        function, and flagging it would have been a false positive.
        """
        diff = (
            "diff --git a/yadgar/tests/backend/test_patterns_unit.py"
            " b/yadgar/tests/backend/test_patterns_unit.py\n"
            '-        assert mixin.abstract_to_schema([]) == ""\n'
            "+        assert mixin.abstract_to_schema([]) is None\n"
        )
        assert not ctw.check_diff(diff, 5, 5, {}), "exact -> exact is a contract change"

    def test_assert_deleted_from_non_e2e_module_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            "     def test_thing(self):\n"
            "         result = run()\n"
            "-        assert result.ok\n"
        )
        errors = ctw.check_diff(diff, 5, 5, {})
        assert errors, "a deleted assertion in a non-e2e test must fire"
        assert "assert" in " ".join(errors)

    def test_new_skip_in_existing_module_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            "     def test_thing(self):\n"
            '+        pytest.skip("flaky on CI")\n'
        )
        errors = ctw.check_diff(diff, 5, 5, {})
        assert errors, "a new pytest.skip in an existing test module must fire"
        assert "skip" in " ".join(errors).lower(), errors

    def test_new_xfail_marker_in_existing_module_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            "+    @pytest.mark.xfail(reason='broken')\n"
            "     def test_thing(self):\n"
        )
        assert ctw.check_diff(diff, 5, 5, {}), "a new xfail marker must fire"

    def test_new_importorskip_in_existing_module_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            '+pytest.importorskip("sqlalchemy")\n'
        )
        errors = ctw.check_diff(diff, 5, 5, {})
        assert errors, "silencing a whole module with importorskip must fire"

    def test_a_brand_new_file_may_carry_a_skip_guard(self) -> None:
        """`new file mode` — nothing was weakened; there was nothing there before."""
        diff = (
            "diff --git a/yadgar/tests/core/test_new.py b/yadgar/tests/core/test_new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/yadgar/tests/core/test_new.py\n"
            '+pytest.importorskip("sqlalchemy")\n'
            "+def test_thing():\n"
            "+    assert True\n"
        )
        assert not ctw.check_diff(diff, 5, 5, {}), "a new file introduces no weakening"

    def test_a_removed_skip_is_never_a_violation(self) -> None:
        """Deleting a skip guard STRENGTHENS the suite — must never fire."""
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            '-        pytest.skip("flaky")\n'
            "+        assert result == 3\n"
        )
        assert not ctw.check_diff(diff, 5, 5, {}), "removing a skip is not a weakening"

    def test_a_pure_rename_is_not_a_weakening(self) -> None:
        """A 100%-similar rename has no +/- lines; a partial one must not blame moves."""
        diff = (
            "diff --git a/yadgar/tests/core/test_old.py b/yadgar/tests/integration/test_old.py\n"
            "similarity index 96%\n"
            "rename from yadgar/tests/core/test_old.py\n"
            "rename to yadgar/tests/integration/test_old.py\n"
            "+pytestmark = [pytest.mark.integration]\n"
        )
        assert not ctw.check_diff(diff, 5, 5, {}), "a rename is not a weakening"


class TestLayer4NonE2EAllowlist:
    """The new shapes route through the SAME per-entry allowlist, same rules."""

    _STRICT_REASON = (
        "Sanctioned by car R of train/bug-bag-2 — the exact-value assertion was "
        "replaced deliberately and the reason is recorded in the diff."
    )

    def test_strict_removal_can_be_sanctioned(self) -> None:
        allowlist = {_CLS_STORE: {"allowed_strict_delta": -1, "rationale": self._STRICT_REASON}}
        assert not ctw.check_diff(_CAR_K_REAL_DIFF, 5, 5, allowlist)

    def test_a_strict_entry_does_not_absorb_further_weakening(self) -> None:
        """Same no-grandfathering rule as `allowed_delta` — exact, or it fails."""
        diff = _CAR_K_REAL_DIFF + (
            "diff --git a/yadgar/tests/backend/test_cls_store.py"
            " b/yadgar/tests/backend/test_cls_store.py\n"
            "-        assert other == 7\n"
            "+        assert other\n"
        )
        allowlist = {_CLS_STORE: {"allowed_strict_delta": -1, "rationale": self._STRICT_REASON}}
        errors = ctw.check_diff(diff, 5, 5, allowlist)
        assert errors, "an entry grants exactly its recorded delta, never more"

    def test_a_strict_entry_does_not_also_sanction_an_assert_deletion(self) -> None:
        """The allowances are per-SHAPE — one is not a licence for another."""
        diff = _CAR_K_REAL_DIFF + (
            "diff --git a/yadgar/tests/backend/test_cls_store.py"
            " b/yadgar/tests/backend/test_cls_store.py\n"
            "-        assert something_else\n"
        )
        allowlist = {_CLS_STORE: {"allowed_strict_delta": -1, "rationale": self._STRICT_REASON}}
        errors = ctw.check_diff(diff, 5, 5, allowlist)
        assert errors, "allowed_strict_delta must not absorb a plain assert deletion"

    def test_entry_with_no_allowance_key_is_malformed(self) -> None:
        allowlist = {_CLS_STORE: {"rationale": self._STRICT_REASON}}
        errors = ctw.check_diff(_CAR_K_REAL_DIFF, 5, 5, allowlist)
        assert errors, "an entry that grants nothing is a typo, not a sanction"
        assert any("MALFORMED" in e for e in errors), errors

    def test_positive_strict_allowance_is_malformed(self) -> None:
        allowlist = {_CLS_STORE: {"allowed_strict_delta": 1, "rationale": self._STRICT_REASON}}
        assert any("MALFORMED" in e for e in ctw.check_diff("", None, None, allowlist))

    def test_negative_skip_allowance_is_malformed(self) -> None:
        allowlist = {_CLS_STORE: {"allowed_new_skips": -1, "rationale": self._STRICT_REASON}}
        assert any("MALFORMED" in e for e in ctw.check_diff("", None, None, allowlist))

    def test_stale_warning_surfaces_a_non_e2e_entry(self) -> None:
        """The stale scan must see the new metrics too, or every non-e2e entry
        would be reported 'not in the branch diff' the moment it was added."""
        allowlist = {_CLS_STORE: {"allowed_strict_delta": -1, "rationale": self._STRICT_REASON}}
        assert not ctw.stale_allowlist_entries(_CAR_K_REAL_DIFF, allowlist), (
            "an entry that exactly describes the diff is not stale"
        )
        assert ctw.stale_allowlist_entries("", allowlist), "absent from the diff IS stale"


# A relaxation this file OWNS: a synthetic path that no car ever ships, so the
# property below cannot go stale the way a shipped-file pin does (see the class
# docstring).  Exact-value `== 3` relaxed to truthiness — net assert delta zero,
# net strict delta -1.
_SYNTHETIC_PATH = "yadgar/tests/core/test_synthetic_subject.py"
_SYNTHETIC_RELAXATION = (
    f"diff --git a/{_SYNTHETIC_PATH} b/{_SYNTHETIC_PATH}\n"
    "-        assert rows == 3\n"
    "+        assert rows\n"
)
_SYNTHETIC_REASON = (
    "Synthetic fixture rationale, written long enough to clear the minimum-length "
    "governance rule every allowlist entry must satisfy."
)


class TestLayer4SanctionIsRecordedNotScannedAway:
    """A real relaxation is RECORDED in the allowlist — never scanned away.

    Car R shipped this property pinned to one real file and its shipped
    allowlist entry.  Car O then reverted that relaxation to something strictly
    stronger, the guard reported the entry STALE on its own ("measured strict +0
    is better than the allowed -1 — tighten or remove"), the entry was removed as
    ``_stale_policy`` directs, and the pinned test went red for doing its job.
    A property stated against a shipped file has a shelf life; the same property
    stated against a synthetic diff does not.  Same arms, no pin.
    """

    def test_the_relaxation_is_flagged(self) -> None:
        errors = ctw.check_diff(_SYNTHETIC_RELAXATION, 5, 5, {})
        assert errors, "an exact-value assertion relaxed to truthiness must fire"
        assert _SYNTHETIC_PATH in " ".join(errors), errors
        assert "exact-value" in " ".join(errors), errors

    def test_an_allowlist_entry_silences_it(self) -> None:
        """The sanctioned route: written down, in the repo, in the reviewed diff."""
        allowlist = {_SYNTHETIC_PATH: {"allowed_strict_delta": -1, "rationale": _SYNTHETIC_REASON}}
        assert ctw.check_diff(_SYNTHETIC_RELAXATION, 5, 5, allowlist) == []

    def test_the_shipped_scope_does_not_let_it_slip(self) -> None:
        """Re-narrowing the scan is the OTHER way to make a violation disappear.

        The subject is invisible to layer 3's e2e declaration, so the
        pre-task-379 scope would have passed it in silence with nothing recorded
        anywhere.  Layer 4's own scope must still see it.
        """
        assert not ctw._E2E_PATH_RE.search(_SYNTHETIC_PATH), (
            "fixture guard: a path the narrow scan CAN see would prove nothing here"
        )
        assert ctw._TEST_PATH_RE.search(_SYNTHETIC_PATH), (
            "layer 4 scans every yadgar/tests/**/*.py — narrowing it back to e2e is "
            "how a relaxation disappears without a sanction being written down"
        )
        assert ctw.check_diff(_SYNTHETIC_RELAXATION, 5, 5, {}), (
            "the shipped scope must flag a non-e2e relaxation"
        )

    def test_narrowing_the_scan_is_what_would_hide_it(self, monkeypatch) -> None:
        """The hazard itself, executed rather than asserted about.

        Shrunk to the e2e declaration, the SAME diff passes clean with no entry
        and no reason recorded.  This is the move the allowlist exists to make
        unnecessary, and ``TestLayer3Layer4ScopeLockstep`` is what keeps the two
        scopes from being quietly collapsed into one.
        """
        monkeypatch.setattr(ctw, "_TEST_PATH_RE", ctw._E2E_PATH_RE)

        assert ctw.check_diff(_SYNTHETIC_RELAXATION, 5, 5, {}) == [], (
            "fixture guard: if the narrowed scan still fired, the demonstration "
            "would not be showing the hazard it claims to"
        )


class TestLayer4ShippedAllowlistIsWellformed:
    """Whatever the shipped allowlist currently records, it must parse and pass.

    Deliberately names no file: entries come and go with every train (see the
    class above), and a test pinning one goes red the moment its car merges.
    What must hold on every commit is that the file the guard loads is not
    malformed.
    """

    def test_the_shipped_allowlist_is_still_wellformed(self) -> None:
        allowlist = ctw.load_allowlist(_REPO_ROOT / ctw._ALLOWLIST_NAME)
        errors = ctw.check_diff("", head_green=None, staged_green=None, allowlist=allowlist)
        assert not errors, errors


class TestLayer4SkipRuleIsAnchored:
    """A skip guard is a STATEMENT, not a substring — quoting one must not fire.

    Found by running the widened guard over its own commit: the diff fixtures in
    this very file contain lines like ``"+        pytest.skip(...)\\n"`` inside
    string literals, and the unanchored pattern counted all five as newly added
    skip guards. Any meta-test, doc example, or error message that QUOTES a skip
    would trip the same way, so the fix is the pattern, not an exemption for
    this file.
    """

    def test_a_quoted_skip_inside_a_string_literal_does_not_fire(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_meta.py b/yadgar/tests/core/test_meta.py\n"
            "+            '+        pytest.skip(\"flaky on CI\")\\n'\n"
            "+            '+pytest.importorskip(\"sqlalchemy\")\\n'\n"
        )
        assert not ctw.check_diff(diff, 5, 5, {}), (
            "a skip call quoted inside a fixture string is not a new skip guard"
        )

    def test_a_real_indented_skip_still_fires(self) -> None:
        """Anchoring must not cost detection of the real thing."""
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            '+        pytest.skip("flaky on CI")\n'
        )
        assert ctw.check_diff(diff, 5, 5, {}), "an indented pytest.skip is still a skip"

    def test_a_module_level_pytestmark_skip_fires(self) -> None:
        diff = (
            "diff --git a/yadgar/tests/core/test_x.py b/yadgar/tests/core/test_x.py\n"
            '+pytestmark = pytest.mark.skip(reason="later")\n'
        )
        assert ctw.check_diff(diff, 5, 5, {}), "a module-wide pytestmark skip must fire"

    def test_every_skip_match_in_this_module_is_inside_a_string_literal(self) -> None:
        """The regression in situ, checked precisely rather than by proxy.

        This module is dense with skip markers that are FIXTURE TEXT — layer 3's
        tests build sample sources with ``textwrap.dedent`` and layer 4's build
        diff bodies, so ``@pytest.mark.xfail(...)`` appears at what looks like
        statement position on a dozen lines. None of them silence anything.

        So: parse the module, collect the line spans of every string literal,
        and assert every ``_SKIP_RE`` hit falls inside one. Hermetic (no git, no
        merge-base, cannot pass vacuously) and it still fails loudly the day a
        REAL skip marker is added to this file — which is the outcome the rule
        exists to make visible.

        Deliberately written without a ``pytest.skip`` fallback: a skip
        statement here would itself be the thing the rule counts.
        """
        rel = "yadgar/tests/core/test_tamper_guards.py"
        source = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)

        literal_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                end = node.end_lineno or node.lineno
                literal_lines.update(range(node.lineno, end + 1))

        hits = [
            (n, line) for n, line in enumerate(source.splitlines(), 1) if ctw._SKIP_RE.search(line)
        ]
        assert hits, (
            "fixture guard: this module contains skip markers in its fixtures, so "
            "zero regex hits means _SKIP_RE stopped matching and every assertion "
            "below would pass vacuously"
        )
        outside = [(n, line.strip()) for n, line in hits if n not in literal_lines]
        assert not outside, (
            f"{outside} are real skip markers in the guard's own meta-test module, "
            "not fixture text — either the anchor regressed or a test was silenced"
        )
