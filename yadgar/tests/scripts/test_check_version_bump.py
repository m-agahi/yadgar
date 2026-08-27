"""Unit tests for scripts/check_version_bump.py.

The gate answers ONE question: does this branch bump ``pyproject.toml``'s
version relative to the commit it forked from?

It used to answer "does the version differ from the latest git tag?", and
``TestMergeBaseRegression`` below is the measured scenario that question got
wrong — a branch that bumped, then had a master merge resolve the version
back to the base value, while the latest tag lagged both. The tag comparison
returned OK on exactly those inputs; the merge-base comparison fails.

Scenarios pinned here:
  (a) version unchanged since the merge-base + yadgar/** touched -> fail
  (b) version bumped vs the merge-base -> pass
  (c) yadgar/** NOT touched -> pass regardless of version
  (d) merge-base unresolvable -> FAIL LOUD (exit 1), never a tag fallback
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the hook from scripts/ — not a package, use direct path injection.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import check_version_bump as cvb  # noqa: E402
from check_version_bump import (  # noqa: E402
    check,
    get_base_version,
    get_yadgar_diff,
    read_pyproject_version,
)

# ---------------------------------------------------------------------------
# check() — pure decision logic
# ---------------------------------------------------------------------------


class TestVersionUnchangedSinceMergeBase:
    def test_unchanged_version_with_yadgar_files_fails(self) -> None:
        """The branch changed core code but carries the fork point's version."""
        ok, msg = check(
            pyproject_version="5.190.2",
            base_version="5.190.2",
            yadgar_files=["yadgar/core/runtime_config_client.py"],
        )
        assert ok is False
        assert "5.190.2" in msg
        assert "bump_version.py" in msg

    def test_multiple_yadgar_files_still_fails(self) -> None:
        ok, msg = check(
            pyproject_version="5.190.2",
            base_version="5.190.2",
            yadgar_files=["yadgar/core/a.py", "yadgar/core/b.py"],
        )
        assert ok is False
        assert "2 yadgar/** file(s)" in msg


class TestVersionBumpedVsMergeBase:
    def test_version_ahead_of_base_passes(self) -> None:
        ok, msg = check(
            pyproject_version="5.190.3",
            base_version="5.190.2",
            yadgar_files=["yadgar/core/runtime_config_client.py"],
        )
        assert ok is True
        assert "5.190.2 -> 5.190.3" in msg

    def test_pyproject_absent_at_merge_base_passes(self) -> None:
        """Nothing to compare against -> unchanged-ness is unprovable -> pass."""
        ok, _msg = check(
            pyproject_version="0.1.0",
            base_version=None,
            yadgar_files=["yadgar/core/a.py"],
        )
        assert ok is True


class TestYadgarNotTouched:
    def test_unchanged_version_no_yadgar_files_passes(self) -> None:
        ok, msg = check(
            pyproject_version="5.190.2",
            base_version="5.190.2",
            yadgar_files=[],
        )
        assert ok is True
        assert "no yadgar/** changes" in msg


# ---------------------------------------------------------------------------
# THE regression — the exact measured state the tag comparison called OK
# ---------------------------------------------------------------------------


