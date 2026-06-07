#!/usr/bin/env bash
# yadgar-worktree-sweep-wrapper.sh — launchd wrapper for weekly worktree sweep (oneshot).
#
# Runs scripts/cleanup-merged-branches.sh to prune stale local branches + worktrees.
# Wall-clock timeout: 600s (10 min).
# Uses gtimeout (homebrew coreutils) or BSD timeout (D3).
# No secrets needed — purely local git operations.
#
# Installed to ~/.local/share/yadgar/scripts/ by generate_launchd.sh.

set -euo pipefail

# D3: prefer gtimeout (GNU coreutils via homebrew) over BSD timeout
TIMEOUT_BIN=$(command -v gtimeout || command -v timeout) || {
    echo "ERROR: timeout binary required (install coreutils via homebrew: brew install coreutils)" >&2
    exit 1
}

# Locate cleanup script — conventionally at ~/git/yadgar/scripts/cleanup-merged-branches.sh
# Allow override via env var for non-standard repo locations.
CLEANUP_SCRIPT="${YADGAR_CLEANUP_SCRIPT:-${HOME}/git/yadgar/scripts/cleanup-merged-branches.sh}"

if [ ! -f "$CLEANUP_SCRIPT" ]; then
    echo "ERROR: cleanup script not found at ${CLEANUP_SCRIPT}" >&2
    echo "  Set YADGAR_CLEANUP_SCRIPT env var to the correct path." >&2
    exit 1
fi

if [ ! -x "$CLEANUP_SCRIPT" ]; then
    echo "ERROR: cleanup script not executable: ${CLEANUP_SCRIPT}" >&2
    exit 1
fi

exec "$TIMEOUT_BIN" 600 bash "$CLEANUP_SCRIPT"
