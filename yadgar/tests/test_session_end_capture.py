"""Tests for v5.10.6 SESSION_END_CAPTURE sentinel-marker pattern.

TDD — written BEFORE implementation. Run `pytest -x` to see red, then implement.

Coverage:
- Hook gates: ENABLED=false, end_reason=clear/resume, message_count < MIN
- Hook happy path: writes atomic JSON sentinel with required fields
- hook_session_context: scans + imports sentinels, deletes on success, retry on failure
- _project_brief_signals: sentinel memory row → extract_last_session_findings action
- Missing transcript path → tombstone note + suggested_call is forget(sentinel_id)
- Vacuum prunes sentinels older than SESSION_END_RETENTION_DAYS
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# v5.54.5 B2: memorize() calls in this module use /tmp/... paths (not git repos)
# and don't supply branch_hint. Mirror CI's YADGAR_CI_BRANCH=master so branch
# resolution doesn't hard-reject. Tests that expect missing_branch must delenv.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ci_branch_fallback(monkeypatch):
    """Set YADGAR_CI_BRANCH when not already set (mirrors CI env)."""
    if not os.environ.get("YADGAR_CI_BRANCH"):
        monkeypatch.setenv("YADGAR_CI_BRANCH", "test-branch")


# ---------------------------------------------------------------------------
# Hook script helpers
# ---------------------------------------------------------------------------

HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "session-end-capture.py"


def _run_hook(
    stdin_data: dict,
    tmp_path: Path,
    env_overrides: dict | None = None,
    sentinel_dir: Path | None = None,
) -> tuple[int, str, str]:
    """Run session-end-capture.py as subprocess, return (returncode, stdout, stderr)."""
    env = {**os.environ}
    if sentinel_dir is not None:
        # Point the hook at our tmp sentinel dir via env
        env["YADGAR_SESSION_END_DIR"] = str(sentinel_dir)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _minimal_transcript(tmp_path: Path, human_messages: int = 3) -> Path:
    """Write a minimal JSONL transcript with the given number of human messages."""
    p = tmp_path / "transcript.jsonl"
    lines = []
    for i in range(human_messages):
        lines.append(
            json.dumps(
                {
                    "message": {
                        "role": "user",
                        "content": f"Human message {i}",
                    }
                }
            )
        )
        # Intersperse assistant turns
        lines.append(
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": f"Assistant reply {i}",
                    }
                }
            )
        )
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _transcript_with_tool_use(tmp_path: Path) -> Path:
    """Write a transcript that includes ToolUse entries for Read/Edit files."""
    p = tmp_path / "transcript_tools.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": "Fix the bug"}}),
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/home/max/git/yadgar/yadgar/server/http.py"},
                        }
                    ],
                }
            }
        ),
        json.dumps({"message": {"role": "user", "content": "Now edit"}}),
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": "/home/max/git/yadgar/yadgar/server/tools/project.py"
                            },
                        }
                    ],
                }
            }
        ),
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("session_end_capture")
    from yadgar import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ===========================================================================
# 1. Hook gate: SESSION_END_CAPTURE_ENABLED=false
# ===========================================================================


def test_hook_disabled_env_exits_0_no_file(tmp_path):
    """SESSION_END_CAPTURE_ENABLED=false → exit 0, no sentinel written."""
    sentinel_dir = tmp_path / "session-ends"
    rc, _out, _err = _run_hook(
        {"end_reason": "logout", "session_id": "test-disabled", "cwd": str(tmp_path)},
        tmp_path,
        env_overrides={"SESSION_END_CAPTURE_ENABLED": "false"},
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob("*.json"))


def test_hook_disabled_env_0_exits_0_no_file(tmp_path):
    """SESSION_END_CAPTURE_ENABLED=0 → exit 0, no sentinel written."""
    sentinel_dir = tmp_path / "session-ends"
    rc, _out, _err = _run_hook(
        {"end_reason": "logout", "session_id": "test-disabled-0", "cwd": str(tmp_path)},
        tmp_path,
        env_overrides={"SESSION_END_CAPTURE_ENABLED": "0"},
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob("*.json"))


# ===========================================================================
# 2. Hook gate: end_reason=clear / end_reason=resume
# ===========================================================================


def test_hook_skips_end_reason_clear(tmp_path):
    """end_reason=clear → no sentinel written (self-referential loop prevention)."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=5)
    rc, _out, _err = _run_hook(
        {
            "end_reason": "clear",
            "session_id": "test-clear",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob("*.json"))


def test_hook_skips_end_reason_resume(tmp_path):
    """end_reason=resume → no sentinel written (session continuing, not terminated)."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=5)
    rc, _out, _err = _run_hook(
        {
            "end_reason": "resume",
            "session_id": "test-resume",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob("*.json"))


# ===========================================================================
# 3. Hook gate: message_count < SESSION_END_MIN_MESSAGES
# ===========================================================================


def test_hook_skips_below_min_messages(tmp_path):
    """message_count < SESSION_END_MIN_MESSAGES (default 2) → no sentinel written."""
    sentinel_dir = tmp_path / "session-ends"
    # Write a transcript with 1 human message (below default threshold of 2)
    transcript = _minimal_transcript(tmp_path, human_messages=1)
    rc, _out, _err = _run_hook(
        {
            "end_reason": "logout",
            "session_id": "test-short",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_MIN_MESSAGES": "2"},
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob("*.json"))


def test_hook_writes_sentinel_at_min_messages(tmp_path):
    """message_count == SESSION_END_MIN_MESSAGES → sentinel IS written."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=2)
    rc, _out, _err = _run_hook(
        {
            "end_reason": "logout",
            "session_id": "test-at-min",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_MIN_MESSAGES": "2"},
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    files = list(sentinel_dir.glob("*.json"))
    assert len(files) == 1


# ===========================================================================
# 4. Hook happy path: sentinel file written with correct content
# ===========================================================================


def test_hook_writes_sentinel_logout(tmp_path):
    """end_reason=logout with enough messages → sentinel file written."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=5)
    rc, _out, _err = _run_hook(
        {
            "end_reason": "logout",
            "session_id": "sess-abc123",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    files = list(sentinel_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == "sess-abc123.json"


def test_hook_sentinel_schema(tmp_path):
    """Sentinel file has all required schema fields."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=5)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "schema-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel file not written"
    record = json.loads(files[0].read_text())

    required_fields = {
        "type",
        "version",
        "cwd",
        "end_reason",
        "ended_at",
        "transcript_path",
        "session_id",
        "message_count",
        "last_human_turns",
        "last_touched_files",
    }
    missing = required_fields - record.keys()
    assert not missing, f"Missing sentinel fields: {missing}"
    assert record["type"] == "session_end_sentinel"
    assert record["version"] == 1
    assert record["end_reason"] == "logout"
    assert record["session_id"] == "schema-test"
    assert isinstance(record["last_human_turns"], list)
    assert isinstance(record["last_touched_files"], list)
    assert record["message_count"] == 5


def test_hook_sentinel_end_reason_other(tmp_path):
    """end_reason=other → sentinel IS written (true exit)."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=3)
    rc, _out, _err = _run_hook(
        {
            "end_reason": "other",
            "session_id": "test-other",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    assert rc == 0
    files = list(sentinel_dir.glob("*.json"))
    assert len(files) == 1


def test_hook_extracts_last_human_turns(tmp_path):
    """sentinel.last_human_turns contains recent user message text."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=5)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "turns-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "3"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    record = json.loads(files[0].read_text())
    # Should have at most 3 turns (env knob)
    assert len(record["last_human_turns"]) <= 3
    # Each turn is a string
    assert all(isinstance(t, str) for t in record["last_human_turns"])
    # Contains actual message content
    assert any("Human message" in t for t in record["last_human_turns"])


def test_hook_extracts_last_touched_files(tmp_path):
    """sentinel.last_touched_files populated from Read/Edit ToolUse entries."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _transcript_with_tool_use(tmp_path)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "files-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    record = json.loads(files[0].read_text())
    touched = record["last_touched_files"]
    assert isinstance(touched, list)
    # Should have the two files we edited
    paths_flat = " ".join(touched)
    assert "http.py" in paths_flat or "project.py" in paths_flat


def test_hook_atomic_write_no_tmp_file_left(tmp_path):
    """After successful write, no .json.tmp files remain (atomic rename)."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=3)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "atomic-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        sentinel_dir=sentinel_dir,
    )
    tmp_files = list(sentinel_dir.glob("*.tmp"))
    assert not tmp_files, f"Leftover .tmp files: {tmp_files}"


def test_hook_idempotent_overwrite(tmp_path):
    """Re-running hook with same session_id overwrites the sentinel (no duplicates)."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _minimal_transcript(tmp_path, human_messages=3)
    payload = {
        "end_reason": "logout",
        "session_id": "idem-test",
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
    }
    _run_hook(payload, tmp_path, sentinel_dir=sentinel_dir)
    _run_hook(payload, tmp_path, sentinel_dir=sentinel_dir)
    files = list(sentinel_dir.glob("*.json"))
    assert len(files) == 1, f"Expected 1 sentinel, got {len(files)}"


# ===========================================================================
# 5. _project_brief_signals: sentinel memory row → extract_last_session_findings
# ===========================================================================


def test_signals_sentinel_emits_extract_action(flush_queue):
    """project_brief signals mode with pending sentinel → extract_last_session_findings."""
    from yadgar import server
    from yadgar.tests.conftest import memorize_sync

    directory = "/tmp/sentinel_signals_test"
    transcript_path = "/tmp/sentinel_signals_test/fake_transcript.jsonl"
    sentinel_content = json.dumps(
        {
            "type": "session_end_sentinel",
            "version": 1,
            "cwd": directory,
            "end_reason": "logout",
            "ended_at": "2026-05-30T10:00:00Z",
            "transcript_path": transcript_path,
            "session_id": "sig-test-123",
            "message_count": 10,
            "last_human_turns": ["Last question", "Previous question"],
            "last_touched_files": ["/tmp/sentinel_signals_test/foo.py"],
        }
    )
    memorize_sync(
        content=sentinel_content,
        context=directory,
        tags=["_session_end_sentinel", "session_end"],
    )
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    actions = result.get("recommended_actions", [])
    action_types = [a["action"] for a in actions]
    assert "extract_last_session_findings" in action_types, (
        f"Expected extract_last_session_findings in {action_types}"
    )


def test_signals_sentinel_action_has_required_fields(flush_queue):
    """extract_last_session_findings action has transcript_path, sentinel_id, reason."""
    from yadgar import server
    from yadgar.tests.conftest import memorize_sync

    directory = "/tmp/sentinel_fields_test"
    transcript_path = "/tmp/sentinel_fields_test/transcript.jsonl"
    sentinel_content = json.dumps(
        {
            "type": "session_end_sentinel",
            "version": 1,
            "cwd": directory,
            "end_reason": "logout",
            "ended_at": "2026-05-30T10:00:00Z",
            "transcript_path": transcript_path,
            "session_id": "field-test-456",
            "message_count": 7,
            "last_human_turns": ["hello"],
            "last_touched_files": [],
        }
    )
    memorize_sync(
        content=sentinel_content,
        context=directory,
        tags=["_session_end_sentinel", "session_end"],
    )
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    actions = result.get("recommended_actions", [])
    sentinel_action = next(
        (a for a in actions if a["action"] == "extract_last_session_findings"), None
    )
    assert sentinel_action is not None, "extract_last_session_findings action not found"
    assert "transcript_path" in sentinel_action, "Missing transcript_path field"
    assert "sentinel_id" in sentinel_action, "Missing sentinel_id field"
    assert "reason" in sentinel_action, "Missing reason field"
    assert sentinel_action["transcript_path"] == transcript_path


def test_signals_no_sentinel_no_extract_action(flush_queue):
    """Without sentinel row, extract_last_session_findings is NOT in actions."""
    from yadgar import server

    result = server.project_brief("/tmp/no_sentinel_dir_xyz", mode="signals")
    actions = result.get("recommended_actions", [])
    action_types = [a["action"] for a in actions]
    assert "extract_last_session_findings" not in action_types


def test_signals_sentinel_different_dir_not_surfaced(flush_queue):
    """Sentinel from a different directory is NOT surfaced for current dir."""
    from yadgar import server
    from yadgar.tests.conftest import memorize_sync

    other_dir = "/tmp/sentinel_other_dir"
    our_dir = "/tmp/sentinel_our_dir_xyz"
    sentinel_content = json.dumps(
        {
            "type": "session_end_sentinel",
            "version": 1,
            "cwd": other_dir,
            "end_reason": "logout",
            "ended_at": "2026-05-30T10:00:00Z",
            "transcript_path": "/tmp/sentinel_other_dir/t.jsonl",
            "session_id": "other-dir-sess",
            "message_count": 5,
            "last_human_turns": [],
            "last_touched_files": [],
        }
    )
    memorize_sync(
        content=sentinel_content,
        context=other_dir,
        tags=["_session_end_sentinel", "session_end"],
    )
    flush_queue()

    # Query for our_dir — should NOT see the other_dir sentinel
    result = server.project_brief(our_dir, mode="signals")
    actions = result.get("recommended_actions", [])
    action_types = [a["action"] for a in actions]
    assert "extract_last_session_findings" not in action_types


# ===========================================================================
# 6. Missing transcript → tombstone note in action
# ===========================================================================


def test_signals_missing_transcript_tombstone(flush_queue):
    """When sentinel transcript_path doesn't exist → tombstone note in action."""
    from yadgar import server
    from yadgar.tests.conftest import memorize_sync

    directory = "/tmp/sentinel_tombstone_test"
    missing_path = "/tmp/nonexistent_transcript_xyz_abc.jsonl"
    sentinel_content = json.dumps(
        {
            "type": "session_end_sentinel",
            "version": 1,
            "cwd": directory,
            "end_reason": "logout",
            "ended_at": "2026-05-30T10:00:00Z",
            "transcript_path": missing_path,
            "session_id": "tomb-test-789",
            "message_count": 6,
            "last_human_turns": ["last question before exit"],
            "last_touched_files": ["/tmp/sentinel_tombstone_test/edited.py"],
        }
    )
    memorize_sync(
        content=sentinel_content,
        context=directory,
        tags=["_session_end_sentinel", "session_end"],
    )
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    actions = result.get("recommended_actions", [])
    sentinel_action = next(
        (a for a in actions if a["action"] == "extract_last_session_findings"), None
    )
    assert sentinel_action is not None
    # Tombstone note should appear somewhere in reason or suggested_call
    dump = json.dumps(sentinel_action)
    assert "transcript_not_found" in dump or "forget" in dump, (
        f"Expected tombstone/forget hint in action: {dump}"
    )


def test_signals_missing_transcript_suggested_call_has_forget(flush_queue):
    """When transcript missing → suggested_call directs forget(sentinel_id)."""
    from yadgar import server
    from yadgar.tests.conftest import memorize_sync

    directory = "/tmp/sentinel_forget_test"
    missing_path = "/tmp/definitely_missing_transcript.jsonl"
    sentinel_content = json.dumps(
        {
            "type": "session_end_sentinel",
            "version": 1,
            "cwd": directory,
            "end_reason": "logout",
            "ended_at": "2026-05-30T10:00:00Z",
            "transcript_path": missing_path,
            "session_id": "forget-test-000",
            "message_count": 4,
            "last_human_turns": ["last human turn"],
            "last_touched_files": [],
        }
    )
    memorize_sync(
        content=sentinel_content,
        context=directory,
        tags=["_session_end_sentinel", "session_end"],
    )
    flush_queue()

    result = server.project_brief(directory, mode="signals")
    actions = result.get("recommended_actions", [])
    sentinel_action = next(
        (a for a in actions if a["action"] == "extract_last_session_findings"), None
    )
    assert sentinel_action is not None
    suggested = sentinel_action.get("suggested_call", "")
    assert "forget" in suggested, f"Expected forget() in suggested_call: {suggested!r}"


# ===========================================================================
# 7. SessionStart import: hook_session_context scans + imports sentinels
# ===========================================================================


def test_session_context_imports_sentinel_file(tmp_path, monkeypatch, flush_queue):
    """hook_session_context scans YADGAR_SESSION_END_DIR and imports pending sentinels."""
    from yadgar import server

    sentinel_dir = tmp_path / "session-ends"
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    directory = str(tmp_path / "myproject")
    transcript_path = str(tmp_path / "myproject" / "transcript.jsonl")
    record = {
        "type": "session_end_sentinel",
        "version": 1,
        "cwd": directory,
        "end_reason": "logout",
        "ended_at": "2026-05-30T10:00:00Z",
        "transcript_path": transcript_path,
        "session_id": "import-test-sess",
        "message_count": 8,
        "last_human_turns": ["final question"],
        "last_touched_files": [],
    }
    sentinel_file = sentinel_dir / "import-test-sess.json"
    sentinel_file.write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setenv("YADGAR_SESSION_END_DIR", str(sentinel_dir))

    # Trigger import by calling the function directly
    from yadgar.server import http as http_mod

    http_mod._import_pending_sentinels(str(sentinel_dir))
    flush_queue()

    # Sentinel file should be deleted
    assert not sentinel_file.exists(), "Sentinel file should be deleted after import"

    # Memory row should exist with _session_end_sentinel tag
    result = server.project_brief(directory, mode="signals")
    actions = result.get("recommended_actions", [])
    action_types = [a["action"] for a in actions]
    assert "extract_last_session_findings" in action_types


def test_session_context_import_failure_leaves_file_retries(tmp_path, monkeypatch):
    """Import failure leaves sentinel file with incremented retries field."""
    from yadgar.server import http as http_mod

    sentinel_dir = tmp_path / "session-ends"
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "type": "session_end_sentinel",
        "version": 1,
        "cwd": "/tmp/fail-test",
        "end_reason": "logout",
        "ended_at": "2026-05-30T10:00:00Z",
        "transcript_path": "/tmp/fail-test/t.jsonl",
        "session_id": "fail-import-sess",
        "message_count": 5,
        "last_human_turns": [],
        "last_touched_files": [],
    }
    sentinel_file = sentinel_dir / "fail-import-sess.json"
    sentinel_file.write_text(json.dumps(record), encoding="utf-8")

    # Patch memorize to raise
    with patch("yadgar.server.http._sentinel_memorize", side_effect=RuntimeError("db down")):
        http_mod._import_pending_sentinels(str(sentinel_dir))

    # File should still exist (not deleted on failure)
    assert sentinel_file.exists(), "Sentinel file should remain after import failure"
    # retries should be incremented
    updated = json.loads(sentinel_file.read_text())
    assert updated.get("retries", 0) >= 1


def test_session_context_import_moves_to_failed_after_3_retries(tmp_path, monkeypatch):
    """After 3 failed imports, sentinel moves to session-ends/failed/."""
    from yadgar.server import http as http_mod

    sentinel_dir = tmp_path / "session-ends"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    failed_dir = sentinel_dir / "failed"

    record = {
        "type": "session_end_sentinel",
        "version": 1,
        "cwd": "/tmp/fail3-test",
        "end_reason": "logout",
        "ended_at": "2026-05-30T10:00:00Z",
        "transcript_path": "/tmp/fail3-test/t.jsonl",
        "session_id": "fail3-sess",
        "message_count": 5,
        "last_human_turns": [],
        "last_touched_files": [],
        "retries": 2,  # Already at 2; next failure (3rd) should move to failed/
    }
    sentinel_file = sentinel_dir / "fail3-sess.json"
    sentinel_file.write_text(json.dumps(record), encoding="utf-8")

    with patch("yadgar.server.http._sentinel_memorize", side_effect=RuntimeError("db down")):
        http_mod._import_pending_sentinels(str(sentinel_dir))

    # Should be in failed/ dir
    assert not sentinel_file.exists(), "Original sentinel should be moved"
    assert (failed_dir / "fail3-sess.json").exists(), "Sentinel should be in failed/ dir"


# ===========================================================================
# 8. Vacuum prunes sentinel memory rows older than SESSION_END_RETENTION_DAYS
# ===========================================================================


def test_vacuum_prunes_old_sentinel_rows(monkeypatch, flush_queue):
    """vacuum_sentinels prunes _session_end_sentinel rows older than retention days."""
    from yadgar import server
    from yadgar.tests.conftest import memorize_sync

    directory = "/tmp/vacuum_sentinel_test"
    # Store a sentinel memory row
    sentinel_content = json.dumps(
        {
            "type": "session_end_sentinel",
            "version": 1,
            "cwd": directory,
            "end_reason": "logout",
            "ended_at": "2026-05-29T10:00:00Z",
            "transcript_path": "/tmp/vacuum_sentinel_test/t.jsonl",
            "session_id": "vacuum-test-sess",
            "message_count": 5,
            "last_human_turns": [],
            "last_touched_files": [],
        }
    )
    memorize_sync(
        content=sentinel_content,
        context=directory,
        tags=["_session_end_sentinel", "session_end"],
    )
    flush_queue()

    # Verify it's there
    result_before = server.project_brief(directory, mode="signals")
    actions_before = [a["action"] for a in result_before.get("recommended_actions", [])]
    assert "extract_last_session_findings" in actions_before

    # Prune with retention = 0 days (prune everything)
    monkeypatch.setenv("SESSION_END_RETENTION_DAYS", "0")
    from yadgar.server.http import _vacuum_stale_sentinels

    _vacuum_stale_sentinels(retention_days=0)
    flush_queue()

    # Should no longer appear
    result_after = server.project_brief(directory, mode="signals")
    actions_after = [a["action"] for a in result_after.get("recommended_actions", [])]
    assert "extract_last_session_findings" not in actions_after


# ===========================================================================
# 9. Config knobs registered
# ===========================================================================


def test_session_end_config_knobs_registered():
    """All 4 SESSION_END_* knobs registered in config_registry (with YADGAR_ prefix)."""
    from yadgar.config_registry import list_config

    names = {e.name for e in list_config()}
    expected = {
        "YADGAR_SESSION_END_CAPTURE_ENABLED",
        "YADGAR_SESSION_END_RETENTION_DAYS",
        "YADGAR_SESSION_END_SNIPPET_TURNS",
        "YADGAR_SESSION_END_MIN_MESSAGES",
    }
    missing = expected - names
    assert not missing, f"Config knobs not registered: {missing}"


def test_session_end_config_knob_defaults():
    """SESSION_END_* knobs have expected default values in Settings."""
    from yadgar.config import get_settings

    cfg = get_settings()
    assert cfg.SESSION_END_CAPTURE_ENABLED is True
    assert cfg.SESSION_END_RETENTION_DAYS == 30
    assert cfg.SESSION_END_SNIPPET_TURNS == 5
    assert cfg.SESSION_END_MIN_MESSAGES == 2


# ===========================================================================
# 10. Hook installation: install_hooks adds SessionEnd entry
# ===========================================================================


def test_install_hooks_registers_session_end(tmp_path):
    """install_hooks_impl with scope=global adds SessionEnd to settings.json."""
    from yadgar.install_hooks_lib import install_hooks_impl

    result = install_hooks_impl(
        home_dir=tmp_path,
        scope="global",
        project_directory=None,
    )
    assert result["status"] == "installed"

    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings.get("hooks", {})
    assert "SessionEnd" in hooks, f"SessionEnd not in hooks: {list(hooks.keys())}"
    # Check it has at least one entry
    se_hooks = hooks["SessionEnd"]
    assert isinstance(se_hooks, list) and len(se_hooks) > 0
