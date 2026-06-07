#!/usr/bin/env bash
# Generate launchd LaunchAgent plists for yadgar from .in templates.
#
# macOS analog of generate_systemd.sh. Installs to ~/Library/LaunchAgents/.
#
# Environment variables (all have defaults):
#   YADGAR_LAUNCHD_OUTPUT_DIR   Target dir (default: ~/Library/LaunchAgents)
#   YADGAR_RUNTIME              Container runtime: podman|docker (default: auto-detected)
#   YADGAR_INSTALL_PREFIX       Data dir mounted at /data (default: ~/.local/share/yadgar)
#   YADGAR_SECRETS_ENV_FILE     Path to secrets.env (default: ~/.config/yadgar/secrets.env)
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
DATA_DIR="${YADGAR_INSTALL_PREFIX:-${HOME}/.local/share/yadgar}"
SECRETS_ENV_FILE="${YADGAR_SECRETS_ENV_FILE:-${HOME}/.config/yadgar/secrets.env}"
BACKEND_IMAGE="${YADGAR_BACKEND_IMAGE:-openfantasy/yadgar-backend:latest}"
CORE_IMAGE="${YADGAR_CORE_IMAGE:-openfantasy/yadgar:latest}"
LOG_DIR="${HOME}/.local/share/yadgar/logs"
SCRIPTS_INSTALL_DIR="${HOME}/.local/share/yadgar/scripts"

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
        -e "s|@YADGAR_RUNTIME@|${RUNTIME}|g" \
        -e "s|@YADGAR_CORE_IMAGE@|${CORE_IMAGE}|g" \
        -e "s|@YADGAR_BACKEND_IMAGE@|${BACKEND_IMAGE}|g" \
        -e "s|@YADGAR_INSTALL_PREFIX@|${DATA_DIR}|g" \
        -e "s|@YADGAR_SECRETS_ENV_FILE@|${SECRETS_ENV_FILE}|g" \
        -e "s|@YADGAR_HOME@|${HOME}|g" \
        -e "s|@YADGAR_SCRIPTS_DIR@|${SCRIPTS_INSTALL_DIR}|g" \
        "${template}" > "${output}"
}

mkdir -p "${OUTPUT_DIR}"
mkdir -p "${LOG_DIR}"
mkdir -p "${SCRIPTS_INSTALL_DIR}"

# ── Render daemon plists (RunAtLoad + KeepAlive) ─────────────────────────────

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar.plist"

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar-backend.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar-backend.plist"

# ── Render oneshot / timer / path plists ─────────────────────────────────────

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar-vacuum.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar-vacuum.plist"

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar-nightly-cycle.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar-nightly-cycle.plist"

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar-vacuum-trigger.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar-vacuum-trigger.plist"

render_template \
    "${TEMPLATE_DIR}/com.openfantasy.yadgar-worktree-sweep.plist.in" \
    "${OUTPUT_DIR}/com.openfantasy.yadgar-worktree-sweep.plist"

# ── Install wrapper scripts ───────────────────────────────────────────────────
# Wrapper scripts handle secrets-env sourcing (launchd has no EnvironmentFile).
# They are installed alongside the plists so ProgramArguments can reference them.

WRAPPER_SRC="${TEMPLATE_DIR}"
for wrapper in \
    yadgar-vacuum-wrapper.sh \
    yadgar-nightly-cycle-wrapper.sh \
    yadgar-vacuum-trigger-wrapper.sh \
    yadgar-worktree-sweep-wrapper.sh; do
    if [[ -f "${WRAPPER_SRC}/${wrapper}" ]]; then
        cp "${WRAPPER_SRC}/${wrapper}" "${SCRIPTS_INSTALL_DIR}/${wrapper}"
        chmod 755 "${SCRIPTS_INSTALL_DIR}/${wrapper}"
    else
        echo "WARNING: Wrapper not found: ${WRAPPER_SRC}/${wrapper}" >&2
    fi
done

# ── Install secrets activation script ────────────────────────────────────────
if [[ -f "${WRAPPER_SRC}/yadgar-secrets-activation.sh" ]]; then
    cp "${WRAPPER_SRC}/yadgar-secrets-activation.sh" \
       "${SCRIPTS_INSTALL_DIR}/yadgar-secrets-activation.sh"
    chmod 755 "${SCRIPTS_INSTALL_DIR}/yadgar-secrets-activation.sh"
fi

# ── plutil validation (macOS only) ───────────────────────────────────────────

OS_MARKER="${YADGAR_TEST_OS_MARKER:-}"
IS_DARWIN=0
if [[ "${OS_MARKER}" == "macos" ]] || [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    IS_DARWIN=1
fi

ALL_PLISTS=(
    "com.openfantasy.yadgar.plist"
    "com.openfantasy.yadgar-backend.plist"
    "com.openfantasy.yadgar-vacuum.plist"
    "com.openfantasy.yadgar-nightly-cycle.plist"
    "com.openfantasy.yadgar-vacuum-trigger.plist"
    "com.openfantasy.yadgar-worktree-sweep.plist"
)

if [[ "${IS_DARWIN}" == "1" ]] && command -v plutil &>/dev/null; then
    for plist_name in "${ALL_PLISTS[@]}"; do
        plutil -lint "${OUTPUT_DIR}/${plist_name}" || {
            echo "ERROR: plutil -lint failed on ${plist_name}" >&2
            exit 1
        }
    done
    echo "    plutil -lint: OK (all plists)"
else
    echo "WARNING: plutil not available (non-macOS host). Skipping plist lint." \
         "Validate on macOS with: plutil -lint <plist>" >&2
fi

echo "LaunchAgent plists written to ${OUTPUT_DIR}/"
for plist_name in "${ALL_PLISTS[@]}"; do
    echo "  ${plist_name}"
done
echo "Wrapper scripts installed to ${SCRIPTS_INSTALL_DIR}/"
echo "Log directory: ${LOG_DIR}/"
echo ""
echo "To activate, run: make enable-units"
