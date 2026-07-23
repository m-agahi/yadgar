#!/usr/bin/env python3
"""Yadgar stop hook — periodic signal-evaluation checkpoint (§27).

Fires every INTERVAL human messages and prompts Claude to evaluate signals
via project_brief() and take action (wiki regen, active_work refresh, etc.).

This hook is a DUMB PIPE for signal-evaluation — no Python signal detection.
All evaluation happens in the Claude session via tool calls.

ADR-0156: subagent findings are NOT auto-stored here. The checkpoint prompt
(step 4, SUBAGENT FINDINGS CURATION) has the main instance LIST pending
subagent findings via the ``yadgar pending-findings`` CLI, CURATE them with
judgment through its own MCP tools, then CLEANUP the consumed on-disk
transcripts. No script writes raw findings to the DB — the old
``_run_subagent_sweep`` auto-store path was ripped.

State: ~/.local/state/yadgar/stop-hook-state.json (keyed by session_id, atomic writes).

Output: JSON to stdout.
  {"decision": "block", "reason": "..."} — inject signal-eval prompt
  {}                                      — allow stop normally
"""

import json
import os
import sys
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing

INTERVAL = 25  # human messages between checkpoints

# v5.158.0 (Car #85): human messages between anchor-audit maintenance injections.
# Config-driven so operators can retune without editing the hook.
ANCHOR_AUDIT_STOP_INTERVAL = get_settings().ANCHOR_AUDIT_STOP_INTERVAL

# Car D (#83): human messages between repo-wiki-refresh maintenance injections.
# Slowest cadence (code-structure drift is rare); config-driven like the above.
REPO_WIKI_REFRESH_STOP_INTERVAL = get_settings().REPO_WIKI_REFRESH_STOP_INTERVAL

# Car D (#83, ADR-0162): human messages between code_graph-refresh injections.
# Shares the priority-2 slot with repo-wiki (mutually exclusive, gated by
# CODE_GRAPH_ENABLED); same slowest cadence. config-driven like the above.
CODE_GRAPH_REFRESH_STOP_INTERVAL = get_settings().CODE_GRAPH_REFRESH_STOP_INTERVAL


@observe(tier="stage")
def _resolve_prompt_template_path(filename: str = "stop_checkpoint_prompt.md") -> str:
    """Resolve the on-disk path of a packaged stop-hook protocol template (Car B, task #74).

    Templates ship as package data under yadgar/core/hooks/templates/ and are
    resolved via importlib.resources — works from a source checkout, an installed
    wheel, AND the standalone copy under ~/.claude/hooks (that copy already
    requires the yadgar package importable for the yadgar._shared imports above,
    so package-resource resolution adds no new runtime dependency; the installer
    does NOT need to copy the template alongside the script).

    Returns the absolute on-disk path as a str so main() can emit it in the
    short pointer reason without loading the full content into the reason field.

    Fail-loud: a missing or unresolvable template is a packaging bug — raise
    RuntimeError instead of silently emitting a broken pointer.
    """
    from importlib.resources import as_file, files

    try:
        ref = files("yadgar.core.hooks").joinpath("templates").joinpath(filename)
        # as_file() extracts to a temp path when inside a zip (wheel); for
        # source/editable installs it returns the real path directly.
        with as_file(ref) as p:
            resolved = str(p)
        # Sanity check: the resolved path must actually exist and be non-empty.
        import pathlib

        pp = pathlib.Path(resolved)
        if not pp.exists():
            raise RuntimeError(
                "yadgar stop-hook prompt template resolves to a path that does not exist: "
                f"{resolved} — broken install/packaging"
            )
        if not pp.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"yadgar stop-hook prompt template is empty: {resolved}")
    except (OSError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "yadgar stop-hook prompt template missing: "
            f"yadgar/core/hooks/templates/{filename} is not "
            "resolvable as package data — broken install/packaging"
        ) from exc
    return resolved


# Resolved at import time so a broken install fails loud on the first hook fire,
# not silently mid-session. The paths are emitted in the short pointer reason;
# the full protocol lives in the file at each path.
_PROMPT_TEMPLATE_PATH = _resolve_prompt_template_path("stop_checkpoint_prompt.md")
_ANCHOR_AUDIT_TEMPLATE_PATH = _resolve_prompt_template_path("anchor_audit_prompt.md")
_REPO_WIKI_REFRESH_TEMPLATE_PATH = _resolve_prompt_template_path("repo_wiki_refresh_prompt.md")
_CODE_GRAPH_REFRESH_TEMPLATE_PATH = _resolve_prompt_template_path("code_graph_refresh_prompt.md")


