#!/usr/bin/env bash
# yadgar-nightly-cycle-wrapper.sh — launchd wrapper for yadgar-nightly-cycle (oneshot, host-exec).
#
# Sources secrets.env with explicit per-key export (D4: avoids whole-env leak).
# Sets DYLD_LIBRARY_PATH for numpy .so resolution on macOS (Gap 6 in design doc).
# Wall-clock timeout: 3600s (1 hour).
# Uses gtimeout (homebrew coreutils) or BSD timeout (D3).
#
# Installed to ~/.local/share/yadgar/scripts/ by generate_launchd.sh.

set -euo pipefail

SECRETS_ENV="${YADGAR_SECRETS_ENV_FILE:-${HOME}/.config/yadgar/secrets.env}"

# D3: prefer gtimeout (GNU coreutils via homebrew) over BSD timeout
TIMEOUT_BIN=$(command -v gtimeout || command -v timeout) || {
    echo "ERROR: timeout binary required (install coreutils via homebrew: brew install coreutils)" >&2
    exit 1
}

# D4: explicit export — only keys nightly-cycle actually needs
KEYS_NEEDED=(
    YADGAR_RW_USER
    YADGAR_RW_PASS
    YADGAR_MCP_AUTH_TOKEN
    SURREAL_USER
    SURREAL_PASS
    ANTHROPIC_API_KEY
    YADGAR_DATA_DIR
)
if [ -f "$SECRETS_ENV" ]; then
    for key in "${KEYS_NEEDED[@]}"; do
        val=$(grep -E "^${key}=" "$SECRETS_ENV" 2>/dev/null | head -1 | cut -d= -f2-)
        [ -n "$val" ] && export "${key}=${val}"
    done
fi

export YADGAR_DB_URL="${YADGAR_DB_URL:-http://127.0.0.1:8000}"
export YADGAR_DATA_DIR="${YADGAR_DATA_DIR:-${HOME}/.yadgar}"

# Gap 6: DYLD_LIBRARY_PATH for numpy/scipy .so on macOS.
# SIP does not strip this for user LaunchAgents. Homebrew lib is the common location.
# If yadgar is installed via Homebrew Python, this may be a no-op (already on linker path).
export DYLD_LIBRARY_PATH="/opt/homebrew/lib${DYLD_LIBRARY_PATH:+:${DYLD_LIBRARY_PATH}}"

exec "$TIMEOUT_BIN" 3600 "${HOME}/.local/bin/yadgar" nightly-cycle --service-mode=manual --yes
