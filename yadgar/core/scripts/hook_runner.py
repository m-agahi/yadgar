#!/usr/bin/env python3
"""Yadgar hook runner — executes hook logic for a given hook type.

This script is installed as a real file and referenced by ABSOLUTE PATH
in ~/.claude/settings.json. The project directory (or other context) is
passed as argv[1], never shell-interpolated.

Usage:
    hook_runner.py <hook_type> [project_directory]

hook_type:
    post-tool-capture       — PostToolUse handler
    session-start-context   — SessionStart handler
    post-compact-rehydrate  — SessionStart (compact) handler
    pre-compact-drain       — PreCompact handler
    prompt-recall           — UserPromptSubmit handler
    db-lockdown-check       — PreToolUse (Bash guard)

By referencing this script by absolute path and passing context as argv[1],
we avoid all shell metacharacter injection risks present in the previous
inline `python3 -c "..."` approach.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

_PORT = os.environ.get("YADGAR_PORT", "8765")
_AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")


def _auth_headers() -> dict:
    """Return Authorization header dict if token is set."""
    if _AUTH_TOKEN:
        return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
    return {}


def _http_get(path: str, params: dict | None = None, timeout: float = 2.0) -> dict | None:
    url = f"http://127.0.0.1:{_PORT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def _http_post(path: str, payload: dict, timeout: float = 1.0) -> dict | None:
    url = f"http://127.0.0.1:{_PORT}{path}"
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def hook_post_tool_capture() -> None:
    """PostToolUse — capture tool action into action_log."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as _e:
        return

    tool_name = data.get("tool_name", "unknown")
    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")

    _SKIP_PREFIXES = (
        "mcp__yadgar__",
        "mcp__plugin_claude-code-home-manager_yadgar__",
        "mcp__plugin_oh-my-claudecode_t__",
    )
    for prefix in _SKIP_PREFIXES:
        if tool_name.startswith(prefix):
            return

    _CAPTURE_TOOLS = frozenset({"Write", "Edit", "Bash", "NotebookEdit", "Agent"})
    if tool_name not in _CAPTURE_TOOLS:
        return

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

    _http_post(
        "/hooks/auto-capture",
        {
            "tool_name": tool_name,
            "summary": summary,
            "directory": cwd,
            "session_id": session_id,
        },
    )


def hook_session_start_context() -> None:
    """SessionStart — inject project context into Claude's conversation."""
    try:
        data = json.load(sys.stdin)
        cwd = data.get("cwd", os.getcwd())
        source = data.get("source", "")
    except Exception:
        cwd = os.getcwd()
        source = ""

    params: dict = {"directory": cwd}
    if source:
        params["source"] = source

    result = _http_get("/hooks/session-context", params)
    if result:
        text = result.get("text", "")
        if text:
            print(text)


def hook_post_compact_rehydrate() -> None:
    """SessionStart (compact) — full restore after context compaction."""
    try:
        data = json.load(sys.stdin)
        directory = data.get("cwd", os.getcwd())
    except Exception:
        directory = os.getcwd()

    result = _http_get("/hooks/post-compact", {"directory": directory})
    if result:
        # BUG 1 fix: backend /hooks/post-compact returns the restore markdown
        # under `formatted` (checkpoint_restore.py:399). Prefer it; keep the old
        # text/context keys as a defensive fallback for any other response shape.
        text = result.get("formatted", result.get("text", result.get("context", "")))
        if text:
            print(text)


def _log_hook_error(msg: str) -> None:
    """Append a one-line hook status to ~/.claude/yadgar-hook-errors.log.

    Best-effort — a logging failure must never propagate into the hook (the hook
    must exit 0 so it never blocks compaction). Car fix-drain-inflight surfaced
    that the wired PreCompact swallow (`> /dev/null 2>&1` in the .sh, and this
    runner's silent HTTP failure) hid drain outcomes entirely.
    """
    try:
        import time  # noqa: PLC0415

        log_path = os.path.join(os.path.expanduser("~"), ".claude", "yadgar-hook-errors.log")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} pre-compact-drain {msg}\n")
    except Exception:
        pass  # never block the hook on a logging failure


def _capture_in_flight_host(transcript_path: str, directory: str) -> dict | None:
    """Parse in-flight orchestration state on the HOST (Car fix-drain-inflight).

    The runner is invoked by Claude Code ON THE HOST, so the transcript + git
    worktree tree are visible here (unlike the backend container). Parse them and
    return the in_flight dict for the POST body. Guarded lazy import: the pinned
    interpreter may be a bare PATH python3 without yadgar importable — on any
    failure return None and degrade to a POST without in_flight (never crash the
    hook, never block compaction).
    """
    from yadgar._shared.restoration.transcript_parse import capture_in_flight  # noqa: PLC0415

    return capture_in_flight(transcript_path, directory)


