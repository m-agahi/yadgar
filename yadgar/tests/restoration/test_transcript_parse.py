"""Unit tests for the PreCompact transcript in-flight parser.

TDD — written BEFORE the implementation.

The parser reads a Claude Code session JSONL and computes the orchestration
state that is in flight at compaction time:

    in_flight = launched - terminal

where
  launched = agentIds carried by a toolUseResult with status == "async_launched"
             (background dispatches ONLY — foreground/synchronous agents echo an
             `agentId:` token in a completed non-async result and MUST be excluded)
  terminal = task-ids from <task-notification> blocks whose <status> is terminal
             ∈ {completed, failed, killed, stopped} (NOT `running` — a running
             agent is still in flight)

Shapes verified against real transcripts (2026-07-13):
  - launch ack:      toolUseResult.status == "async_launched", toolUseResult.agentId
  - completion:      <task-notification><task-id>X</task-id><status>...</status>
  - foreground:      toolUseResult.agentId + status == "completed", no async_launched
  - bg bash:         toolUseResult.backgroundTaskId
  - status values:   completed / failed / killed / stopped / running
"""

from __future__ import annotations

from pathlib import Path

from yadgar.backend.restoration.transcript_parse import parse_in_flight

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transcript_in_flight.jsonl"


def test_fixture_exists():
    assert FIXTURE.is_file(), f"fixture missing: {FIXTURE}"


def test_in_flight_agents_are_launched_minus_terminal():
    """Only background agents with NO terminal notification are in-flight.
    Fixture: B (no terminal) and E (running-only) are in-flight;
    A (completed), C (killed) are terminal; D (foreground) is excluded."""
    result = parse_in_flight(str(FIXTURE))
    assert set(result["agents"]) == {"bbbbbbbbbbbbbbbb2", "eeeeeeeeeeeeeeee5"}


def test_foreground_agent_excluded():
    """The crux regression: a foreground/synchronous agent that echoes
    `agentId:` in a completed non-async toolUseResult MUST NOT appear in-flight."""
    result = parse_in_flight(str(FIXTURE))
    assert "dddddddddddddddd4" not in result["agents"]


def test_completed_agent_not_in_flight():
    """An async_launched agent WITH a completed notification is terminal."""
    result = parse_in_flight(str(FIXTURE))
    assert "aaaaaaaaaaaaaaaa1" not in result["agents"]


def test_killed_agent_counts_as_terminal():
    """A `killed` status is terminal — the agent finished, not in-flight."""
    result = parse_in_flight(str(FIXTURE))
    assert "cccccccccccccccc3" not in result["agents"]


def test_running_status_stays_in_flight():
    """A `running` status is NOT terminal — the agent is still in flight."""
    result = parse_in_flight(str(FIXTURE))
    assert "eeeeeeeeeeeeeeee5" in result["agents"]


def test_bg_bash_shell_captured():
    """run_in_background bash with NO terminal notification → captured in-flight."""
    result = parse_in_flight(str(FIXTURE))
    assert "bg_shell_001" in result["bg_shells"]


def test_completed_bg_shell_subtracted():
    """A bg-bash that emitted a completed <task-notification> (task-id == shell id,
    via a queue-operation top-level content block) must NOT be surfaced as
    in-flight. Verified on real transcripts: 242/251 shells carry a terminal
    notification through the same channel as agents."""
    result = parse_in_flight(str(FIXTURE))
    assert "bg_done_002" not in result["bg_shells"]


def test_queue_operation_notification_reached():
    """The completed notification for bg_done_002 lives in a `queue-operation`
    top-level `content` string — proving the shape-agnostic walk reaches
    notifications outside message.content (the real-transcript traversal bug)."""
    result = parse_in_flight(str(FIXTURE))
    # If the queue-operation notification were missed, bg_done_002 would leak in.
    assert "bg_done_002" not in result["bg_shells"]


def test_malformed_line_skipped():
    """A non-JSON line must be skipped without raising."""
    result = parse_in_flight(str(FIXTURE))
    # If the malformed line raised, we would never reach a populated result.
    assert result["agents"]  # non-empty proves the parse ran to completion


def test_terminal_without_launch_ignored():
    """A <task-notification> for an id that was never async_launched is ignored
    (not added to in-flight, not an error). Fixture id 6 has no launch."""
    result = parse_in_flight(str(FIXTURE))
    assert "ffffffffffffffff6" not in result["agents"]


def test_missing_file_returns_empty():
    """A non-existent transcript path degrades to an empty result, never raises."""
    result = parse_in_flight("/nonexistent/path/does-not-exist.jsonl")
    assert result["agents"] == []
    assert result["bg_shells"] == []


def test_none_path_returns_empty():
    """transcript_path=None → empty result (back-compat degrade)."""
    result = parse_in_flight(None)
    assert result["agents"] == []


def test_result_has_note_and_shape():
    """Result carries the liveness caveat and the expected keys."""
    result = parse_in_flight(str(FIXTURE))
    assert "agents" in result
    assert "bg_shells" in result
    assert "note" in result
    assert "liveness" in result["note"].lower() or "verify" in result["note"].lower()
