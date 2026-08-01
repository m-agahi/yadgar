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
#   YADGAR_STATE_DIR            XDG state dir bound into the core container for the
#                               vacuum trigger (default: ~/.local/state/yadgar)
#   YADGAR_BACKEND_SURREAL_PORT Host port SurrealDB is published on, loopback-only
#                               (default: 8000). Override when :8000 is occupied.
#   YADGAR_HOST_CLI             Explicit path to the `yadgar` host CLI (escape hatch)
#   YADGAR_HOST_NIGHTLY_CLI     Explicit path to the `yadgar-nightly-cycle` host CLI
#   YADGAR_TEST_SIMULATE_NIX_SYMLINK  Set to 1 in tests to trigger nix guard via symlink check
#
# Exits non-zero if:
#   - existing units are nix-managed symlinks (defense-in-depth per DP5)
#   - template files are missing
#   - YADGAR_RUNTIME not detected
#   - no host yadgar CLI resolves (the maintenance units would fail at 4am)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUTPUT_DIR="${YADGAR_SYSTEMD_OUTPUT_DIR:-${HOME}/.config/systemd/user}"
RUNTIME="${YADGAR_RUNTIME:-}"
DATA_DIR="${YADGAR_INSTALL_PREFIX:-${HOME}/.local/share/yadgar}"
SECRETS_ENV_FILE="${YADGAR_SECRETS_ENV_FILE:-${HOME}/.config/yadgar/secrets.env}"
BACKEND_IMAGE="${YADGAR_BACKEND_IMAGE:-openfantasy/yadgar-backend:latest}"
CORE_IMAGE="${YADGAR_CORE_IMAGE:-openfantasy/yadgar:latest}"
# XDG state dir. Bound into the core container so vacuum_now()'s trigger file
# lands on the host, where yadgar-vacuum-trigger.path watches for it. The SAME
# token is used on the left of the `-v` bind and in PathExists= — the
# cross-generator test compares those two as exact strings (R7).
STATE_DIR="${YADGAR_STATE_DIR:-${HOME}/.local/state/yadgar}"
# Host port for the backend's SurrealDB (:8000), loopback-only. The nightly and
# vacuum units run on the HOST and reach SurrealDB over HTTP, so without the
# publish they render, activate, fire and connection-refuse. Overridable because
# :8000 is commonly occupied by a dev server (R2).
BACKEND_SURREAL_PORT="${YADGAR_BACKEND_SURREAL_PORT:-8000}"

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

# ── Runtime-conditional readiness (task:0105) ────────────────────────────────
#
# Readiness cannot be expressed the same way on both runtimes. Podman proxies
# sd_notify (default --sdnotify=container passes NOTIFY_SOCKET into the container
# and forwards the daemon's own READY=1), so its units are Type=notify. Docker
# has no sd_notify proxy at all, so nothing would ever send READY=1 and the unit
# would sit until TimeoutStartSec; it gets Type=exec plus a bounded ExecStartPost
# /health poll instead.
#
# sed cannot branch, so the templates carry LINE-PREFIX markers and this decides,
# once, whether each marker means "strip the prefix" or "delete the whole line".
# Deleting rather than substituting an empty value keeps the rendered unit free
# of stray blank lines — the podman render stays byte-identical to pre-0105.
#
# ONE template per unit, not one per runtime: this repo already carries four
# cross-generator invariant tests because generator drift is its recurring defect
# class, and forking the templates would recreate that drift inside a single
# generator. Guarded by yadgar/tests/scripts/test_runtime_readiness_cross_generator.py.
# Both expressions are ANCHORED to column 0 (`^`). These markers are LINE
# PREFIXES by definition, and an unanchored `/@DOCKER_ONLY@/d` deletes any line
# that so much as MENTIONS the marker — which silently ate a prose comment line
# in this very template on the first render. Anchoring makes a mid-line mention
# inert, so the templates can document their own mechanism.
if [[ "${RUNTIME##*/}" == "podman" ]]; then
    SERVICE_TYPE="notify"
    RUNTIME_SED=(-e "s|^@PODMAN_ONLY@||" -e "/^@DOCKER_ONLY@/d")
else
    SERVICE_TYPE="exec"
    RUNTIME_SED=(-e "/^@PODMAN_ONLY@/d" -e "s|^@DOCKER_ONLY@||")
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

# ── Host CLI resolution (@VACUUM_EXEC@ / @NIGHTLY_EXEC@) ─────────────────────
#
# The vacuum and nightly-cycle units execute on the HOST, not in a container:
# the vacuum flow interleaves phases requiring different daemon states
# (export → backend DOWN → reimport → backend UP) and the image ships no
# systemctl. So both need a host entry point resolved AT RENDER TIME.
#
# Resolution is deliberately fail-loud: an unresolvable CLI aborts the install
# with an actionable message, rather than baking a broken ExecStart into a unit
# that starts, fails, and is never looked at until consolidation has silently
# stopped for weeks.
#
# NOTE the two entry points are DIFFERENT binaries. `yadgar-nightly-cycle` is a
# console script (pyproject [project.scripts]); there is NO `yadgar
# nightly-cycle` subcommand, and nightly_cycle.main() has no argparse at all —
# it is configured entirely through the environment and invoked bare.