@observe(tier="hot")
def _count_human_messages(transcript_path: str) -> int:
    """Count human (user) turns in the JSONL transcript.

    Skips system-injected turns (<system-reminder>, <command-message>).
    Handles both flat and nested Claude Code transcript formats.
    """
    p = Path(transcript_path)
    if not p.exists():
        return 0

    count = 0
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Nested format: {"message": {"role": "user", "content": "..."}, ...}
        # Flat format:   {"role": "user", "content": "..."}
        msg = entry.get("message", entry)
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        if isinstance(content, str) and (
            "<system-reminder>" in content or "<command-message>" in content
        ):
            continue
        # List content that is only tool results — skip
        if (
            isinstance(content, list)
            and content
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            continue

        count += 1

    return count


def _state_file_path() -> Path:
    """Return path to stop-hook-state.json under XDG state dir."""
    return _paths.STOP_HOOK_STATE_PATH


@observe(tier="stage")
def _load_state() -> dict:
    """Load the global stop-hook state dict. Returns {} on any error."""
    sf = _state_file_path()
    if not sf.exists():
        return {}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {}


@observe(tier="stage")
def _save_state(state: dict) -> None:
    """Atomically write state dict to stop-hook-state.json (tmp + os.replace)."""
    sf = _state_file_path()
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        tmp = sf.parent / (sf.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(sf))
    except Exception:
        pass


def _subagent_sweep_state_path() -> Path:
    """Return path to subagent-capture dedup state (path -> consumed mtime).

    ADR-0156: the auto-store sweep was ripped; this path is now the dedup state
    reused by the ``yadgar pending-findings`` CLI (via
    ``findings_capture._default_sweep_state_path``) so LIST/CLEANUP across
    checkpoints stays idempotent.
    """
    return _paths.STOP_HOOK_STATE_PATH.parent / "subagent-capture-state.json"


# ── Maintenance scheduler (Car #85) ────────────────────────────────────────
#
# The single Stop hook drives an ordered registry of maintenance items. On each
# stop (past the loop/transcript guards) items are evaluated by priority and
# EXACTLY ONE {decision: block} is injected — FIRST DUE WINS. Only the injected
# item's per-session counter is advanced, so a checkpoint that preempts a due
# anchor-audit does not consume the audit's turn: the audit fires on the next
# eligible stop.
#
# Each item is a dict:
#   name        — stable key; also the session_state counter key via `state_key`.
#   priority    — lower wins ties (0 = checkpoint, 1 = anchor-audit).
#   state_key   — session_state field holding the last-injected count watermark.
#   is_due      — (count, session_state, cwd) -> bool. ``cwd`` is the session's
#                 working directory (from the stop payload); only the dir-aware
#                 code_graph predicate uses it — the others ignore it.
#   reason      — (count) -> str; the {decision: block} reason string.


def _checkpoint_is_due(count: int, session_state: dict, cwd: str | None = None) -> bool:
    return count - int(session_state.get("last_save", 0)) >= INTERVAL


def _checkpoint_reason(count: int) -> str:
    return (
        f"[yadgar] Checkpoint due. Read {_PROMPT_TEMPLATE_PATH}"
        " and follow all the instructions in it."
    )


def _anchor_audit_is_due(count: int, session_state: dict, cwd: str | None = None) -> bool:
    return count - int(session_state.get("last_anchor_audit", 0)) >= ANCHOR_AUDIT_STOP_INTERVAL


def _anchor_audit_reason(count: int) -> str:
    return (
        f"[yadgar] Anchor-audit maintenance due. Read {_ANCHOR_AUDIT_TEMPLATE_PATH}"
        " and follow all the instructions in it."
    )


def _code_graph_enabled(cwd: str | None = None) -> bool:
    """Dir-aware read of ``code_graph.enabled`` from the runtime config store.

    ADR-0163: resolves the flag via ``config.is_enabled(cwd)`` (host client →
    runtime config store, per-dir override → global → False). A per-repo opt-out
    (``code_graph.enabled=false`` at ``cwd``) makes this False THERE even when the
    global flag is on — so the code_graph refresh nudge is not wasted on an
    opted-out repo. Fail-open: daemon down / any error → False (code_graph inert,
    repo-wiki keeps running). Imported lazily so the hook still loads if the
    code_graph package is absent. Car D gating.
    """
    try:
        from yadgar.core.code_graph import config as _cg_config

        return bool(_cg_config.is_enabled(cwd))
    except Exception:
        return False


def _repo_wiki_refresh_is_due(count: int, session_state: dict, cwd: str | None = None) -> bool:
    # Car D gated swap: when code_graph is ENABLED it takes the priority-2 slot,
    # so repo-wiki goes inert (mutually exclusive — no double-fire). repo-wiki is
    # NOT deleted (decommission is #33); it simply yields while the flag is on.
    # repo_wiki stays GLOBAL-scoped (no cwd) — it is being retired (#33), so its
    # gate keeps the pre-ADR-0163 behavior; only code_graph's is_due is dir-aware.
    if _code_graph_enabled():
        return False
    return (
        count - int(session_state.get("last_repo_wiki_refresh", 0))
        >= REPO_WIKI_REFRESH_STOP_INTERVAL
    )


def _repo_wiki_refresh_reason(count: int) -> str:
    return (
        f"[yadgar] Repo-wiki-refresh maintenance due. Read {_REPO_WIKI_REFRESH_TEMPLATE_PATH}"
        " and follow all the instructions in it."
    )


def _code_graph_refresh_is_due(count: int, session_state: dict, cwd: str | None = None) -> bool:
    # Car D gated swap: only due when code_graph is ENABLED for THIS repo (else
    # repo-wiki owns the priority-2 slot). ADR-0163: dir-aware via ``cwd`` so an
    # opted-out repo (per-dir code_graph.enabled=false) is not due here — no wasted
    # nudge. The two priority-2 items are mutually exclusive.
    if not _code_graph_enabled(cwd):
        return False
    return (
        count - int(session_state.get("last_code_graph_refresh", 0))
        >= CODE_GRAPH_REFRESH_STOP_INTERVAL
    )


def _code_graph_refresh_reason(count: int) -> str:
    return (
        f"[yadgar] code_graph-refresh maintenance due. Read {_CODE_GRAPH_REFRESH_TEMPLATE_PATH}"
        " and follow all the instructions in it."
    )


# Ordered by priority (ascending). FIRST DUE WINS.
_MAINTENANCE_ITEMS: list[dict] = [
    {
        "name": "checkpoint",
        "priority": 0,
        "state_key": "last_save",
        "is_due": _checkpoint_is_due,
        "reason": _checkpoint_reason,
    },
    {
        "name": "anchor_audit",
        "priority": 1,
        "state_key": "last_anchor_audit",
        "is_due": _anchor_audit_is_due,
        "reason": _anchor_audit_reason,
    },
    # Priority-2 slot is a GATED swap (Car D, #83): repo_wiki and code_graph are
    # mutually exclusive via CODE_GRAPH_ENABLED — exactly one is ever due, so their
    # shared priority never double-fires. repo_wiki stays registered (decommission
    # is #33); code_graph goes here so the flag flips the active item.
    {
        "name": "repo_wiki_refresh",
        "priority": 2,
        "state_key": "last_repo_wiki_refresh",
        "is_due": _repo_wiki_refresh_is_due,
        "reason": _repo_wiki_refresh_reason,
    },
    {
        "name": "code_graph_refresh",
        "priority": 2,
        "state_key": "last_code_graph_refresh",
        "is_due": _code_graph_refresh_is_due,
        "reason": _code_graph_refresh_reason,
    },
]


@observe(tier="boundary")
def main() -> None:
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}

        session_id = data.get("session_id", "unknown")
        transcript_path = data.get("transcript_path", "")
        # Session working directory (Claude Code stop payload). Threaded into the
        # dir-aware code_graph gate so a per-repo opt-out is honored (ADR-0163).
        cwd = data.get("cwd") or None
        stop_hook_active = str(data.get("stop_hook_active", "false")).lower() in (
            "true",
            "1",
            "yes",
        )

        # Infinite-loop guard: Claude already ran maintenance this turn — allow stop
        if stop_hook_active:
            print("{}")
            return

        # No transcript available (some agent contexts) — skip
        if not transcript_path:
            print("{}")
            return

        state = _load_state()
        session_state: dict = state.get(session_id, {})

        current_count = _count_human_messages(transcript_path)

        # Evaluate maintenance items by priority; FIRST DUE WINS. Compute the
        # count once and hand the SAME value to every item's is_due so preemption
        # is deterministic.
        chosen: dict | None = None
        for item in sorted(_MAINTENANCE_ITEMS, key=lambda it: it["priority"]):
            if item["is_due"](current_count, session_state, cwd):
                chosen = item
                break

        if chosen is None:
            print("{}")
            return

        # Advance ONLY the injected item's counter, then block. A preempted item
        # keeps its old watermark and fires on the next eligible stop.
        session_state[chosen["state_key"]] = current_count
        state[session_id] = session_state
        _save_state(state)

        print(json.dumps({"decision": "block", "reason": chosen["reason"](current_count)}))
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
