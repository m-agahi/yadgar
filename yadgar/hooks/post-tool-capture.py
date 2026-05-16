#!/usr/bin/env python3
"""Yadgar auto-capture — PostToolCall hook handler.

Reads tool call JSON from stdin, writes to action_log table.
Only imports stdlib (json, sys) plus surrealdb — no ML model loading.
Runs in <100ms. Backgrounded by the shell wrapper for zero latency.

HTTP-only: writes via daemon HTTP endpoint. No direct surrealkv
access — the host path differs from the container path (/data/...).
"""

import json
import os
import sys
from pathlib import Path

# Tool name prefixes that are self-referential — never capture
_SKIP_PREFIXES = (
    "mcp__yadgar__",
    "mcp__plugin_claude-code-home-manager_yadgar__",
    "mcp__plugin_oh-my-claudecode_t__",
)

# Only capture state-modifying tools; skip Read, Glob, Grep, WebFetch, etc.
_CAPTURE_TOOLS = frozenset({"Write", "Edit", "Bash", "NotebookEdit", "Agent"})

# High-value tool input fields to extract as summary
_SUMMARY_FIELDS = (
    "command",
    "content",
    "query",
    "file_path",
    "pattern",
    "prompt",
    "old_string",
    "skill",
    "description",
)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as _e:
        return

    tool_name = data.get("tool_name", "unknown")

    # Skip self-referential Yadgar tools
    for prefix in _SKIP_PREFIXES:
        if tool_name.startswith(prefix):
            return

    # Only capture state-modifying tools
    if tool_name not in _CAPTURE_TOOLS:
        return

    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")

    # Extract a brief summary from the tool input
    tool_input = data.get("tool_input", {})
    summary = ""
    if isinstance(tool_input, dict):
        for field in _SUMMARY_FIELDS:
            val = tool_input.get(field)
            if val:
                summary = str(val)[:200]
                break
        if not summary:
            summary = str(tool_input)[:200]
    else:
        summary = str(tool_input)[:200]

    Path(os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")).expanduser()

    # Try HTTP endpoint first — works in daemon mode where DB lock is always held
    _port = os.environ.get("YADGAR_PORT", "8765")
    try:
        import urllib.request as _req

        _payload = json.dumps(
            {
                "tool_name": tool_name,
                "summary": summary,
                "directory": cwd,
                "session_id": session_id,
            }
        ).encode()
        _r = _req.Request(
            f"http://127.0.0.1:{_port}/hooks/auto-capture",
            data=_payload,
            headers={"Content-Type": "application/json"},
        )
        _req.urlopen(_r, timeout=1)
        return
    except Exception:
        pass  # Daemon down — skip; never use surrealkv directly from host


if __name__ == "__main__":
    main()
