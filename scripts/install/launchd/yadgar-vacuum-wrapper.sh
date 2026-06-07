#!/usr/bin/env bash
# yadgar-vacuum-wrapper.sh — launchd wrapper for yadgar vacuum (oneshot, host-exec).
#
# Sources secrets.env with explicit per-key export (D4: avoids whole-env leak).
# Wall-clock timeout: 1800s (30 min) — matches systemd TimeoutStartSec=30min.
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

# D4: explicit export — only keys vacuum actually needs
KEYS_NEEDED=(
    YADGAR_RW_USER
    YADGAR_RW_PASS
    YADGAR_MCP_AUTH_TOKEN
    SURREAL_USER
    SURREAL_PASS
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

# D6: --service-mode=manual (ops.py confirms 'manual' is supported)
exec "$TIMEOUT_BIN" 1800 "${HOME}/.local/bin/yadgar" vacuum --service-mode=manual --yes
