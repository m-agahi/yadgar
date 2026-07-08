"""Unit tests for yadgar/hooks/db-lockdown-check.py — importlib-based.

Wave 5 group B coverage. Strategy: load the module via importlib (hyphen in
filename prevents normal import). Directly exercise _allow, _deny, and main()
by patching sys.stdin — no subprocess overhead, yields line coverage.

TDD: written before verifying coverage (red → green).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

_HOOK = Path(__file__).parent.parent.parent / "core" / "hooks" / "db-lockdown-check.py"


def _load_hook():
    """Import hook module from its file path, bypassing __main__ guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_db_lockdown_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _allow() helper
# ---------------------------------------------------------------------------


class TestAllowHelper:
    def test_allow_returns_dict_with_correct_schema(self):
        mod = _load_hook()
        result = mod._allow()
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_allow_has_no_system_message(self):
        mod = _load_hook()
        result = mod._allow()
        assert "systemMessage" not in result


# ---------------------------------------------------------------------------
# _deny() helper
# ---------------------------------------------------------------------------


class TestDenyHelper:
    def test_deny_returns_dict_with_correct_schema(self):
        mod = _load_hook()
        result = mod._deny("blocked")
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_deny_includes_system_message(self):
        mod = _load_hook()
        msg = "my custom block message"
        result = mod._deny(msg)
        assert result["systemMessage"] == msg

    def test_deny_arbitrary_message(self):
        mod = _load_hook()
        result = mod._deny("another message")
        assert "another message" in result["systemMessage"]


# ---------------------------------------------------------------------------
# main() — benign command → allow
# ---------------------------------------------------------------------------


class TestMainAllow:
    def _run_main(self, stdin_json: str) -> dict:
        mod = _load_hook()
        captured = []
        with (
            patch.object(mod.sys, "stdin", io.StringIO(stdin_json)),
            patch("builtins.print", side_effect=lambda s: captured.append(s)),
        ):
            mod.main()
        assert captured, "main() printed nothing"
        return json.loads(captured[-1])

    def test_benign_command_allows(self):
        payload = json.dumps({"tool_input": {"command": "ls -la /tmp"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_hook_event_name_on_allow(self):
        payload = json.dumps({"tool_input": {"command": "echo hello"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_empty_command_allows(self):
        payload = json.dumps({"tool_input": {"command": ""}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_no_tool_input_key_allows(self):
        payload = json.dumps({"tool_name": "Bash"})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_tool_input_not_dict_allows(self):
        payload = json.dumps({"tool_input": "string-not-dict"})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_docker_exec_other_container_allows(self):
        payload = json.dumps({"tool_input": {"command": "docker exec my-app bash"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# main() — blocked patterns → deny
# ---------------------------------------------------------------------------


class TestMainDeny:
    def _run_main(self, stdin_json: str) -> dict:
        mod = _load_hook()
        captured = []
        with (
            patch.object(mod.sys, "stdin", io.StringIO(stdin_json)),
            patch("builtins.print", side_effect=lambda s: captured.append(s)),
        ):
            mod.main()
        assert captured, "main() printed nothing"
        return json.loads(captured[-1])

    def test_docker_exec_yadgar_backend_denies(self):
        payload = json.dumps({"tool_input": {"command": "docker exec yadgar-backend bash"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_docker_exec_yadgar_db_denies(self):
        payload = json.dumps({"tool_input": {"command": "docker exec yadgar-db psql"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_deny_includes_system_message(self):
        payload = json.dumps({"tool_input": {"command": "docker exec yadgar-backend ls"}})
        out = self._run_main(payload)
        assert "systemMessage" in out
        assert "yadgar" in out["systemMessage"].lower() or "docker" in out["systemMessage"].lower()

    def test_deny_hook_event_name(self):
        payload = json.dumps({"tool_input": {"command": "docker exec yadgar-db bash"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_backend_pattern_substring_match(self):
        """Pattern matched anywhere in command string."""
        payload = json.dumps({"tool_input": {"command": "sudo docker exec yadgar-backend /bin/sh"}})
        out = self._run_main(payload)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# main() — malformed stdin → fail-soft allow
# ---------------------------------------------------------------------------


class TestMainFailSoft:
    def _run_main(self, stdin_text: str) -> dict:
        mod = _load_hook()
        captured = []
        with (
            patch.object(mod.sys, "stdin", io.StringIO(stdin_text)),
            patch("builtins.print", side_effect=lambda s: captured.append(s)),
        ):
            mod.main()
        assert captured, "main() printed nothing"
        return json.loads(captured[-1])

    def test_garbage_input_allows(self):
        out = self._run_main("not-json-at-all{{{{")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_empty_input_allows(self):
        out = self._run_main("")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_partial_json_allows(self):
        out = self._run_main("{broken")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_failsoft_hook_event_name(self):
        out = self._run_main("not valid json")
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


# ---------------------------------------------------------------------------
# BLOCKED_PATTERNS constant
# ---------------------------------------------------------------------------


class TestBlockedPatterns:
    def test_both_patterns_present(self):
        mod = _load_hook()
        patterns = mod._BLOCKED_PATTERNS
        assert any("yadgar-backend" in p for p in patterns)
        assert any("yadgar-db" in p for p in patterns)

    def test_patterns_is_tuple(self):
        mod = _load_hook()
        assert isinstance(mod._BLOCKED_PATTERNS, tuple)
