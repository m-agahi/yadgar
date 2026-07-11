"""Tests for yadgar/cli/install_subagents.py — install-subagents CLI subcommand.

Wave 3 coverage: yadgar/cli/install_subagents.py (33 stmts, 0% pre-wave).
Strategy: mock install_subagents_impl at boundary. Test all status branches
and register() parser wiring.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.install_subagents import _handle_check_result, cmd_install_subagents, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> SimpleNamespace:
    defaults = {"dry_run": False, "force": False, "check": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_install_subagents_subparser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-subagents"])
        assert hasattr(args, "dry_run")

    def test_dry_run_flag(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-subagents", "--dry-run"])
        assert args.dry_run is True

    def test_force_flag(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-subagents", "--force"])
        assert args.force is True

    def test_check_flag(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-subagents", "--check"])
        assert args.check is True


# ---------------------------------------------------------------------------
# _handle_check_result
# ---------------------------------------------------------------------------


class TestHandleCheckResult:
    def test_no_changes_exits_zero(self, capsys):
        result = {"would_install": [], "agents_dir": "/fake"}
        with pytest.raises(SystemExit) as exc_info:
            _handle_check_result(result)
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "no changes needed" in out.lower()

    def test_with_changes_exits_one(self, capsys):
        result = {"would_install": ["agent1.md", "agent2.md"], "agents_dir": "/fake/agents"}
        with pytest.raises(SystemExit) as exc_info:
            _handle_check_result(result)
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "agent1.md" in out
        assert "agent2.md" in out


# ---------------------------------------------------------------------------
# cmd_install_subagents
# ---------------------------------------------------------------------------


class TestCmdInstallSubagents:
    def test_success_prints_json(self, capsys):
        result = {"status": "installed", "files": ["agent.md"]}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ):
            cmd_install_subagents(_make_args())
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "installed"

    def test_error_exits_one(self, capsys):
        result = {"status": "error", "reason": "agents dir missing"}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_install_subagents(_make_args())
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "agents dir missing" in err

    def test_nix_managed_prints_message(self, capsys):
        result = {"status": "nix_managed", "message": "NixOS detected — skipping."}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ):
            cmd_install_subagents(_make_args())
        out = capsys.readouterr().out
        assert "NixOS" in out

    def test_dry_run_status_no_extra_output(self, capsys):
        result = {"status": "dry_run"}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ):
            cmd_install_subagents(_make_args(dry_run=True))
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_check_status_exits_zero_when_no_changes(self, capsys):
        result = {"status": "check", "would_install": [], "agents_dir": "/fake"}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_install_subagents(_make_args(check=True))
        assert exc_info.value.code == 0

    def test_check_status_exits_one_with_changes(self, capsys):
        result = {"status": "check", "would_install": ["x.md"], "agents_dir": "/fake"}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_install_subagents(_make_args(check=True))
        assert exc_info.value.code == 1

    def test_passes_flags_to_impl(self):
        result = {"status": "ok"}
        with patch(
            "yadgar.core.install.install_subagents_lib.install_subagents_impl", return_value=result
        ) as mock_impl:
            with patch("builtins.print"):
                cmd_install_subagents(_make_args(dry_run=True, force=True, check=False))
        kw = mock_impl.call_args.kwargs
        assert kw["dry_run"] is True
        assert kw["force"] is True
        assert kw["check"] is False
