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

_HOOK_PATH = Path(__file__).parent.parent.parent / "core" / "hooks" / "stop-memory-checkpoint.py"


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
        # Reason is the short pointer (Car B, task #74) — not the full protocol
        reason = result["reason"]
        assert reason.startswith("[yadgar] Checkpoint due. Read "), (
            f"Reason must be short pointer, got: {reason[:100]}"
        )
        assert reason.endswith(" and follow all the instructions in it.")
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

    def test_block_reason_is_short_pointer_not_full_protocol(self, tmp_path, capsys):
        """Car B (task #74): reason must be the short pointer, never the full protocol.

        Guards against regressions where the full ~60-line protocol is inlined back
        into the reason field.  The protocol now lives in the file pointed to by the
        reason; main() emits only a one-line pointer.
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
        with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
        out = capsys.readouterr().out
        reason = json.loads(out)["reason"]

        # Short pointer — single line with known prefix/suffix
        assert reason.startswith("[yadgar] Checkpoint due. Read ")
        assert reason.endswith(" and follow all the instructions in it.")
        # Must NOT inline the full protocol
        assert "CAPTURE FIRST" not in reason
        assert "adr_add(" not in reason
        assert "project_brief(" not in reason
        # Path in the reason must resolve to the real template file
        prefix = "[yadgar] Checkpoint due. Read "
        suffix = " and follow all the instructions in it."
        path_in_reason = reason[len(prefix) : -len(suffix)]
        from pathlib import Path as _Path

        assert _Path(path_in_reason).is_file(), f"Path in reason does not resolve: {path_in_reason}"
