#!/usr/bin/env bash
# Generate systemd user units for yadgar from .in templates.
#
# Environment variables (all have defaults):
#   YADGAR_SYSTEMD_OUTPUT_DIR   Target dir (default: ~/.config/systemd/user)
#   YADGAR_RUNTIME              Container runtime: podman|docker (default: auto-detected)
#   YADGAR_INSTALL_PREFIX       Data dir mounted at /data (default: ~/.local/share/yadgar)
#   YADGAR_SECRETS_ENV_FILE     Path to secrets.env (default: ~/.config/yadgar/secrets.env)
#   YADGAR_BACKEND_IMAGE        Backend image tag (default: openfantasy/yadgar-backend:latest)
#   YADGAR_CORE_IMAGE           Core image tag (default: openfantasy/yadgar:latest)
#   YADGAR_TEST_SIMULATE_NIX_SYMLINK  Set to 1 in tests to trigger nix guard via symlink check
#
# Exits non-zero if:
#   - existing units are nix-managed symlinks (defense-in-depth per DP5)
#   - template files are missing
#   - YADGAR_RUNTIME not detected

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUTPUT_DIR="${YADGAR_SYSTEMD_OUTPUT_DIR:-${HOME}/.config/systemd/user}"
RUNTIME="${YADGAR_RUNTIME:-}"
DATA_DIR="${YADGAR_INSTALL_PREFIX:-${HOME}/.local/share/yadgar}"
SECRETS_ENV_FILE="${YADGAR_SECRETS_ENV_FILE:-${HOME}/.config/yadgar/secrets.env}"
BACKEND_IMAGE="${YADGAR_BACKEND_IMAGE:-openfantasy/yadgar-backend:latest}"
CORE_IMAGE="${YADGAR_CORE_IMAGE:-openfantasy/yadgar:latest}"

# ── Runtime detection (if not set) ───────────────────────────────────────────

if [[ -z "${RUNTIME}" ]]; then
    if [[ -x "${SCRIPT_DIR}/detect_runtime.sh" ]]; then
        RUNTIME="$(bash "${SCRIPT_DIR}/detect_runtime.sh" 2>/dev/null)" || {
            echo "ERROR: Could not detect container runtime. Install podman or docker." >&2
            exit 1
        }
    else
        # Fallback inline detection
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

# ── Nix-symlink guard (DP5 defense-in-depth) ─────────────────────────────────

for unit in yadgar.service yadgar-backend.service; do
    unit_path="${OUTPUT_DIR}/${unit}"
    if [[ -L "${unit_path}" ]]; then
        target="$(readlink "${unit_path}")"
        if echo "${target}" | grep -q "/nix/store"; then
            echo "ERROR: ${unit} is managed by Nix (symlink → ${target})." >&2
            echo "  Do not use 'make setup' on NixOS — use the nix flake (v5.46+)." >&2
            echo "  See: https://github.com/m-agahi/yadgar#nixos-install" >&2
            exit 1
        fi
    fi
done

# ── Template rendering ────────────────────────────────────────────────────────

render_template() {
    local template="$1"
    local output="$2"
    [[ -f "${template}" ]] || {
        echo "ERROR: Template not found: ${template}" >&2
        exit 1
    }
    sed \
        -e "s|@RUNTIME@|${RUNTIME}|g" \
        -e "s|@IMAGE@|${CORE_IMAGE}|g" \
        -e "s|@BACKEND_IMAGE@|${BACKEND_IMAGE}|g" \
        -e "s|@DATA_DIR@|${DATA_DIR}|g" \
        -e "s|@SECRETS_ENV_FILE@|${SECRETS_ENV_FILE}|g" \
        "${template}" > "${output}"
}

mkdir -p "${OUTPUT_DIR}"

render_template "${SCRIPT_DIR}/yadgar.service.in"         "${OUTPUT_DIR}/yadgar.service"
render_template "${SCRIPT_DIR}/yadgar-backend.service.in" "${OUTPUT_DIR}/yadgar-backend.service"
render_template "${SCRIPT_DIR}/yadgar.target.in"          "${OUTPUT_DIR}/yadgar.target"

# ── Seed ~/.local/state/yadgar/upgrade.env with initial image tag ─────────────
# yadgar.service uses EnvironmentFile=-%h/.local/state/yadgar/upgrade.env to read
# YADGAR_IMAGE_TAG at runtime. The leading '-' makes a missing file non-fatal,
# but the orchestrator (Phase 9) requires the file to exist before first upgrade.
# We seed it here with the image tag used at install time.
# The orchestrator atomically rewrites this file on each routine upgrade.
UPGRADE_ENV_DIR="${HOME}/.local/state/yadgar"
UPGRADE_ENV_FILE="${UPGRADE_ENV_DIR}/upgrade.env"
mkdir -p "${UPGRADE_ENV_DIR}"
if [[ ! -f "${UPGRADE_ENV_FILE}" ]]; then
    printf 'YADGAR_IMAGE_TAG=%s\n' "${CORE_IMAGE}" > "${UPGRADE_ENV_FILE}"
    echo "Seeded ${UPGRADE_ENV_FILE} with YADGAR_IMAGE_TAG=${CORE_IMAGE}"
else
    echo "Note: ${UPGRADE_ENV_FILE} already exists — not overwritten (orchestrator manages it)."
fi

echo "Systemd units written to ${OUTPUT_DIR}/"
echo "  yadgar.service"
echo "  yadgar-backend.service"
echo "  yadgar.target"
