#!/usr/bin/env bash
# install_runtime.sh — shared helper for podman install prompt + execution.
#
# Called by both yadgar-setup.sh (_offer_install_runtime) and Makefile (install-runtime target).
# Detects the OS, selects the appropriate install command, and either:
#   - In interactive mode (TTY + not --noninteractive): prompts the user, installs on Y.
#   - In non-interactive mode: prints the install command and exits 1.
#
# After successful install, re-runs detect_runtime.sh to verify podman is operational.
#
# Flags:
#   --install-runtime     Skip prompt; run install directly (yes-mode).
#   --no-install-runtime  Skip prompt; print hint and exit 1 (no-mode).
#
# Environment:
#   INSTALL_NONINTERACTIVE=1   Non-interactive mode (same as --noninteractive flag on yadgar-setup).
#   YADGAR_TEST_OS_RELEASE=<p> Override /etc/os-release path (test seam).
#   YADGAR_TEST_INSTALL_DRYRUN=1  Print install command without executing (test seam).
#   YADGAR_TEST_TTY=0|1        Override TTY detection (test seam; 0=no-tty, 1=tty).
#   YADGAR_TEST_OS_MARKER=macos  Force macOS detection path (test seam, from detect_os.sh).
#
# Exit codes:
#   0  runtime detected / installed successfully
#   1  no runtime + user declined / non-interactive / install failed

set -euo pipefail

# Resolve script dir using bash builtins only (no dirname — not available on all PATH configs)
_BASH_SOURCE="${BASH_SOURCE[0]}"
# Trim last path component using bash parameter expansion
SCRIPT_DIR="${_BASH_SOURCE%/*}"
# If the result equals the source (no slash), we're in cwd
[[ "${SCRIPT_DIR}" == "${_BASH_SOURCE}" ]] && SCRIPT_DIR="."
# Resolve to absolute path
SCRIPT_DIR="$(cd "${SCRIPT_DIR}" && pwd)"

# ── Flags ─────────────────────────────────────────────────────────────────────

FORCE_YES=0
FORCE_NO=0

for _arg in "$@"; do
    case "$_arg" in
        --install-runtime)    FORCE_YES=1 ;;
        --no-install-runtime) FORCE_NO=1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

log()  { echo "==> $*"; }
info() { echo "    $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ── OS / Distro detection ─────────────────────────────────────────────────────

_OS_RELEASE_FILE="${YADGAR_TEST_OS_RELEASE:-/etc/os-release}"
_DISTRO_ID=""
_DISTRO_ID_LIKE=""

# macOS detection (honours same test seam as detect_runtime.sh)
OS_MARKER="${YADGAR_TEST_OS_MARKER:-}"
IS_MACOS=0
if [[ "${OS_MARKER}" == "macos" ]] || [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    IS_MACOS=1
fi

if [[ "${IS_MACOS}" == "1" ]]; then
    _DISTRO_ID="darwin"
elif [[ -n "${_OS_RELEASE_FILE}" && -f "${_OS_RELEASE_FILE}" ]]; then
    _src_result=$(
        ID=""
        ID_LIKE=""
        # shellcheck source=/dev/null
        . "${_OS_RELEASE_FILE}" 2>/dev/null || true
        printf '%s|%s' "${ID}" "${ID_LIKE}"
    )
    _raw_id="${_src_result%%|*}"
    _DISTRO_ID="${_raw_id//\"/}"
    _raw_id_like="${_src_result#*|}"
    _DISTRO_ID_LIKE="${_raw_id_like//\"/}"
fi

# ── Resolve install command ───────────────────────────────────────────────────

INSTALL_CMD=""
_resolve_install_cmd() {
    local id="$1"
    case "${id}" in
        ubuntu|debian|pop|linuxmint|raspbian)
            INSTALL_CMD="sudo apt-get install -y podman" ;;
        fedora|rhel|centos|rocky|almalinux)
            INSTALL_CMD="sudo dnf install -y podman" ;;
        arch|manjaro|endeavouros)
            INSTALL_CMD="sudo pacman -S --noconfirm podman" ;;
        alpine)
            INSTALL_CMD="sudo apk add podman" ;;
        opensuse*|sles|suse)
            INSTALL_CMD="sudo zypper install -y podman" ;;
        darwin)
            INSTALL_CMD="brew install podman" ;;
    esac
}

_resolve_install_cmd "${_DISTRO_ID}"

