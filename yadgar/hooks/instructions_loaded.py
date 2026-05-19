"""Yadgar InstructionsLoaded hook logic — importable module.

This module contains the implementation used by both:
- yadgar/hooks/instructions-loaded.py (Claude Code hook script, run directly)
- yadgar/tests/test_instructions_loaded_hook.py (test imports)

Fires a lightweight recall (~3 results) when CLAUDE.md is loaded at
session_start or compact. Skips all other load_reason values to avoid spam.

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

import json
import os
import sys
import urllib.parse
import urllib.request

_PORT = os.environ.get("YADGAR_PORT", "8765")
_AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

# Only fire on these load_reason values — session_start and compact are the
# meaningful cases where fresh context injection adds value. Other values
# (nested_traversal, path_glob_match, include) fire repeatedly on every
# nested CLAUDE.md load and would cause excessive daemon traffic.
_FIRE_ON_REASONS = frozenset({"session_start", "compact"})


def _auth_headers() -> dict:
    if _AUTH_TOKEN:
        return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
    return {}


def _should_fire(data: dict) -> bool:
    """Return True iff this InstructionsLoaded event should trigger a recall."""
    return data.get("load_reason", "") in _FIRE_ON_REASONS


def _parse_payload(data: dict) -> dict:
    """Extract and normalise fields from the InstructionsLoaded payload."""
    return {
        "session_id": str(data.get("session_id", "")),
        "file_path": str(data.get("file_path", "")),
        "load_reason": str(data.get("load_reason", "")),
        "memory_type": str(data.get("memory_type", "")),
    }


def _call_daemon(file_path: str, load_reason: str) -> str:
    """GET /hooks/instructions-loaded on the daemon.

    Returns the text to inject, or empty string on any error.
    """
    params = {"file_path": file_path, "load_reason": load_reason}
    url = f"http://127.0.0.1:{_PORT}/hooks/instructions-loaded?{urllib.parse.urlencode(params)}"
    headers = _auth_headers()
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=2.0)
        data = json.loads(resp.read().decode())
        return data.get("text", "")
    except Exception:
        return ""


def main() -> None:
    """Entry point called by the hook script."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return

    if not _should_fire(data):
        return

    parsed = _parse_payload(data)
    text = _call_daemon(parsed["file_path"], parsed["load_reason"])
    if text:
        print(text)
