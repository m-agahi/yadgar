"""Tests for yadgar/cli/restore.py — post-compaction context restore subcommand.

Wave 5 coverage: yadgar/cli/restore.py (15 stmts, 40% pre-wave).
Strategy: patch init_replay_lightweight at yadgar.cli._shared (lazy import inside
cmd_restore body). The replay mock returns a formatted string or empty dict
to exercise both print and no-print branches. Also test register() wiring.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.cli.restore import cmd_restore, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(directory="/tmp/proj", db_path=None):
    return SimpleNamespace(directory=directory, db_path=db_path)


def _make_storage_replay(formatted=""):
    storage = MagicMock()
    replay = MagicMock()
    replay.restore.return_value = {"formatted": formatted}
    return storage, replay


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

    def test_db_path_optional_default_none(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["restore", "/some/dir"])
        assert args.db_path is None

    def test_db_path_can_be_set(self):
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
# cmd_restore — happy path: formatted output
# ---------------------------------------------------------------------------


class TestCmdRestoreWithFormattedOutput:
    def test_prints_formatted_to_stdout(self, capsys):
        storage, replay = _make_storage_replay("# Context\nsome markdown")
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            cmd_restore(_make_args())
        out = capsys.readouterr().out
        assert "# Context" in out
        assert "some markdown" in out

    def test_calls_replay_restore_with_directory(self):
        storage, replay = _make_storage_replay("something")
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            cmd_restore(_make_args(directory="/my/proj"))
        replay.restore.assert_called_once_with("/my/proj")

    def test_storage_closed_after_success(self):
        storage, replay = _make_storage_replay("text")
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            cmd_restore(_make_args())
        storage.close.assert_called_once()

    def test_init_called_with_db_path(self):
        storage, replay = _make_storage_replay("text")
        with patch(
            "yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ) as mock_init:
            cmd_restore(_make_args(db_path="/custom.db"))
        mock_init.assert_called_once_with("/custom.db")


# ---------------------------------------------------------------------------
# cmd_restore — empty formatted: no print
# ---------------------------------------------------------------------------


class TestCmdRestoreEmptyFormatted:
    def test_no_output_when_formatted_empty(self, capsys):
        storage, replay = _make_storage_replay("")
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            cmd_restore(_make_args())
        out = capsys.readouterr().out
        assert out == ""

    def test_storage_closed_even_when_empty(self):
        storage, replay = _make_storage_replay("")
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            cmd_restore(_make_args())
        storage.close.assert_called_once()

    def test_no_output_when_formatted_key_missing(self, capsys):
        storage = MagicMock()
        replay = MagicMock()
        replay.restore.return_value = {}
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            cmd_restore(_make_args())
        out = capsys.readouterr().out
        assert out == ""


# ---------------------------------------------------------------------------
# cmd_restore — storage closed in finally (exception safety)
# ---------------------------------------------------------------------------


class TestCmdRestoreFinally:
    def test_storage_closed_when_replay_raises(self):
        storage = MagicMock()
        replay = MagicMock()
        replay.restore.side_effect = RuntimeError("boom")
        with patch("yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)):
            with pytest.raises(RuntimeError):
                cmd_restore(_make_args())
        storage.close.assert_called_once()

    def test_init_called_with_none_db_path_by_default(self):
        storage, replay = _make_storage_replay("ok")
        with patch(
            "yadgar.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ) as mock_init:
            cmd_restore(_make_args(db_path=None))
        mock_init.assert_called_once_with(None)
