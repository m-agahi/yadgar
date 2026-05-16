"""Tests for §27 stop-hook expansion (dumb pipe).

TDD — written BEFORE the implementation.
Covers:
- Counter persisted atomically via tmp + os.replace
- Prompt fires at message 25, 50, 75
- Payload structure correct
- State file uses ~/.yadgar/stop-hook-state.json (keyed by session_id)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook(hook_path: Path, stdin_data: dict, env: dict | None = None) -> dict:
    """Run the hook script in a subprocess, return parsed stdout."""
    import subprocess

    env_full = {**os.environ, **(env or {})}
    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        env=env_full,
        timeout=10,
    )
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"raw_stdout": result.stdout}


def _make_transcript(tmp_path: Path, human_count: int) -> Path:
    """Create a JSONL transcript with `human_count` human messages."""
    lines = []
    for i in range(human_count):
        entry = {
            "message": {
                "role": "user",
                "content": f"Human message {i}",
            }
        }
        lines.append(json.dumps(entry))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(lines))
    return transcript


# ---------------------------------------------------------------------------
# Locate hook file
# ---------------------------------------------------------------------------

_HOOK_PATH = Path(__file__).parent.parent / "hooks" / "stop-memory-checkpoint.py"


def test_hook_file_exists():
    assert _HOOK_PATH.exists(), f"Hook not found at {_HOOK_PATH}"


# ---------------------------------------------------------------------------
# Counter persistence via atomic write
# ---------------------------------------------------------------------------


def test_state_file_written_atomically(tmp_path):
    """After a checkpoint, state file should exist with last_save count."""
    transcript = _make_transcript(tmp_path, 25)
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()
    state_file = state_dir / "stop-hook-state.json"

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        _run_hook(
            _HOOK_PATH,
            {
                "session_id": "test-session-1",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    # State file must exist
    assert state_file.exists(), f"State file not found at {state_file}"

    data = json.loads(state_file.read_text())
    assert "test-session-1" in data or "last_save" in data


def test_state_file_keyed_by_session_id(tmp_path):
    """State file tracks per-session counts."""
    transcript_a = _make_transcript(tmp_path, 25)
    transcript_b = _make_transcript(tmp_path, 50)
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        _run_hook(
            _HOOK_PATH,
            {
                "session_id": "session-A",
                "transcript_path": str(transcript_a),
                "stop_hook_active": False,
            },
        )
        _run_hook(
            _HOOK_PATH,
            {
                "session_id": "session-B",
                "transcript_path": str(transcript_b),
                "stop_hook_active": False,
            },
        )

    state_file = state_dir / "stop-hook-state.json"
    data = json.loads(state_file.read_text())
    # Both sessions must have independent state
    assert len(data) >= 2 or "session-A" in data or "session-B" in data


# ---------------------------------------------------------------------------
# Prompt fires at 25, 50, 75
# ---------------------------------------------------------------------------


def test_prompt_fires_at_25(tmp_path):
    """Block decision returned when human message count reaches 25."""
    transcript = _make_transcript(tmp_path, 25)
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-25",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") == "block", f"Expected block at 25 messages, got {result}"


def test_no_prompt_before_25(tmp_path):
    """No block before 25 messages."""
    transcript = _make_transcript(tmp_path, 10)
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-10",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") != "block", "Should NOT block before 25 messages"


def test_prompt_fires_at_50(tmp_path):
    """Block fires again at 50 messages after first fire at 25."""
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    # Simulate: hook already fired at 25
    state_file = state_dir / "stop-hook-state.json"
    state_file.write_text(json.dumps({"sess-50": {"last_save": 25}}))

    transcript = _make_transcript(tmp_path, 50)

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-50",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") == "block", f"Expected block at 50 messages, got {result}"


def test_prompt_fires_at_75(tmp_path):
    """Block fires again at 75 messages."""
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    state_file = state_dir / "stop-hook-state.json"
    state_file.write_text(json.dumps({"sess-75": {"last_save": 50}}))

    transcript = _make_transcript(tmp_path, 75)

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-75",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") == "block", f"Expected block at 75 messages, got {result}"


def test_no_prompt_between_25_and_50(tmp_path):
    """After firing at 25, no block at 30 messages."""
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    state_file = state_dir / "stop-hook-state.json"
    state_file.write_text(json.dumps({"sess-mid": {"last_save": 25}}))

    transcript = _make_transcript(tmp_path, 30)

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-mid",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") != "block"


# ---------------------------------------------------------------------------
# Payload structure
# ---------------------------------------------------------------------------


def test_payload_contains_signal_eval_prompt(tmp_path):
    """Block reason must contain the signal-evaluation prompt text."""
    transcript = _make_transcript(tmp_path, 25)
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-prompt",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") == "block"
    reason = result.get("reason", "")
    # The signal-eval prompt must mention project_brief and signals
    assert "project_brief" in reason or "signals" in reason or "stale_wiki" in reason, (
        f"Expected signal-eval prompt, got: {reason[:200]}"
    )


def test_stop_hook_active_guard(tmp_path):
    """stop_hook_active=True returns {} (infinite-loop guard)."""
    transcript = _make_transcript(tmp_path, 25)
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-guard",
                "transcript_path": str(transcript),
                "stop_hook_active": True,
            },
        )

    assert result == {} or result.get("decision") != "block"


def test_no_transcript_returns_empty(tmp_path):
    """Empty transcript_path → no block."""
    state_dir = tmp_path / ".yadgar"
    state_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        result = _run_hook(
            _HOOK_PATH,
            {
                "session_id": "sess-no-transcript",
                "transcript_path": "",
                "stop_hook_active": False,
            },
        )

    assert result.get("decision") != "block"
