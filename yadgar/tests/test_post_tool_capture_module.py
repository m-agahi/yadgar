"""Tests for yadgar/hooks/post-tool-capture.py — PostToolCall hook.

Wave 3 coverage: yadgar/hooks/post-tool-capture.py (~60 stmts, 0% pre-wave).
Strategy: load module via importlib.util, mock sys.stdin and urllib.request.urlopen.
Test skip prefixes, non-capture skip, capture tools, summary extraction, HTTP POST.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_HOOK_PATH = Path(__file__).parent.parent / "hooks" / "post-tool-capture.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("post_tool_capture", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Skip prefix tests
# ---------------------------------------------------------------------------


class TestSkipPrefixes:
    def _run(self, tool_name):
        mod = _load_module()
        payload = json.dumps(
            {"tool_name": tool_name, "tool_input": {}, "cwd": "/p", "session_id": "s"}
        )
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        return mock_urlopen

    def test_mcp_yadgar_prefix_skipped(self):
        mock_urlopen = self._run("mcp__yadgar__recall")
        mock_urlopen.assert_not_called()

    def test_mcp_plugin_yadgar_prefix_skipped(self):
        mock_urlopen = self._run("mcp__plugin_claude-code-home-manager_yadgar__wiki_add")
        mock_urlopen.assert_not_called()

    def test_mcp_plugin_oh_my_prefix_skipped(self):
        mock_urlopen = self._run("mcp__plugin_oh-my-claudecode_t__something")
        mock_urlopen.assert_not_called()

    def test_non_capture_tool_skipped(self):
        mock_urlopen = self._run("Read")
        mock_urlopen.assert_not_called()

    def test_grep_tool_skipped(self):
        mock_urlopen = self._run("Grep")
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Capture tool tests
# ---------------------------------------------------------------------------


class TestCaptureTools:
    def _run_with_payload(self, payload_dict):
        mod = _load_module()
        payload = json.dumps(payload_dict)
        mock_resp = MagicMock()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                mod.main()
        return mock_urlopen

    def test_bash_tool_captured(self):
        mock_urlopen = self._run_with_payload(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
                "cwd": "/project",
                "session_id": "sess1",
            }
        )
        mock_urlopen.assert_called_once()

    def test_write_tool_captured(self):
        mock_urlopen = self._run_with_payload(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/test.py", "content": "# code"},
                "cwd": "/project",
                "session_id": "sess1",
            }
        )
        mock_urlopen.assert_called_once()

    def test_edit_tool_captured(self):
        mock_urlopen = self._run_with_payload(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/test.py", "old_string": "a", "new_string": "b"},
                "cwd": "/project",
                "session_id": "sess1",
            }
        )
        mock_urlopen.assert_called_once()

    def test_agent_tool_captured(self):
        mock_urlopen = self._run_with_payload(
            {
                "tool_name": "Agent",
                "tool_input": {"prompt": "do something"},
                "cwd": "/project",
                "session_id": "sess1",
            }
        )
        mock_urlopen.assert_called_once()


# ---------------------------------------------------------------------------
# Summary field extraction
# ---------------------------------------------------------------------------


class TestSummaryFieldExtraction:
    def _get_posted_payload(self, tool_input):
        mod = _load_module()
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": tool_input,
                "cwd": "/p",
                "session_id": "s",
            }
        )
        captured_req = []
        mock_resp = MagicMock()

        def _capture(req, timeout=None):
            captured_req.append(req)
            return mock_resp

        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", side_effect=_capture):
                mod.main()

        assert captured_req, "expected urlopen to be called"
        body = json.loads(captured_req[0].data.decode())
        return body["summary"]

    def test_command_field_used_first(self):
        summary = self._get_posted_payload({"command": "git status", "file_path": "/tmp/f"})
        assert summary == "git status"

    def test_content_precedes_file_path(self):
        # In _SUMMARY_FIELDS, content comes before file_path
        summary = self._get_posted_payload({"content": "# code", "file_path": "/tmp/f.py"})
        assert summary == "# code"

    def test_long_value_truncated_to_200(self):
        long_cmd = "x" * 300
        summary = self._get_posted_payload({"command": long_cmd})
        assert len(summary) == 200

    def test_no_known_fields_falls_back_to_repr(self):
        summary = self._get_posted_payload({"unknown_key": "some value"})
        assert "unknown_key" in summary or "some value" in summary


# ---------------------------------------------------------------------------
# HTTP POST fields and auth
# ---------------------------------------------------------------------------


class TestHttpPost:
    def _get_request_obj(self, tool_input=None, token=None, tool_name="Bash"):
        mod = _load_module()
        payload = json.dumps(
            {
                "tool_name": tool_name,
                "tool_input": tool_input or {"command": "ls"},
                "cwd": "/proj",
                "session_id": "sess",
            }
        )
        captured_req = []
        mock_resp = MagicMock()

        def _capture(req, timeout=None):
            captured_req.append(req)
            return mock_resp

        env_patch = {"YADGAR_MCP_AUTH_TOKEN": token} if token else {}
        with patch("sys.stdin", io.StringIO(payload)):
            with patch.dict("os.environ", env_patch, clear=False):
                with patch("urllib.request.urlopen", side_effect=_capture):
                    mod.main()

        return captured_req[0] if captured_req else None

    def test_content_type_header_set(self):
        req = self._get_request_obj()
        assert req.get_header("Content-type") == "application/json"

    def test_auth_header_set_when_token_present(self):
        req = self._get_request_obj(token="secret123")
        assert req.get_header("Authorization") == "Bearer secret123"

    def test_no_auth_header_without_token(self, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        req = self._get_request_obj(token=None)
        assert req.get_header("Authorization") is None

    def test_correct_url_endpoint(self):
        req = self._get_request_obj()
        assert "/hooks/auto-capture" in req.full_url

    def test_invalid_json_stdin_returns_silently(self):
        mod = _load_module()
        with patch("sys.stdin", io.StringIO("not json {{")):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()

    def test_http_failure_suppressed(self):
        mod = _load_module()
        payload = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/p", "session_id": "s"}
        )
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", side_effect=OSError("refused")):
                # Should not raise
                mod.main()
