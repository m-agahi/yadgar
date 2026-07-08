"""Tests for yadgar/cli/drain.py — pre-compaction context drain subcommand.

Wave 5 coverage: yadgar/cli/drain.py (14 stmts, 43% pre-wave).
Strategy: patch init_replay_lightweight at yadgar.cli._shared (lazy import inside
cmd_drain body). The replay mock returns a dict; verify JSON output, storage close,
and register() wiring.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core.cli.drain import cmd_drain, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(directory="/tmp/proj", db_path=None):
    return SimpleNamespace(directory=directory, db_path=db_path)


def _make_storage_replay(drain_result=None):
    storage = MagicMock()
    replay = MagicMock()
    if drain_result is None:
        drain_result = {"status": "ok", "count": 3}
    replay.pre_compact_drain.return_value = drain_result
    return storage, replay


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_creates_drain_subparser(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/some/dir"])
        assert args.directory == "/some/dir"
        assert hasattr(args, "func")

    def test_db_path_default_none(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/some/dir"])
        assert args.db_path is None

    def test_db_path_can_be_set(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/dir", "--db-path", "/custom.db"])
        assert args.db_path == "/custom.db"

    def test_func_is_cmd_drain(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/dir"])
        assert args.func is cmd_drain


# ---------------------------------------------------------------------------
# cmd_drain — happy path
# ---------------------------------------------------------------------------


class TestCmdDrainHappyPath:
    def test_prints_json_to_stdout(self, capsys):
        storage, replay = _make_storage_replay({"status": "ok", "saved": 5})
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ):
            cmd_drain(_make_args())
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["status"] == "ok"
        assert parsed["saved"] == 5

    def test_output_is_valid_json(self, capsys):
        storage, replay = _make_storage_replay({"x": 1})
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ):
            cmd_drain(_make_args())
        out = capsys.readouterr().out
        json.loads(out)  # must not raise

    def test_calls_pre_compact_drain_with_directory(self):
        storage, replay = _make_storage_replay()
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ):
            cmd_drain(_make_args(directory="/my/project"))
        replay.pre_compact_drain.assert_called_once_with("/my/project")

    def test_storage_closed_after_success(self):
        storage, replay = _make_storage_replay()
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ):
            cmd_drain(_make_args())
        storage.close.assert_called_once()

    def test_init_called_with_db_path(self):
        storage, replay = _make_storage_replay()
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ) as mock_init:
            cmd_drain(_make_args(db_path="/some.db"))
        mock_init.assert_called_once_with("/some.db")

    def test_init_called_with_none_by_default(self):
        storage, replay = _make_storage_replay()
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ) as mock_init:
            cmd_drain(_make_args(db_path=None))
        mock_init.assert_called_once_with(None)

    def test_empty_dict_result_prints_empty_object(self, capsys):
        storage, replay = _make_storage_replay({})
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ):
            cmd_drain(_make_args())
        out = capsys.readouterr().out
        assert json.loads(out) == {}


# ---------------------------------------------------------------------------
# cmd_drain — exception safety (finally closes storage)
# ---------------------------------------------------------------------------


class TestCmdDrainFinally:
    def test_storage_closed_when_drain_raises(self):
        storage = MagicMock()
        replay = MagicMock()
        replay.pre_compact_drain.side_effect = RuntimeError("drain failed")
        with patch(
            "yadgar.core.cli._shared.init_replay_lightweight", return_value=(storage, replay)
        ):
            with pytest.raises(RuntimeError):
                cmd_drain(_make_args())
        storage.close.assert_called_once()
