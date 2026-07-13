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
