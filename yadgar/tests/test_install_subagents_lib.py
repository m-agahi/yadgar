"""Tests for yadgar/install_subagents_lib.py — v5.49.9 wave 4 coverage.

Module: yadgar.install_subagents_lib
Target: ≥80% line coverage

Strategy:
- Always patch yadgar.platform_paths.is_nix_managed to control nix_managed path.
- Use tmp_path for home_dir parameter; create fake bundled agents dir by patching
  _get_bundled_agents_dir() to point to a tmp dir with .md files.
- Test all status branches: nix_managed, error (no bundled dir), error (no .md files),
  check, dry_run, no_changes, installed, installed with force.

Floor: None — all reachable branches are testable.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bundled_dir(tmp_path: Path, files: list[str] | None = None) -> Path:
    """Create a fake bundled agents dir with .md files."""
    bundled = tmp_path / "bundled_agents"
    bundled.mkdir()
    if files is None:
        files = ["agent1.md", "agent2.md"]
    for name in files:
        (bundled / name).write_text(f"# {name}\nThis is {name}\n")
    return bundled


# ---------------------------------------------------------------------------
# _get_bundled_agents_dir
# ---------------------------------------------------------------------------


class TestGetBundledAgentsDir:
    def test_returns_path_inside_package(self):
        """_get_bundled_agents_dir returns a Path inside the yadgar package."""
        from yadgar.install_subagents_lib import _get_bundled_agents_dir

        result = _get_bundled_agents_dir()
        assert isinstance(result, Path)
        assert "install_assets" in str(result)
        assert "agents" in str(result)


# ---------------------------------------------------------------------------
# install_subagents_impl — nix_managed early return
# ---------------------------------------------------------------------------


class TestInstallSubagentsNixManaged:
    def test_nix_managed_returns_nix_managed_status(self, tmp_path):
        """When is_nix_managed() is True, returns nix_managed status."""
        with patch("yadgar.platform_paths.is_nix_managed", return_value=True):
            from yadgar.install_subagents_lib import install_subagents_impl

            result = install_subagents_impl(home_dir=tmp_path)

        assert result["status"] == "nix_managed"

    def test_nix_managed_contains_message(self, tmp_path):
        """nix_managed result contains a message key."""
        with patch("yadgar.platform_paths.is_nix_managed", return_value=True):
            from yadgar.install_subagents_lib import install_subagents_impl

            result = install_subagents_impl(home_dir=tmp_path)

        assert "message" in result
        assert len(result["message"]) > 0


# ---------------------------------------------------------------------------
# install_subagents_impl — error paths
# ---------------------------------------------------------------------------


class TestInstallSubagentsErrors:
    def test_error_when_bundled_dir_missing(self, tmp_path):
        """Returns error status when bundled agents dir doesn't exist."""
        nonexistent = tmp_path / "does_not_exist"

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=nonexistent,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path)

        assert result["status"] == "error"
        assert "reason" in result

    def test_error_when_no_md_files(self, tmp_path):
        """Returns error status when bundled dir exists but has no .md files."""
        bundled = tmp_path / "empty_bundled"
        bundled.mkdir()
        bundled_non_md = bundled / "readme.txt"
        bundled_non_md.write_text("not an agent\n")

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path)

        assert result["status"] == "error"
        assert "reason" in result


# ---------------------------------------------------------------------------
# install_subagents_impl — check mode
# ---------------------------------------------------------------------------


class TestInstallSubagentsCheck:
    def test_check_returns_check_status(self, tmp_path):
        """check=True returns status='check'."""
        bundled = _make_bundled_dir(tmp_path)

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, check=True)

        assert result["status"] == "check"

    def test_check_returns_would_install_list(self, tmp_path):
        """check=True returns would_install list with new files."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md", "agent2.md"])

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, check=True)

        assert set(result["would_install"]) == {"agent1.md", "agent2.md"}

    def test_check_returns_agents_dir(self, tmp_path):
        """check=True result includes agents_dir path."""
        bundled = _make_bundled_dir(tmp_path)

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, check=True)

        assert "agents_dir" in result
        assert ".claude/agents" in result["agents_dir"]

    def test_check_does_not_write_files(self, tmp_path):
        """check=True does not actually create any files."""
        bundled = _make_bundled_dir(tmp_path)
        agents_dir = tmp_path / ".claude" / "agents"

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                install_subagents_impl(home_dir=tmp_path, check=True)

        assert not agents_dir.exists()

    def test_check_no_changes_when_files_exist(self, tmp_path):
        """check=True would_install is empty when all files already exist."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent1.md").write_text("existing content\n")

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, check=True)

        assert result["would_install"] == []


