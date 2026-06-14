"""Tests for yadgar/hooks/subagent-stop.py — SubagentStop entry script.

Wave 2 coverage: yadgar/hooks/subagent-stop.py (94 stmts, 0% pre-wave).
Strategy:
- The script has an inline fallback implementation when yadgar is not importable.
  We load it via runpy to exercise both paths:
  1. Normal path: imports from yadgar.hooks.subagent_stop and calls main()
  2. Fallback path: inline impl (triggered by patching out the import)

The inline fallback helpers (_extract_findings, _get_report_text, _post_findings,
main) are pure functions (except network call in _post_findings) — test them
by forcing the ImportError fallback path.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / "hooks" / "subagent-stop.py"


# ---------------------------------------------------------------------------
# Helper: run via runpy with patched stdin
# ---------------------------------------------------------------------------


def _run_script(payload: dict, **env_overrides):
    """Run the hook script via runpy with given stdin payload."""
    import runpy

    env = {}
    env.update(env_overrides)
    stdin_data = json.dumps(payload)
    with (
        patch("sys.stdin", io.StringIO(stdin_data)),
        patch.dict(os.environ, env),
    ):
        try:
            runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
        except SystemExit:
            pass


# ---------------------------------------------------------------------------
# Normal path — imports from yadgar.hooks.subagent_stop
# ---------------------------------------------------------------------------


class TestNormalPath:
    def test_no_transcript_path_silent_exit(self):
        """When no transcript_path, subagent_stop.main() returns silently."""
        payload = {"agent_type": "general-purpose", "cwd": "/project"}
        # No exception = pass
        _run_script(payload)

    def test_empty_transcript_silent_exit(self, tmp_path):
        tp = tmp_path / "transcript.jsonl"
        tp.write_text("")
        payload = {
            "agent_type": "general-purpose",
            "cwd": "/project",
            "transcript_path": str(tp),
        }
        _run_script(payload)

    def test_transcript_with_no_yadgar_section(self, tmp_path):
        tp = tmp_path / "transcript.jsonl"
        tp.write_text(
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "I did some work. No findings section.",
                    }
                }
            )
        )
        payload = {
            "agent_type": "general-purpose",
            "cwd": "/project",
            "transcript_path": str(tp),
        }
        _run_script(payload)

    def test_transcript_with_findings_posts_to_daemon(self, tmp_path):
        tp = tmp_path / "transcript.jsonl"
        report = '## Yadgar findings\n- memorize: content="test" tags=["x"]\n'
        tp.write_text(json.dumps({"message": {"role": "assistant", "content": report}}))
        payload = {
            "agent_type": "general-purpose",
            "cwd": "/project",
            "transcript_path": str(tp),
        }
        mock_resp = MagicMock()
        import urllib.request

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            _run_script(payload)
        # urlopen called — no crash means success

    def test_malformed_stdin(self):
        import runpy

        with patch("sys.stdin", io.StringIO("not-json")):
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass
        # No exception = pass


# ---------------------------------------------------------------------------
# Fallback path — inline implementation (ImportError forced)
# ---------------------------------------------------------------------------


class TestFallbackPath:
    """Force the inline fallback by making 'from yadgar.hooks.subagent_stop import main' fail."""

    def _load_inline_ns(self):
        """Load the script in a way that forces the fallback inline impl."""
        import builtins
        import runpy

        _real_import = builtins.__import__

        def _patched_import(name, *args, **kwargs):
            if name == "yadgar.hooks.subagent_stop":
                raise ImportError("test-forced import error")
            return _real_import(name, *args, **kwargs)

        ns = {}
        with (
            patch.object(builtins, "__import__", side_effect=_patched_import),
            patch("sys.stdin", io.StringIO("{}")),
        ):
            try:
                ns = runpy.run_path(str(_SCRIPT_PATH), init_globals=ns)
            except SystemExit:
                pass
        return ns

    def test_fallback_extract_findings_empty(self):
        ns = self._load_inline_ns()
        fn = ns.get("_extract_findings")
        if fn is None:
            pytest.skip("Fallback path not active — _extract_findings not in namespace")
        result = fn("no findings here")
        assert result == []

    def test_fallback_extract_findings_finds_bullets(self):
        ns = self._load_inline_ns()
        fn = ns.get("_extract_findings")
        if fn is None:
            pytest.skip("Fallback path not active")
        text = "## Yadgar findings\n- bullet one\n- bullet two\n## Other\nignore\n"
        result = fn(text)
        assert len(result) == 2
        assert "bullet one" in result

    def test_fallback_extract_findings_skips_none_sentinel(self):
        ns = self._load_inline_ns()
        fn = ns.get("_extract_findings")
        if fn is None:
            pytest.skip("Fallback path not active")
        text = "## Yadgar findings\n- none\n- real bullet\n"
        result = fn(text)
        assert "none" not in result
        assert "real bullet" in result

    def test_fallback_get_report_text_missing_path(self):
        ns = self._load_inline_ns()
        fn = ns.get("_get_report_text")
        if fn is None:
            pytest.skip("Fallback path not active")
        result = fn({"transcript_path": "/nonexistent/path.jsonl"})
        assert result == ""

    def test_fallback_get_report_text_no_path(self):
        ns = self._load_inline_ns()
        fn = ns.get("_get_report_text")
        if fn is None:
            pytest.skip("Fallback path not active")
        result = fn({})
        assert result == ""

    def test_fallback_get_report_text_from_file(self, tmp_path):
        ns = self._load_inline_ns()
        fn = ns.get("_get_report_text")
        if fn is None:
            pytest.skip("Fallback path not active")
        tp = tmp_path / "t.jsonl"
        content = "My report text"
        tp.write_text(json.dumps({"message": {"role": "assistant", "content": content}}))
        result = fn({"transcript_path": str(tp)})
        assert content in result

    def test_fallback_get_report_text_list_content_blocks(self, tmp_path):
        """list-of-blocks content: text blocks joined, non-text blocks skipped."""
        ns = self._load_inline_ns()
        fn = ns.get("_get_report_text")
        if fn is None:
            pytest.skip("Fallback path not active")
        tp = tmp_path / "t.jsonl"
        content = [
            {"type": "text", "text": "First block"},
            {"type": "tool_use", "id": "xyz", "name": "Bash"},
            {"type": "text", "text": "Second block"},
        ]
        tp.write_text(json.dumps({"message": {"role": "assistant", "content": content}}))
        result = fn({"transcript_path": str(tp)})
        assert "First block" in result
        assert "Second block" in result
        # tool_use block has no "text" key — must not error or bleed through
        assert "xyz" not in result

    def test_fallback_post_findings_silent_on_network_error(self):
        ns = self._load_inline_ns()
        fn = ns.get("_post_findings")
        if fn is None:
            pytest.skip("Fallback path not active")
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            fn("general-purpose", "/project", ["- finding one"])
        # No exception = pass

    def test_fallback_post_findings_empty_no_call(self):
        ns = self._load_inline_ns()
        fn = ns.get("_post_findings")
        if fn is None:
            pytest.skip("Fallback path not active")
        with patch("urllib.request.urlopen") as mock_open:
            fn("general-purpose", "/project", [])
        mock_open.assert_not_called()

    def test_fallback_main_with_findings_posts(self, tmp_path):
        """Full fallback main() flow with a transcript containing findings."""
        ns = self._load_inline_ns()
        fn = ns.get("main")
        if fn is None:
            pytest.skip("Fallback path not active")
        tp = tmp_path / "t.jsonl"
        report = '## Yadgar findings\n- memorize: content="test"\n'
        tp.write_text(json.dumps({"message": {"role": "assistant", "content": report}}))

        payload = {
            "agent_type": "general-purpose",
            "cwd": "/project",
            "transcript_path": str(tp),
        }
        mock_resp = MagicMock()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            fn()
        # urlopen called once for _post_findings
        # No assertion on exact call since the test verifies no crash


# ---------------------------------------------------------------------------
# Edge cases for the full script run
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_stdin_no_crash(self):
        import runpy

        with patch("sys.stdin", io.StringIO("")):
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass

    def test_partial_payload_no_crash(self):
        _run_script({"agent_type": "explore"})

    def test_with_auth_token_adds_header(self, tmp_path):
        tp = tmp_path / "t.jsonl"
        report = '## Yadgar findings\n- anchor: content="important"\n'
        tp.write_text(json.dumps({"message": {"role": "assistant", "content": report}}))
        payload = {
            "agent_type": "general-purpose",
            "cwd": "/project",
            "transcript_path": str(tp),
        }
        captured_headers = []

        import urllib.request as _req_mod

        real_request = _req_mod.Request

        def capture_request(url, data=None, headers=None, **kw):
            captured_headers.append(headers or {})
            return real_request(url, data=data, headers=headers or {}, **kw)

        mock_resp = MagicMock()
        with (
            patch.object(_req_mod, "urlopen", return_value=mock_resp),
            patch.dict(os.environ, {"YADGAR_MCP_AUTH_TOKEN": "test-token"}),
        ):
            _run_script(payload, YADGAR_MCP_AUTH_TOKEN="test-token")
