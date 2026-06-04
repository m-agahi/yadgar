#!/usr/bin/env bash
# Detect available container runtime (podman or docker).
#
# Detection order per DP2 resolution:
#   1. YADGAR_CONTAINER_RUNTIME env override (if set + non-empty)
#   2. podman (rootless-friendly, preferred)
#   3. docker
#   4. Neither found → exits 1 with canonical error message
#
# Outputs the runtime name (podman|docker) on stdout.
# Exits 0 on success, 1 on failure.
#
# Usage:
#   RUNTIME=$(bash scripts/install/detect_runtime.sh)
#   YADGAR_CONTAINER_RUNTIME=docker bash scripts/install/detect_runtime.sh

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

# ── Probe podman (preferred — rootless-friendly) ──────────────────────────────

if command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
    echo "podman"
    exit 0
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
