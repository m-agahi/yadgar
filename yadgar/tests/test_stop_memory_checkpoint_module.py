"""Tests for yadgar/hooks/stop-memory-checkpoint.py — stop hook checkpoint logic.

Wave 3 coverage: yadgar/hooks/stop-memory-checkpoint.py (~80 stmts, 0% pre-wave).
Strategy: load module via importlib.util.module_from_spec. Patch yadgar.paths.
Test _count_human_messages, _load_state, _save_state, and main().
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_HOOK_PATH = Path(__file__).parent.parent / "core" / "hooks" / "stop-memory-checkpoint.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("stop_memory_checkpoint", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _count_human_messages
# ---------------------------------------------------------------------------


class TestCountHumanMessages:
    def test_empty_file_returns_zero(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text("")
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 0

    def test_missing_file_returns_zero(self, tmp_path):
        mod = _load_module()
        assert mod._count_human_messages(str(tmp_path / "no_such.jsonl")) == 0

    def test_counts_user_turns(self, tmp_path):
        lines = [
            json.dumps({"role": "user", "content": "hello"}),
            json.dumps({"role": "assistant", "content": "hi"}),
            json.dumps({"role": "user", "content": "what is 2+2?"}),
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(lines))
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 2

    def test_skips_system_reminder_content(self, tmp_path):
        lines = [
            json.dumps(
                {"role": "user", "content": "<system-reminder>Some reminder</system-reminder>"}
            ),
            json.dumps({"role": "user", "content": "real question"}),
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(lines))
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 1

    def test_skips_command_message_content(self, tmp_path):
        lines = [
            json.dumps({"role": "user", "content": "<command-message>cmd</command-message>"}),
            json.dumps({"role": "user", "content": "real msg"}),
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(lines))
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 1

    def test_nested_format_counted(self, tmp_path):
        # Nested: {"message": {"role": "user", ...}}
        lines = [
            json.dumps({"message": {"role": "user", "content": "hello"}, "ts": 12345}),
            json.dumps({"message": {"role": "assistant", "content": "hi"}, "ts": 12346}),
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(lines))
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 1

    def test_skips_tool_result_only_content(self, tmp_path):
        content = [{"type": "tool_result", "content": "result"}]
        lines = [json.dumps({"role": "user", "content": content})]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(lines))
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 0

    def test_malformed_lines_skipped(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("not json\n{broken\n" + json.dumps({"role": "user", "content": "ok"}))
        mod = _load_module()
        assert mod._count_human_messages(str(f)) == 1


# ---------------------------------------------------------------------------
# _load_state / _save_state
# ---------------------------------------------------------------------------


class TestLoadSaveState:
    def test_load_missing_returns_empty(self, tmp_path):
        mod = _load_module()
        missing = tmp_path / "no_such.json"
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", missing):
            assert mod._load_state() == {}

    def test_save_then_load_roundtrip(self, tmp_path):
        state_path = tmp_path / "state.json"
        mod = _load_module()
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
            mod._save_state({"session1": {"last_save": 10}})
            loaded = mod._load_state()
        assert loaded == {"session1": {"last_save": 10}}

    def test_save_corrupt_file_safe_to_load(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("{broken json")
        mod = _load_module()
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
            result = mod._load_state()
        assert result == {}

    def test_save_creates_parent_dirs(self, tmp_path):
        deep_path = tmp_path / "a" / "b" / "state.json"
        mod = _load_module()
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", deep_path):
            mod._save_state({"x": 1})
        assert deep_path.exists()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    def _run_main(self, stdin_data, state_path):
        mod = _load_module()
        payload = json.dumps(stdin_data)
        with patch("sys.stdin", io.StringIO(payload)):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                import io as _io
                from contextlib import redirect_stdout

                buf = _io.StringIO()
                with redirect_stdout(buf):
                    mod.main()
        return buf.getvalue()

    def test_no_transcript_path_prints_empty_json(self, tmp_path, capsys):
        state_path = tmp_path / "state.json"
        mod = _load_module()
        stdin_data = {"session_id": "s1", "transcript_path": "", "stop_hook_active": False}
        with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
        out = capsys.readouterr().out
        assert out.strip() == "{}"

    def test_stop_hook_active_prints_empty_json(self, tmp_path, capsys):
        state_path = tmp_path / "state.json"
        mod = _load_module()
        stdin_data = {"session_id": "s1", "transcript_path": "/some/path", "stop_hook_active": True}
        with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
        out = capsys.readouterr().out
        assert out.strip() == "{}"

    def test_below_interval_prints_empty_json(self, tmp_path, capsys):
        state_path = tmp_path / "state.json"
        transcript = tmp_path / "t.jsonl"
        # Write 5 user messages (INTERVAL=25, so below threshold)
        lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(5)]
        transcript.write_text("\n".join(lines))

        mod = _load_module()
        stdin_data = {
            "session_id": "s1",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "cwd": str(tmp_path),
        }
        with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
        out = capsys.readouterr().out
        assert out.strip() == "{}"

    def test_at_interval_blocks_and_updates_state(self, tmp_path, capsys):
        state_path = tmp_path / "state.json"
        transcript = tmp_path / "t.jsonl"
        # Write 25 user messages — exactly at INTERVAL
        lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(25)]
        transcript.write_text("\n".join(lines))

        mod = _load_module()
        stdin_data = {
            "session_id": "s1",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "cwd": str(tmp_path),
        }
        with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result.get("decision") == "block"
        assert "reason" in result
        # State updated
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
            saved = mod._load_state()
        assert saved["s1"]["last_save"] == 25

    def test_invalid_stdin_uses_empty_dict(self, tmp_path, capsys):
        state_path = tmp_path / "state.json"
        mod = _load_module()
        with patch("sys.stdin", io.StringIO("NOT JSON")):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
        out = capsys.readouterr().out
        # Should not crash, prints {} (no transcript_path)
        assert out.strip() == "{}"

    def test_block_prompt_injects_default_branch_into_wiki_calls(self, tmp_path, capsys):
        """#19: the blocked prompt must carry branch_hint= on the wiki write calls.

        The ADR-log wiki write contract REQUIRES branch_hint (else missing_branch),
        and the ADR log is project-canonical so it must target the default branch.
        main() computes the default branch and injects it as {default_branch}.
        """
        state_path = tmp_path / "state.json"
        transcript = tmp_path / "t.jsonl"
        lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(25)]
        transcript.write_text("\n".join(lines))

        mod = _load_module()
        stdin_data = {
            "session_id": "s1",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "cwd": str(tmp_path),
        }
        # Force a deterministic default branch so the assertion is exact.
        with patch.object(mod, "_default_branch", return_value="trunk"):
            with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
                with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                    mod.main()
        out = capsys.readouterr().out
        reason = json.loads(out)["reason"]
        # Every wiki write in the prompt must carry the resolved default branch.
        assert 'branch_hint="trunk"' in reason
        # ADR step: now calls adr_add (not hand-rolled wiki_append_section).
        # Step 2 still uses wiki_add for structural write-back.
        assert "wiki_add(" in reason and "adr_add(" in reason
        # READ side: the ADR-log wiki_read must also pin branch_hint (§25 fix —
        # branch_hint-less reads on a feature branch miss master-scoped pages,
        # causing spurious "absent → create" and duplicate ADR logs).
        assert "wiki_read(" in reason
        assert reason.count('branch_hint="trunk"') >= 3, (
            "Expected branch_hint on wiki_read (ADR dedup-read), and at least 2 "
            "from step-2 wiki_add calls"
        )
        # The {default_branch} placeholder must be fully substituted (no leftovers).
        assert "{default_branch}" not in reason

    def test_adr_step_uses_adr_add_not_hand_rolled(self, tmp_path, capsys):
        """#37 (v5.85 car 1): ADR capture step calls adr_add, keeps dedup-read-first.

        Guards against regressions:
        (a) adr_add( must be present — the step now delegates ID/format/append to the tool.
        (b) read-existing-first + dedup-by-decision language must survive — adr_add does
            NOT check decision-level duplicates; the prompt is responsible for that.
        (c) wiki_append_section( must NOT appear in the prompt — the hand-rolled append
            is replaced by adr_add.
        """
        state_path = tmp_path / "state.json"
        transcript = tmp_path / "t.jsonl"
        lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(25)]
        transcript.write_text("\n".join(lines))

        mod = _load_module()
        stdin_data = {
            "session_id": "s1",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
            "cwd": str(tmp_path),
        }
        with patch.object(mod, "_default_branch", return_value="master"):
            with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
                with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                    mod.main()
        out = capsys.readouterr().out
        reason = json.loads(out)["reason"]

        # (a) ADR capture must call adr_add.
        assert "adr_add(" in reason, "ADR step must call adr_add( — not found in prompt"

        # (b) Dedup-read-first language must survive (adr_add does ID-dedup only,
        #     NOT decision-level dedup — the prompt keeps that judgment).
        dedup_signals = [
            "read existing",
            "dedup",
            "already",
            "only",
        ]
        reason_lower = reason.lower()
        assert any(sig in reason_lower for sig in dedup_signals), (
            "Dedup-by-decision / read-first language must survive in ADR step; "
            f"none of {dedup_signals!r} found in prompt"
        )
        assert "wiki_read(" in reason, (
            "ADR dedup-read (wiki_read call) must still be present so the model "
            "reads existing ADRs before deciding what to add"
        )

        # (c) Hand-rolled append gone — adr_add handles it now.
        assert "wiki_append_section(" not in reason, (
            "wiki_append_section( must NOT appear — ADR step now uses adr_add, "
            "which internally handles the append"
        )

    def test_default_branch_resolves_from_git_symbolic_ref(self, tmp_path):
        """_default_branch parses `git symbolic-ref refs/remotes/origin/HEAD`."""
        mod = _load_module()

        class _R:
            returncode = 0
            stdout = "refs/remotes/origin/main\n"

        with patch("subprocess.run", return_value=_R()):
            assert mod._default_branch(str(tmp_path)) == "main"

    def test_default_branch_falls_back_to_master_for_non_git(self, tmp_path):
        """Non-git dir / no remote HEAD / any error → 'master' fallback (ADR
        supports non-git projects)."""
        mod = _load_module()

        # Non-zero return code (no remote HEAD) → fallback.
        class _R:
            returncode = 128
            stdout = ""

        with patch("subprocess.run", return_value=_R()):
            assert mod._default_branch(str(tmp_path)) == "master"

        # Subprocess raising (git missing) → fallback, no crash.
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            assert mod._default_branch(str(tmp_path)) == "master"
