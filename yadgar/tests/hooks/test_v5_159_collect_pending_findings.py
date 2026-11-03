"""Tests for Car A (ADR-0156) — collect / list_pending_findings + advance_listed_state + CLI.

TDD: these tests were written BEFORE the implementation.

Covers:
1. Fixture tasks-dir with a footer → collect returns the bullets, no state written.
2. No footer → collect returns [] for that transcript.
3. After LIST + advance_listed_state, second collect returns [].
4. No network: collect must not call post_findings.
5. CLI --json emits valid JSON list.
6. CLI LIST then --advance-state writes the state file and re-collect returns [].
7. CLI absent/bad transcript → empty output, exit 0.
8. Advance consumes exactly the LISTED set + mtimes (no re-scan race).
9. State save prunes keys whose path is gone.
10. Tasks roots follow the harness tmp dir (CLAUDE_CODE_TMPDIR, TMPDIR/TMP/TEMP, /tmp).
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
    """advance_listed_state: writes dedup state so next collect returns []."""

    def test_advance_then_collect_returns_empty(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "advance-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        # First LIST returns results (and snapshots them).
        results1 = fc.list_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert len(results1) == 1

        # Advance exactly the listed set.
        fc.advance_listed_state(transcript_path, state_path)

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

        results = fc.list_pending_findings(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert results

        fc.advance_listed_state(transcript_path, state_path)
        assert Path(state_path).exists(), "advance must write the state file"
        state = json.loads(Path(state_path).read_text())
        assert any(key.endswith("agentA.output") for key in state), "state keyed by output path"

    def test_advance_without_snapshot_is_noop(self, tmp_path):
        """advance with no prior LIST must not crash and must write nothing."""
        from yadgar.core.hooks import findings_capture as fc

        state_path = str(tmp_path / "sweep-state.json")
        transcript_path = str(tmp_path / "no-list-session.jsonl")
        assert fc.advance_listed_state(transcript_path, state_path) == 0
        assert not Path(state_path).exists(), "no snapshot -> no state write"


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
        # LIST first: advance consumes exactly what LIST showed.
        listed = subprocess.run(
            [
                sys.executable,
                "-m",
                "yadgar",
                "pending-findings",
                "--transcript-path",
                str(transcript),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert listed.returncode == 0, listed.stderr
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


# ── advance consumes exactly the LISTED set (no re-scan race) ─────────────────


def _list(fc, transcript_path: str, state_path: str, root: Path) -> list[dict]:
    return fc.list_pending_findings(transcript_path, "/proj", state_path, tasks_root=str(root))


def _collect(fc, transcript_path: str, state_path: str, root: Path) -> list[dict]:
    return fc.collect_pending_findings(transcript_path, "/proj", state_path, tasks_root=str(root))


class TestAdvanceConsumesListedSet:
    def test_output_added_after_list_is_not_advanced(self, tmp_path):
        """An agent that finishes between LIST and advance stays pending."""
        from yadgar.core.hooks import findings_capture as fc

        tasks = _make_tasks_tree(tmp_path, "race-session")
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript_path = str(tmp_path / "race-session.jsonl")
        state_path = str(tmp_path / "state" / "sweep-state.json")

        listed = _list(fc, transcript_path, state_path, tmp_path)
        assert [Path(e["transcript_path"]).name for e in listed] == ["agentA.output"]

        # agentB finishes AFTER the LIST the curator saw.
        _write_output_transcript(tasks / "agentB.output", _FOOTER_2)
        assert fc.advance_listed_state(transcript_path, state_path) == 1

        state = json.loads(Path(state_path).read_text())
        assert str(tasks / "agentB.output") not in state, "unlisted path must not be advanced"
        after = _collect(fc, transcript_path, state_path, tmp_path)
        assert [Path(e["transcript_path"]).name for e in after] == ["agentB.output"]

    def test_advance_records_listed_mtime_not_current(self, tmp_path):
        """A target that grows after LIST re-lists on the next cadence."""
        from yadgar.core.hooks import findings_capture as fc

        tasks = _make_tasks_tree(tmp_path, "grow-session")
        out = tasks / "agentA.output"
        _write_output_transcript(out, _FOOTER_2)
        os.utime(out, (1_000_000, 1_000_000))
        transcript_path = str(tmp_path / "grow-session.jsonl")
        state_path = str(tmp_path / "state" / "sweep-state.json")

        assert len(_list(fc, transcript_path, state_path, tmp_path)) == 1
        # The transcript grows after LIST (a new footer lands).
        os.utime(out, (2_000_000, 2_000_000))
        fc.advance_listed_state(transcript_path, state_path)

        state = json.loads(Path(state_path).read_text())
        assert state[str(out)] == 1_000_000, "state must hold the mtime LIST saw"
        assert len(_collect(fc, transcript_path, state_path, tmp_path)) == 1

    def test_advance_removes_snapshot_and_second_advance_is_noop(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        tasks = _make_tasks_tree(tmp_path, "once-session")
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript_path = str(tmp_path / "once-session.jsonl")
        state_path = str(tmp_path / "state" / "sweep-state.json")

        _list(fc, transcript_path, state_path, tmp_path)
        snap = Path(fc._listed_snapshot_path(state_path, "once-session"))
        assert snap.exists(), "LIST must write the per-session snapshot"
        assert fc.advance_listed_state(transcript_path, state_path) == 1
        assert not snap.exists(), "advance must remove the snapshot"
        assert fc.advance_listed_state(transcript_path, state_path) == 0

    def test_empty_list_deletes_stale_snapshot(self, tmp_path):
        """A later empty LIST must not leave an older listed set to be consumed."""
        from yadgar.core.hooks import findings_capture as fc

        tasks = _make_tasks_tree(tmp_path, "stale-session")
        out = tasks / "agentA.output"
        _write_output_transcript(out, _FOOTER_2)
        transcript_path = str(tmp_path / "stale-session.jsonl")
        state_path = str(tmp_path / "state" / "sweep-state.json")

        _list(fc, transcript_path, state_path, tmp_path)
        out.unlink()
        assert _list(fc, transcript_path, state_path, tmp_path) == []
        snap = Path(fc._listed_snapshot_path(state_path, "stale-session"))
        assert not snap.exists()

    def test_collect_writes_no_snapshot(self, tmp_path):
        """collect_pending_findings stays pure (session-end-capture calls it)."""
        from yadgar.core.hooks import findings_capture as fc

        tasks = _make_tasks_tree(tmp_path, "pure-session")
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript_path = str(tmp_path / "pure-session.jsonl")
        state_path = str(tmp_path / "state" / "sweep-state.json")

        assert len(_collect(fc, transcript_path, state_path, tmp_path)) == 1
        assert not Path(fc._listed_snapshot_path(state_path, "pure-session")).exists()


class TestStatePruning:
    def test_save_drops_keys_whose_path_is_gone(self, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        live = tmp_path / "live.output"
        live.write_text("x", encoding="utf-8")
        dangling = tmp_path / "dangling.output"
        dangling.symlink_to(tmp_path / "missing-target.jsonl")
        state_path = str(tmp_path / "state.json")

        fc._save_sweep_state(
            state_path,
            {str(live): 1.0, str(dangling): 2.0, str(tmp_path / "gone.output"): 3.0},
        )
        state = json.loads(Path(state_path).read_text())
        assert state == {str(live): 1.0, str(dangling): 2.0}, "only vanished paths are pruned"


class TestCLIListThenAdvance:
    def test_cli_list_then_advance_end_to_end(self, tmp_path):
        import subprocess
        import sys

        from yadgar.core.hooks import findings_capture as fc

        tasks = _make_tasks_tree(tmp_path, "cli-e2e-session")
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript = tmp_path / "cli-e2e-session.jsonl"
        transcript.write_text("", encoding="utf-8")
        env = {**os.environ, "YADGAR_TASKS_ROOT": str(tmp_path)}
        base = [sys.executable, "-m", "yadgar", "pending-findings"]

        listed = subprocess.run(
            [*base, "--transcript-path", str(transcript), "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert listed.returncode == 0, listed.stderr
        assert len(json.loads(listed.stdout)) == 1

        _write_output_transcript(tasks / "agentB.output", _FOOTER_2)
        adv = subprocess.run(
            [*base, "--transcript-path", str(transcript), "--advance-state"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert adv.returncode == 0, adv.stderr

        state_path = fc._default_sweep_state_path()
        after = _collect(fc, str(transcript), state_path, tmp_path)
        assert [Path(e["transcript_path"]).name for e in after] == ["agentB.output"]


# ── tasks root follows the harness tmp dir ────────────────────────────────────


_ROOT_ENV = ("CLAUDE_CODE_TMPDIR", "TMPDIR", "TMP", "TEMP")


def _clear_root_env(monkeypatch) -> None:
    for name in _ROOT_ENV:
        monkeypatch.delenv(name, raising=False)


class TestTasksRoots:
    def test_fallback_is_tmp(self, monkeypatch):
        from yadgar.core.hooks import findings_capture as fc

        _clear_root_env(monkeypatch)
        assert fc._tasks_roots_default() == ["/tmp"]

    def test_claude_code_tmpdir_first(self, monkeypatch, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        _clear_root_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(tmp_path / "cc"))
        monkeypatch.setenv("TMPDIR", str(tmp_path / "td"))
        assert fc._tasks_roots_default() == [str(tmp_path / "cc"), str(tmp_path / "td"), "/tmp"]

    def test_tmpdir_then_tmp_then_temp(self, monkeypatch, tmp_path):
        """Node os.tmpdir() order: TMPDIR, then TMP, then TEMP (first set wins)."""
        from yadgar.core.hooks import findings_capture as fc

        _clear_root_env(monkeypatch)
        monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
        assert fc._tasks_roots_default() == [str(tmp_path / "temp"), "/tmp"]
        monkeypatch.setenv("TMP", str(tmp_path / "tmp1"))
        assert fc._tasks_roots_default() == [str(tmp_path / "tmp1"), "/tmp"]
        monkeypatch.setenv("TMPDIR", str(tmp_path / "td") + "/")
        assert fc._tasks_roots_default() == [str(tmp_path / "td"), "/tmp"]

    def test_roots_deduped_by_realpath(self, monkeypatch, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        _clear_root_env(monkeypatch)
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "alias").symlink_to(real)
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(real))
        monkeypatch.setenv("TMPDIR", str(tmp_path / "alias"))
        assert fc._tasks_roots_default() == [str(real), "/tmp"]

    def test_collect_globs_every_default_root(self, monkeypatch, tmp_path):
        """A sandboxed Bash child sees another TMPDIR than the harness: union, not first hit."""
        from yadgar.core.hooks import findings_capture as fc

        _clear_root_env(monkeypatch)
        cc_root, td_root = tmp_path / "cc", tmp_path / "td"
        _write_output_transcript(_make_tasks_tree(cc_root, "u-session") / "a1.output", _FOOTER_2)
        _write_output_transcript(_make_tasks_tree(td_root, "u-session") / "a2.output", _FOOTER_2)
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(cc_root))
        monkeypatch.setenv("TMPDIR", str(td_root))

        found = fc.collect_pending_findings(
            str(tmp_path / "u-session.jsonl"), "/proj", str(tmp_path / "s.json")
        )
        assert sorted(Path(e["transcript_path"]).name for e in found) == ["a1.output", "a2.output"]

    def test_explicit_tasks_root_is_exclusive(self, monkeypatch, tmp_path):
        from yadgar.core.hooks import findings_capture as fc

        _clear_root_env(monkeypatch)
        other = tmp_path / "other"
        _write_output_transcript(_make_tasks_tree(other, "x-session") / "a1.output", _FOOTER_2)
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(other))
        pinned = tmp_path / "pinned"
        pinned.mkdir()

        found = fc.collect_pending_findings(
            str(tmp_path / "x-session.jsonl"),
            "/proj",
            str(tmp_path / "s.json"),
            tasks_root=str(pinned),
        )
        assert found == []
