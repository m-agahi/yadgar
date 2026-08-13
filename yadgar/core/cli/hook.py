"""Shared ``yadgar hook <event>`` entrypoint — the single hook body (Car 0).

This module is the ONE implementation of yadgar's per-hook logic (auth + branch
detection + the ``/hooks/*`` HTTP call + inject/POST). It is client-neutral: the
Claude Code ``hook_runner.py`` script is a thin shim that re-exports this
module's surface and delegates ``main()`` here, and the multi-client hook-emitter
(``clients/hooks_render.py``) wires every ported client's native hook to shell
out to ``yadgar hook <event>`` so the load-bearing logic lives here, once.

Extraction note (Car 0): the handler bodies + HTTP helpers + ``_HOOKS`` dispatch
table were MOVED here verbatim from ``yadgar/core/scripts/hook_runner.py`` to
collapse the two paths into one. ``hook_runner.py`` now imports this module.
Behavior is preserved exactly — the ``hook_runner`` characterization suite was
repointed at this module to prove it.

Event names (``yadgar hook <event>``) are the ``_HOOKS`` dispatch keys verbatim
so the shim maps 1:1:

    post-tool-capture       — PostToolUse handler
    session-start-context   — SessionStart handler
    post-compact-rehydrate  — SessionStart (compact) handler
    pre-compact-drain       — PreCompact handler
    prompt-recall           — UserPromptSubmit handler
    block-reflect           — PostToolUse memory-block re-inject

``db-lockdown-check``'s ``hook_db_lockdown_check`` remains exported (imported by
the PreToolUse-schema tests) but is NOT a ``yadgar hook`` event — the live
PreToolUse guard is the standalone router, and Stop lives in
``stop-memory-checkpoint.py`` (neither routes through this dispatcher).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from yadgar.core.install.auth_token import resolve_auth_token

_PORT = os.environ.get("YADGAR_PORT", "8765")


def _auth_headers() -> dict:
    """Return Authorization header dict if token is set.

    Car 9: route through the ONE sanctioned bearer-token resolver (env var,
    else secrets.env) — auth_token.py's own docstring notes it is "Stdlib +
    observability only" specifically so hook scripts like this one can import
    it cheaply. Resolved HERE (call time), not as a module-level constant:
    each hook invocation is a fresh short-lived process, and computing this
    at import time would do the secrets.env file read unconditionally on
    every process start rather than only when a header is actually needed.
    """
    token = resolve_auth_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _http_get(path: str, params: dict | None = None, timeout: float = 2.0) -> dict | None:
    url = f"http://127.0.0.1:{_PORT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with contextlib.closing(urllib.request.urlopen(req, timeout=timeout)) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Close the file wrapper (py3.14 ResourceWarning leak guard).
        e.close()
        return None
    except Exception:
        return None


def _http_post(path: str, payload: dict, timeout: float = 1.0) -> dict | None:
    url = f"http://127.0.0.1:{_PORT}{path}"
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with contextlib.closing(urllib.request.urlopen(req, timeout=timeout)) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Close the file wrapper (py3.14 ResourceWarning leak guard).
        e.close()
        return None
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

    # C4 (0047 PR#40 §5): stamp the identity HERE. This hook runner is
    # host-side (the same carve-out ``_emit_project_id`` uses), and the
    # daemon it POSTs to is not — the container has no git binary and no
    # project mounts, so a project_id resolved on the far side of this call
    # could only be manufactured (ADR-0227 §1.1).
    #
    # Fail-OPEN, unlike ``yadgar capture``: a PostToolUse hook that exits
    # non-zero interferes with the user's session, and the row's declared
    # failure path already exists downstream — an action_log row with no
    # project_id is skipped and counted by the consolidation summariser
    # rather than attributed to a guess.
    _payload: dict = {
        "tool_name": tool_name,
        "summary": summary,
        "directory": cwd,
        "session_id": session_id,
    }
    try:
        from yadgar.core.hooks._identity_mint import mint_project_id  # noqa: PLC0415

        _payload["project_id"] = mint_project_id(cwd)
    except Exception:  # noqa: BLE001 — never brick a tool call over identity
        pass

    _http_post("/hooks/auto-capture", _payload)


def _emit_project_id(cwd: str) -> str | None:
    """Mint the session's project_id, print the banner, return the id (or None).

    Car C2 / ADR-0227: the CLI hook runner is the second host-side SessionStart
    entry point (the opencode plugin shells out to ``yadgar hook <event>``), so
    it mints exactly like ``core/hooks/session-start-context.py`` does. Shared
    wording and shared policy live in ``_identity_mint``; only the printing is
    duplicated, because the two runners print to different transports.

    Fail-open on a crash: an unresolvable tree still prints a loud notice with
    no candidate key, but nothing here may prevent the rest of the hook running.
    """
    from yadgar.core.hooks._identity_mint import (  # noqa: PLC0415
        resolve_session_project,
    )

    try:
        project_id, notice = resolve_session_project(cwd)
    except Exception:  # noqa: BLE001 — a mint crash must not brick the hook
        return None
    if notice:
        print(notice)
    return project_id


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
    project_id = _emit_project_id(cwd)
    if project_id:
        params["project"] = project_id
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

    params: dict = {"directory": directory}
    # Car C2: re-emit the identity. Compaction ate the original banner, and the
    # transport is the line itself (§1.3 T1) — an un-repeated identity is a lost
    # identity for the whole remainder of the session.
    project_id = _emit_project_id(directory)
    if project_id:
        params["project"] = project_id

    result = _http_get("/hooks/post-compact", params)
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


def dispatch(hook_type: str) -> int:
    """Run the handler for *hook_type*; return a process exit code.

    Shared by the CLI subcommand and the ``hook_runner.py`` shim so both agree
    on dispatch + error semantics. Unknown type → usage on stderr, exit 1.
    """
    handler = _HOOKS.get(hook_type)
    if handler is None:
        print(f"Unknown hook type: {hook_type!r}", file=sys.stderr)
        print(f"Available: {', '.join(_HOOKS)}", file=sys.stderr)
        return 1
    handler()
    return 0


def cmd_hook(args) -> None:
    """``yadgar hook <event>`` — run the shared hook body for one event.

    The single code path every client's emitted hook shells out to. Reads the
    native hook payload from stdin (each handler parses its own shape) and writes
    the client-appropriate output (inject text / nothing) to stdout.
    """
    sys.exit(dispatch(args.event))


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "hook",
        help="Run a yadgar hook body for one event (shared across clients)",
    )
    p.add_argument(
        "event",
        choices=sorted(_HOOKS),
        help="Hook event to run (reads the native hook payload from stdin)",
    )
    # project_directory is accepted positionally for hook_runner.py CLI parity
    # (the runner is invoked `hook_runner.py <hook_type> [project_directory]`);
    # handlers read cwd from the stdin payload, so this is informational only.
    p.add_argument(
        "project_directory",
        nargs="?",
        default=None,
        help="Optional project directory (informational; cwd comes from stdin payload)",
    )
    p.set_defaults(func=cmd_hook)
