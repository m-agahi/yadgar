"""Tests for yadgar/scripts/hook_runner.py — hook dispatch + handler logic.

Coverage targets:
- _auth_headers: with/without token
- _http_get / _http_post: URL construction + exception handling
- hook_post_tool_capture: skip prefixes, skip non-capture tools, summary extraction
- hook_session_start_context: passes directory + prints output
- hook_post_compact_rehydrate: fallback on bad stdin
- hook_pre_compact_drain: posts data
- hook_prompt_recall: short prompt skipped, long prompt fires
- hook_db_lockdown_check: allow + deny patterns
- hook_block_reflect: only fires for block tools
- main: unknown hook type exits 1
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import yadgar.core.scripts.hook_runner as hr

# ── _auth_headers ─────────────────────────────────────────────────────────────


def test_auth_headers_empty_when_no_token(monkeypatch):
    monkeypatch.setattr(hr, "_AUTH_TOKEN", "")
    result = hr._auth_headers()
    assert result == {}


def test_auth_headers_returns_bearer_when_token_set(monkeypatch):
    monkeypatch.setattr(hr, "_AUTH_TOKEN", "secret-tok")
    result = hr._auth_headers()
    assert result == {"Authorization": "Bearer secret-tok"}


# ── _http_get ─────────────────────────────────────────────────────────────────


def test_http_get_returns_none_on_connection_error(monkeypatch):
    monkeypatch.setattr(hr, "_PORT", "9999")

    def mock_urlopen(*args, **kwargs):
        raise OSError("connection refused")

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = hr._http_get("/health")
    assert result is None


def test_http_get_appends_params_to_url(monkeypatch):
    captured_urls = []

    def mock_urlopen(req, timeout=None):
        captured_urls.append(req.full_url)
        raise OSError("no server")

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        hr._http_get("/api/test", params={"key": "val", "x": "1"})

    assert len(captured_urls) == 1
    assert "key=val" in captured_urls[0] or "x=1" in captured_urls[0]


def test_http_get_returns_parsed_json(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"status": "ok"}).encode()

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = hr._http_get("/api/test")
    assert result == {"status": "ok"}


# ── _http_post ────────────────────────────────────────────────────────────────


def test_http_post_returns_none_on_error():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        result = hr._http_post("/api/test", {"a": 1})
    assert result is None


def test_http_post_sends_json_body():
    captured = []

    def mock_urlopen(req, timeout=None):
        captured.append(req.data)
        raise OSError("no server")

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        hr._http_post("/api/test", {"key": "value"})

    assert len(captured) == 1
    payload = json.loads(captured[0].decode())
    assert payload["key"] == "value"


# ── hook_post_tool_capture ────────────────────────────────────────────────────


def _run_hook_with_stdin(hook_fn, stdin_data: dict | str | None = None) -> None:
    """Run a hook function with mocked stdin."""
    if isinstance(stdin_data, dict):
        stdin_content = json.dumps(stdin_data)
    elif isinstance(stdin_data, str):
        stdin_content = stdin_data
    else:
        stdin_content = ""

    with patch("sys.stdin", io.StringIO(stdin_content)):
        hook_fn()


def test_post_tool_capture_skips_yadgar_prefix():
    data = {"tool_name": "mcp__yadgar__memorize", "cwd": "/proj", "session_id": "s1"}
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_post_tool_capture, data)
    assert len(posted) == 0


def test_post_tool_capture_skips_non_capture_tool():
    data = {"tool_name": "Read", "cwd": "/proj", "session_id": "s1", "tool_input": {}}
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_post_tool_capture, data)
    assert len(posted) == 0


def test_post_tool_capture_captures_bash():
    data = {
        "tool_name": "Bash",
        "cwd": "/proj",
        "session_id": "s1",
        "tool_input": {"command": "ls -la"},
    }
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_post_tool_capture, data)
    assert len(posted) == 1
    assert posted[0][0] == "/hooks/auto-capture"
    assert posted[0][1]["tool_name"] == "Bash"
    assert "ls -la" in posted[0][1]["summary"]


def test_post_tool_capture_captures_write():
    data = {
        "tool_name": "Write",
        "cwd": "/proj",
        "session_id": "s1",
        "tool_input": {"file_path": "/proj/main.py", "content": "print('hello')"},
    }
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_post_tool_capture, data)
    assert len(posted) == 1


def test_post_tool_capture_skips_plugin_prefix():
    data = {"tool_name": "mcp__plugin_claude-code-home-manager_yadgar__recall", "cwd": "/"}
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_post_tool_capture, data)
    assert len(posted) == 0


def test_post_tool_capture_handles_malformed_json():
    with patch.object(hr, "_http_post") as mock_post:
        with patch("sys.stdin", io.StringIO("{not json")):
            hr.hook_post_tool_capture()
    mock_post.assert_not_called()


def test_post_tool_capture_handles_non_dict_tool_input():
    data = {"tool_name": "Bash", "cwd": "/", "session_id": "s", "tool_input": "some string"}
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_post_tool_capture, data)
    assert len(posted) == 1


# ── hook_session_start_context ────────────────────────────────────────────────


def test_session_start_context_prints_text(capsys):
    data = {"cwd": "/myproject"}
    mock_result = {"text": "context data here"}
    with patch.object(hr, "_http_get", return_value=mock_result):
        _run_hook_with_stdin(hr.hook_session_start_context, data)
    captured = capsys.readouterr()
    assert "context data here" in captured.out


def test_session_start_context_no_output_when_empty(capsys):
    data = {"cwd": "/myproject"}
    with patch.object(hr, "_http_get", return_value={"text": ""}):
        _run_hook_with_stdin(hr.hook_session_start_context, data)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_session_start_context_no_output_when_server_down(capsys):
    data = {"cwd": "/myproject"}
    with patch.object(hr, "_http_get", return_value=None):
        _run_hook_with_stdin(hr.hook_session_start_context, data)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_session_start_context_bad_stdin_uses_cwd(capsys):
    with patch.object(hr, "_http_get", return_value=None) as mock_get:
        with patch("sys.stdin", io.StringIO("{bad json")):
            hr.hook_session_start_context()
    mock_get.assert_called_once()
    params = mock_get.call_args[0][1]
    assert "directory" in params


# ── hook_post_compact_rehydrate ───────────────────────────────────────────────


def test_post_compact_rehydrate_prints_text(capsys):
    data = {"cwd": "/proj"}
    with patch.object(hr, "_http_get", return_value={"text": "restored context"}):
        _run_hook_with_stdin(hr.hook_post_compact_rehydrate, data)
    assert "restored context" in capsys.readouterr().out


def test_post_compact_rehydrate_uses_context_fallback(capsys):
    data = {"cwd": "/proj"}
    with patch.object(hr, "_http_get", return_value={"context": "ctx"}):
        _run_hook_with_stdin(hr.hook_post_compact_rehydrate, data)
    assert "ctx" in capsys.readouterr().out


# ── hook_pre_compact_drain ────────────────────────────────────────────────────


def test_pre_compact_drain_posts_data():
    data = {"session_id": "s1", "cwd": "/proj"}
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        _run_hook_with_stdin(hr.hook_pre_compact_drain, data)
    assert len(posted) == 1
    assert posted[0][0] == "/hooks/pre-compact"


def test_pre_compact_drain_handles_bad_stdin():
    posted = []
    with patch.object(hr, "_http_post", side_effect=lambda *a, **kw: posted.append(a)):
        with patch("sys.stdin", io.StringIO("{bad")):
            hr.hook_pre_compact_drain()
    assert len(posted) == 1  # Posts empty dict on error


# ── hook_prompt_recall ────────────────────────────────────────────────────────


def test_prompt_recall_skips_short_prompt(capsys):
    data = {"prompt": "x", "cwd": "/proj"}
    with patch.object(hr, "_http_get") as mock_get:
        _run_hook_with_stdin(hr.hook_prompt_recall, data)
    mock_get.assert_not_called()


def test_prompt_recall_skips_empty_prompt(capsys):
    data = {"prompt": "", "cwd": "/proj"}
    with patch.object(hr, "_http_get") as mock_get:
        _run_hook_with_stdin(hr.hook_prompt_recall, data)
    mock_get.assert_not_called()


def test_prompt_recall_fires_for_long_prompt(capsys):
    data = {"prompt": "What is the meaning of life?", "cwd": "/proj"}
    with patch.object(hr, "_http_get", return_value={"text": "42"}) as mock_get:
        _run_hook_with_stdin(hr.hook_prompt_recall, data)
    mock_get.assert_called_once()
    assert "42" in capsys.readouterr().out


def test_prompt_recall_uses_user_prompt_field():
    data = {"user_prompt": "How does recall work?", "cwd": "/proj"}
    with patch.object(hr, "_http_get", return_value=None) as mock_get:
        _run_hook_with_stdin(hr.hook_prompt_recall, data)
    mock_get.assert_called_once()


def test_prompt_recall_handles_malformed_stdin():
    with patch.object(hr, "_http_get") as mock_get:
        with patch("sys.stdin", io.StringIO("not json")):
            hr.hook_prompt_recall()
    mock_get.assert_not_called()


# ── hook_db_lockdown_check ────────────────────────────────────────────────────


def test_db_lockdown_check_allows_normal_command(capsys):
    data = {"tool_input": {"command": "git status"}}
    _run_hook_with_stdin(hr.hook_db_lockdown_check, data)
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_db_lockdown_check_denies_blocked_exec(capsys):
    data = {"tool_input": {"command": "docker exec yadgar-backend bash"}}
    _run_hook_with_stdin(hr.hook_db_lockdown_check, data)
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_db_lockdown_check_denies_yadgar_db(capsys):
    data = {"tool_input": {"command": "docker exec yadgar-db surreal sql"}}
    _run_hook_with_stdin(hr.hook_db_lockdown_check, data)
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_db_lockdown_check_allows_bad_json(capsys):
    with patch("sys.stdin", io.StringIO("not json at all")):
        hr.hook_db_lockdown_check()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


# ── hook_block_reflect ────────────────────────────────────────────────────────


def test_block_reflect_skips_non_block_tool():
    data = {"tool_name": "Bash", "cwd": "/proj"}
    with patch.object(hr, "_http_get") as mock_get:
        _run_hook_with_stdin(hr.hook_block_reflect, data)
    mock_get.assert_not_called()


def test_block_reflect_fires_for_block_create(capsys):
    data = {"tool_name": "mcp__yadgar__block_create", "cwd": "/proj"}
    with patch.object(hr, "_http_get", return_value={"text": "block content"}):
        _run_hook_with_stdin(hr.hook_block_reflect, data)
    assert "block content" in capsys.readouterr().out


def test_block_reflect_fires_for_all_block_tools():
    block_tools = [
        "mcp__yadgar__block_create",
        "mcp__yadgar__block_update",
        "mcp__yadgar__block_delete",
        "mcp__yadgar__block_replace",
        "mcp__yadgar__block_append",
    ]
    for tool_name in block_tools:
        data = {"tool_name": tool_name, "cwd": "/proj"}
        with patch.object(hr, "_http_get", return_value=None) as mock_get:
            _run_hook_with_stdin(hr.hook_block_reflect, data)
        mock_get.assert_called_once()


def test_block_reflect_handles_bad_stdin():
    with patch.object(hr, "_http_get") as mock_get:
        with patch("sys.stdin", io.StringIO("{{bad")):
            hr.hook_block_reflect()
    mock_get.assert_not_called()