class TestMergeBaseRegression:
    """Reproduce the live state that kept the OLD gate green.

    Measured on train/identity-completion-2026-08-21 (task 382):

        merge-base (77e2d0df, = origin/master)   pyproject 5.190.2
        the branch's own bump (bc60f7ce)         pyproject 5.190.3
        after master merge 16c895b3 (650ba791)   pyproject 5.190.2  <- reverted
        latest tag                               v5.190.1

    ``v5.190.2 != v5.190.1`` -> the tag comparison passed. ~100 files of core
    change shipped under a release number master had already consumed.
    """

    @staticmethod
    def _run_git(args: list[str]) -> str:
        if args[:1] == ["merge-base"]:
            return "77e2d0df8c715b97fc7ca69813bd951368d630d7\n"
        if args[:1] == ["describe"]:
            # The tag the OLD gate compared against — deliberately still
            # available, to show it is no longer consulted.
            return "v5.190.1\n"
        if args[:1] == ["show"]:
            # pyproject.toml at the merge-base: the value the branch's bump
            # was reverted BACK to.
            return 'version = "5.190.2"\n'
        if args[:1] == ["diff"]:
            return "yadgar/core/server/tools/wiki.py\ndocs/CHANGELOG.md\n"
        raise AssertionError(f"unexpected git call: {args}")

    def test_reverted_bump_now_fails(self) -> None:
        """HEAD == merge-base version, yadgar/** changed -> FAIL.

        Under the tag comparison this exact input set returned OK.
        """
        run_git = self._run_git
        merge_base = run_git(["merge-base", "origin/master", "HEAD"]).strip()
        base_version = get_base_version(merge_base, run_git)
        yadgar_files = get_yadgar_diff(merge_base, run_git)
        head_version = "5.190.2"  # pyproject.toml on disk after the master merge

        assert base_version == "5.190.2"
        assert yadgar_files == ["yadgar/core/server/tools/wiki.py"]

        ok, msg = check(head_version, base_version, yadgar_files)
        assert ok is False, "a bump reverted by a master merge must not pass"
        assert "merge-base" in msg

    def test_old_tag_comparison_would_have_passed(self) -> None:
        """Pin WHY the old gate was green: version != latest tag."""
        latest_tag = self._run_git(["describe", "--tags", "--abbrev=0"]).strip()
        assert f"v{'5.190.2'}" != latest_tag

    def test_gate_no_longer_consults_the_tag(self) -> None:
        """No `git describe` call may reach git — the tag is out of the logic."""
        calls: list[list[str]] = []

        def spy(args: list[str]) -> str:
            calls.append(args)
            return TestMergeBaseRegression._run_git(args)

        merge_base = spy(["merge-base", "origin/master", "HEAD"]).strip()
        get_base_version(merge_base, spy)
        get_yadgar_diff(merge_base, spy)
        assert not any(a[:1] == ["describe"] for a in calls)
        assert not hasattr(cvb, "get_latest_tag")


# ---------------------------------------------------------------------------
# Input collection — injectable git runner
# ---------------------------------------------------------------------------


class TestGetBaseVersion:
    def test_parses_version_at_merge_base(self) -> None:
        def run_git(_args: list[str]) -> str:
            return '[project]\nname = "yadgar"\nversion = "5.190.2"\n'

        assert get_base_version("abc123", run_git) == "5.190.2"

    def test_missing_file_at_merge_base_returns_none(self) -> None:
        def run_git(_args: list[str]) -> str:
            return ""  # `git show <ref>:pyproject.toml` failed

        assert get_base_version("abc123", run_git) is None

    def test_unparseable_pyproject_returns_none(self) -> None:
        def run_git(_args: list[str]) -> str:
            return "[project]\nname = 'yadgar'\n"

        assert get_base_version("abc123", run_git) is None


class TestGetYadgarDiff:
    def test_filters_to_yadgar_paths(self) -> None:
        def run_git(_args: list[str]) -> str:
            return "yadgar/core/a.py\ndocs/x.md\nscripts/y.py\n"

        assert get_yadgar_diff("abc123", run_git) == ["yadgar/core/a.py"]

    def test_empty_diff(self) -> None:
        def run_git(_args: list[str]) -> str:
            return ""

        assert get_yadgar_diff("abc123", run_git) == []


class TestReadPyprojectVersion:
    def test_reads_version(self) -> None:
        assert read_pyproject_version('version = "1.2.3"\n') == "1.2.3"

    def test_returns_none_when_absent(self) -> None:
        assert read_pyproject_version('name = "yadgar"\n') is None


# ---------------------------------------------------------------------------
# (d) FAIL LOUD — unresolvable merge-base must not degrade to the old gate
# ---------------------------------------------------------------------------


class TestUnresolvableMergeBaseFailsLoud:
    def test_run_returns_1_when_merge_base_unresolvable(self, monkeypatch, capsys) -> None:
        """Shallow clone / unfetched remote: exit 1 with a remedy, not exit 0.

        The predecessor fail-OPEN here is what let the broken comparison keep
        shipping verdicts; a gate that degrades to the wrong question when it
        cannot ask the right one has not been fixed.
        """
        monkeypatch.setattr(cvb, "_git", lambda _args: "")
        rc = cvb.run("origin/master", "check-version-bump")
        assert rc == 1
        err = capsys.readouterr().err
        assert "merge-base" in err
        assert "fetch-depth: 0" in err
        assert "SKIP=verify-version-bump-local" in err

    def test_ci_mode_requires_base(self, capsys) -> None:
        assert cvb.main(["--ci"]) == 1
        assert "--base" in capsys.readouterr().err
