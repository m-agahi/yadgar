#!/usr/bin/env python3
"""Yadgar FileChanged hook — entry point for Claude Code hook system.

This script is installed to ~/.claude/hooks/yadgar-file-changed.py
and registered in settings.json under the FileChanged event with an
empty matcher (all file changes — script-side filter decides what to act on).

Claude Code FileChanged event payload (stdin JSON):
  {
    "session_id": "...",
    "transcript_path": "...",
    "cwd": "/path/to/project",
    "hook_event_name": "FileChanged",
    "file_path": "/absolute/path/to/changed/file",
    "file_action": "created" | "modified" | "deleted"
  }

Filters (two use cases):
  1. team_inbox — path matches ~/.claude/team_inbox/**/*.jsonl
     → POST /hooks/file-changed with path + action → daemon reads new JSONL lines
       and writes them to action_log as team_message entries.
  2. plan files — path matches **/docs/plans/<slug>.md (excludes docs/plans/archive/)
     → POST /hooks/file-changed with path + action → daemon reads file content
       and memorizes with _plan tag.

Output: nothing (FileChanged hooks cannot inject into model context).
Errors: swallowed silently — never block the session.
"""

from __future__ import annotations

import sys

# Try to import from the yadgar package (daemon-installed or pipx venv).
# Fall back to the self-contained inline implementation if not importable.
try:
    from yadgar.core.hooks.file_changed import main
except ImportError:
    # Standalone fallback — duplicate of file_changed.py logic for portability.
    # Keeps this script functional even without the yadgar package on sys.path.
    import contextlib
    import json
    import os
    import re
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

    # team_inbox pattern: ~/.claude/team_inbox/**/*.jsonl
    _TEAM_INBOX_RE = re.compile(
        r"[/\\]\.claude[/\\]team_inbox[/\\][^/\\]+[/\\][^/\\]+[/\\][^/\\]+\.jsonl$"
    )
    # PLAN file pattern: **/docs/plans/<slug>.md (open plans; excludes docs/plans/archive/)
    _PLAN_FILE_RE = re.compile(r"[/\\]docs[/\\]plans[/\\][^/\\]+\.md$")

    def _is_team_inbox(path: str) -> bool:
        return bool(_TEAM_INBOX_RE.search(path))

    def _is_plan_file(path: str) -> bool:
        return bool(_PLAN_FILE_RE.search(path))

    @observe(
        exempt="portability inline fallback duplicate — runs only when yadgar package unimportable; primary path is the underscore module"
    )
    def _post_file_changed(file_path: str, file_action: str) -> None:
        payload = json.dumps({"file_path": file_path, "file_action": file_action}).encode()
        headers = {"Content-Type": "application/json"}
        if _AUTH_TOKEN:
            headers["Authorization"] = f"Bearer {_AUTH_TOKEN}"
        encoded_path = urllib.parse.quote(file_path, safe="")
        url = f"http://127.0.0.1:{_PORT}/hooks/file-changed?path={encoded_path}"
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with contextlib.closing(urllib.request.urlopen(req, timeout=3.0)):
                pass
        except urllib.error.HTTPError as e:
            # Close the file wrapper (py3.14 ResourceWarning leak guard).
            e.close()
        except (OSError, ValueError):  # fmt: skip
            pass

    @observe(
        exempt="portability inline fallback duplicate — runs only when yadgar package unimportable; primary path is the underscore module"
    )
    def main():
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except (OSError, ValueError):  # fmt: skip
            return

        file_path = str(data.get("file_path", "")).strip()
        file_action = str(data.get("file_action", "modified")).strip()

        if not file_path:
            return

        # Only act on team_inbox or PLAN files
        if not (_is_team_inbox(file_path) or _is_plan_file(file_path)):
            return

        # Skip deletions — nothing to read
        if file_action == "deleted":
            return

        _post_file_changed(file_path, file_action)


if __name__ == "__main__":
    main()
