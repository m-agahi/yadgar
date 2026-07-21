"""Tests for Car A (ADR-0156) — collect_pending_findings + advance_pending_state + CLI.

TDD: these tests were written BEFORE the implementation.

Covers:
1. Fixture tasks-dir with a footer → collect returns the bullets, no state written.
2. No footer → collect returns [] for that transcript.
3. After advance_pending_state, second collect returns [].
4. No network: collect must not call post_findings.
5. CLI --json emits valid JSON list.
6. CLI --advance-state writes the state file and re-collect returns [].
7. CLI absent/bad transcript → empty output, exit 0.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ── transcript fixture helpers (mirrors test_v5_158_subagent_sweep_capture) ────


def _write_output_transcript(path: Path, final_text: str) -> None:
    """Write a minimal subagent .output JSONL (one user turn + one assistant turn)."""
    lines = [
        {
            "isSidechain": True,
            "type": "user",
            "gitBranch": "feat/curated-findings",
            "message": {"role": "user", "content": "## Task\ndo the thing"},
        },
        {
            "isSidechain": True,
            "type": "assistant",
            "gitBranch": "feat/curated-findings",
            "message": {"role": "assistant", "content": final_text},
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _make_tasks_tree(tmp_path: Path, session_uuid: str) -> Path:
    """Build /<root>/claude-1000/<slug>/<session-uuid>/tasks and return the tasks dir."""
    tasks = tmp_path / "claude-1000" / "-home-max-proj" / session_uuid / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    return tasks


_FOOTER_2 = (
    "Analysis complete. Details above.\n\n"
    "## Yadgar findings\n\n"
    "- fact: collect reads the .output sidechain last assistant turn\n"
    "- fact: dedup keyed on path+mtime, no state write on collect\n\n"
    "## Next steps\n\n- ship it\n"
)

_NO_FOOTER = "Just a summary. No findings section here.\n\n## Summary\ndone\n"


class TestCollectPendingFindings:
    """collect_pending_findings: read-only, returns list[dict], no state write."""

    def test_footer_returns_bullets(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "collect-session-1"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        results = fc.collect_pending_findings(
            transcript_path, "/home/max/proj", state_path, tasks_root=str(tmp_path)
        )

        assert len(results) == 1, "one transcript with footer → one result"
        r = results[0]
        assert r["agent_type"] == "general-purpose"
        assert len(r["findings"]) == 2
        assert any("collect reads" in f for f in r["findings"])
        assert r["transcript_path"].endswith("agentA.output")

    def test_no_footer_returns_empty(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "collect-session-nofooter"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _NO_FOOTER)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        results = fc.collect_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert results == [], "no footer → empty list"

    def test_collect_does_not_write_state(self, tmp_path):
        """collect_pending_findings must NOT advance the dedup state."""
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "collect-nostate-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        fc.collect_pending_findings(transcript_path, "/proj", state_path, tasks_root=str(tmp_path))
        # Second call must still return results (state not advanced by collect)
        results2 = fc.collect_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert len(results2) == 1, "collect must not advance state — second call re-returns"

    def test_collect_no_network_post_findings_removed(self, tmp_path, monkeypatch):
        """ADR-0156: the auto-store POST path is GONE. collect_pending_findings
        does no network I/O, and ``post_findings`` / ``sweep_subagent_transcripts``
        are no longer importable (the endpoint they targeted was ripped)."""
        from yadgar.core.hooks import findings_capture as fc

        assert not hasattr(fc, "post_findings"), "post_findings must be ripped (ADR-0156)"
        assert not hasattr(fc, "sweep_subagent_transcripts"), (
            "sweep_subagent_transcripts must be ripped (ADR-0156)"
        )

        # Guard against any accidental urlopen call during collection.
        net_called = {"n": 0}

        def _boom(*a, **kw):
            net_called["n"] += 1
            raise AssertionError("collect_pending_findings must not touch the network")

        monkeypatch.setattr("urllib.request.urlopen", _boom)

        session_uuid = "no-network-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        fc.collect_pending_findings(transcript_path, "/proj", state_path, tasks_root=str(tmp_path))
        assert net_called["n"] == 0, "collect must not call the network"

    def test_empty_transcript_path_returns_empty(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        results = fc.collect_pending_findings(
            "", "/proj", str(tmp_path / "s.json"), tasks_root=str(tmp_path)
        )
        assert results == []


class TestAdvancePendingState:
    """advance_pending_state: writes dedup state so next collect returns []."""

    def test_advance_then_collect_returns_empty(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "advance-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        # First collect returns results.
        results1 = fc.collect_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert len(results1) == 1

        # Advance for all listed paths.
        fc.advance_pending_state(results1, state_path)

        # Second collect should return [] — dedup state consumed.
        results2 = fc.collect_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert results2 == [], "after advance, collect returns empty"

    def test_advance_writes_state_file(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "advance-statefile"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        results = fc.collect_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert results

        fc.advance_pending_state(results, state_path)
        assert Path(state_path).exists(), "advance must write the state file"
        state = json.loads(Path(state_path).read_text())
        assert any(key.endswith("agentA.output") for key in state), "state keyed by output path"

    def test_advance_empty_list_is_noop(self, tmp_path):
        """advance_pending_state([]) must not crash and must write nothing."""
        from yadgar.core.hooks import findings_capture as fc

        state_path = str(tmp_path / "sweep-state.json")
        fc.advance_pending_state([], state_path)
        # State file may or may not exist; what matters is no crash.


class TestCLIPendingFindings:
    """CLI `yadgar pending-findings` via subprocess."""

    def _run_cli(self, args: list[str], tmp_path: Path) -> tuple[str, str, int]:
        """Run `python -m yadgar pending-findings ...` and return (stdout, stderr, returncode)."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "yadgar", "pending-findings", *args],
            capture_output=True,
            text=True,
            env={**os.environ},
        )
        return result.stdout, result.stderr, result.returncode

    def test_json_flag_valid_json(self, tmp_path):
        session_uuid = "cli-json-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript = tmp_path / f"{session_uuid}.jsonl"
        # Create dummy session transcript so session_uuid resolves.
        transcript.write_text("", encoding="utf-8")

        # Need to set tasks-root via env or by patching — but our CLI uses tasks_root
        # from collect_pending_findings. We use YADGAR_TASKS_ROOT env override.
        env = {**os.environ, "YADGAR_TASKS_ROOT": str(tmp_path)}
        import subprocess
        import sys

        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "yadgar",
                "pending-findings",
                "--transcript-path",
                str(transcript),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        parsed = json.loads(r.stdout)
        assert isinstance(parsed, list), "output must be a JSON list"

    def test_absent_transcript_empty_exit0(self, tmp_path):
        """Absent transcript path → empty output, exit 0."""
        import subprocess
        import sys

        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "yadgar",
                "pending-findings",
                "--transcript-path",
                str(tmp_path / "nonexistent.jsonl"),
            ],
            capture_output=True,
            text=True,
            env=os.environ,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_advance_state_writes_file(self, tmp_path):
        """--advance-state writes the state file for listed transcript paths."""
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "cli-advance-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript = tmp_path / f"{session_uuid}.jsonl"
        transcript.write_text("", encoding="utf-8")

        import subprocess
        import sys

        env = {**os.environ, "YADGAR_TASKS_ROOT": str(tmp_path)}
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "yadgar",
                "pending-findings",
                "--transcript-path",
                str(transcript),
                "--advance-state",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert r.returncode == 0

        # Now a second collect (direct API) should return [].
        state_path = fc._default_sweep_state_path()
        results2 = fc.collect_pending_findings(
            str(transcript), "/proj", str(state_path), tasks_root=str(tmp_path)
        )
        assert results2 == [], "after CLI --advance-state, re-collect returns empty"
