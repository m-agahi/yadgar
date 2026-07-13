"""Seam test: admin_exec.restoration.pre_compact_drain payload → replay wiring.

Car fix-drain-inflight (v5.135): the host-side drain callers pass a parsed
``in_flight`` dict in the /admin payload. The admin op must thread it through to
``CheckpointRestore.pre_compact_drain(..., in_flight=...)`` so the backend can
persist it verbatim. This is the wiring gap between the host parse and the
backend persistence — pinned here so a signature drift is caught.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from yadgar.backend.admin_exec import restoration as admin_restoration


def _patched_replay():
    replay = MagicMock()
    replay.pre_compact_drain.return_value = {
        "status": "drained",
        "epoch": 3,
        "auto_checkpoint_created": True,
    }
    return replay


def test_payload_in_flight_threaded_to_replay():
    replay = _patched_replay()
    provided = {"agents": ["a1"], "bg_shells": [], "worktrees": ["/w (m)"], "note": "n"}
    with (
        patch.object(admin_restoration, "ensure_restoration_engines"),
        patch.object(admin_restoration, "_get_replay", return_value=replay),
    ):
        result = admin_restoration.pre_compact_drain(
            {
                "directory": "/proj",
                "transcript_path": "/t.jsonl",
                "in_flight": provided,
            }
        )
    replay.pre_compact_drain.assert_called_once_with(
        "/proj", transcript_path="/t.jsonl", in_flight=provided
    )
    assert result["epoch"] == 3


def test_absent_in_flight_threads_none():
    replay = _patched_replay()
    with (
        patch.object(admin_restoration, "ensure_restoration_engines"),
        patch.object(admin_restoration, "_get_replay", return_value=replay),
    ):
        admin_restoration.pre_compact_drain({"directory": "/proj", "transcript_path": "/t.jsonl"})
    replay.pre_compact_drain.assert_called_once_with(
        "/proj", transcript_path="/t.jsonl", in_flight=None
    )
