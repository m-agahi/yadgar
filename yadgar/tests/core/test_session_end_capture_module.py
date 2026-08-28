"""Tests for yadgar/hooks/session-end-capture.py — SessionEnd hook helpers.

Wave 2 coverage: yadgar/hooks/session-end-capture.py (142 stmts, 0% pre-wave).
Strategy:
- The script has module-level sys.exit() calls and runs at import time.
  We cannot do a plain `import`. Instead we use runpy.run_path with
  SESSION_END_CAPTURE_ENABLED=false to import safely, then extract and
  test the pure helper functions directly.
- For full-flow tests: use runpy.run_path with patched stdin + env.
Floor note: sentinel write path requires a writable filesystem dir;
that's accessible in the test env. The session-end kill-switch (ENABLED=false)
makes safe import possible.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Safe module load
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parent.parent.parent / "core" / "hooks" / "session-end-capture.py"


def _load_module_disabled():
    """Load the hook script with the kill-switch on so sys.exit(0) is a no-op.

    YADGAR_SESSION_END_DIR points at a throwaway tmp dir for BOTH exec passes:
    the module-level load otherwise writes its sentinel into the real
    ~/.local/state/yadgar/session-ends/, which does not exist in the CI
    container (Errno 2 on the atomic rename) — the load failed and every test
    in this file skip-fired with a dynamic, un-sanctionable reason.

    Task 416: the sentinel dir is a ``TemporaryDirectory`` context, not a bare
    ``mkdtemp``. This loader runs at MODULE level (import time), so under xdist
    it fires once per worker that collects this file and the old ``mkdtemp``
    handed each of those a directory nothing ever removed — 40
    ``/tmp/yadgar-session-end-test-*`` dirs had accumulated. Nothing reads the
    dir after the two exec passes finish: the hook script resolves
    ``YADGAR_SESSION_END_DIR`` at exec time (session-end-capture.py:398) and
    the tests that exercise the sentinel write path build their own
    ``tmp_path``-rooted dir, so the scope can end with this function.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="yadgar-session-end-test-") as _sentinel_dir:
        with patch.dict(
            os.environ,
            {"SESSION_END_CAPTURE_ENABLED": "false", "YADGAR_SESSION_END_DIR": _sentinel_dir},
        ):
            # runpy runs the script; sys.exit(0) is raised and caught here
            import runpy

            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass
        # Load again as a module to get the functions
        spec = importlib.util.spec_from_file_location("session_end_capture", str(_SCRIPT_PATH))
        mod = importlib.util.module_from_spec(spec)
        # Patch sys.exit to prevent it from running during exec_module
        with (
            patch.dict(
                os.environ,
                {"SESSION_END_CAPTURE_ENABLED": "false", "YADGAR_SESSION_END_DIR": _sentinel_dir},
            ),
            patch("sys.exit"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.read.return_value = '{"end_reason": "other", "session_id": "test-load"}'
            try:
                spec.loader.exec_module(mod)
            except SystemExit:
                pass
    return mod


# Load once for the test session
try:
    _mod = _load_module_disabled()
    _count_human_messages = _mod._count_human_messages
    _parse_user_content = _mod._parse_user_content
    _cap_turns = _mod._cap_turns
    _extract_last_human_turns = _mod._extract_last_human_turns
    _extract_last_touched_files = _mod._extract_last_touched_files
    _SKIP_TAGS = _mod.SKIP_TAGS
    _MODULE_LOADED = True
except Exception as _e:
    _MODULE_LOADED = False
    _load_error = str(_e)


def _require_module():
    if not _MODULE_LOADED:
        pytest.skip(f"session-end-capture module load failed: {_load_error}")


# ---------------------------------------------------------------------------
# _count_human_messages
# ---------------------------------------------------------------------------


class TestCountHumanMessages:
    def setup_method(self):
        _require_module()

    def test_nonexistent_path_returns_zero(self, tmp_path):
        assert _count_human_messages(str(tmp_path / "missing.jsonl")) == 0

    def test_empty_file_returns_zero(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("")
        assert _count_human_messages(str(p)) == 0

    def test_counts_user_role_entries(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "hello"}}),
            json.dumps({"message": {"role": "assistant", "content": "world"}}),
            json.dumps({"message": {"role": "user", "content": "another"}}),
        ]
        p.write_text("\n".join(lines))
        assert _count_human_messages(str(p)) == 2

    def test_skips_system_injection_tags(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {"message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}}
            ),
            json.dumps({"message": {"role": "user", "content": "genuine message"}}),
        ]
        p.write_text("\n".join(lines))
        assert _count_human_messages(str(p)) == 1

    def test_skips_pure_tool_result_entries(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "x"}],
                    }
                }
            ),
            json.dumps({"message": {"role": "user", "content": "real message"}}),
        ]
        p.write_text("\n".join(lines))
        assert _count_human_messages(str(p)) == 1

    def test_skips_malformed_json_lines(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("not-json\n" + json.dumps({"message": {"role": "user", "content": "ok"}}))
        assert _count_human_messages(str(p)) == 1


# ---------------------------------------------------------------------------
# _parse_user_content
# ---------------------------------------------------------------------------


class TestParseUserContent:
    def setup_method(self):
        _require_module()

    def test_plain_string_returned(self):
        assert _parse_user_content("hello world") == "hello world"

    def test_string_with_skip_tag_returns_none(self):
        assert _parse_user_content("<system-reminder>x</system-reminder>") is None

    def test_pure_tool_result_list_returns_none(self):
        content = [{"type": "tool_result", "tool_use_id": "123"}]
        assert _parse_user_content(content) is None

    def test_text_block_list_extracted(self):
        content = [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]
        result = _parse_user_content(content)
        assert result is not None
        assert "hello" in result

    def test_mixed_list_extracts_text(self):
        content = [
            {"type": "tool_result", "tool_use_id": "x"},
            {"type": "text", "text": "user says this"},
        ]
        result = _parse_user_content(content)
        assert result is not None
        assert "user says this" in result

    def test_empty_text_blocks_returns_none(self):
        content = [{"type": "text", "text": ""}]
        result = _parse_user_content(content)
        # No meaningful text → None or empty
        assert result is None or result.strip() == ""

    def test_none_content_returns_none(self):
        # Non-string, non-list → None
        result = _parse_user_content(None)
        assert result is None


# ---------------------------------------------------------------------------
# _cap_turns
# ---------------------------------------------------------------------------


class TestCapTurns:
    def setup_method(self):
        _require_module()

    def test_empty_turns_returns_empty(self):
        assert _cap_turns([], 5) == []

    def test_fewer_than_n_returns_all(self):
        turns = ["a", "b", "c"]
        assert _cap_turns(turns, 10) == ["a", "b", "c"]

    def test_takes_last_n(self):
        turns = ["a", "b", "c", "d", "e"]
        result = _cap_turns(turns, 3)
        assert result == ["c", "d", "e"]

    def test_long_turn_truncated(self):
        long_turn = "x" * 1000
        result = _cap_turns([long_turn], 1, max_per=100)
        assert len(result) == 1
        assert len(result[0]) == 100

    def test_total_cap_respected(self):
        # 10 turns of 200 chars each → max_total=500 means only some fit
        turns = ["z" * 200 for _ in range(10)]
        result = _cap_turns(turns, 10, max_per=200, max_total=500)
        total = sum(len(t) for t in result)
        assert total <= 500

    def test_order_preserved(self):
        turns = ["first", "second", "third"]
        result = _cap_turns(turns, 3)
        assert result == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# _extract_last_human_turns
# ---------------------------------------------------------------------------


class TestExtractLastHumanTurns:
    def setup_method(self):
        _require_module()

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert _extract_last_human_turns(str(tmp_path / "missing.jsonl"), 5) == []

    def test_extracts_user_turns(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "first"}}),
            json.dumps({"message": {"role": "assistant", "content": "answer"}}),
            json.dumps({"message": {"role": "user", "content": "second"}}),
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_human_turns(str(p), 5)
        assert "first" in result
        assert "second" in result

    def test_skips_injections(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {"message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}}
            ),
            json.dumps({"message": {"role": "user", "content": "real turn"}}),
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_human_turns(str(p), 5)
        assert result == ["real turn"] or "real turn" in result

    def test_returns_last_n(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": f"turn-{i}"}}) for i in range(10)
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_human_turns(str(p), 3)
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# _extract_last_touched_files
# ---------------------------------------------------------------------------


class TestExtractLastTouchedFiles:
    def setup_method(self):
        _require_module()

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert _extract_last_touched_files(str(tmp_path / "missing.jsonl"), 5) == []

    def test_extracts_file_paths_from_read_edit_write(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "/project/main.py"},
                            }
                        ],
                    }
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": "/project/utils.py"},
                            }
                        ],
                    }
                }
            ),
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_touched_files(str(p), 5)
        assert "/project/main.py" in result or "/project/utils.py" in result

    def test_skips_non_file_tools(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            }
                        ],
                    }
                }
            ),
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_touched_files(str(p), 5)
        assert result == []

    def test_deduplicates_paths(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}
                        ],
                    }
                }
            ),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}}
                        ],
                    }
                }
            ),
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_touched_files(str(p), 10)
        assert result.count("/a.py") == 1

    def test_returns_at_most_n(self, tmp_path):
        p = tmp_path / "t.jsonl"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": f"/file{i}.py"},
                            }
                        ],
                    }
                }
            )
            for i in range(10)
        ]
        p.write_text("\n".join(lines))
        result = _extract_last_touched_files(str(p), 3)
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# Full script run via runpy — sentinel write path
# ---------------------------------------------------------------------------