def hook_pre_compact_drain() -> None:
    """PreCompact — drain context before compaction."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    # Car fix-drain-inflight: parse in_flight HOST-SIDE (post-reinstall the
    # installer wires this runner instead of the .sh). The backend container
    # cannot see the host .claude transcript / git tree, so it must arrive parsed.
    transcript_path = data.get("transcript_path")
    if transcript_path:
        try:
            directory = data.get("cwd") or os.getcwd()
            in_flight = _capture_in_flight_host(transcript_path, directory)
            if in_flight is not None:
                data["in_flight"] = in_flight
        except Exception as e:  # import/parse failure → degrade, never crash
            _log_hook_error(f"in_flight capture failed: {e!r}")

    result = _http_post("/hooks/pre-compact", data)
    if result is None:
        _log_hook_error("drain POST /hooks/pre-compact failed (backend unreachable?)")


def hook_prompt_recall() -> None:
    """UserPromptSubmit — auto-recall relevant memories."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as _e:
        return

    prompt = data.get("prompt", "") or data.get("user_prompt", "")
    if not prompt or len(str(prompt).strip()) < 2:
        return

    directory = data.get("cwd", "") or os.getcwd()
    result = _http_get(
        "/hooks/prompt-recall",
        {"query": str(prompt).strip(), "directory": directory},
        timeout=0.5,
    )
    if result:
        text = result.get("text", "")
        if text:
            print(text)


_BLOCKED_EXEC_PATTERNS = (
    "docker exec yadgar-backend",
    "docker exec yadgar-db",
)


def hook_db_lockdown_check() -> None:
    """PreToolUse (Bash) — block direct docker exec into yadgar containers.

    Restored in v5.46.5: the standalone yadgar/hooks/db-lockdown-check.py
    replaced this function in v5.20.0, but test_hook_runner_pretooluse_schema.py
    still imports hook_db_lockdown_check from this module (B3 CI fix).
    This function exports the same logic for test compatibility.
    """
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):  # fmt: skip
        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))
        return

    cmd = ""
    tool_input = data.get("tool_input", {})
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command", "")

    for pattern in _BLOCKED_EXEC_PATTERNS:
        if pattern in cmd:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {"permissionDecision": "deny"},
                        "systemMessage": (
                            "Direct docker exec into yadgar DB/backend containers is blocked "
                            "to prevent data corruption. Use yadgar MCP tools instead."
                        ),
                    }
                )
            )
            return

    print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))


_BLOCK_REFLECT_TOOLS = frozenset(
    {
        "mcp__yadgar__block_create",
        "mcp__yadgar__block_update",
        "mcp__yadgar__block_delete",
        "mcp__yadgar__block_replace",
        "mcp__yadgar__block_append",
    }
)


def hook_block_reflect() -> None:
    """PostToolUse — re-inject block contents after any block_* write tool (v5.35.1).

    Fires only when tool_name is one of the five block write tools.
    Fetches current block state from the daemon and prints to stdout for injection.
    """
    try:
        data = json.load(sys.stdin)
    except Exception:  # JSONDecodeError, ValueError
        return

    tool_name = data.get("tool_name", "")
    if tool_name not in _BLOCK_REFLECT_TOOLS:
        return

    cwd = data.get("cwd", os.getcwd())
    result = _http_get("/hooks/block-reflect", {"directory": cwd}, timeout=0.5)
    if result:
        text = result.get("text", "")
        if text:
            print(text)


_HOOKS = {
    "post-tool-capture": hook_post_tool_capture,
    "session-start-context": hook_session_start_context,
    "post-compact-rehydrate": hook_post_compact_rehydrate,
    "pre-compact-drain": hook_pre_compact_drain,
    "prompt-recall": hook_prompt_recall,
    "block-reflect": hook_block_reflect,
    # db-lockdown-check removed in v5.20.0 — migrated to standalone
    # yadgar/hooks/db-lockdown-check.py, installed as
    # ~/.claude/hooks/yadgar-db-lockdown-check.py by install_hooks.
}


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <hook_type> [project_directory]", file=sys.stderr)
        print(f"Available hook types: {', '.join(_HOOKS)}", file=sys.stderr)
        sys.exit(1)

    hook_type = sys.argv[1]
    handler = _HOOKS.get(hook_type)
    if handler is None:
        print(f"Unknown hook type: {hook_type!r}", file=sys.stderr)
        print(f"Available: {', '.join(_HOOKS)}", file=sys.stderr)
        sys.exit(1)

    handler()


if __name__ == "__main__":
    main()
