"""Tests for yadgar/core/cli/restore.py — post-compaction restore subcommand.

T2 Car B: cmd_restore is a thin forwarder to the backend POST /restore
(via yadgar.core.cli._shared.forward_restore). Strategy: patch the forward
helper at its source module (cmd_restore lazy-imports it by name from
yadgar.core.cli._shared) and pin the print / no-print branches plus the
register() wiring. silence_logging is patched too — the real one calls
logging.disable(CRITICAL) process-wide, which would leak into sibling tests.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.restore import cmd_restore, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(directory="/tmp/proj", db_path=None):
    return SimpleNamespace(directory=directory, db_path=db_path)


@contextmanager
def _patched(forward_return=None, forward_side_effect=None):
    with (
        patch("yadgar.core.cli._shared.silence_logging"),
        patch(
            "yadgar.core.cli._shared.forward_restore",
            return_value=forward_return,
            side_effect=forward_side_effect,
        ) as fwd,
    ):
        yield fwd


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_creates_restore_subparser(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["restore", "/some/dir"])
        assert args.directory == "/some/dir"
        assert hasattr(args, "func")

    def test_db_path_still_accepted_for_compat(self):
        """--db-path is kept for CLI compatibility (ignored since T2 Car B)."""
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["restore", "/some/dir", "--db-path", "/x/y.db"])
        assert args.db_path == "/x/y.db"

    def test_func_is_cmd_restore(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["restore", "/some/dir"])
        assert args.func is cmd_restore


# ---------------------------------------------------------------------------
# cmd_restore — forward + print behavior
# ---------------------------------------------------------------------------


class TestCmdRestoreForward:
    def test_prints_formatted_to_stdout(self, capsys):
        with _patched(forward_return={"formatted": "# Context\nsome markdown"}):
            cmd_restore(_make_args())
        out = capsys.readouterr().out
        assert "# Context" in out
        assert "some markdown" in out

    def test_forwards_directory(self):
        with _patched(forward_return={"formatted": "x"}) as fwd:
            cmd_restore(_make_args(directory="/my/proj"))
        fwd.assert_called_once_with("/my/proj")

    def test_no_output_when_formatted_empty(self, capsys):
        with _patched(forward_return={"formatted": ""}):
            cmd_restore(_make_args())
        assert capsys.readouterr().out == ""

    def test_no_output_when_formatted_key_missing(self, capsys):
        with _patched(forward_return={}):
            cmd_restore(_make_args())
        assert capsys.readouterr().out == ""

    def test_forward_error_propagates(self):
        """Forward-only: backend-unreachable errors surface, no local fallback."""
        with _patched(forward_side_effect=RuntimeError("YADGAR_EMBED_URL is not set")):
            with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
                cmd_restore(_make_args())
