#!/usr/bin/env bash
# Yadgar Hippocampal Replay — PreCompact Hook
# Drains context into Yadgar before Claude Code compacts the conversation.
# Reads hook input from stdin (JSON with session_id, cwd, trigger).

# Read hook input from stdin
INPUT=$(cat)

# Extract cwd from hook input, fallback to current directory
CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")
if [ -z "$CWD" ]; then
    CWD=$(pwd)
fi

# HOOKS Car 2: extract transcript_path (in-flight orchestration capture). Empty
# when absent — the CLI degrades to pre-Car-2 behaviour. The `yadgar drain` CLI
# parses in_flight HOST-SIDE (Car fix-drain-inflight) — this wrapper runs on the
# host where the transcript + git worktree tree are visible.
TRANSCRIPT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")

# Car fix-drain-inflight: surface the drain outcome instead of swallowing it
# blind. Capture stderr + status to a hook-error log; ALWAYS exit 0 so a drain
# failure never blocks compaction. (The prior `> /dev/null 2>&1; exit 0` hid
# every failure — the drain was inert for weeks with no signal.)
ERRLOG="${HOME}/.claude/yadgar-hook-errors.log"
STAMP=$(date +%Y-%m-%dT%H:%M:%S 2>/dev/null || echo "?")

# Drain context directly via CLI (works in both stdio and SSE mode)
if [ -n "$TRANSCRIPT" ]; then
    DRAIN_ERR=$(yadgar drain "$CWD" --transcript-path "$TRANSCRIPT" 2>&1 >/dev/null)
    DRAIN_RC=$?
else
    DRAIN_ERR=$(yadgar drain "$CWD" 2>&1 >/dev/null)
    DRAIN_RC=$?
fi

if [ "$DRAIN_RC" -ne 0 ]; then
    printf '%s pre-compact-drain rc=%s %s\n' "$STAMP" "$DRAIN_RC" "$DRAIN_ERR" >> "$ERRLOG" 2>/dev/null || true
fi

exit 0
