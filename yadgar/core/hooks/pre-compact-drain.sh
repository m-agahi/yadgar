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
# when absent — the CLI degrades to pre-Car-2 behaviour.
TRANSCRIPT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")

# Drain context directly via CLI (works in both stdio and SSE mode)
if [ -n "$TRANSCRIPT" ]; then
    yadgar drain "$CWD" --transcript-path "$TRANSCRIPT" > /dev/null 2>&1
else
    yadgar drain "$CWD" > /dev/null 2>&1
fi

exit 0
