#!/usr/bin/env bash
# Detect available container runtime (podman or docker).
#
# Detection order per DP2 resolution:
#   1. YADGAR_CONTAINER_RUNTIME env override (if set + non-empty)
#   2. YADGAR_TEST_PODMAN_MACHINE_SOCKET (test hook — macOS podman-machine socket path)
#   3. podman (rootless-friendly, preferred)
#   4. docker
#   5. Neither found → exits 1 with OS-aware install hints
#
# macOS (DP-C): podman-machine exposes a socket at a path different from Linux.
#   On macOS, DOCKER_HOST / CONTAINER_HOST should be set to the machine socket.
#   Probe order: YADGAR_TEST_PODMAN_MACHINE_SOCKET → podman machine default socket
#   → standard podman probe → docker probe.
#   If podman binary found but socket not reachable: print sentinel message
#   "podman machine not running — start with: podman machine init && podman machine start"
#
# Outputs the runtime name (podman|docker) on stdout.
# Exits 0 on success, 1 on failure.
#
# Flags:
#   --quiet    Suppress verbose install hints (use when called by chained scripts)
#
# Usage:
#   RUNTIME=$(bash scripts/install/detect_runtime.sh)
#   YADGAR_CONTAINER_RUNTIME=docker bash scripts/install/detect_runtime.sh
#   YADGAR_TEST_PODMAN_MACHINE_SOCKET=/path/to/podman.sock bash detect_runtime.sh
#   YADGAR_TEST_OS_RELEASE=/path/to/fake-os-release bash detect_runtime.sh
#
# Test hooks:
#   YADGAR_TEST_PODMAN_MACHINE_SOCKET  Override podman machine socket path
#   YADGAR_TEST_OS_MARKER              Force 'macos' detection on Linux CI
#   YADGAR_TEST_OS_RELEASE             Override /etc/os-release path (default: /etc/os-release)

set -euo pipefail

# ── Flag parsing ──────────────────────────────────────────────────────────────

QUIET=0
for _arg in "$@"; do
    case "$_arg" in
        --quiet) QUIET=1 ;;
    esac
done

# ── Env override ─────────────────────────────────────────────────────────────

if [[ -n "${YADGAR_CONTAINER_RUNTIME:-}" ]]; then
    rt="${YADGAR_CONTAINER_RUNTIME}"
    # Validate: run `<runtime> info` to confirm it's operational
    if command -v "${rt}" &>/dev/null && "${rt}" info &>/dev/null 2>&1; then
        echo "${rt}"
        exit 0
    else
        echo "ERROR: YADGAR_CONTAINER_RUNTIME=${rt} set but '${rt} info' failed." >&2
        echo "  Is the ${rt} daemon running?" >&2
        exit 1
    fi
fi

# ── macOS podman-machine socket probe (DP-C) ─────────────────────────────────
# Test hook: YADGAR_TEST_PODMAN_MACHINE_SOCKET overrides socket path for unit tests.
# In production, podman-machine sets DOCKER_HOST automatically; this is a fallback
# probe for when the machine socket exists but the env var isn't set.

_PODMAN_MACHINE_SOCKET="${YADGAR_TEST_PODMAN_MACHINE_SOCKET:-}"

if [[ -n "${_PODMAN_MACHINE_SOCKET}" ]]; then
    # Test/explicit socket path provided — probe via CONTAINER_HOST
    if command -v podman &>/dev/null; then
        if CONTAINER_HOST="unix://${_PODMAN_MACHINE_SOCKET}" podman info &>/dev/null 2>&1; then
            echo "podman"
            exit 0
        else
            echo "ERROR: podman machine socket found at ${_PODMAN_MACHINE_SOCKET} but 'podman info' failed." >&2
            echo "  podman machine not running — start with: podman machine init && podman machine start" >&2
            exit 1
        fi
    fi
fi

# ── Probe podman (preferred — rootless-friendly) ──────────────────────────────

if command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
    echo "podman"
    exit 0
fi

