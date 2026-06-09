"""Tests for yadgar/cli/install_hooks.py — install-hooks CLI subcommand.

Wave 3 coverage: yadgar/cli/install_hooks.py (~30 stmts, 0% pre-wave).
Strategy: mock install_hooks_impl at boundary. Test all status branches
and register() parser wiring.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.cli.install_hooks import cmd_install_hooks, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> SimpleNamespace:
    defaults = {"scope": "global", "project_directory": "", "dry_run": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_install_hooks_subparser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-hooks"])
        assert hasattr(args, "dry_run")

    def test_dry_run_flag(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-hooks", "--dry-run"])
        assert args.dry_run is True

    def test_scope_flag(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-hooks", "--scope", "project"])
        assert args.scope == "project"

    def test_default_scope_is_global(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["install-hooks"])
        assert args.scope == "global"


# ---------------------------------------------------------------------------
# cmd_install_hooks
# ---------------------------------------------------------------------------


class TestCmdInstallHooks:
    def test_success_prints_json(self, capsys):
        result = {"status": "ok", "files": ["yadgar-file-changed.py"]}
        with patch("yadgar.install_hooks_lib.install_hooks_impl", return_value=result):
            cmd_install_hooks(_make_args())
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "ok"

    def test_error_exits_one(self, capsys):
        result = {"status": "error", "reason": "home dir not found"}
        with patch("yadgar.install_hooks_lib.install_hooks_impl", return_value=result):
            with pytest.raises(SystemExit) as exc_info:
                cmd_install_hooks(_make_args())
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "home dir not found" in err

    def test_dry_run_status_no_extra_output(self, capsys):
        result = {"status": "dry_run"}
        with patch("yadgar.install_hooks_lib.install_hooks_impl", return_value=result):
            cmd_install_hooks(_make_args(dry_run=True))
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_passes_flags_to_impl(self):
        result = {"status": "ok"}
        with patch("yadgar.install_hooks_lib.install_hooks_impl", return_value=result) as mock_impl:
            with patch("builtins.print"):
                cmd_install_hooks(
                    _make_args(scope="project", project_directory="/tmp/proj", dry_run=True)
                )
        kw = mock_impl.call_args.kwargs
        assert kw["scope"] == "project"
        assert kw["dry_run"] is True
