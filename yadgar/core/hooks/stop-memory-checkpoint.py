#!/usr/bin/env python3
"""Yadgar stop hook — periodic signal-evaluation checkpoint (§27).

Fires every INTERVAL human messages and prompts Claude to evaluate signals
via project_brief() and take action (wiki regen, active_work refresh, etc.).

This hook is a DUMB PIPE — no Python signal detection, no API calls.
All evaluation happens in the Claude session via tool calls.

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
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing

INTERVAL = 25  # human messages between checkpoints


@observe(tier="stage")
def _load_prompt_template() -> str:
    """Load the checkpoint prompt template from package data (task #34).

    The template ships as package data at
    yadgar/core/hooks/templates/stop_checkpoint_prompt.md and is resolved via
    importlib.resources — works from a source checkout, an installed wheel, AND
    the standalone copy under ~/.claude/hooks (that copy already requires the
    yadgar package importable for the yadgar._shared imports above, so
    package-resource resolution adds no new runtime dependency; the installer
    does NOT need to copy the template alongside the script).

    Fail-loud: a missing or empty template is a packaging bug — raise
    RuntimeError instead of silently emitting a broken checkpoint prompt.
    """
    from importlib.resources import files

    try:
        text = (
            files("yadgar.core.hooks")
            .joinpath("templates")
            .joinpath("stop_checkpoint_prompt.md")
            .read_text(encoding="utf-8")
        )
    except (OSError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "yadgar stop-hook prompt template missing: "
            "yadgar/core/hooks/templates/stop_checkpoint_prompt.md is not "
            "resolvable as package data — broken install/packaging"
        ) from exc
    if not text.strip():
        raise RuntimeError(
            "yadgar stop-hook prompt template is empty: "
            "yadgar/core/hooks/templates/stop_checkpoint_prompt.md"
        )
    return text


# Loaded at import time so a broken install fails loud on the first hook fire,
# not silently mid-session. Placeholders ({directory}, {project},
# {default_branch}) are rendered in main() via str.format.
_PROMPT_TEMPLATE = _load_prompt_template()


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


@observe(tier="hot")
def _default_branch(directory: str) -> str:
    """Return the repo's default branch name (e.g. "master").

    The ADR log is project-canonical and must live on the default branch, not a
    feature branch. Resolves via `git symbolic-ref refs/remotes/origin/HEAD`.
    Falls back to "master" for non-git projects or any failure — the ADR system
    supports non-git projects too.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["git", "-C", directory, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ref = out.stdout.strip()
        # ref looks like "refs/remotes/origin/master" — take the last segment
        if out.returncode == 0 and ref:
            name = ref.rsplit("/", 1)[-1]
            if name:
                return name
    except Exception:
        pass
    return "master"


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


@observe(tier="boundary")
def main() -> None:
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}

        session_id = data.get("session_id", "unknown")
        transcript_path = data.get("transcript_path", "")
        stop_hook_active = str(data.get("stop_hook_active", "false")).lower() in (
            "true",
            "1",
            "yes",
        )
        directory = data.get("cwd", os.getcwd())

        # Infinite-loop guard: Claude already ran a checkpoint this turn — allow stop
        if stop_hook_active:
            print("{}")
            return

        # No transcript available (some agent contexts) — skip
        if not transcript_path:
            print("{}")
            return

        state = _load_state()
        session_state: dict = state.get(session_id, {})
        last_save: int = session_state.get("last_save", 0)

        current_count = _count_human_messages(transcript_path)

        if current_count - last_save < INTERVAL:
            print("{}")
            return

        # Checkpoint time — update state atomically and block
        session_state["last_save"] = current_count
        state[session_id] = session_state
        _save_state(state)

        project = os.path.basename(directory.rstrip("/")) or "project"
        default_branch = _default_branch(directory)
        prompt = _PROMPT_TEMPLATE.format(
            directory=directory, project=project, default_branch=default_branch
        )
        print(json.dumps({"decision": "block", "reason": prompt}))
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
