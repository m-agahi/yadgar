"""T2 Car E1 — action_log rides the file-queue seam.

Core raw ``storage.insert_action_log`` writes (auto-capture flush, team-inbox
ingest, ``yadgar capture`` CLI) move behind the sanctioned write seam: core
enqueues an ``action_log`` job; the backend QueueDrainer replays it via
``run_action_log_replay`` (ADR-0078 — core touches zero DB directly on the
write side).

TDD: written before the seam existed — RED without the op, GREEN with it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from yadgar._shared.file_queue.queue import FileQueue
from yadgar.backend.queue_drainer import QueueDrainer

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it.
_TEST_PROJECT = "m-agahi/yadgar"

# ---------------------------------------------------------------------------
# Backend replay impl
# ---------------------------------------------------------------------------


class TestRunActionLogReplay:
    def test_inserts_row_via_storage(self, monkeypatch):
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.write_exec import run_action_log_replay

        storage = MagicMock()
        monkeypatch.setattr(_st, "_storage", storage)

        run_action_log_replay(
            {
                "tool_name": "Write",
                "summary": "edited file",
                "directory": "/proj/dir",
                "session_id": "sess-1",
                "timestamp": "2026-07-10T00:00:00+00:00",
                "project_id": _TEST_PROJECT,
            }
        )

        # C13: ``project_id`` joins the kwargs the replay forwards. C4 made it
        # part of the row and this exact-call assertion is the seam's contract,
        # so it names the value rather than letting the default "" slip past.
        storage.insert_action_log.assert_called_once_with(
            tool_name="Write",
            tool_input_summary="edited file",
            directory="/proj/dir",
            session_id="sess-1",
            timestamp="2026-07-10T00:00:00+00:00",
            project_id=_TEST_PROJECT,
        )

    def test_missing_timestamp_defaults_to_now(self, monkeypatch):
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.write_exec import run_action_log_replay

        storage = MagicMock()
        monkeypatch.setattr(_st, "_storage", storage)

        run_action_log_replay(
            {"tool_name": "Bash", "summary": "", "directory": "", "session_id": ""}
        )

        ts = storage.insert_action_log.call_args.kwargs["timestamp"]
        assert isinstance(ts, str) and "T" in ts


# ---------------------------------------------------------------------------
# Drainer dispatch: op == "action_log"
# ---------------------------------------------------------------------------


class TestDrainerActionLogOp:
    def test_apply_routes_action_log_to_storage(self, tmp_path, monkeypatch):
        import yadgar._shared.runtime.state as _st

        storage = MagicMock()
        monkeypatch.setattr(_st, "_storage", storage)

        fq = FileQueue(tmp_path, wiki_prefix="wiki-")
        drainer = QueueDrainer(fq, storage_factory=lambda: None, drain_interval=999.0)
        fq.enqueue(
            "action_log",
            {
                "tool_name": "batch[Write,Edit]",
                "summary": "combined",
                "directory": "/proj",
                "session_id": "s-9",
                "timestamp": "2026-07-10T01:02:03+00:00",
                "project_id": _TEST_PROJECT,
            },
        )

        drainer._drain_once()

        storage.insert_action_log.assert_called_once()
        kwargs = storage.insert_action_log.call_args.kwargs
        assert kwargs["tool_name"] == "batch[Write,Edit]"
        assert kwargs["directory"] == "/proj"
        # Consumed: nothing left pending.
        assert fq.pending() == []


# ---------------------------------------------------------------------------
# CLI capture enqueues instead of opening the DB
# ---------------------------------------------------------------------------


class TestCmdCaptureEnqueues:
    def _args(self, **over):
        base = {
            "tool_name": "Read",
            "summary": "some summary",
            "directory": "/tmp/proj",
            "session": "sess-123",
            "project": _TEST_PROJECT,
            "db_path": None,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def test_enqueues_action_log_op(self, tmp_path, monkeypatch):
        # DATA_DIR is resolved lazily from the env at access time (PEP-562).
        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path))
        from yadgar.core.cli.capture import cmd_capture

        cmd_capture(self._args(tool_name="Bash", summary="ran tests"))

        fq = FileQueue(tmp_path)
        pending = fq.pending()
        assert len(pending) == 1
        import json

        record = json.loads(pending[0].read_text())
        assert record["op"] == "action_log"
        assert record["payload"]["tool_name"] == "Bash"
        assert record["payload"]["summary"] == "ran tests"
        assert record["payload"]["directory"] == "/tmp/proj"
        assert record["payload"]["session_id"] == "sess-123"
        # C4/C13: the CLI is host-side, so it resolves the identity itself and
        # stamps it — the backend replay forwards but never mints one.
        assert record["payload"]["project_id"] == _TEST_PROJECT

    def test_no_direct_storage_engine_use(self):
        """The capture CLI must not open a StorageEngine (raw DB write path)."""
        import inspect

        from yadgar.core.cli import capture as capture_mod

        src = inspect.getsource(capture_mod)
        assert "StorageEngine" not in src, (
            "cmd_capture must enqueue via the file-queue seam, not write the DB directly"
        )


class TestMixedProjectBatchIsSplitNotCollapsed:
    """A flushed action batch spanning two projects must not become one ``""``.

    ``/hooks/auto-capture`` accumulates 5 actions per session then flushes them
    as ONE action_log row. It took the project only when every action agreed
    (``_batch_projects.pop() if len(...) == 1 else ""``) and enqueued the row
    regardless — so a batch spanning two repos, or one containing a single
    unattributed action, was written with ``project_id=""``.

    C5 then widened the drainer's ``missing_project_id`` gate to every op_type,
    which classifies that row a PERMANENT failure. Result measured on the live
    corpus 2026-08-15: 1,094 action_log entries in the DLQ, ~600/day, growing.
    The batch already HOLDS the identities — collapsing them to ``""`` throws
    away information the row needs to survive.
    """

    def test_two_projects_yield_two_groups(self):
        from yadgar.core.server.http import _split_batch_by_project

        actions = [
            {"tool_name": "Edit", "summary": "a", "project_id": "m-agahi/yadgar"},
            {"tool_name": "Write", "summary": "b", "project_id": "quinyx/flux"},
            {"tool_name": "Edit", "summary": "c", "project_id": "m-agahi/yadgar"},
        ]
        groups = dict(_split_batch_by_project(actions))
        assert set(groups) == {"m-agahi/yadgar", "quinyx/flux"}
        assert [a["summary"] for a in groups["m-agahi/yadgar"]] == ["a", "c"]
        assert [a["summary"] for a in groups["quinyx/flux"]] == ["b"]

    def test_single_project_batch_stays_one_group(self):
        from yadgar.core.server.http import _split_batch_by_project

        actions = [
            {"tool_name": "Edit", "summary": "a", "project_id": "m-agahi/yadgar"},
            {"tool_name": "Edit", "summary": "b", "project_id": "m-agahi/yadgar"},
        ]
        groups = dict(_split_batch_by_project(actions))
        assert list(groups) == ["m-agahi/yadgar"]
        assert len(groups["m-agahi/yadgar"]) == 2

    def test_unattributed_actions_are_dropped_not_emitted(self):
        """A row with no identity cannot be written — the drainer rejects it.

        Dropping at the source is strictly better than enqueuing a job whose
        only possible outcome is a DLQ entry a human must then clear.
        """
        from yadgar.core.server.http import _split_batch_by_project

        actions = [
            {"tool_name": "Edit", "summary": "a", "project_id": "m-agahi/yadgar"},
            {"tool_name": "Edit", "summary": "orphan", "project_id": ""},
            {"tool_name": "Edit", "summary": "orphan2"},
        ]
        groups = dict(_split_batch_by_project(actions))
        assert list(groups) == ["m-agahi/yadgar"]
        assert "" not in groups and None not in groups

    def test_wholly_unattributed_batch_yields_nothing(self):
        from yadgar.core.server.http import _split_batch_by_project

        assert list(_split_batch_by_project([{"tool_name": "Edit", "project_id": ""}])) == []
