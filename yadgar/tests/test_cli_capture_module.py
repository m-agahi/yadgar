"""Tests for yadgar/cli/capture.py — lightweight action-capture subcommand.

Wave 5 coverage: yadgar/cli/capture.py (23 stmts, 48% pre-wave).
Strategy: patch yadgar.config.Settings and yadgar.storage.StorageEngine (lazy
imports inside cmd_capture body). Exercise happy path, exception path (exits 1),
and finally-close behaviour. Also test register() argument wiring.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core.cli.capture import cmd_capture, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(
    tool_name="Read",
    summary="some summary",
    directory="/tmp/proj",
    session="sess-123",
    db_path=None,
):
    return SimpleNamespace(
        tool_name=tool_name,
        summary=summary,
        directory=directory,
        session=session,
        db_path=db_path,
    )


def _make_settings(db_path="/default/yadgar.db"):
    s = MagicMock()
    s.DB_PATH = db_path
    return s


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_creates_capture_subparser(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["capture", "--tool", "Read"])
        assert args.tool_name == "Read"
        assert hasattr(args, "func")

    def test_summary_default_empty(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["capture", "--tool", "Write"])
        assert args.summary == ""

    def test_directory_default_empty(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["capture", "--tool", "Write"])
        assert args.directory == ""

    def test_session_default_empty(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["capture", "--tool", "Write"])
        assert args.session == ""

    def test_db_path_default_none(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["capture", "--tool", "Write"])
        assert args.db_path is None

    def test_func_is_cmd_capture(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["capture", "--tool", "Read"])
        assert args.func is cmd_capture


# ---------------------------------------------------------------------------
# cmd_capture — happy path
# ---------------------------------------------------------------------------


class TestCmdCaptureHappyPath:
    def test_insert_action_log_called(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args()
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        storage.insert_action_log.assert_called_once()

    def test_insert_receives_tool_name(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args(tool_name="Bash")
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        call_kwargs = storage.insert_action_log.call_args.kwargs
        assert call_kwargs["tool_name"] == "Bash"

    def test_insert_receives_summary(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args(summary="reading a file")
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        call_kwargs = storage.insert_action_log.call_args.kwargs
        assert call_kwargs["tool_input_summary"] == "reading a file"

    def test_insert_receives_directory(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args(directory="/project/dir")
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        call_kwargs = storage.insert_action_log.call_args.kwargs
        assert call_kwargs["directory"] == "/project/dir"

    def test_insert_receives_session(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args(session="my-session")
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        call_kwargs = storage.insert_action_log.call_args.kwargs
        assert call_kwargs["session_id"] == "my-session"

    def test_insert_receives_timestamp_string(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args()
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        call_kwargs = storage.insert_action_log.call_args.kwargs
        assert isinstance(call_kwargs["timestamp"], str)
        assert "T" in call_kwargs["timestamp"]

    def test_storage_closed_after_success(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args()
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            cmd_capture(args)
        storage.close.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_capture — db_path resolution
# ---------------------------------------------------------------------------


class TestCmdCaptureDbPath:
    def test_uses_settings_db_path_when_args_db_path_none(self):
        settings = _make_settings(db_path="/default/yadgar.db")
        storage = MagicMock()
        args = _make_args(db_path=None)
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage) as mock_storage_cls,
        ):
            cmd_capture(args)
        called_path = mock_storage_cls.call_args.args[0]
        assert "yadgar.db" in called_path

    def test_uses_explicit_db_path_when_given(self):
        settings = _make_settings()
        storage = MagicMock()
        args = _make_args(db_path="/explicit/path.db")
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage) as mock_storage_cls,
        ):
            cmd_capture(args)
        called_path = mock_storage_cls.call_args.args[0]
        assert "explicit" in called_path


# ---------------------------------------------------------------------------
# cmd_capture — exception path (exits 1)
# ---------------------------------------------------------------------------


class TestCmdCaptureException:
    def test_exits_one_on_insert_error(self, capsys):
        settings = _make_settings()
        storage = MagicMock()
        storage.insert_action_log.side_effect = Exception("db gone")
        args = _make_args()
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_capture(args)
        assert exc_info.value.code == 1

    def test_prints_error_message_to_stderr(self, capsys):
        settings = _make_settings()
        storage = MagicMock()
        storage.insert_action_log.side_effect = Exception("disk full")
        args = _make_args()
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            with pytest.raises(SystemExit):
                cmd_capture(args)
        err = capsys.readouterr().err
        assert "disk full" in err

    def test_storage_closed_on_exception(self):
        settings = _make_settings()
        storage = MagicMock()
        storage.insert_action_log.side_effect = Exception("boom")
        args = _make_args()
        with (
            patch("yadgar._shared.config.Settings", return_value=settings),
            patch("yadgar._shared.storage.StorageEngine", return_value=storage),
        ):
            with pytest.raises(SystemExit):
                cmd_capture(args)
        storage.close.assert_called_once()
