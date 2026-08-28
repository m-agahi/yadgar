#!/usr/bin/env python3
"""Yadgar InstructionsLoaded hook — entry point for Claude Code hook system.

This script is installed to ~/.claude/hooks/yadgar-instructions-loaded.py
and registered in settings.json under the InstructionsLoaded event.

Logic lives in yadgar/hooks/instructions_loaded.py for testability.
When run standalone (installed copy), falls back to inline impl.

Claude Code InstructionsLoaded event payload (stdin JSON):
  {
    "session_id": "...",
    "hook_event_name": "InstructionsLoaded",
    "file_path": "/path/to/CLAUDE.md",
    "memory_type": "global" | "local" | "project",
    "load_reason": "session_start" | "nested_traversal" | "path_glob_match"
                   | "include" | "compact",
    "globs": [...],
    "trigger_file_path": "...",
    "parent_file_path": "..."
  }

Output: text printed to stdout — Claude Code injects into model context.
Errors: swallowed silently — never block instructions load.
"""

from __future__ import annotations

import sys

# Try to import from the yadgar package (daemon-installed or pipx venv).
# Fall back to the self-contained inline implementation if not importable.
try:
    from yadgar.core.hooks.instructions_loaded import main
except ImportError:
    # Standalone fallback — duplicate of instructions_loaded.py logic.
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
    _FIRE_ON_REASONS = frozenset({"session_start", "compact"})

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
        except (OSError, ValueError):  # fmt: skip
            return

        load_reason = data.get("load_reason", "")
        if load_reason not in _FIRE_ON_REASONS:
            return

        file_path = str(data.get("file_path", ""))
        params = {"file_path": file_path, "load_reason": load_reason}
        url = f"http://127.0.0.1:{_PORT}/hooks/instructions-loaded?{urllib.parse.urlencode(params)}"
        headers = _auth_headers()
        try:
            req = urllib.request.Request(url, headers=headers)
            with contextlib.closing(urllib.request.urlopen(req, timeout=2.0)) as resp:
                result = json.loads(resp.read().decode())
            text = result.get("text", "")
            if text:
                print(text)
        except urllib.error.HTTPError as e:
            # Close the file wrapper (py3.14 ResourceWarning leak guard).
            e.close()
        except (AttributeError, OSError, ValueError):  # fmt: skip
            pass


if __name__ == "__main__":
    main()
