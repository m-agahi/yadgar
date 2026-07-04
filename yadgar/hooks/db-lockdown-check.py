#!/usr/bin/env python3
"""Yadgar db-lockdown-check — PreToolUse (Bash) hook.

Blocks direct docker exec into yadgar-backend and yadgar-db containers.
Direct exec bypasses the MCP layer and risks data corruption / lock conflicts.

Schema: Claude Code 2026 PreToolUse — output must include hookEventName.
Fail-soft: malformed stdin → allow (never silently block work).
"""

import json
import sys

try:
    from yadgar.observability.observe import observe
    from yadgar.tracing import shutdown_tracing
except ImportError:

    def observe(*_a, **_k):
        return lambda fn: fn

    def shutdown_tracing(*_a, **_k):
        pass


_BLOCKED_PATTERNS = (
    "docker exec yadgar-backend",
    "docker exec yadgar-db",
)


def _allow() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _deny(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
        },
        "systemMessage": message,
    }


@observe(tier="boundary")
def main() -> None:
    try:
        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):  # fmt: skip
            print(json.dumps(_allow()))
            return

        cmd = ""
        tool_input = data.get("tool_input", {})
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command", "")

        for pattern in _BLOCKED_PATTERNS:
            if pattern in cmd:
                print(
                    json.dumps(
                        _deny(
                            "Direct docker exec into yadgar DB/backend containers is blocked "
                            "to prevent data corruption. Use yadgar MCP tools instead."
                        )
                    )
                )
                return

        print(json.dumps(_allow()))
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