# Fallback: try ID_LIKE tokens if primary ID didn't match
if [[ -z "${INSTALL_CMD}" && -n "${_DISTRO_ID_LIKE}" ]]; then
    for _like_id in ${_DISTRO_ID_LIKE}; do
        _resolve_install_cmd "${_like_id}"
        [[ -n "${INSTALL_CMD}" ]] && break
    done
fi

# Final fallback: generic URL hint
if [[ -z "${INSTALL_CMD}" ]]; then
    INSTALL_CMD=""  # will trigger URL fallback branch below
fi

# ── Interactive / non-interactive gate ────────────────────────────────────────

# TTY detection: honour YADGAR_TEST_TTY seam if set, else probe stdin
IS_TTY=0
if [[ "${YADGAR_TEST_TTY:-unset}" == "1" ]]; then
    IS_TTY=1
elif [[ "${YADGAR_TEST_TTY:-unset}" == "0" ]]; then
    IS_TTY=0
else
    test -t 0 && IS_TTY=1 || IS_TTY=0
fi

# Non-interactive gate: INSTALL_NONINTERACTIVE=1 OR no TTY (unless FORCE_YES)
_is_noninteractive() {
    [[ "${INSTALL_NONINTERACTIVE:-0}" == "1" ]] && return 0
    [[ "${FORCE_NO}" == "1" ]] && return 0
    [[ "${IS_TTY}" == "0" ]] && return 0
    return 1
}

# ── Print install hint ────────────────────────────────────────────────────────

_print_install_hint() {
    echo "" >&2
    if [[ -n "${INSTALL_CMD}" ]]; then
        info "Install podman with:"
        echo "    ${INSTALL_CMD}" >&2
        if [[ "${_DISTRO_ID}" == "darwin" ]]; then
            echo "" >&2
            info "Then initialize the podman machine:"
            echo "    podman machine init && podman machine start" >&2
        fi
    else
        info "Install podman: https://podman.io/getting-started/installation"
        info "Or install docker: https://docs.docker.com/engine/install/"
    fi
    echo "" >&2
}

# ── Retry detect_runtime after install ───────────────────────────────────────

_retry_detect() {
    info "Re-checking for container runtime..."
    if [[ -f "${SCRIPT_DIR}/detect_runtime.sh" ]]; then
        if bash "${SCRIPT_DIR}/detect_runtime.sh" --quiet >/dev/null 2>&1; then
            info "Container runtime detected successfully."
            return 0
        fi
    fi
    echo "WARN: Runtime still not detected after install. Try: yadgar-setup --doctor" >&2
    return 1
}

# ── Run install ───────────────────────────────────────────────────────────────

_run_install() {
    if [[ -z "${INSTALL_CMD}" ]]; then
        _print_install_hint
        die "Cannot auto-install: unknown distro. Install podman manually and re-run: yadgar-setup"
    fi

    if [[ "${YADGAR_TEST_INSTALL_DRYRUN:-0}" == "1" ]]; then
        info "[dryrun] Would run: ${INSTALL_CMD}"
    else
        log "Running: ${INSTALL_CMD}"
        eval "${INSTALL_CMD}"
    fi
    _retry_detect
}

# ── Main logic ────────────────────────────────────────────────────────────────

# FORCE_YES: skip prompt, install directly
if [[ "${FORCE_YES}" == "1" ]]; then
    _run_install
    exit $?
fi

# Non-interactive: print hint + exit 1
if _is_noninteractive; then
    echo "No container runtime found." >&2
    if [[ -n "${INSTALL_CMD}" ]]; then
        echo "  Install podman with:" >&2
        echo "    ${INSTALL_CMD}" >&2
        if [[ "${_DISTRO_ID}" == "darwin" ]]; then
            echo "  Then: podman machine init && podman machine start" >&2
        fi
    else
        echo "  Install podman: https://podman.io/getting-started/installation" >&2
    fi
    echo "  Re-run: yadgar-setup" >&2
    exit 1
fi

# Interactive: prompt
log "No container runtime (podman or docker) found."
_print_install_hint

if [[ -n "${INSTALL_CMD}" ]]; then
    printf "Install podman now? [Y/n] " >&2
    read -r _answer </dev/stdin || _answer=""
    _answer="${_answer:-Y}"   # empty Enter defaults to Y

    case "${_answer}" in
        [Yy]|[Yy][Ee][Ss])
            _run_install
            exit $?
            ;;
        *)
            echo "Skipping install. Re-run 'yadgar-setup' after installing podman." >&2
            exit 1
            ;;
    esac
else
    die "No runtime found and no install command for this OS. Install podman manually and re-run: yadgar-setup"
fi
