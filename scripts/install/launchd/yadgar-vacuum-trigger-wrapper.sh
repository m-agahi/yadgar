#!/usr/bin/env bash
# yadgar-vacuum-trigger-wrapper.sh — launchd WatchPaths trigger for yadgar vacuum.
#
# Fired by launchd on ANY change to ~/.yadgar/triggers/ directory.
# Must filter spurious fires: only acts if vacuum_requested file exists.
# Uses atomic mv to claim the trigger — prevents concurrent invocation double-fire.
#
# Design follows Q5 flowchart in MACOS_LAUNCHD_PORT_DESIGN.md.
# Installed to ~/.local/share/yadgar/scripts/ by generate_launchd.sh.

set -euo pipefail

TRIGGERS_DIR="${HOME}/.yadgar/triggers"
TRIGGER_FILE="${TRIGGERS_DIR}/vacuum_requested"
HANDLING_FILE="${TRIGGERS_DIR}/vacuum_requested.handling"
HANDLING_MAX_AGE_SECONDS=600  # 10 minutes

# Clean up stale .handling marker from a previous crashed invocation
if [ -f "$HANDLING_FILE" ]; then
    # macOS stat -f %m for mtime; fallback to find -mmin
    if command -v stat &>/dev/null; then
        file_mtime=$(stat -f %m "$HANDLING_FILE" 2>/dev/null || echo 0)
        now=$(date +%s)
        age=$(( now - file_mtime ))
    else
        age=0
    fi
    if [ "$age" -ge "$HANDLING_MAX_AGE_SECONDS" ]; then
        echo "WARN: stale .handling marker found (age=${age}s), removing and re-kicking" >&2
        rm -f "$HANDLING_FILE"
    else
        # A concurrent invocation may be running — exit quietly
        exit 0
    fi
fi

# Check trigger file exists (spurious fire guard)
[ -f "$TRIGGER_FILE" ] || exit 0

# Atomically claim the trigger (mv is atomic within same filesystem)
mv "$TRIGGER_FILE" "$HANDLING_FILE" 2>/dev/null || {
    # mv failed — another instance won the race
    exit 0
}

# Kick the vacuum job. launchctl returns error if job is already running; ignore.
launchctl kickstart "gui/$(id -u)/com.openfantasy.yadgar-vacuum" 2>/dev/null || true

# Clean up handling marker
rm -f "$HANDLING_FILE"

exit 0
