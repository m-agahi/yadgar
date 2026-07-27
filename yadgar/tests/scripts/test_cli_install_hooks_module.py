"""Tests for yadgar/cli/install_hooks.py — the hard-removed stub (Car 7).

The legacy ``yadgar install-hooks`` CLI was hard-removed in Car 7 of the
opencode port train (v5.166.0). The directory
``yadgar/core/cli/install_hooks.py`` remains as a stub so the legacy
``register(subparsers)`` call site in ``yadgar/core/cli/__init__.py``
still imports cleanly; ``cmd_install_hooks`` prints a migration message
and exits 1 when invoked. The single canonical path is now
``yadgar install --client <name> [--hooks | --no-hooks] [--scope ...]``.

These tests pin the hard-remove contract:
  * The legacy CLI exits 1 with a migration message on every flag combination
  * The migration message includes the new canonical command for every scope
  * The argparse ``register`` still accepts the legacy flags (so old scripts
    don't get a confusing argparse error BEFORE the migration message fires)
  * No settings.json side effect on host
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace


def _make_args(**kwargs) -> SimpleNamespace:
    """Build a SimpleNamespace matching the legacy argparse namespace."""
    defaults = {"scope": "global", "project_directory": "", "dry_run": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# register() — parser wiring (still works; legacy flags accepted)
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_install_hooks_subparser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        from yadgar.core.cli.install_hooks import register

        register(subs)
        args = root.parse_args(["install-hooks"])
        # Legacy flag still accepted (SUPPRESSED help) so old scripts get
        # the migration message, not an argparse error.
        assert hasattr(args, "dry_run")
        assert hasattr(args, "scope")
        assert hasattr(args, "project_directory")

    def test_dry_run_flag_accepted(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        from yadgar.core.cli.install_hooks import register

        register(subs)
        args = root.parse_args(["install-hooks", "--dry-run"])
        assert args.dry_run is True

    def test_scope_flag_accepted(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        from yadgar.core.cli.install_hooks import register

        register(subs)
        args = root.parse_args(["install-hooks", "--scope", "project"])
        assert args.scope == "project"

    def test_default_scope_is_global(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        from yadgar.core.cli.install_hooks import register

        register(subs)
        args = root.parse_args(["install-hooks"])
        assert args.scope == "global"


# ---------------------------------------------------------------------------
# cmd_install_hooks — the hard-remove stub
# ---------------------------------------------------------------------------


class TestCmdInstallHooks:
    """Car 7: the legacy CLI is a stub that prints a migration message
    and exits 1. The MCP ``install_hooks`` tool is the live path (it
    delegates to ``install_client``); the CLI is a one-time
    user-migration nudge."""

    def test_invoke_exits_1(self, capsys):
        from yadgar.core.cli.install_hooks import cmd_install_hooks

        with __import__("pytest").raises(SystemExit) as exc_info:
            cmd_install_hooks(_make_args())
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        # Migration message references the new canonical command.
        assert "install-hooks" in err
        assert "removed" in err
        assert "yadgar install --client claude-code --hooks" in err

    def test_invoke_with_all_legacy_flags_exits_1(self, capsys):
        """Every legacy flag combination must exit 1 with a migration message."""
        from yadgar.core.cli.install_hooks import cmd_install_hooks

        for args in (
            _make_args(),
            _make_args(scope="project", project_directory="/tmp/proj"),
            _make_args(dry_run=True),
            _make_args(scope="project", project_directory="/tmp/proj", dry_run=True),
        ):
            with __import__("pytest").raises(SystemExit) as exc_info:
                cmd_install_hooks(args)
            assert exc_info.value.code == 1, f"expected exit 1 for {args}"
            err = capsys.readouterr().err
            assert "removed" in err, f"migration message missing 'removed' for {args}"

    def test_migration_message_includes_scope_specific_examples(self):
        """Migration message shows examples for --scope global, --scope project,
        and --dry-run variants so users can find their specific path."""
        from yadgar.core.cli.install_hooks import _REMOVED_MESSAGE

        # Global scope example
        assert "yadgar install-hooks --scope global" in _REMOVED_MESSAGE
        assert "yadgar install --client claude-code --hooks --scope global" in _REMOVED_MESSAGE
        # Project scope example
        assert "yadgar install-hooks --scope project" in _REMOVED_MESSAGE
        assert "yadgar install --client claude-code --hooks --scope project" in _REMOVED_MESSAGE
        # Dry-run example
        assert "yadgar install-hooks --dry-run" in _REMOVED_MESSAGE
        assert "yadgar install --client claude-code --hooks --print" in _REMOVED_MESSAGE

    def test_migration_message_references_docs_and_release(self):
        """Migration message references docs/plans + the release version
        so support engineers can find context quickly."""
        from yadgar.core.cli.install_hooks import _REMOVED_MESSAGE

        assert "v5.166.0" in _REMOVED_MESSAGE
        # The Car-7 message references the re-audit plan (now archived;
        # the active cross-ref is in CAP-INFRA-034 + ADR-0168). The
        # train summary lives at opencode-hook-port-train-2026-07-26.md.
        assert "opencode port" in _REMOVED_MESSAGE.lower()
        assert "yadgar/core/install/clients/install.py" in _REMOVED_MESSAGE
        assert "install_client" in _REMOVED_MESSAGE

    def test_no_install_hooks_lib_call_on_stub(self):
        """The stub must NOT call ``install_hooks_impl`` — the legacy impl
        is dead. The migration message is the only output. (Pins the
        fact that we don't accidentally call the old code path.)"""
        from unittest.mock import patch

        from yadgar.core.cli.install_hooks import cmd_install_hooks

        with patch("yadgar.core.install.install_hooks_lib.install_hooks_impl") as mock_impl:
            with __import__("pytest").raises(SystemExit):
                cmd_install_hooks(_make_args())
        mock_impl.assert_not_called()

    def test_no_settings_json_written(self, tmp_path):
        """The stub doesn't touch the host filesystem — no settings.json, no
        any artifact. Migration message is the only output."""
        import os

        from yadgar.core.cli.install_hooks import cmd_install_hooks

        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(tmp_path)
        try:
            with __import__("pytest").raises(SystemExit):
                cmd_install_hooks(_make_args())
            # No settings.json created anywhere under tmp_path.
            assert not (tmp_path / ".claude" / "settings.json").exists()
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

    def test_no_subprocess_to_yadgar_binary(self):
        """The stub does NOT shell out to `yadgar install` itself — that
        would re-enter the daemon and be slow. Migration is text-only."""
        from unittest.mock import patch

        from yadgar.core.cli.install_hooks import cmd_install_hooks

        with patch("subprocess.run") as mock_run:
            with __import__("pytest").raises(SystemExit):
                cmd_install_hooks(_make_args())
        mock_run.assert_not_called()

    def test_via_subprocess_dry_run(self, tmp_path):
        """End-to-end: spawn `python -m yadgar install-hooks --dry-run` via
        subprocess and verify it exits 1 with the migration message.
        Patches HOME so the test doesn't touch real ~/.claude/. Catches
        the integration between the argparse stub and the message body."""
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(tmp_path)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "yadgar", "install-hooks", "--dry-run"],
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(tmp_path)},
                timeout=10,
            )
            assert result.returncode == 1, (
                f"install-hooks must exit 1; got rc={result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            assert "removed" in result.stderr
            assert "install --client claude-code --hooks" in result.stderr
            # No files were written.
            assert not (tmp_path / ".claude" / "settings.json").exists()
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
