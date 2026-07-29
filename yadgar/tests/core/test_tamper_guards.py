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

    def test_allow_env_still_bypasses(self, monkeypatch) -> None:
        monkeypatch.setenv("ALLOW_TEST_WEAKEN", "1")
        assert ctw.main() == 0, "documented one-time override must keep working"

    def test_ci_mode_uses_branch_diff(self, monkeypatch) -> None:
        """`--ci --base <ref>` runs the same collector — same inputs, same verdict."""
        git = _FakeGit(_responses(branch_diff=_BRANCH_DIFF_WEAKENED))
        monkeypatch.setattr(ctw, "_git", git)
        monkeypatch.delenv("ALLOW_TEST_WEAKEN", raising=False)
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
