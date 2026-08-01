#!/usr/bin/env python3
"""Yadgar SubagentStart hook — entry point for Claude Code hook system.

This script is installed to ~/.claude/hooks/yadgar-subagent-start.py
and registered in settings.json under the SubagentStart event.

Logic lives in yadgar/hooks/subagent_start.py for testability.
When run standalone (installed copy), falls back to inline impl.

Claude Code SubagentStart event payload (stdin JSON):
  {
    "session_id": "...",
    "hook_event_name": "SubagentStart",
    "agent_type": "general-purpose" | "Explore" | ...,
    "agent_id": "...",
    "cwd": "/path/to/project",
    "description": "task description"   (may be "prompt" in older versions)
  }

Note: SubagentStart payload schema is empirically unverified as of v5.3.2.
Safe defaults are used for all optional fields.

Output: text printed to stdout — Claude Code injects into subagent's context.
Errors: swallowed silently — never block subagent start.
"""

from __future__ import annotations

import sys

# Try to import from the yadgar package (daemon-installed or pipx venv).
# Fall back to the self-contained inline implementation if not importable.
try:
    from yadgar.core.hooks.subagent_start import main
except ImportError:
    # Standalone fallback — duplicate of subagent_start.py logic for portability.
    # Keeps this script functional even without the yadgar package on sys.path.
    import contextlib
    import json
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    try:
        from yadgar._shared.observability.observe import observe
    except ImportError:

        def observe(*_a, **_k):
            return lambda fn: fn

    _PORT = os.environ.get("YADGAR_PORT", "8765")
    _AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

    @observe(
        exempt="portability inline fallback duplicate — runs only when yadgar package unimportable; primary path is the underscore module"
    )
    def _auth_headers():
        if _AUTH_TOKEN:
            return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
        return {}

    @observe(
        exempt="portability inline fallback duplicate — runs only when yadgar package unimportable; primary path is the underscore module"
    )
    def main():
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            return

        description = str(data.get("description", "") or data.get("prompt", ""))
        agent_type = str(data.get("agent_type", "general-purpose")) or "general-purpose"
        cwd = str(data.get("cwd", os.getcwd())) or os.getcwd()

        params = {"agent_type": agent_type, "cwd": cwd}
        url = f"http://127.0.0.1:{_PORT}/hooks/subagent-start?{urllib.parse.urlencode(params)}"
        payload_bytes = json.dumps({"description": description, "cwd": cwd}).encode()
        headers = {"Content-Type": "application/json"}
        if _AUTH_TOKEN:
            headers["Authorization"] = f"Bearer {_AUTH_TOKEN}"
        try:
            req = urllib.request.Request(url, data=payload_bytes, headers=headers)
            with contextlib.closing(urllib.request.urlopen(req, timeout=2.0)) as resp:
                result = json.loads(resp.read().decode())
            text = result.get("text", "")
            if text:
                print(text)
        except urllib.error.HTTPError as e:
            # Close the file wrapper (py3.14 ResourceWarning leak guard).
            e.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
