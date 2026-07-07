"""TDD tests for v5.10.4: hook_runner.py PreToolUse output uses new schema.

Scope:
  - hook_db_lockdown_check() outputs {"hookSpecificOutput": {"permissionDecision": "allow"}}
    for benign commands (not old {"decision": "allow"}).
  - hook_db_lockdown_check() outputs {"hookSpecificOutput": {"permissionDecision": "deny"}}
    with systemMessage for blocked commands.
  - hook_db_lockdown_check() handles JSON decode error gracefully (returns allow).

Written BEFORE implementation — tests start red.
"""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch


def _run_hook(stdin_data: dict) -> dict:
    """Run hook_db_lockdown_check with given stdin JSON, return parsed stdout dict."""
    from yadgar.core.scripts.hook_runner import hook_db_lockdown_check

    stdin_payload = json.dumps(stdin_data)
    output = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(stdin_payload)):
        with patch("sys.stdout", output):
            hook_db_lockdown_check()
    return json.loads(output.getvalue())


def _run_hook_bad_stdin() -> dict:
    """Run hook_db_lockdown_check with malformed stdin, return parsed stdout."""
    from yadgar.core.scripts.hook_runner import hook_db_lockdown_check

    output = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO("not json {")):
        with patch("sys.stdout", output):
            hook_db_lockdown_check()
    return json.loads(output.getvalue())


class TestHookRunnerPreToolUseSchema:
    """hook_db_lockdown_check emits new hookSpecificOutput schema."""

    def test_hook_runner_pretooluse_allow_uses_new_schema(self):
        """Benign command → hookSpecificOutput.permissionDecision='allow', no 'decision' key."""
        result = _run_hook({"tool_input": {"command": "ls -la"}})
        assert "hookSpecificOutput" in result, "Must use new hookSpecificOutput schema"
        assert "decision" not in result, "Must NOT use old 'decision' key at top level"
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_hook_runner_pretooluse_deny_uses_new_schema(self):
        """docker exec yadgar-backend → hookSpecificOutput.permissionDecision='deny'."""
        result = _run_hook({"tool_input": {"command": "docker exec yadgar-backend bash"}})
        assert "hookSpecificOutput" in result, "Must use new hookSpecificOutput schema"
        assert "decision" not in result, "Must NOT use old 'decision' key at top level"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_hook_runner_pretooluse_deny_includes_system_message(self):
        """Block response must include systemMessage for user context."""
        result = _run_hook({"tool_input": {"command": "docker exec yadgar-db psql"}})
        assert "systemMessage" in result, "Block response must include systemMessage"
        assert len(result["systemMessage"]) > 0

    def test_hook_runner_pretooluse_json_error_allows(self):
        """Malformed stdin → graceful allow (fail-open), new schema."""
        result = _run_hook_bad_stdin()
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "decision" not in result
