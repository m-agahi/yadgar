#!/usr/bin/env bash
# Detect available container runtime (podman or docker).
#
# Detection order per DP2 resolution:
#   1. YADGAR_CONTAINER_RUNTIME env override (if set + non-empty)
#   2. YADGAR_TEST_PODMAN_MACHINE_SOCKET (test hook — macOS podman-machine socket path)
#   3. podman (rootless-friendly, preferred)
#   4. docker
#   5. Neither found → exits 1 with canonical error message
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
# Usage:
#   RUNTIME=$(bash scripts/install/detect_runtime.sh)
#   YADGAR_CONTAINER_RUNTIME=docker bash scripts/install/detect_runtime.sh
#   YADGAR_TEST_PODMAN_MACHINE_SOCKET=/path/to/podman.sock bash detect_runtime.sh

set -euo pipefail

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

# ── Neither found ────────────────────────────────────────────────────────────

echo "No container runtime found." >&2
echo "  Install podman: https://podman.io/getting-started/installation" >&2
echo "  Or install docker: https://docs.docker.com/engine/install/" >&2
echo "Ensure the daemon is running, then re-run: yadgar install" >&2
exit 1
