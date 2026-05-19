"""Yadgar FileChanged hook implementation — importable module.

Split from file-changed.py (entry-point) for testability.
The entry-point script imports main() from here; falls back to inline
implementation when the yadgar package is not on sys.path.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

_PORT = os.environ.get("YADGAR_PORT", "8765")
_AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

# team_inbox pattern: ~/.claude/team_inbox/<projectId>/<teamName>/<agentName>.jsonl
_TEAM_INBOX_RE = re.compile(
    r"[/\\]\.claude[/\\]team_inbox[/\\][^/\\]+[/\\][^/\\]+[/\\][^/\\]+\.jsonl$"
)
# PLAN file pattern: **/docs/PLAN_*.md (covers PLAN_V*.md via wildcard)
_PLAN_FILE_RE = re.compile(r"[/\\]docs[/\\]PLAN_[^/\\]*\.md$")


def is_team_inbox_path(path: str) -> bool:
    """Return True if path is a team_inbox JSONL file."""
    return bool(_TEAM_INBOX_RE.search(path))


def is_plan_file_path(path: str) -> bool:
    """Return True if path is a PLAN_*.md file under a docs/ directory."""
    return bool(_PLAN_FILE_RE.search(path))


def _post_file_changed(file_path: str, file_action: str) -> None:
    """POST to the daemon's /hooks/file-changed endpoint."""
    payload = json.dumps({"file_path": file_path, "file_action": file_action}).encode()
    headers = {"Content-Type": "application/json"}
    if _AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {_AUTH_TOKEN}"
    encoded_path = urllib.parse.quote(file_path, safe="")
    url = f"http://127.0.0.1:{_PORT}/hooks/file-changed?path={encoded_path}"
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        urllib.request.urlopen(req, timeout=3.0)
    except Exception:
        pass


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return

    file_path = str(data.get("file_path", "")).strip()
    file_action = str(data.get("file_action", "modified")).strip()

    if not file_path:
        return

    # Only act on team_inbox or PLAN files
    if not (is_team_inbox_path(file_path) or is_plan_file_path(file_path)):
        return

    # Skip deletions — nothing to read
    if file_action == "deleted":
        return

    _post_file_changed(file_path, file_action)
