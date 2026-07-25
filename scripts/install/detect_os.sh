#!/usr/bin/env bash
# Detect host OS and output canonical OS identifier.
#
# Outputs one of:
#   linux-nixos    — Linux + NixOS detected (/etc/NIXOS or nixos-version)
#   linux          — Linux (non-NixOS)
#   linux-other    — Linux with no systemd
#   macos          — macOS/Darwin
#   unsupported    — anything else
#
# NixOS behaviour depends on env variables:
#   YADGAR_NIXOS_ABORT=1         — exits non-zero when NixOS detected
#   YADGAR_TEST_NIXOS_MARKER=<p> — override /etc/NIXOS marker path (testing)
#   YADGAR_TEST_OS_MARKER=macos  — spoof macOS detection (testing on Linux)
#
# Exits 0 normally. Exits 1 when YADGAR_NIXOS_ABORT=1 and NixOS detected.

set -euo pipefail

# Test hook: allow overriding the nixos marker path for unit tests
NIXOS_MARKER="${YADGAR_TEST_NIXOS_MARKER:-/etc/NIXOS}"

# ── Test hook: spoof macOS for cross-platform testing ─────────────────────────

if [[ "${YADGAR_TEST_OS_MARKER:-}" == "macos" ]]; then
    echo "macos"
    exit 0
fi

# ── macOS / Darwin ────────────────────────────────────────────────────────────

if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "macos"
    exit 0
fi

# ── Linux ─────────────────────────────────────────────────────────────────────

if [[ "$(uname -s)" == "Linux" ]]; then
    # NixOS detection: check file marker first, then nixos-version command
    if [[ -f "${NIXOS_MARKER}" ]] || command -v nixos-version &>/dev/null 2>&1; then
        if [[ "${YADGAR_NIXOS_ABORT:-0}" == "1" ]]; then
            cat >&2 <<'NIXOS_MSG'
ERROR: yadgar appears to be running on NixOS.
  'make setup' does not support NixOS (home-manager / nix flake is the correct path).

  For NixOS installation, use the nix flake (v5.46+):
    https://github.com/m-agahi/yadgar#nixos-install

  Your existing NixOS-managed install (via home-manager activation) continues
  to work unchanged. This installer is for non-NixOS Linux only.
NIXOS_MSG
            exit 1
        fi
        echo "linux-nixos"
        exit 0
    fi

    # Check for systemd
    if command -v systemctl &>/dev/null; then
        echo "linux"
    else
        echo "linux-other"
    fi
    exit 0
fi

# ── Unsupported ───────────────────────────────────────────────────────────────

echo "unsupported"
exit 0
