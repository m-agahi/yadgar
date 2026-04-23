#!/usr/bin/env python3
"""Yadgar stop hook — periodic memory checkpoint.

Fires every INTERVAL human messages and prompts Claude to call remember()
with key decisions/context from the session, then continue the conversation.

Installed globally to ~/.claude/hooks/ so it fires in every session
regardless of project directory.

Output: JSON to stdout.
  {"decision": "block", "reason": "..."} — prompt Claude to write memories
  {}                                      — allow stop normally
"""

import json
import sys
from pathlib import Path

INTERVAL = 25  # human messages between checkpoints

_PROMPT = (
    "Yadgar checkpoint: call remember() once or twice to capture key decisions, "
    "context, or learnings from this session. Be concise. "
    "Then continue the conversation naturally."
)


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


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    stop_hook_active = str(data.get("stop_hook_active", "false")).lower() in ("true", "1", "yes")

    # Infinite-loop guard: Claude already wrote memories this turn — allow stop
    if stop_hook_active:
        print("{}")
        return

    # No transcript available (some agent contexts) — skip
    if not transcript_path:
        print("{}")
        return

    # Load per-session state
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    state_file = Path(f"/tmp/yadgar_stop_{safe_id}.json")
    state: dict = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            state = {}

    last_save: int = state.get("last_save", 0)
    current_count = _count_human_messages(transcript_path)

    if current_count - last_save < INTERVAL:
        print("{}")
        return

    # Checkpoint time — update state and block
    state["last_save"] = current_count
    try:
        state_file.write_text(json.dumps(state))
    except Exception:
        pass

    print(json.dumps({"decision": "block", "reason": _PROMPT}))


if __name__ == "__main__":
    main()