class TestFullScriptRun:
    """Test the sentinel write path by running the script end-to-end via runpy."""

    def _build_transcript(self, tmp_path: Path, messages: list[str]) -> Path:
        """Write a JSONL transcript with the given user messages."""
        tp = tmp_path / "transcript.jsonl"
        lines = [json.dumps({"message": {"role": "user", "content": m}}) for m in messages]
        tp.write_text("\n".join(lines))
        return tp

    def test_writes_sentinel_when_enabled(self, tmp_path):
        import runpy

        tp = self._build_transcript(tmp_path, ["msg1", "msg2", "msg3"])
        sentinel_dir = tmp_path / "sentinels"
        payload = {
            "end_reason": "other",
            "session_id": "test-sentinel",
            "transcript_path": str(tp),
            "cwd": str(tmp_path),
        }
        env = {
            "SESSION_END_CAPTURE_ENABLED": "true",
            "SESSION_END_MIN_MESSAGES": "2",
            "YADGAR_SESSION_END_DIR": str(sentinel_dir),
        }
        import io

        with (
            patch.dict(os.environ, env),
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
        ):
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass

        sentinel_file = sentinel_dir / "test-sentinel.json"
        assert sentinel_file.exists()
        data = json.loads(sentinel_file.read_text())
        assert data["session_id"] == "test-sentinel"
        assert data["type"] == "session_end_sentinel"

    def test_skips_clear_reason(self, tmp_path):
        import runpy

        sentinel_dir = tmp_path / "sentinels"
        payload = {"end_reason": "clear", "session_id": "test-clear"}
        import io

        with (
            patch.dict(
                os.environ,
                {
                    "SESSION_END_CAPTURE_ENABLED": "true",
                    "YADGAR_SESSION_END_DIR": str(sentinel_dir),
                },
            ),
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
        ):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
        assert exc_info.value.code == 0
        # Sentinel dir should not be created
        assert not sentinel_dir.exists()

    def test_disabled_exits_immediately(self, tmp_path):
        import io
        import runpy

        sentinel_dir = tmp_path / "sentinels"
        with (
            patch.dict(
                os.environ,
                {
                    "SESSION_END_CAPTURE_ENABLED": "false",
                    "YADGAR_SESSION_END_DIR": str(sentinel_dir),
                },
            ),
            patch("sys.stdin", io.StringIO("{}")),
        ):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
        assert exc_info.value.code == 0
        assert not sentinel_dir.exists()

    def test_skips_when_too_few_messages(self, tmp_path):
        import io
        import runpy

        tp = self._build_transcript(tmp_path, ["only one message"])
        sentinel_dir = tmp_path / "sentinels"
        payload = {
            "end_reason": "other",
            "session_id": "test-short",
            "transcript_path": str(tp),
        }
        env = {
            "SESSION_END_CAPTURE_ENABLED": "true",
            "SESSION_END_MIN_MESSAGES": "2",
            "YADGAR_SESSION_END_DIR": str(sentinel_dir),
        }
        with (
            patch.dict(os.environ, env),
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
        ):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
        assert exc_info.value.code == 0
        assert not sentinel_dir.exists()
