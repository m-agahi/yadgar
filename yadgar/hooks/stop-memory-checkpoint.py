#!/usr/bin/env python3
"""Yadgar stop hook — periodic signal-evaluation checkpoint (§27).

Fires every INTERVAL human messages and prompts Claude to evaluate signals
via project_brief() and take action (wiki regen, active_work refresh, etc.).

This hook is a DUMB PIPE — no Python signal detection, no API calls.
All evaluation happens in the Claude session via tool calls.

State: ~/.yadgar/stop-hook-state.json (keyed by session_id, atomic writes).

Output: JSON to stdout.
  {"decision": "block", "reason": "..."} — inject signal-eval prompt
  {}                                      — allow stop normally
"""

import json
import os
import sys
from pathlib import Path

INTERVAL = 25  # human messages between checkpoints

_PROMPT = """\
Yadgar checkpoint. Evaluate signals and decide actions.

1. Call `project_brief(directory)` and check `signals`:
   - `stale_wiki_count > 0` AND branch is master/main/default → consider repo-wiki regen
   - `active_work_present == False` OR `active_work_age_hours > 24` → refresh _active_work
   - `init_memory == None` after >5 sessions in this dir → create one

2. If repo-wiki regen warranted, dispatch background Agent:
   Agent(
     subagent_type="general-purpose",
     run_in_background=True,
     description="repo-wiki regen on default branch",
     prompt="cd into the project, run /repo-wiki:repo-wiki update, "
            "verify export-yadgar fires, report regenerated slug list."
   )

3. If _active_work needs refresh, call update_active_work(directory, content).

4. If init_memory missing and you have enough session context, propose one
   and call bootstrap_project(directory, content) (<=2000 chars).

5. Otherwise: capture any key decisions via memorize/wiki_add.

Then look at your last message — if mid-thought, repeat the question so
conversation continues naturally.
"""


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
    """Return path to stop-hook-state.json under ~/.yadgar/."""
    home = Path(os.environ.get("HOME", Path.home()))
    return home / ".yadgar" / "stop-hook-state.json"


def _load_state() -> dict:
    """Load the global stop-hook state dict. Returns {} on any error."""
    sf = _state_file_path()
    if not sf.exists():
        return {}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    stop_hook_active = str(data.get("stop_hook_active", "false")).lower() in ("true", "1", "yes")

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

    print(json.dumps({"decision": "block", "reason": _PROMPT}))


if __name__ == "__main__":
    main()
