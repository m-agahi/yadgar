"""v5.20.0 — db-lockdown-check standalone hook tests.

TDD: written before implementation. Tests verify:
1. Benign command → allow + hookEventName present
2. docker exec yadgar-backend → deny + hookEventName present
3. docker exec yadgar-db → deny + hookEventName present
4. Malformed stdin JSON → fail-soft → allow + hookEventName present
5. Schema: hookEventName == "PreToolUse" in both allow and deny paths

These tests invoke the hook script as a subprocess (simulating Claude Code's
hook runner) so they exercise the real entry point without import gymnastics.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK_SCRIPT = Path(__file__).parent.parent / "core" / "hooks" / "db-lockdown-check.py"


def _run_hook(stdin_data: str) -> dict:
    """Run the hook script, feed stdin_data, return parsed JSON output."""
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, (
        f"Hook exited non-zero: {result.returncode}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout.strip())


def _tool_input(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


# ── Test 1: benign command → allow ──────────────────────────────────────────


def test_benign_command_allows():
    """A harmless command must emit allow."""
    out = _run_hook(_tool_input("ls -la"))
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision == "allow"


# ── Test 2: docker exec yadgar-backend → deny ──────────────────────────────


def test_docker_exec_yadgar_backend_denies():
    """docker exec yadgar-backend must be blocked."""
    out = _run_hook(_tool_input("docker exec yadgar-backend bash"))
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"


# ── Test 3: docker exec yadgar-db → deny ───────────────────────────────────


def test_docker_exec_yadgar_db_denies():
    """docker exec yadgar-db must be blocked."""
    out = _run_hook(_tool_input("docker exec yadgar-db psql"))
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"


# ── Test 4: malformed JSON → fail-soft allow ────────────────────────────────


def test_malformed_json_failsoft_allows():
    """Garbage stdin must not crash; must emit allow."""
    out = _run_hook("not-json-at-all{{{{")
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision == "allow"


# ── Test 5: hookEventName present in both paths ─────────────────────────────


def test_hook_event_name_present_on_allow():
    """Schema: hookEventName == 'PreToolUse' on allow path."""
    out = _run_hook(_tool_input("echo hello"))
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_hook_event_name_present_on_deny():
    """Schema: hookEventName == 'PreToolUse' on deny path."""
    out = _run_hook(_tool_input("docker exec yadgar-backend ls"))
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_hook_event_name_present_on_failsoft():
    """Schema: hookEventName == 'PreToolUse' on fail-soft allow path."""
    out = _run_hook("{broken")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