_resolve_host_exec() {
    local script="$1" module="$2" override="$3" found

    if [[ -n "${override}" ]]; then
        printf '%s' "${override}"
        return 0
    fi
    # The pipx shape (flake.nix installs the CLI this way).
    if [[ -x "${HOME}/.local/bin/${script}" ]]; then
        printf '%s' "${HOME}/.local/bin/${script}"
        return 0
    fi
    # brew, /usr/local, other prefixes.
    if found="$(command -v "${script}" 2>/dev/null)"; then
        printf '%s' "${found}"
        return 0
    fi
    # `python3 -I`: isolated mode drops cwd from sys.path. WITHOUT -I this probe
    # succeeds from inside a repo checkout even with nothing installed, and the
    # unit — which runs from a different working directory — then fails at 4am.
    # Probe what the unit will actually experience (R6).
    if command -v python3 > /dev/null 2>&1 && python3 -I -c "import ${module}" > /dev/null 2>&1; then
        printf 'python3 -m %s' "${module}"
        return 0
    fi
    return 1
}

_fail_no_host_cli() {
    echo "ERROR: no host yadgar CLI found for the $1 maintenance unit." >&2
    echo "  Tried: \$$2, ~/.local/bin/$3, 'command -v $3', 'python3 -m $4'." >&2
    echo "  Background maintenance (consolidation, heat decay, vacuum) runs on the" >&2
    echo "  HOST, so a host CLI is required. Install one with:" >&2
    echo "      pipx install yadgar" >&2
    echo "  ...then re-run setup. Or point \$$2 at an existing install." >&2
    exit 1
}

if ! VACUUM_EXEC="$(_resolve_host_exec yadgar yadgar "${YADGAR_HOST_CLI:-}")"; then
    _fail_no_host_cli vacuum YADGAR_HOST_CLI yadgar yadgar
fi
if ! NIGHTLY_EXEC="$(
    _resolve_host_exec yadgar-nightly-cycle yadgar.core.scripts.nightly_cycle \
        "${YADGAR_HOST_NIGHTLY_CLI:-}"
)"; then
    _fail_no_host_cli nightly-cycle YADGAR_HOST_NIGHTLY_CLI \
        yadgar-nightly-cycle yadgar.core.scripts.nightly_cycle
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
        "${RUNTIME_SED[@]}" \
        -e "s|@SERVICE_TYPE@|${SERVICE_TYPE}|g" \
        -e "s|@RUNTIME@|${RUNTIME}|g" \
        -e "s|@IMAGE@|${CORE_IMAGE}|g" \
        -e "s|@BACKEND_IMAGE@|${BACKEND_IMAGE}|g" \
        -e "s|@DATA_DIR@|${DATA_DIR}|g" \
        -e "s|@SECRETS_ENV_FILE@|${SECRETS_ENV_FILE}|g" \
        -e "s|@STATE_DIR@|${STATE_DIR}|g" \
        -e "s|@BACKEND_SURREAL_PORT@|${BACKEND_SURREAL_PORT}|g" \
        -e "s|@VACUUM_EXEC@|${VACUUM_EXEC}|g" \
        -e "s|@NIGHTLY_EXEC@|${NIGHTLY_EXEC}|g" \
        "${template}" > "${output}"
}

mkdir -p "${OUTPUT_DIR}"

# Pre-create the trigger dir so the .path unit has an existing parent at first
# activation. Mirrors generate_launchd.sh (where launchd's WatchPaths genuinely
# needs the dir present at load); on systemd it removes the first-boot race.
mkdir -p "${STATE_DIR}/triggers"

# Single source of truth for the unit set: the render loop, the closing summary,
# and (via the generator-derived uninstall test) uninstall.sh all read from here.
# A unit added to the array but forgotten in the summary is the drift class this
# array exists to make impossible.
UNITS=(
    yadgar.service
    yadgar-backend.service
    yadgar.target
    yadgar-vacuum.service
    yadgar-vacuum.timer
    yadgar-vacuum-trigger.path
    yadgar-vacuum-trigger.service
    yadgar-nightly-cycle.service
    yadgar-nightly-cycle.timer
)

for unit_name in "${UNITS[@]}"; do
    render_template "${SCRIPT_DIR}/${unit_name}.in" "${OUTPUT_DIR}/${unit_name}"
done

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
for unit_name in "${UNITS[@]}"; do
    echo "  ${unit_name}"
done
echo "Maintenance entry points resolved at render time:"
echo "  vacuum:        ${VACUUM_EXEC}"
echo "  nightly-cycle: ${NIGHTLY_EXEC}"
echo "SurrealDB published on 127.0.0.1:${BACKEND_SURREAL_PORT} (loopback only)."
echo "Vacuum trigger dir: ${STATE_DIR}/triggers"
