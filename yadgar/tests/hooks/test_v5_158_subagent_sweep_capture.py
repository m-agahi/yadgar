"""Tests for car #87 — Stop-hook subagent-transcript sweep (Option A).

The subagent findings-capture loop moved off the dead ``SubagentStop`` trigger
(never fires for ``run_in_background`` Agent dispatches — upstream #33049 /
#25147) onto the main-thread ``Stop`` hook, which sweeps completed-subagent
``.output`` transcript files on disk and posts their ``## Yadgar findings``
footers to the unchanged ``/hooks/subagent-stop`` endpoint.

Covers:
1. A tasks-dir ``.output`` with an N-bullet footer → ONE post with N bullets.
2. Second sweep, unchanged mtime → ZERO posts (dedup).
3. mtime advances + footer present → re-capture (partial-file retry).
4. A ``.output`` with no footer → nothing posted, not marked captured.
5. A non-Agent / empty-report file → no post.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _write_output_transcript(path: Path, final_text: str) -> None:
    """Write a minimal subagent .output JSONL (one user turn + one assistant turn)."""
    lines = [
        {
            "isSidechain": True,
            "type": "user",
            "gitBranch": "feat/multi-client-hooks",
            "message": {"role": "user", "content": "## Task\ndo the thing"},
        },
        {
            "isSidechain": True,
            "type": "assistant",
            "gitBranch": "feat/multi-client-hooks",
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
    "- fact: sweep reads the .output sidechain last assistant turn\n"
    "- fact: dedup keyed on path+mtime\n\n"
    "## Next steps\n\n- ship it\n"
)


class TestSweepCapture:
    def test_sweep_posts_footer_bullets_once(self, tmp_path, monkeypatch):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "abc123-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)

        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        posts: list[dict] = []
        monkeypatch.setattr(
            fc,
            "post_findings",
            lambda agent_type, cwd, findings, branch_hint=None, timeout=3.0: (
                posts.append({"findings": findings, "branch_hint": branch_hint}) or True
            ),
        )

        n = fc.sweep_subagent_transcripts(
            transcript_path, "/home/max/proj", state_path, tasks_root=str(tmp_path)
        )

        assert n == 1, "one transcript with a footer should post once"
        assert len(posts) == 1
        assert len(posts[0]["findings"]) == 2, "both footer bullets extracted"
        assert any("sweep reads" in f for f in posts[0]["findings"])
        assert posts[0]["branch_hint"] == "feat/multi-client-hooks"

    def test_second_sweep_unchanged_mtime_no_repost(self, tmp_path, monkeypatch):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "dedup-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", _FOOTER_2)
        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        posts: list[dict] = []
        monkeypatch.setattr(
            fc,
            "post_findings",
            lambda *a, **kw: posts.append(kw or a) or True,
        )

        n1 = fc.sweep_subagent_transcripts(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        n2 = fc.sweep_subagent_transcripts(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert n1 == 1
        assert n2 == 0, "unchanged mtime must not re-post"
        assert len(posts) == 1

    def test_mtime_advance_reposts_partial_then_footer(self, tmp_path, monkeypatch):
        """A file captured as no-footer (partial) must re-post once it grows a footer."""
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "partial-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        out = tasks / "agentA.output"
        # First: no footer yet (agent still running)
        _write_output_transcript(out, "Working on it, no footer yet.\n")
        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        posts: list[list] = []
        monkeypatch.setattr(
            fc,
            "post_findings",
            lambda agent_type, cwd, findings, branch_hint=None, timeout=3.0: (
                posts.append(findings) or True
            ),
        )

        n1 = fc.sweep_subagent_transcripts(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert n1 == 0, "no footer → no post"
        assert posts == []

        # Now the agent finishes: rewrite with a footer + advance mtime.
        _write_output_transcript(out, _FOOTER_2)
        os.utime(out, (out.stat().st_atime + 10, out.stat().st_mtime + 10))

        n2 = fc.sweep_subagent_transcripts(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert n2 == 1, "footer now present + mtime advanced → capture"
        assert len(posts) == 1
        assert len(posts[0]) == 2

    def test_no_footer_file_does_nothing(self, tmp_path, monkeypatch):
        from yadgar.core.hooks import findings_capture as fc

        session_uuid = "nofooter-session"
        tasks = _make_tasks_tree(tmp_path, session_uuid)
        _write_output_transcript(tasks / "agentA.output", "Just a summary.\n\n## Summary\ndone\n")
        transcript_path = str(tmp_path / f"{session_uuid}.jsonl")
        state_path = str(tmp_path / "sweep-state.json")

        posts: list = []
        monkeypatch.setattr(fc, "post_findings", lambda *a, **kw: posts.append(1) or True)

        n = fc.sweep_subagent_transcripts(
            transcript_path, "/proj", state_path, tasks_root=str(tmp_path)
        )
        assert n == 0
        assert posts == []

    def test_no_transcript_path_no_op(self, tmp_path, monkeypatch):
        from yadgar.core.hooks import findings_capture as fc

        posts: list = []
        monkeypatch.setattr(fc, "post_findings", lambda *a, **kw: posts.append(1) or True)
        n = fc.sweep_subagent_transcripts(
            "", "/proj", str(tmp_path / "s.json"), tasks_root=str(tmp_path)
        )
        assert n == 0
        assert posts == []


class TestSharedHelperReexport:
    """subagent_stop.py must keep exposing the extract/post helpers (back-compat)."""

    def test_extract_findings_reexported(self):
        from yadgar.core.hooks.subagent_stop import _extract_findings

        report = "## Yadgar findings\n- fact: still importable\n"
        assert _extract_findings(report) == ["fact: still importable"]

    def test_post_findings_reexported(self, monkeypatch):
        from yadgar.core.hooks import subagent_stop as _hs

        captured = {}

        class _R:
            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode())
            return _R()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        _hs._post_findings("general-purpose", "/tmp/proj", ["fact: x"])
        assert captured["url"].endswith("/hooks/subagent-stop")
        assert "fact: x" in captured["data"]["findings"]


class TestStopHookSweepWiring:
    """stop-memory-checkpoint main() runs the sweep on EVERY stop, before gates."""

    def test_sweep_runs_even_when_interval_not_reached(self, tmp_path, monkeypatch):
        """The interval gate returns early on ~24/25 stops; the sweep must still run."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "stop_memory_checkpoint",
            str(
                Path(__file__).resolve().parents[2] / "core" / "hooks" / "stop-memory-checkpoint.py"
            ),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        called = {"n": 0}
        monkeypatch.setattr(
            mod,
            "_run_subagent_sweep",
            lambda data: called.__setitem__("n", called["n"] + 1),
        )

        # A transcript that exists but is BELOW the checkpoint interval → early return.
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n", encoding="utf-8"
        )
        payload = json.dumps(
            {"session_id": "s1", "transcript_path": str(transcript), "stop_hook_active": False}
        )

        import io
        import sys as _sys

        old = _sys.stdin
        _sys.stdin = io.StringIO(payload)
        try:
            mod.main()
        finally:
            _sys.stdin = old

        assert called["n"] == 1, "sweep must run on every stop, not just at the checkpoint interval"

    def test_sweep_runs_when_stop_hook_active(self, tmp_path, monkeypatch):
        """Even the terminal stop_hook_active=true stop must sweep (last agent lives there)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "stop_memory_checkpoint2",
            str(
                Path(__file__).resolve().parents[2] / "core" / "hooks" / "stop-memory-checkpoint.py"
            ),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        called = {"n": 0}
        monkeypatch.setattr(
            mod,
            "_run_subagent_sweep",
            lambda data: called.__setitem__("n", called["n"] + 1),
        )

        transcript = tmp_path / "t.jsonl"
        transcript.write_text("", encoding="utf-8")
        payload = json.dumps(
            {"session_id": "s1", "transcript_path": str(transcript), "stop_hook_active": True}
        )

        import io
        import sys as _sys

        old = _sys.stdin
        _sys.stdin = io.StringIO(payload)
        try:
            mod.main()
        finally:
            _sys.stdin = old

        assert called["n"] == 1, "sweep must run on stop_hook_active stops too"
