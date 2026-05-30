"""v5.10.7.1: SKIP_TAGS filter — slash-command tags excluded from last_human_turns.

TDD — written red-first, then session-end-capture.py extended.

Coverage:
- Each new tag (command-name, command-args, local-command-caveat,
  local-command-stdout, local-command-stderr) is filtered from last_human_turns.
- Real human turns (including verbatim typos) survive filtering.
- Transcripts with no slash-command tags return expected turn count unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers (parallel to test_session_end_capture.py — hook runs as subprocess)
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
    """Write a minimal JSONL transcript with the given number of clean human messages."""
    p = tmp_path / "transcript_clean.jsonl"
    lines = []
    for i in range(human_messages):
        lines.append(json.dumps({"message": {"role": "user", "content": f"Human message {i}"}}))
        lines.append(
            json.dumps({"message": {"role": "assistant", "content": f"Assistant reply {i}"}})
        )
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _transcript_with_slash_commands(tmp_path: Path) -> Path:
    """Write a transcript mixing real human turns with slash-command tag noise.

    Turns:
    0. real: "implement the feature"
    1. slash: <command-name>/model</command-name>
    2. slash: <command-args>claude-opus-4-5</command-args>
    3. slash: <local-command-caveat>...</local-command-caveat>
    4. slash: <local-command-stdout>...</local-command-stdout>
    5. slash: <local-command-stderr>...</local-command-stderr>
    6. real: "resotre" (verbatim typo — must survive)
    7. slash: <command-message>...</command-message>  (already skipped pre-v5.10.7.1)
    8. real: "now deploy"
    """
    p = tmp_path / "transcript_slash_commands.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": "implement the feature"}}),
        json.dumps({"message": {"role": "user", "content": "<command-name>/model</command-name>"}}),
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": "<command-args>claude-opus-4-5</command-args>",
                }
            }
        ),
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": "<local-command-caveat>This is a local command that may fail</local-command-caveat>",
                }
            }
        ),
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": "<local-command-stdout>Model: claude-opus-4-5\nContext window: 200000</local-command-stdout>",
                }
            }
        ),
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": "<local-command-stderr>Warning: model changed</local-command-stderr>",
                }
            }
        ),
        json.dumps({"message": {"role": "user", "content": "resotre"}}),
        json.dumps(
            {
                "message": {
                    "role": "user",
                    "content": "<command-message>Running /mcp command...</command-message>",
                }
            }
        ),
        json.dumps({"message": {"role": "user", "content": "now deploy"}}),
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Server fixture (required by test infrastructure even for subprocess-only tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    from yadgar import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ===========================================================================
# v5.10.7.1 — SKIP_TAGS per-tag skip tests
# ===========================================================================


def test_last_human_turns_skips_local_command_caveat(tmp_path):
    """local-command-caveat tag must be filtered from last_human_turns."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _transcript_with_slash_commands(tmp_path)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "skip-caveat-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "10"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel not written"
    record = json.loads(files[0].read_text())
    turns = record["last_human_turns"]
    assert not any("<local-command-caveat>" in t for t in turns), (
        f"<local-command-caveat> leaked into last_human_turns: {turns}"
    )


def test_last_human_turns_skips_local_command_stdout(tmp_path):
    """local-command-stdout tag must be filtered from last_human_turns."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _transcript_with_slash_commands(tmp_path)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "skip-stdout-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "10"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel not written"
    record = json.loads(files[0].read_text())
    turns = record["last_human_turns"]
    assert not any("<local-command-stdout>" in t for t in turns), (
        f"<local-command-stdout> leaked into last_human_turns: {turns}"
    )


def test_last_human_turns_skips_local_command_stderr(tmp_path):
    """local-command-stderr tag must be filtered from last_human_turns."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _transcript_with_slash_commands(tmp_path)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "skip-stderr-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "10"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel not written"
    record = json.loads(files[0].read_text())
    turns = record["last_human_turns"]
    assert not any("<local-command-stderr>" in t for t in turns), (
        f"<local-command-stderr> leaked into last_human_turns: {turns}"
    )


def test_last_human_turns_skips_command_name_and_args(tmp_path):
    """command-name and command-args tags must be filtered from last_human_turns."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _transcript_with_slash_commands(tmp_path)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "skip-name-args-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "10"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel not written"
    record = json.loads(files[0].read_text())
    turns = record["last_human_turns"]
    assert not any("<command-name>" in t for t in turns), (
        f"<command-name> leaked into last_human_turns: {turns}"
    )
    assert not any("<command-args>" in t for t in turns), (
        f"<command-args> leaked into last_human_turns: {turns}"
    )


def test_last_human_turns_preserves_typo_human_message(tmp_path):
    """Real human turn 'resotre' (verbatim typo) must survive slash-command filtering."""
    sentinel_dir = tmp_path / "session-ends"
    transcript = _transcript_with_slash_commands(tmp_path)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "preserve-typo-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "10"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel not written"
    record = json.loads(files[0].read_text())
    turns = record["last_human_turns"]
    assert any("resotre" in t for t in turns), (
        f"Real human turn 'resotre' missing from last_human_turns: {turns}"
    )


def test_last_human_turns_count_unchanged_when_no_slash_commands(tmp_path):
    """Transcript with no slash-command tags returns expected turn count unchanged."""
    sentinel_dir = tmp_path / "session-ends"
    # 4 clean human turns, requesting last 4
    transcript = _minimal_transcript(tmp_path, human_messages=4)
    _run_hook(
        {
            "end_reason": "logout",
            "session_id": "no-slash-test",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        tmp_path,
        env_overrides={"SESSION_END_SNIPPET_TURNS": "4"},
        sentinel_dir=sentinel_dir,
    )
    files = list(sentinel_dir.glob("*.json"))
    assert files, "sentinel not written"
    record = json.loads(files[0].read_text())
    turns = record["last_human_turns"]
    assert len(turns) == 4, f"Expected 4 turns, got {len(turns)}: {turns}"
