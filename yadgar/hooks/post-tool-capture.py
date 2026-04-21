#!/usr/bin/env python3
"""Yadgar auto-capture — PostToolCall hook handler.

Reads tool call JSON from stdin, writes to action_log table.
Only imports stdlib (json, sys) plus surrealdb — no ML model loading.
Runs in <100ms. Backgrounded by the shell wrapper for zero latency.

Works in both stdio and HTTP transport modes because it writes
directly to the shared SurrealDB database.
"""

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path


# Tools to skip (Yadgar's own tools — prevents infinite loops)
_SKIP_PREFIXES = ("mcp__yadgar__",)

# High-value tool input fields to extract as summary
_SUMMARY_FIELDS = (
    "command", "content", "query", "file_path", "pattern",
    "prompt", "old_string", "skill", "description",
)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    tool_name = data.get("tool_name", "unknown")

    # Skip Yadgar's own tools to prevent capture loops
    for prefix in _SKIP_PREFIXES:
        if tool_name.startswith(prefix):
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

    db_path = Path(os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")).expanduser()

    try:
        from surrealdb import Surreal
        db = Surreal(f"surrealkv://{db_path}")
        db.use("yadgar", "main")
        ts = datetime.now(timezone.utc).isoformat()
        db.query(
            "CREATE action_log SET "
            "tool_name = $tn, "
            "tool_input_summary = $s, "
            "directory = $d, "
            "session_id = $sid, "
            "timestamp = $ts, "
            "processed = false",
            {"tn": tool_name, "s": summary, "d": cwd, "sid": session_id, "ts": ts},
        )
    except Exception:
        # Never fail the hook — swallow all errors
        pass


if __name__ == "__main__":
    main()
