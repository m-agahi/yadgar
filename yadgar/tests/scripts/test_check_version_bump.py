"""Unit tests for scripts/check_version_bump.py.

Local pre-push mirror of CI's ``verify-version-bump`` job
(.github/workflows/ci-pr.yml). Same four scenarios the user hit today:
  (a) version matches latest tag + yadgar/** touched -> fail
  (b) version differs from latest tag -> pass
  (c) yadgar/** NOT touched (even if version matches tag) -> pass
  (d) origin/master unreachable -> fail-open pass, warning on stderr
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

from check_version_bump import (  # noqa: E402
    check,
    get_latest_tag,
    get_yadgar_diff,
)

# ---------------------------------------------------------------------------
# check() — pure decision logic
# ---------------------------------------------------------------------------


class TestVersionMatchesTagAndYadgarTouched:
    def test_matching_version_with_yadgar_files_fails(self) -> None:
        """THE exact failure the user hit: version == latest tag, yadgar/** staged."""
        ok, msg = check(
            pyproject_version="5.166.4",
            latest_tag="v5.166.4",
            yadgar_files=["yadgar/core/runtime_config_client.py"],
        )
        assert ok is False
        assert "5.166.4" in msg
        assert "bump_version.py" in msg

    def test_multiple_yadgar_files_still_fails(self) -> None:
        ok, _msg = check(
            pyproject_version="5.166.4",
            latest_tag="v5.166.4",
            yadgar_files=["yadgar/core/a.py", "yadgar/core/b.py"],
        )
        assert ok is False


class TestVersionDiffersFromTag:
    def test_version_ahead_of_tag_passes(self) -> None:
        """Already bumped -> pass, even with yadgar/** changes."""
        ok, msg = check(
            pyproject_version="5.166.5",
            latest_tag="v5.166.4",
            yadgar_files=["yadgar/core/runtime_config_client.py"],
        )
        assert ok is True
        assert "5.166.5" in msg

    def test_no_tags_yet_default_v0_passes(self) -> None:
        """Fresh repo with no tags -> latest_tag falls back to v0.0.0."""
        ok, _msg = check(
            pyproject_version="0.1.0",
            latest_tag="v0.0.0",
            yadgar_files=["yadgar/core/a.py"],
        )
        assert ok is True


class TestYadgarNotTouched:
    def test_matching_version_no_yadgar_files_passes(self) -> None:
        """Version matches tag but diff never touches yadgar/** -> pass."""
        ok, msg = check(
            pyproject_version="5.166.4",
            latest_tag="v5.166.4",
            yadgar_files=[],
        )
        assert ok is True
        assert "OK" in msg

    def test_matching_version_only_docs_changed_passes(self) -> None:
        ok, _msg = check(
            pyproject_version="5.166.4",
            latest_tag="v5.166.4",
            yadgar_files=[],
        )
        assert ok is True


# ---------------------------------------------------------------------------
# get_latest_tag — injectable git runner
# ---------------------------------------------------------------------------


class TestGetLatestTag:
    def test_returns_tag_on_success(self) -> None:
        def run_git(_args: list[str]) -> tuple[int, str]:
            return 0, "v5.166.4\n"

        assert get_latest_tag(run_git) == "v5.166.4"

    def test_falls_back_to_v0_0_0_on_nonzero_exit(self) -> None:
        """No tags in repo -> git describe fails -> fallback (mirrors CI's `|| echo v0.0.0`)."""

        def run_git(_args: list[str]) -> tuple[int, str]:
            return 128, ""

        assert get_latest_tag(run_git) == "v0.0.0"

    def test_falls_back_to_v0_0_0_on_empty_output(self) -> None:
        def run_git(_args: list[str]) -> tuple[int, str]:
            return 0, ""

        assert get_latest_tag(run_git) == "v0.0.0"


# ---------------------------------------------------------------------------
# get_yadgar_diff — injectable git runner, reachability signal
# ---------------------------------------------------------------------------


class TestGetYadgarDiff:
    def test_reachable_with_yadgar_files(self) -> None:
        def run_git(_args: list[str]) -> tuple[int, str]:
            return 0, "yadgar/core/a.py\ndocs/x.md\n"

        reachable, files = get_yadgar_diff(run_git)
        assert reachable is True
        assert files == ["yadgar/core/a.py"]

    def test_reachable_no_yadgar_files(self) -> None:
        def run_git(_args: list[str]) -> tuple[int, str]:
            return 0, "docs/x.md\npyproject.toml\n"

        reachable, files = get_yadgar_diff(run_git)
        assert reachable is True
        assert files == []

    def test_unreachable_origin_master(self) -> None:
        """origin/master ref doesn't exist locally (no remote / not fetched) ->
        unreachable, not a false pass/fail."""

        def run_git(_args: list[str]) -> tuple[int, str]:
            return 128, ""

        reachable, files = get_yadgar_diff(run_git)
        assert reachable is False
        assert files == []

    def test_empty_diff_is_reachable_not_unreachable(self) -> None:
        """No files changed at all is a valid (reachable) empty result, distinct
        from a git error — must not be conflated with the unreachable case."""

        def run_git(_args: list[str]) -> tuple[int, str]:
            return 0, ""

        reachable, files = get_yadgar_diff(run_git)
        assert reachable is True
        assert files == []
