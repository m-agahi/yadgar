"""Unit tests for the SHARED PreCompact in-flight capture (host-side parse).

Car fix-drain-inflight (v5.135): the pure parse logic moved from
``yadgar.backend.restoration.transcript_parse`` to
``yadgar._shared.restoration.transcript_parse`` so BOTH the host-side CLI drain
path (``yadgar.core.cli._shared``) and the backend fallback can import it (layer
rule: ``_shared`` is importable by core AND backend; the backend module can no
longer be reached from core).

The combined ``capture_in_flight(transcript_path, directory)`` helper parses the
transcript for in-flight agents/bg-shells AND lists worktrees for ``directory``
on the caller's host — the whole point of the fix: this runs where ``.claude``
transcripts + the git worktree tree are actually visible.
"""

from __future__ import annotations

from pathlib import Path

from yadgar._shared.restoration import transcript_parse as tp

FIXTURE = Path(__file__).parent.parent / "fixtures" / "transcript_in_flight.jsonl"


def test_parse_in_flight_importable_from_shared():
    """The pure parser is reachable at the new _shared home."""
    assert hasattr(tp, "parse_in_flight")
    assert hasattr(tp, "capture_in_flight")


def test_parse_in_flight_agents_from_fixture():
    result = tp.parse_in_flight(str(FIXTURE))
    assert set(result["agents"]) == {"bbbbbbbbbbbbbbbb2", "eeeeeeeeeeeeeeee5"}


def test_parse_in_flight_bg_shells_from_fixture():
    result = tp.parse_in_flight(str(FIXTURE))
    assert "bg_shell_001" in result["bg_shells"]
    assert "bg_done_002" not in result["bg_shells"]


def test_capture_in_flight_parses_and_lists_worktrees(tmp_path):
    """capture_in_flight() = parse (agents/shells) + worktrees for directory.

    The directory here is not a git repo → worktrees degrades to [] (never
    raises), while agents/shells still come from the fixture parse. This is the
    host-side combined helper both drain callers use.
    """
    result = tp.capture_in_flight(str(FIXTURE), str(tmp_path))
    assert set(result["agents"]) == {"bbbbbbbbbbbbbbbb2", "eeeeeeeeeeeeeeee5"}
    assert "bg_shell_001" in result["bg_shells"]
    assert result["worktrees"] == []  # tmp_path is not a git repo
    assert "note" in result


def test_capture_in_flight_lists_real_worktrees():
    """Against a real git repo the worktrees list is non-empty."""
    repo = str(Path(__file__).resolve().parents[3])  # worktree root
    result = tp.capture_in_flight(str(FIXTURE), repo)
    assert isinstance(result["worktrees"], list)
    assert result["worktrees"], "a git checkout must report at least one worktree"


def test_capture_in_flight_none_transcript_empty_agents(tmp_path):
    """None transcript → empty agents/shells (worktrees may still be captured)."""
    result = tp.capture_in_flight(None, str(tmp_path))
    assert result["agents"] == []
    assert result["bg_shells"] == []


def test_list_worktrees_non_git_returns_empty(tmp_path):
    assert tp._list_worktrees(str(tmp_path)) == []


def test_backend_shim_reexports_parser():
    """Back-compat: the old backend import path still resolves to the same fn."""
    from yadgar.backend.restoration.transcript_parse import parse_in_flight

    assert parse_in_flight is tp.parse_in_flight


# --- Car bug-inflight (#72): resume re-activates a completed agent -----------
#
# An agent that async_launched, then COMPLETED on an earlier round, then was
# re-dispatched via SendMessage lands in the transcript as a resume event:
#   toolUseResult = {"success": true,
#                    "message": 'Agent "<id>" had no active task; resumed from
#                                transcript in the background ...'}
# with NO structured status/agentId and NO <task-notification>. The old
# order-blind ``launched - terminal`` subtracts such an agent permanently (it is
# in BOTH sets). The order-aware state machine re-activates it.
#
# These fixtures are written to tmp_path (NOT the shared JSONL) so they do not
# perturb ``test_parse_in_flight_agents_from_fixture``'s {bbbb2, eeee5} assertion.

_LAUNCH = (
    '{{"type":"user","message":{{"role":"user","content":[{{"tool_use_id":'
    '"toolu_{aid}","type":"tool_result","content":[{{"type":"text","text":'
    '"Async agent launched. agentId: {aid}"}}]}}]}},"toolUseResult":'
    '{{"isAsync":true,"status":"async_launched","agentId":"{aid}"}}}}'
)
_COMPLETE = (
    '{{"type":"user","message":{{"role":"user","content":[{{"type":"text",'
    '"text":"<task-notification>\\n<task-id>{aid}</task-id>\\n<status>'
    'completed</status>\\n</task-notification>"}}]}}}}'
)
_RESUME = (
    '{{"type":"user","message":{{"role":"user","content":[{{"type":"text",'
    '"text":"resumed"}}]}},"toolUseResult":{{"success":true,"message":'
    '"Agent \\"{aid}\\" had no active task; resumed from transcript in the '
    "background with your message. You'll be notified when it finishes.\"}}}}"
)


def _write(tmp_path, *lines: str):
    p = tmp_path / "resume_case.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_launch_completed_resume_is_in_flight(tmp_path):
    """THE bug case: launch → completed → resume ⇒ agent IS in-flight.

    The resume re-activates the completed agent. Order-blind subtraction misses
    it; the order-aware state machine surfaces it.
    """
    aid = "a1111111111111111"
    path = _write(
        tmp_path,
        _LAUNCH.format(aid=aid),
        _COMPLETE.format(aid=aid),
        _RESUME.format(aid=aid),
    )
    result = tp.parse_in_flight(path)
    assert aid in result["agents"]


def test_launch_completed_no_resume_not_in_flight(tmp_path):
    """launch → completed (no resume) ⇒ agent NOT in-flight (last state terminal)."""
    aid = "a2222222222222222"
    path = _write(tmp_path, _LAUNCH.format(aid=aid), _COMPLETE.format(aid=aid))
    result = tp.parse_in_flight(path)
    assert aid not in result["agents"]


def test_launch_resume_completed_not_in_flight(tmp_path):
    """launch → resume → completed ⇒ agent NOT in-flight.

    A real terminal notification AFTER a resume correctly re-terminalizes the
    agent — the LAST state wins.
    """
    aid = "a3333333333333333"
    path = _write(
        tmp_path,
        _LAUNCH.format(aid=aid),
        _RESUME.format(aid=aid),
        _COMPLETE.format(aid=aid),
    )
    result = tp.parse_in_flight(path)
    assert aid not in result["agents"]
