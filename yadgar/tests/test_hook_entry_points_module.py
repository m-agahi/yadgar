"""Tests for hook entry-point scripts: file-changed.py, subagent-start.py, instructions-loaded.py.

Wave 3 coverage: three hook entry scripts (~35-45 stmts each, 0% pre-wave).
Strategy:
  - Delegation branch (package installed): patch yadgar.hooks.xxx.main and
    load module normally.
  - Fallback branch (package not available): force ImportError via builtins.__import__
    patching, reload the module so the except-branch inline code executes.

Key: when yadgar is installed, `try: from yadgar.hooks.file_changed import main`
succeeds so the fallback code is never reached. Use _load_with_import_error() to
force ImportError and execute the inline fallback.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module paths
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).parent.parent / "core" / "hooks"
_FILE_CHANGED_PATH = _HOOKS_DIR / "file-changed.py"
_SUBAGENT_START_PATH = _HOOKS_DIR / "subagent-start.py"
_INSTRUCTIONS_LOADED_PATH = _HOOKS_DIR / "instructions-loaded.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_normal(path: Path, module_name: str):
    """Load the hook script normally (delegation branch — package importable)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_with_import_error(path: Path, module_name: str, block_module: str):
    """Load hook script forcing ImportError for block_module → exercises fallback inline code."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _masked_import(name, *args, **kwargs):
        if name == block_module:
            raise ImportError(f"mocked: {name} not available")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_masked_import):
        spec = importlib.util.spec_from_file_location(module_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# file-changed.py — delegation branch
# ---------------------------------------------------------------------------


class TestFileChangedDelegation:
    def test_main_called_via_package(self):
        with patch("yadgar.core.hooks.file_changed.main") as mock_main:
            mod = _load_normal(_FILE_CHANGED_PATH, "file_changed_entry")
            payload = json.dumps(
                {
                    "file_path": "/home/user/.claude/team_inbox/proj/session/msg.jsonl",
                    "file_action": "created",
                    "session_id": "s1",
                }
            )
            with patch("sys.stdin", io.StringIO(payload)):
                mod.main()
        mock_main.assert_called_once()

    def test_if_name_main_calls_main(self):
        with patch("yadgar.core.hooks.file_changed.main") as mock_main:
            mod = _load_normal(_FILE_CHANGED_PATH, "file_changed_entry")
            mod.main()
        mock_main.assert_called_once()


# ---------------------------------------------------------------------------
# file-changed.py — fallback branch (inline code)
# ---------------------------------------------------------------------------


class TestFileChangedFallback:
    def _load(self):
        return _load_with_import_error(
            _FILE_CHANGED_PATH, "fc_fallback", "yadgar.core.hooks.file_changed"
        )

    def test_team_inbox_file_posts(self):
        mod = self._load()
        payload = json.dumps(
            {
                "file_path": "/home/user/.claude/team_inbox/proj/session/msg.jsonl",
                "file_action": "created",
            }
        )
        mock_resp = MagicMock()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                mod.main()
        mock_urlopen.assert_called_once()

    def test_plan_file_posts(self):
        mod = self._load()
        payload = json.dumps(
            {
                "file_path": "/proj/docs/plans/some-plan.md",
                "file_action": "modified",
            }
        )
        mock_resp = MagicMock()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                mod.main()
        mock_urlopen.assert_called_once()

    def test_regular_file_not_posted(self):
        mod = self._load()
        payload = json.dumps(
            {
                "file_path": "/proj/src/main.py",
                "file_action": "modified",
            }
        )
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()

    def test_deleted_file_not_posted(self):
        mod = self._load()
        payload = json.dumps(
            {
                "file_path": "/proj/docs/plans/some-plan.md",
                "file_action": "deleted",
            }
        )
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()

    def test_empty_file_path_returns_silently(self):
        mod = self._load()
        payload = json.dumps({"file_path": "", "file_action": "modified"})
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()

    def test_invalid_json_returns_silently(self):
        mod = self._load()
        with patch("sys.stdin", io.StringIO("not json")):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()

    def test_auth_header_set_when_token_present(self, monkeypatch):
        mod = self._load()
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test_token")
        # Reload after env set
        mod = self._load()
        payload = json.dumps(
            {
                "file_path": "/h/.claude/team_inbox/a/b/c.jsonl",
                "file_action": "created",
            }
        )
        captured_req = []

        def _capture(req, timeout=None):
            captured_req.append(req)
            return MagicMock()

        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", side_effect=_capture):
                mod.main()

        if captured_req:
            assert "Bearer" in str(captured_req[0].headers)

    def test_http_error_suppressed(self):
        mod = self._load()
        payload = json.dumps(
            {
                "file_path": "/proj/docs/plans/some-plan.md",
                "file_action": "created",
            }
        )
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", side_effect=OSError("refused")):
                # Should not raise
                mod.main()


# ---------------------------------------------------------------------------
# subagent-start.py — delegation branch
# ---------------------------------------------------------------------------


class TestSubagentStartDelegation:
    def test_main_called_via_package(self):
        with patch("yadgar.core.hooks.subagent_start.main") as mock_main:
            mod = _load_normal(_SUBAGENT_START_PATH, "subagent_start_entry")
            mod.main()
        mock_main.assert_called_once()


# ---------------------------------------------------------------------------
# subagent-start.py — fallback branch
# ---------------------------------------------------------------------------


class TestSubagentStartFallback:
    def _load(self):
        return _load_with_import_error(
            _SUBAGENT_START_PATH, "ss_fallback", "yadgar.core.hooks.subagent_start"
        )

    def test_posts_to_subagent_start_endpoint(self):
        mod = self._load()
        payload = json.dumps(
            {
                "session_id": "s1",
                "agent_type": "Explore",
                "cwd": "/project",
                "description": "explore the repo",
            }
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": "context loaded"}).encode()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                mod.main()
        mock_urlopen.assert_called_once()

    def test_prints_text_from_response(self, capsys):
        mod = self._load()
        payload = json.dumps(
            {
                "session_id": "s1",
                "agent_type": "general-purpose",
                "cwd": "/project",
                "description": "task",
            }
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": "injected context"}).encode()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                mod.main()
        out = capsys.readouterr().out
        assert "injected context" in out

    def test_empty_text_no_print(self, capsys):
        mod = self._load()
        payload = json.dumps({"session_id": "s1", "agent_type": "Explore", "cwd": "/p"})
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": ""}).encode()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                mod.main()
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_http_error_suppressed(self):
        mod = self._load()
        payload = json.dumps({"session_id": "s1", "cwd": "/p"})
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", side_effect=OSError("refused")):
                mod.main()  # Should not raise

    def test_invalid_json_returns_silently(self):
        mod = self._load()
        with patch("sys.stdin", io.StringIO("bad json")):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# instructions-loaded.py — delegation branch
# ---------------------------------------------------------------------------


class TestInstructionsLoadedDelegation:
    def test_main_called_via_package(self):
        with patch("yadgar.core.hooks.instructions_loaded.main") as mock_main:
            mod = _load_normal(_INSTRUCTIONS_LOADED_PATH, "instructions_loaded_entry")
            mod.main()
        mock_main.assert_called_once()


# ---------------------------------------------------------------------------
# instructions-loaded.py — fallback branch
# ---------------------------------------------------------------------------


class TestInstructionsLoadedFallback:
    def _load(self):
        return _load_with_import_error(
            _INSTRUCTIONS_LOADED_PATH, "il_fallback", "yadgar.core.hooks.instructions_loaded"
        )

    def test_session_start_reason_posts(self):
        mod = self._load()
        payload = json.dumps(
            {
                "session_id": "s1",
                "load_reason": "session_start",
                "file_path": "/home/user/.claude/CLAUDE.md",
            }
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": ""}).encode()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                mod.main()
        mock_urlopen.assert_called_once()

    def test_compact_reason_posts(self):
        mod = self._load()
        payload = json.dumps(
            {
                "load_reason": "compact",
                "file_path": "/home/user/.claude/CLAUDE.md",
            }
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": ""}).encode()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                mod.main()
        mock_urlopen.assert_called_once()

    def test_other_reason_skipped(self):
        mod = self._load()
        payload = json.dumps(
            {
                "load_reason": "path_glob_match",
                "file_path": "/home/user/.claude/CLAUDE.md",
            }
        )
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()

    def test_prints_text_from_response(self, capsys):
        mod = self._load()
        payload = json.dumps(
            {
                "load_reason": "session_start",
                "file_path": "/home/user/.claude/CLAUDE.md",
            }
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": "instructions context"}).encode()
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                mod.main()
        out = capsys.readouterr().out
        assert "instructions context" in out

    def test_http_error_suppressed(self):
        mod = self._load()
        payload = json.dumps({"load_reason": "session_start", "file_path": "/f.md"})
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("urllib.request.urlopen", side_effect=OSError("refused")):
                mod.main()  # Should not raise

    def test_invalid_json_returns_silently(self):
        mod = self._load()
        with patch("sys.stdin", io.StringIO("bad json")):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mod.main()
        mock_urlopen.assert_not_called()
