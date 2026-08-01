#!/usr/bin/env bash
# v5.48.0 — Detect yadgar install method for non-Python callers (Makefile, CI).
#
# Emits one of:
#   pipx / brew / nix-flake / container / source / unknown / not_installed
#
# Exit codes:
#   0  detection succeeded (even if result is "unknown")
#   1  yadgar not found on PATH

set -euo pipefail

# 1. Find yadgar on PATH
if ! command -v yadgar &>/dev/null; then
    echo "not_installed"
    exit 1
fi

YADGAR_BIN="$(command -v yadgar)"
REAL_PATH="$(realpath "$YADGAR_BIN" 2>/dev/null || readlink -f "$YADGAR_BIN" 2>/dev/null || echo "$YADGAR_BIN")"

# 2. nix-flake: resolves into /nix/store/
if [[ "$REAL_PATH" == /nix/store/* ]]; then
    echo "nix-flake"
    exit 0
fi

# 3. brew: resolves into Cellar
if [[ "$REAL_PATH" == */Cellar/yadgar/* ]]; then
    echo "brew"
    exit 0
fi

# 4. pipx: resolves into <PIPX_HOME>/venvs/yadgar/. Legacy default is
# ~/.local/pipx; pipx >=1.6 changed the default to the XDG data dir, i.e.
# ~/.local/share/pipx, inserting a "share" segment. Respect an explicit
# PIPX_HOME first (honors custom installs), then fall back to matching the
# pipx/venvs/yadgar/ segment regardless of what precedes it so both known
# defaults resolve without hardcoding either prefix.
if [[ -n "${PIPX_HOME:-}" ]]; then
    PIPX_HOME_REAL="$(realpath "$PIPX_HOME" 2>/dev/null || readlink -f "$PIPX_HOME" 2>/dev/null || echo "$PIPX_HOME")"
    if [[ "$REAL_PATH" == "$PIPX_HOME_REAL/venvs/yadgar/"* ]]; then
        echo "pipx"
        exit 0
    fi
fi
if [[ "$REAL_PATH" == */pipx/venvs/yadgar/* ]]; then
    echo "pipx"
    exit 0
fi

# 5. container: shim whose content includes "docker run"
if head -5 "$YADGAR_BIN" 2>/dev/null | grep -q "docker run"; then
    echo "container"
    exit 0
fi

# 6. source: walk ancestors looking for .git
CHECK_DIR="$(dirname "$REAL_PATH")"
while [[ "$CHECK_DIR" != "/" && "$CHECK_DIR" != "." ]]; do
    if [[ -d "$CHECK_DIR/.git" ]]; then
        echo "source"
        exit 0
    fi
    CHECK_DIR="$(dirname "$CHECK_DIR")"
done

echo "unknown"
exit 0
