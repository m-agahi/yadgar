"""v5.46.5 RED test — B3: hook_db_lockdown_check importable from hook_runner.

Verifies the function is exported at the module top-level and returns the
expected hookSpecificOutput schema (no hookEventName required at this level).
"""

from __future__ import annotations


def test_hook_db_lockdown_check_importable():
    """Function must be importable from yadgar.scripts.hook_runner."""
    from yadgar.scripts.hook_runner import hook_db_lockdown_check  # noqa: F401


def test_hook_db_lockdown_check_callable():
    """Imported function must be callable."""
    from yadgar.scripts.hook_runner import hook_db_lockdown_check

    assert callable(hook_db_lockdown_check)


def test_hook_db_lockdown_check_allow_schema():
    """Benign command → hookSpecificOutput.permissionDecision='allow'."""
    import io
    import json
    import sys
    from unittest.mock import patch

    from yadgar.scripts.hook_runner import hook_db_lockdown_check

    output = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"command": "ls"}}))):
        with patch("sys.stdout", output):
            hook_db_lockdown_check()

    result = json.loads(output.getvalue())
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hook_db_lockdown_check_deny_schema():
    """docker exec yadgar-backend → hookSpecificOutput.permissionDecision='deny'."""
    import io
    import json
    import sys
    from unittest.mock import patch

    from yadgar.scripts.hook_runner import hook_db_lockdown_check

    output = io.StringIO()
    with patch.object(
        sys,
        "stdin",
        io.StringIO(json.dumps({"tool_input": {"command": "docker exec yadgar-backend bash"}})),
    ):
        with patch("sys.stdout", output):
            hook_db_lockdown_check()

    result = json.loads(output.getvalue())
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
