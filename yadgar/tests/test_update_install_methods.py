"""Tests for yadgar/update/install_methods.py — v5.49.9 wave 4 coverage.

Module: yadgar.update.install_methods
Target: ≥80% line coverage

Strategy:
- patch subprocess.check_output to control `which yadgar` output.
- Use tmp_path for real file paths when testing _is_docker_shim / _has_git_ancestor.
- importlib.reload after each mock to reset module-level state (matches style in
  test_update_check.py which covers the same module).
- upgrade_command() and can_self_install() are pure functions — no mocking needed.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

# ---------------------------------------------------------------------------
# detect_install_method
# ---------------------------------------------------------------------------


class TestDetectInstallMethod:
    """Unit tests for yadgar.update.install_methods.detect_install_method."""

    def test_detects_pipx(self, tmp_path):
        """Returns 'pipx' when yadgar resolves to pipx venv path."""
        fake_bin = tmp_path / "home" / ".local" / "pipx" / "venvs" / "yadgar" / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")

        with patch("subprocess.check_output", return_value=str(fake_bin)):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "pipx"

    def test_detects_brew(self, tmp_path):
        """Returns 'brew' when yadgar resolves to Cellar path."""
        fake_bin = tmp_path / "opt" / "homebrew" / "Cellar" / "yadgar" / "5.47.0" / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")

        with patch("subprocess.check_output", return_value=str(fake_bin)):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "brew"

    def test_detects_nix_flake(self):
        """Returns 'nix-flake' when yadgar resolves to /nix/store path."""
        with patch(
            "subprocess.check_output",
            return_value="/nix/store/abc123-yadgar-5.47/bin/yadgar",
        ):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "nix-flake"

    def test_detects_container(self, tmp_path):
        """Returns 'container' when yadgar is a docker-run shim."""
        fake_shim = tmp_path / "yadgar"
        fake_shim.write_text('#!/bin/sh\ndocker run --rm openfantasy/yadgar:latest "$@"\n')
        fake_shim.chmod(0o755)

        with patch("subprocess.check_output", return_value=str(fake_shim)):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "container"

    def test_detects_source(self, tmp_path):
        """Returns 'source' when yadgar binary lives in a git repo ancestor."""
        git_dir = tmp_path / "git" / "yadgar" / ".git"
        git_dir.mkdir(parents=True)
        fake_bin = tmp_path / "git" / "yadgar" / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")

        with patch("subprocess.check_output", return_value=str(fake_bin)):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "source"

    def test_returns_not_installed_on_called_process_error(self):
        """Returns 'not_installed' when which yadgar exits non-zero."""
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "which"),
        ):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "not_installed"

    def test_returns_not_installed_on_file_not_found(self):
        """Returns 'not_installed' when 'which' is not available (FileNotFoundError)."""
        with patch(
            "subprocess.check_output",
            side_effect=FileNotFoundError("which not found"),
        ):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "not_installed"

    def test_returns_not_installed_for_empty_output(self):
        """Returns 'not_installed' when which returns empty string."""
        with patch("subprocess.check_output", return_value=""):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "not_installed"

    def test_returns_unknown_for_unrecognized_path(self, tmp_path):
        """Returns 'unknown' when path matches no known pattern and no git ancestor."""
        fake_bin = tmp_path / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")  # not a docker shim

        with patch("subprocess.check_output", return_value=str(fake_bin)):
            import importlib

            from yadgar.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "unknown"


# ---------------------------------------------------------------------------
# _is_docker_shim
# ---------------------------------------------------------------------------


class TestIsDockerShim:
    def test_true_for_docker_run_shim(self, tmp_path):
        """Returns True when file contains 'docker run'."""
        from yadgar.update.install_methods import _is_docker_shim

        shim = tmp_path / "yadgar"
        shim.write_text('#!/bin/sh\ndocker run --rm openfantasy/yadgar "$@"\n')
        assert _is_docker_shim(str(shim)) is True

    def test_false_for_normal_script(self, tmp_path):
        """Returns False when file doesn't contain 'docker run'."""
        from yadgar.update.install_methods import _is_docker_shim

        script = tmp_path / "yadgar"
        script.write_text("#!/usr/bin/env python3\nimport sys\n")
        assert _is_docker_shim(str(script)) is False

    def test_false_for_missing_file(self):
        """Returns False when file doesn't exist (OSError)."""
        from yadgar.update.install_methods import _is_docker_shim

        assert _is_docker_shim("/nonexistent/path/yadgar") is False


# ---------------------------------------------------------------------------
# _has_git_ancestor
# ---------------------------------------------------------------------------


class TestHasGitAncestor:
    def test_true_when_git_in_ancestor(self, tmp_path):
        """Returns True when a .git dir exists in an ancestor."""
        from yadgar.update.install_methods import _has_git_ancestor

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        file_path = tmp_path / "bin" / "yadgar"
        file_path.parent.mkdir()
        file_path.write_text("#!/usr/bin/env python3\n")

        assert _has_git_ancestor(str(file_path)) is True

    def test_false_when_no_git_ancestor(self, tmp_path):
        """Returns False when no .git dir in any ancestor."""
        from yadgar.update.install_methods import _has_git_ancestor

        file_path = tmp_path / "bin" / "yadgar"
        file_path.parent.mkdir()
        file_path.write_text("#!/usr/bin/env python3\n")

        # Can't guarantee tmp_path has no .git ancestor in general,
        # but tmp_path is under /tmp which never has .git
        assert _has_git_ancestor(str(file_path)) is False


# ---------------------------------------------------------------------------
# upgrade_command
# ---------------------------------------------------------------------------


class TestUpgradeCommand:
    def test_pipx(self):
        from yadgar.update.install_methods import upgrade_command

        assert upgrade_command("pipx") == "pipx upgrade yadgar"

    def test_brew(self):
        from yadgar.update.install_methods import upgrade_command

        assert upgrade_command("brew") == "brew upgrade yadgar"

    def test_nix_flake_contains_nix(self):
        from yadgar.update.install_methods import upgrade_command

        assert "nix" in upgrade_command("nix-flake").lower()

    def test_container_contains_docker_pull(self):
        from yadgar.update.install_methods import upgrade_command

        assert "docker pull" in upgrade_command("container")

    def test_source_contains_git_pull_or_pip(self):
        from yadgar.update.install_methods import upgrade_command

        cmd = upgrade_command("source")
        assert "git pull" in cmd or "pip install" in cmd

    def test_unknown_fallback(self):
        from yadgar.update.install_methods import upgrade_command

        cmd = upgrade_command("unknown")
        assert "pypi.org" in cmd or "manual" in cmd.lower() or "Cannot determine" in cmd

    def test_not_installed_fallback(self):
        from yadgar.update.install_methods import upgrade_command

        cmd = upgrade_command("not_installed")
        assert cmd  # non-empty string

    def test_arbitrary_method_fallback(self):
        from yadgar.update.install_methods import upgrade_command

        cmd = upgrade_command("foobar")
        assert isinstance(cmd, str) and len(cmd) > 0


# ---------------------------------------------------------------------------
# can_self_install
# ---------------------------------------------------------------------------


class TestCanSelfInstall:
    def test_pipx_true(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("pipx") is True

    def test_source_true(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("source") is True

    def test_brew_false(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("brew") is False

    def test_nix_flake_false(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("nix-flake") is False

    def test_container_false(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("container") is False

    def test_unknown_false(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("unknown") is False

    def test_not_installed_false(self):
        from yadgar.update.install_methods import can_self_install

        assert can_self_install("not_installed") is False
