"""Tests for yadgar/core/cli/capture.py — lightweight action-capture subcommand.

T2 Car E1 (ADR-0078): cmd_capture no longer opens a StorageEngine. It enqueues
an ``action_log`` job on the file queue; the backend QueueDrainer replays the
write via ``run_action_log_replay``. Tests exercise register() wiring, the
enqueue happy path, and the exception path (exits 1).
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar._shared.file_queue.queue import FileQueue
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
    project="owner/repo",
):
    """Build a parsed-args double for ``cmd_capture``.

    ``project`` is explicit because ``/tmp/proj`` is not a git tree and carries
    no ``.yadgar/project-id``: since C5 (ADR-0227) ``resolve_cli_project`` has
    no tier left to fall through to, so an args double without the attribute
    exits 2 before the enqueue these tests are about. ``register()`` puts the
    real ``--project`` flag on the subparser (``add_project_argument``), so
    naming it here matches the CLI a caller actually invokes rather than
    papering over a gap. Pass ``project=None`` to exercise the unresolvable
    path deliberately.
    """
    return SimpleNamespace(
        tool_name=tool_name,
        summary=summary,
        directory=directory,
        session=session,
        db_path=db_path,
        project=project,
    )


@pytest.fixture
def queue_dir(tmp_path, monkeypatch):
    """Point the lazily-resolved DATA_DIR at a per-test tmp dir."""
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
    return tmp_path


def _pending_records(base_dir) -> list[dict]:
    return [json.loads(p.read_text()) for p in FileQueue(base_dir).pending()]


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
# cmd_capture — happy path (enqueue)
# ---------------------------------------------------------------------------


class TestCmdCaptureHappyPath:
    def test_enqueues_one_action_log_job(self, queue_dir):
        cmd_capture(_make_args())
        records = _pending_records(queue_dir)
        assert len(records) == 1
        assert records[0]["op"] == "action_log"

    def test_payload_receives_tool_name(self, queue_dir):
        cmd_capture(_make_args(tool_name="Bash"))
        (record,) = _pending_records(queue_dir)
        assert record["payload"]["tool_name"] == "Bash"

    def test_payload_receives_summary(self, queue_dir):
        cmd_capture(_make_args(summary="reading a file"))
        (record,) = _pending_records(queue_dir)
        assert record["payload"]["summary"] == "reading a file"

    def test_payload_receives_directory(self, queue_dir):
        cmd_capture(_make_args(directory="/project/dir"))
        (record,) = _pending_records(queue_dir)
        assert record["payload"]["directory"] == "/project/dir"

    def test_payload_receives_session(self, queue_dir):
        cmd_capture(_make_args(session="my-session"))
        (record,) = _pending_records(queue_dir)
        assert record["payload"]["session_id"] == "my-session"

    def test_payload_receives_project_id(self, queue_dir):
        """C4/C5: the host-side CLI is the only participant that can resolve it."""
        cmd_capture(_make_args(project="acme/widget"))
        (record,) = _pending_records(queue_dir)
        assert record["payload"]["project_id"] == "acme/widget"

    def test_payload_receives_timestamp_string(self, queue_dir):
        cmd_capture(_make_args())
        (record,) = _pending_records(queue_dir)
        ts = record["payload"]["timestamp"]
        assert isinstance(ts, str)
        assert "T" in ts

    def test_no_direct_storage_engine_use(self):
        """The capture CLI must not open a StorageEngine (raw DB write path)."""
        import inspect

        from yadgar.core.cli import capture as capture_mod

        src = inspect.getsource(capture_mod)
        assert "StorageEngine" not in src, (
            "cmd_capture must enqueue via the file-queue seam, not write the DB directly"
        )


# ---------------------------------------------------------------------------
# cmd_capture — exception path (exits 1)
# ---------------------------------------------------------------------------


class TestCmdCaptureException:
    def test_exits_one_on_enqueue_error(self, queue_dir):
        args = _make_args()
        with patch.object(FileQueue, "enqueue", side_effect=Exception("db gone")):
            with pytest.raises(SystemExit) as exc_info:
                cmd_capture(args)
        assert exc_info.value.code == 1

    def test_prints_error_message_to_stderr(self, queue_dir, capsys):
        args = _make_args()
        with patch.object(FileQueue, "enqueue", side_effect=Exception("disk full")):
            with pytest.raises(SystemExit):
                cmd_capture(args)
        err = capsys.readouterr().err
        assert "disk full" in err

    def test_unresolvable_tree_exits_two_and_enqueues_nothing(self, queue_dir):
        """C5/ADR-0227: no ``--project`` + no identity in the tree is fatal.

        ``/tmp/proj`` has neither a ``.yadgar/project-id`` nor an origin remote,
        so the mint raises and ``resolve_cli_project`` exits 2. The assertion
        that matters is the SECOND one: the failure happens BEFORE the enqueue,
        so no action_log job lands carrying a guessed namespace.
        """
        with pytest.raises(SystemExit) as exc_info:
            cmd_capture(_make_args(project=None))
        assert exc_info.value.code == 2
        assert _pending_records(queue_dir) == []