# ---------------------------------------------------------------------------
# install_subagents_impl — dry_run mode
# ---------------------------------------------------------------------------


class TestInstallSubagentsDryRun:
    def test_dry_run_returns_dry_run_status(self, tmp_path, capsys):
        """dry_run=True returns status='dry_run'."""
        bundled = _make_bundled_dir(tmp_path)

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, dry_run=True)

        assert result["status"] == "dry_run"

    def test_dry_run_prints_preview(self, tmp_path, capsys):
        """dry_run=True prints file names to stdout."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                install_subagents_impl(home_dir=tmp_path, dry_run=True)

        out = capsys.readouterr().out
        assert "agent1.md" in out or "dry-run" in out.lower() or "Would install" in out

    def test_dry_run_does_not_write_files(self, tmp_path, capsys):
        """dry_run=True does not create any files."""
        bundled = _make_bundled_dir(tmp_path)
        agents_dir = tmp_path / ".claude" / "agents"

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                install_subagents_impl(home_dir=tmp_path, dry_run=True)

        assert not agents_dir.exists()

    def test_dry_run_no_changes_prints_no_changes(self, tmp_path, capsys):
        """dry_run=True with all files existing prints no changes."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent1.md").write_text("existing\n")

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, dry_run=True)

        out = capsys.readouterr().out
        assert "no changes" in out.lower() or result["would_install"] == []


# ---------------------------------------------------------------------------
# install_subagents_impl — no_changes path
# ---------------------------------------------------------------------------


class TestInstallSubagentsNoChanges:
    def test_no_changes_when_all_exist(self, tmp_path):
        """Returns 'no_changes' when all files already exist and force=False."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md", "agent2.md"])
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        for name in ["agent1.md", "agent2.md"]:
            (agents_dir / name).write_text("existing\n")

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path)

        assert result["status"] == "no_changes"
        assert result["installed"] == []


# ---------------------------------------------------------------------------
# install_subagents_impl — installed path
# ---------------------------------------------------------------------------


class TestInstallSubagentsInstalled:
    def test_install_new_files(self, tmp_path):
        """Fresh install: all files copied, status='installed'."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md", "agent2.md"])

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path)

        assert result["status"] == "installed"
        assert set(result["installed"]) == {"agent1.md", "agent2.md"}

    def test_install_creates_agents_dir(self, tmp_path):
        """agents_dir is created if it doesn't exist."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])
        agents_dir = tmp_path / ".claude" / "agents"

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                install_subagents_impl(home_dir=tmp_path)

        assert agents_dir.exists()

    def test_install_copies_file_contents(self, tmp_path):
        """Installed files have the same content as bundled sources."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])
        src_content = (bundled / "agent1.md").read_text()

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                install_subagents_impl(home_dir=tmp_path)

        dst = tmp_path / ".claude" / "agents" / "agent1.md"
        assert dst.read_text() == src_content

    def test_install_returns_agents_dir_path(self, tmp_path):
        """Result includes agents_dir path."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path)

        assert ".claude/agents" in result["agents_dir"]

    def test_force_overwrites_existing_files(self, tmp_path):
        """force=True overwrites existing files."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md"])
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent1.md").write_text("old content\n")

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path, force=True)

        assert result["status"] == "installed"
        assert "agent1.md" in result["installed"]
        # File was overwritten with bundled content
        new_content = (agents_dir / "agent1.md").read_text()
        assert new_content != "old content\n"

    def test_partial_install_only_new_files(self, tmp_path):
        """Only missing files are installed when some already exist."""
        bundled = _make_bundled_dir(tmp_path, files=["agent1.md", "agent2.md"])
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent1.md").write_text("existing\n")

        with patch("yadgar.platform_paths.is_nix_managed", return_value=False):
            with patch(
                "yadgar.install_subagents_lib._get_bundled_agents_dir",
                return_value=bundled,
            ):
                from yadgar.install_subagents_lib import install_subagents_impl

                result = install_subagents_impl(home_dir=tmp_path)

        assert result["status"] == "installed"
        assert result["installed"] == ["agent2.md"]
