"""Yadgar FileChanged hook implementation — importable module.

Split from file-changed.py (entry-point) for testability.
The entry-point script imports main() from here; falls back to inline
implementation when the yadgar package is not on sys.path.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing
from yadgar.core.install.auth_token import resolve_auth_token

_PORT = os.environ.get("YADGAR_PORT", "8765")
# Car 9: route through the ONE sanctioned bearer-token resolver (env var,
# else secrets.env) rather than a bare os.environ.get.
_AUTH_TOKEN = resolve_auth_token()

# team_inbox pattern: ~/.claude/team_inbox/<projectId>/<teamName>/<agentName>.jsonl
_TEAM_INBOX_RE = re.compile(
    r"[/\\]\.claude[/\\]team_inbox[/\\][^/\\]+[/\\][^/\\]+[/\\][^/\\]+\.jsonl$"
)
# PLAN file pattern: **/docs/plans/<slug>.md (open plans; excludes docs/plans/archive/)
_PLAN_FILE_RE = re.compile(r"[/\\]docs[/\\]plans[/\\][^/\\]+\.md$")


def is_team_inbox_path(path: str) -> bool:
    """Return True if path is a team_inbox JSONL file."""
    return bool(_TEAM_INBOX_RE.search(path))


def is_plan_file_path(path: str) -> bool:
    """Return True if path is an open plan: docs/plans/<slug>.md (not archive/)."""
    return bool(_PLAN_FILE_RE.search(path))


@observe(tier="stage")
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
        with contextlib.closing(urllib.request.urlopen(req, timeout=3.0)):
            pass
    except urllib.error.HTTPError as e:
        # Close the file wrapper (py3.14 ResourceWarning leak guard).
        e.close()
    except Exception:
        pass


@observe(tier="boundary")
def main() -> None:
    try:
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
    finally:
        shutdown_tracing()
