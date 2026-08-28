#!/usr/bin/env python3
"""Yadgar auto-capture — PostToolCall hook handler.

Reads tool call JSON from stdin, POSTs it to ``/hooks/auto-capture``.
No ML model loading. Backgrounded by the shell wrapper for zero latency.

HTTP-only: writes via daemon HTTP endpoint. No direct surrealkv
access — the host path differs from the container path (/data/...).

Two copies of this handler exist: this standalone script and
``core/cli/hook.py::hook_post_tool_capture`` (dispatched by
``hook_runner.py post-tool-capture``, which is what yadgar's own installer
wires). Which one runs is the CLIENT's choice — nix's home-manager module
installs a copy of THIS file into ``~/.claude/hooks/``. Both must therefore
carry the same payload contract; when they diverged on ``project_id`` the
capture pipeline died for six days on the nix-wired box while every signal
still read healthy (Car 20 / ledger task 303).
"""

import json
import os
import sys
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing
from yadgar.core.install.auth_token import resolve_auth_token

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


@observe(tier="stage")
def _mint_or_none(cwd: str) -> str | None:
    """Resolve *cwd*'s ``project_id`` host-side, or ``None``. Never raises.

    Car 20 (ledger task 303). The identity must be stamped HERE: this script
    is host-side, the daemon it POSTs to is not — the container has no git
    binary and no project mounts, so a project_id resolved on the far side of
    the call could only be manufactured (ADR-0227 §1.1). Without one,
    ``/hooks/auto-capture``'s ``_split_batch_by_project`` drops the action.

    Fail-OPEN, matching ``core/cli/hook.py::hook_post_tool_capture``:
    ``mint_project_id`` raises by design (ADR-0227 deleted every fallback),
    and a PostToolUse hook that crashes interferes with the user's tool call
    while a dropped telemetry row does not. ``None`` is returned rather than
    a guess — an unattributed row is skipped-and-counted downstream, never
    bucketed under an invented key.
    """
    try:
        from yadgar.core.hooks._identity_mint import mint_project_id  # noqa: PLC0415

        return mint_project_id(cwd)
    except Exception:  # noqa: BLE001 — never brick a tool call over identity
        return None


@observe(tier="boundary")
def main():
    try:
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

        Path(os.environ.get("YADGAR_DB_PATH", str(_paths.DB_PATH))).expanduser()

        # Try HTTP endpoint first — works in daemon mode where DB lock is always held
        _port = os.environ.get("YADGAR_PORT", "8765")
        try:
            import contextlib as _contextlib
            import urllib.error as _err
            import urllib.request as _req

            # Car 20: stamp the identity (see _mint_or_none). Omitted, never
            # empty — an absent key and a "" both read as unattributed
            # downstream, and omission keeps the payload honest.
            _body: dict = {
                "tool_name": tool_name,
                "summary": summary,
                "directory": cwd,
                "session_id": session_id,
            }
            _project_id = _mint_or_none(cwd)
            if _project_id:
                _body["project_id"] = _project_id

            _payload = json.dumps(_body).encode()
            _headers = {"Content-Type": "application/json"}
            # Car 9: route through the ONE sanctioned bearer-token resolver
            # (env var, else secrets.env) rather than a bare os.environ.get.
            _token = resolve_auth_token()
            if _token:
                _headers["Authorization"] = f"Bearer {_token}"
            _r = _req.Request(
                f"http://127.0.0.1:{_port}/hooks/auto-capture",
                data=_payload,
                headers=_headers,
            )
            with _contextlib.closing(_req.urlopen(_r, timeout=1)):
                pass
            return
        except _err.HTTPError as _http_exc:
            # Close the file wrapper (py3.14 ResourceWarning leak guard).
            _http_exc.close()
        except (ImportError, OSError, ValueError):  # fmt: skip
            pass  # Daemon down — skip; never use surrealkv directly from host
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
