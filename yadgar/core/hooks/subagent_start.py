"""Yadgar SubagentStart hook logic — importable module.

This module contains the implementation used by both:
- yadgar/hooks/subagent-start.py (Claude Code hook script, run directly)
- yadgar/tests/test_subagent_start_hook.py (test imports)

Reads the SubagentStart payload, extracts task description + context,
and POSTs to the daemon for recall-based context injection.

Empirical note: SubagentStart payload schema is not verified against a live
Claude Code run as of v5.3.2 implementation. Expected fields based on
Claude Code 2026 docs:
  {
    "session_id": "...",
    "hook_event_name": "SubagentStart",
    "agent_type": "general-purpose" | "Explore" | ...,
    "agent_id": "...",
    "cwd": "/path/to/project",
    "description": "task description" | "prompt"
  }

Some versions may use "prompt" instead of "description". Both are tried.

Output: text printed to stdout — Claude Code injects into subagent's context.
Errors: swallowed silently — never block subagent start.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing
from yadgar.core.install.auth_token import resolve_auth_token

_PORT = os.environ.get("YADGAR_PORT", "8765")


@observe(tier="hot")
def _auth_headers() -> dict:
    # Car 9: route through the ONE sanctioned bearer-token resolver (env var,
    # else secrets.env) rather than a bare os.environ.get. Resolved HERE
    # (call time), not as a module-level constant — each hook invocation is
    # a fresh short-lived process, so computing this at import time would do
    # the secrets.env file read unconditionally on every process start.
    token = resolve_auth_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


@observe(tier="hot")
def _parse_payload(data: dict) -> dict:
    """Extract and normalise fields from the SubagentStart payload.

    Falls back to safe defaults for any missing field — the schema is not
    fully verified empirically against a live Claude Code SubagentStart event.
    """
    # "description" is the primary field; "prompt" is a fallback for older versions
    description = str(data.get("description", "") or data.get("prompt", ""))
    return {
        "session_id": str(data.get("session_id", "")),
        "agent_type": str(data.get("agent_type", "general-purpose")) or "general-purpose",
        "agent_id": str(data.get("agent_id", "")),
        "cwd": str(data.get("cwd", os.getcwd())) or os.getcwd(),
        "description": description,
    }


@observe(tier="stage")
def _call_daemon(agent_type: str, cwd: str, description: str) -> str:
    """POST to /hooks/subagent-start on the daemon.

    Returns the text to inject, or empty string on any error.
    """
    params = {"agent_type": agent_type, "cwd": cwd}
    url = f"http://127.0.0.1:{_PORT}/hooks/subagent-start?{urllib.parse.urlencode(params)}"
    payload = json.dumps({"description": description, "cwd": cwd}).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with contextlib.closing(urllib.request.urlopen(req, timeout=2.0)) as resp:
            data = json.loads(resp.read().decode())
        return data.get("text", "")
    except urllib.error.HTTPError as e:
        # Close the file wrapper (py3.14 ResourceWarning leak guard).
        e.close()
        return ""
    except (AttributeError, OSError, ValueError):  # fmt: skip
        return ""


@observe(tier="boundary")
def main() -> None:
    """Entry point called by the hook script."""
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except (OSError, ValueError):  # fmt: skip
            return

        parsed = _parse_payload(data)
        text = _call_daemon(parsed["agent_type"], parsed["cwd"], parsed["description"])
        if text:
            print(text)
    finally:
        shutdown_tracing()