# ── macOS: check for podman-machine-specific socket when standard probe fails ─

OS_MARKER="${YADGAR_TEST_OS_MARKER:-}"
IS_MACOS=0
if [[ "${OS_MARKER}" == "macos" ]] || [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    IS_MACOS=1
fi

if [[ "${IS_MACOS}" == "1" ]] && command -v podman &>/dev/null; then
    # Podman binary found but podman info failed — likely podman machine not started
    echo "ERROR: podman found but 'podman info' failed on macOS." >&2
    echo "  podman machine not running — start with: podman machine init && podman machine start" >&2
    exit 1
fi

# ── Probe docker ─────────────────────────────────────────────────────────────

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    echo "docker"
    exit 0
fi

# ── Neither found — OS-aware install hints ────────────────────────────────────

echo "ERROR: No container runtime (podman or docker) found." >&2

if [[ "${QUIET}" == "1" ]]; then
    echo "  Install podman or docker, then re-run: yadgar-setup" >&2
    exit 1
fi

# Detect distro for OS-aware hint
_OS_RELEASE_FILE="${YADGAR_TEST_OS_RELEASE:-/etc/os-release}"
_DISTRO_ID=""
_DISTRO_ID_LIKE=""

if [[ "${IS_MACOS}" == "1" ]]; then
    _DISTRO_ID="darwin"
elif [[ -f "${_OS_RELEASE_FILE}" ]]; then
    # Source os-release in a subshell to extract ID and ID_LIKE without grep/sed.
    # Use explicit var-unset guard; source only exports ID and ID_LIKE.
    _src_result=$(
        ID=""
        ID_LIKE=""
        # shellcheck source=/dev/null
        . "${_OS_RELEASE_FILE}" 2>/dev/null || true
        printf '%s|%s' "${ID}" "${ID_LIKE}"
    )
    _raw_id="${_src_result%%|*}"        # everything before first |
    _DISTRO_ID="${_raw_id//\"/}"        # strip any residual quotes
    _raw_id_like="${_src_result#*|}"    # everything after first |
    _DISTRO_ID_LIKE="${_raw_id_like//\"/}"
fi

# Resolve install command from ID, falling back to ID_LIKE
_install_cmd=""
_resolve_install_cmd() {
    local id="$1"
    case "${id}" in
        ubuntu|debian|pop|linuxmint|raspbian)
            _install_cmd="sudo apt-get install -y podman" ;;
        fedora|rhel|centos|rocky|almalinux)
            _install_cmd="sudo dnf install -y podman" ;;
        arch|manjaro|endeavouros)
            _install_cmd="sudo pacman -S --noconfirm podman" ;;
        alpine)
            _install_cmd="sudo apk add podman" ;;
        opensuse*|sles|suse)
            _install_cmd="sudo zypper install -y podman" ;;
        darwin)
            _install_cmd="brew install podman" ;;
    esac
}

_resolve_install_cmd "${_DISTRO_ID}"

# Fallback: try ID_LIKE tokens if primary ID didn't match
if [[ -z "${_install_cmd}" && -n "${_DISTRO_ID_LIKE}" ]]; then
    for _like_id in ${_DISTRO_ID_LIKE}; do
        _resolve_install_cmd "${_like_id}"
        [[ -n "${_install_cmd}" ]] && break
    done
fi

# Print OS-aware hint
echo "" >&2
if [[ -n "${_install_cmd}" ]]; then
    echo "  Install podman with:" >&2
    echo "    ${_install_cmd}" >&2
    if [[ "${_DISTRO_ID}" == "darwin" ]]; then
        echo "  Then initialize the podman machine:" >&2
        echo "    podman machine init && podman machine start" >&2
    fi
else
    echo "  Install podman: https://podman.io/getting-started/installation" >&2
    echo "  Or install docker: https://docs.docker.com/engine/install/" >&2
fi
echo "" >&2
echo "  Ensure the daemon is running, then re-run: yadgar-setup" >&2
exit 1
