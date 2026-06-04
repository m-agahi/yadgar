#!/usr/bin/env bash
# Generate launchd LaunchAgent plists for yadgar from .in templates.
#
# macOS analog of generate_systemd.sh. Installs to ~/Library/LaunchAgents/.
#
# Environment variables (all have defaults):
#   YADGAR_LAUNCHD_OUTPUT_DIR   Target dir (default: ~/Library/LaunchAgents)
#   YADGAR_RUNTIME              Container runtime: podman|docker (default: auto-detected)
#   YADGAR_INSTALL_PREFIX       Data dir mounted at /data (default: ~/.yadgar)
#   YADGAR_SECRETS_ENV_FILE     Path to secrets.env (default: ~/.yadgar/secrets.env)
#   YADGAR_BACKEND_IMAGE        Backend image tag (default: openfantasy/yadgar-backend:latest)
#   YADGAR_CORE_IMAGE           Core image tag (default: openfantasy/yadgar:latest)
#   YADGAR_TEST_OS_MARKER       Override OS detection for testing (set to 'macos' to spoof)
#
# Exits non-zero if:
#   - template files are missing
#   - YADGAR_RUNTIME not detected
#
# plutil -lint validation: only runs on macOS (Darwin). On Linux, a warning is
# printed and the step is skipped (P7 precedent 2).
#
# launchctl bootstrap: uses 'bootstrap gui/$UID' for macOS 11+ (Big Sur+).
# Falls back to 'launchctl load -w' for 10.15 (Catalina).
# Unloads existing jobs before re-loading to avoid stale registrations (P7 precedent 1).
# NOTE: This script only renders + writes plist files. Actual bootstrap/load is
# performed by 'make enable-units' to separate generation from activation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="${SCRIPT_DIR}/launchd"

OUTPUT_DIR="${YADGAR_LAUNCHD_OUTPUT_DIR:-${HOME}/Library/LaunchAgents}"
RUNTIME="${YADGAR_RUNTIME:-}"
DATA_DIR="${YADGAR_INSTALL_PREFIX:-${HOME}/.yadgar}"
SECRETS_ENV_FILE="${YADGAR_SECRETS_ENV_FILE:-${DATA_DIR}/secrets.env}"
BACKEND_IMAGE="${YADGAR_BACKEND_IMAGE:-openfantasy/yadgar-backend:latest}"
CORE_IMAGE="${YADGAR_CORE_IMAGE:-openfantasy/yadgar:latest}"
LOG_DIR="${HOME}/Library/Logs/yadgar"

# ── Runtime detection (if not set) ───────────────────────────────────────────

if [[ -z "${RUNTIME}" ]]; then
    if [[ -x "${SCRIPT_DIR}/detect_runtime.sh" ]]; then
        RUNTIME="$(bash "${SCRIPT_DIR}/detect_runtime.sh" 2>/dev/null)" || {
            echo "ERROR: Could not detect container runtime. Install podman or docker." >&2
            exit 1
        }
    else
        if command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
            RUNTIME="podman"
        elif command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
            RUNTIME="docker"
        else
            echo "ERROR: No container runtime found. Install podman or docker." >&2
            exit 1
        fi
    fi
fi

# ── Template rendering ────────────────────────────────────────────────────────

render_template() {
    local template="$1"
    local output="$2"
    [[ -f "${template}" ]] || {
        echo "ERROR: Template not found: ${template}" >&2
        exit 1
    }
    sed \
        -e "s|\${YADGAR_RUNTIME}|${RUNTIME}|g" \
        -e "s|\${YADGAR_CORE_IMAGE}|${CORE_IMAGE}|g" \
        -e "s|\${YADGAR_BACKEND_IMAGE}|${BACKEND_IMAGE}|g" \
        -e "s|\${YADGAR_INSTALL_PREFIX}|${DATA_DIR}|g" \
        -e "s|\${YADGAR_SECRETS_ENV_FILE}|${SECRETS_ENV_FILE}|g" \
        -e "s|\${YADGAR_HOME}|${HOME}|g" \
        "${template}" > "${output}"
}

mkdir -p "${OUTPUT_DIR}"
mkdir -p "${LOG_DIR}"

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar.plist"

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar-backend.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar-backend.plist"

# ── plutil validation (macOS only) ───────────────────────────────────────────

OS_MARKER="${YADGAR_TEST_OS_MARKER:-}"
IS_DARWIN=0
if [[ "${OS_MARKER}" == "macos" ]] || [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    IS_DARWIN=1
fi

if [[ "${IS_DARWIN}" == "1" ]] && command -v plutil &>/dev/null; then
    plutil -lint "${OUTPUT_DIR}/com.openfantasy.yadgar.plist" || {
        echo "ERROR: plutil -lint failed on com.openfantasy.yadgar.plist" >&2
        exit 1
    }
    plutil -lint "${OUTPUT_DIR}/com.openfantasy.yadgar-backend.plist" || {
        echo "ERROR: plutil -lint failed on com.openfantasy.yadgar-backend.plist" >&2
        exit 1
    }
    echo "    plutil -lint: OK"
else
    echo "WARNING: plutil not available (non-macOS host). Skipping plist lint." \
         "Validate on macOS with: plutil -lint <plist>" >&2
fi

echo "LaunchAgent plists written to ${OUTPUT_DIR}/"
echo "  com.openfantasy.yadgar.plist"
echo "  com.openfantasy.yadgar-backend.plist"
echo "Log directory: ${LOG_DIR}/"
echo ""
echo "To activate, run: make enable-units"
