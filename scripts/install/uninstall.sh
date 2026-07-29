#!/usr/bin/env bash
# uninstall.sh — Remove yadgar service units. Preserves data dir unless --purge.
# Usage: uninstall.sh [--purge]
#
# Supports both Linux (systemd) and macOS (launchd) paths.
# OS is auto-detected via uname or YADGAR_TEST_OS_MARKER (test hook).
#
# Environment variables:
#   YADGAR_DIR                  Data directory (default: $HOME/.local/share/yadgar)
#   YADGAR_TEST_MODE            Set to 1 to skip systemctl/launchctl calls
#   YADGAR_TEST_OS_MARKER       Set to 'macos' to spoof macOS detection (testing)
#   YADGAR_SYSTEMD_OUTPUT_DIR   Override systemd unit directory (default: $HOME/.config/systemd/user)
#   YADGAR_LAUNCHD_OUTPUT_DIR   Override LaunchAgents dir (default: ~/Library/LaunchAgents)
#   YADGAR_LOGS_DIR             Override yadgar logs dir (default: ~/.local/share/yadgar/logs)
set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

YADGAR_DIR="${YADGAR_DIR:-${HOME}/.local/share/yadgar}"
SYSTEMD_OUTPUT_DIR="${YADGAR_SYSTEMD_OUTPUT_DIR:-${HOME}/.config/systemd/user}"
LAUNCHD_OUTPUT_DIR="${YADGAR_LAUNCHD_OUTPUT_DIR:-${HOME}/Library/LaunchAgents}"
YADGAR_LOGS_DIR="${YADGAR_LOGS_DIR:-${HOME}/.local/share/yadgar/logs}"
TEST_MODE="${YADGAR_TEST_MODE:-0}"

# ── OS detection ──────────────────────────────────────────────────────────────

OS_MARKER="${YADGAR_TEST_OS_MARKER:-}"
IS_MACOS=0
if [[ "${OS_MARKER}" == "macos" ]] || [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    IS_MACOS=1
fi

echo "Uninstalling yadgar..."

# ── macOS: launchd path ───────────────────────────────────────────────────────

if [[ "${IS_MACOS}" == "1" ]]; then
    # Unload launchd jobs (skip in test mode)
    if [[ "$TEST_MODE" != "1" ]]; then
        launchctl unload "${LAUNCHD_OUTPUT_DIR}/com.openfantasy.yadgar.plist" 2>/dev/null || true
        launchctl unload "${LAUNCHD_OUTPUT_DIR}/com.openfantasy.yadgar-backend.plist" 2>/dev/null || true
    fi

    # Remove plist files
    for plist in \
        "${LAUNCHD_OUTPUT_DIR}/com.openfantasy.yadgar.plist" \
        "${LAUNCHD_OUTPUT_DIR}/com.openfantasy.yadgar-backend.plist"
    do
        if [[ -e "$plist" ]]; then
            rm -f "$plist"
            echo "Removed: $plist"
        fi
    done

    # Remove yadgar data directory only on --purge
    if [[ "$PURGE" == "1" ]]; then
        if [[ -d "$YADGAR_DIR" ]]; then
            rm -rf "$YADGAR_DIR"
            echo "Removed data directory: $YADGAR_DIR"
        fi
        # Also remove log directory on macOS --purge
        if [[ -d "$YADGAR_LOGS_DIR" ]]; then
            rm -rf "$YADGAR_LOGS_DIR"
            echo "Removed logs directory: $YADGAR_LOGS_DIR"
        fi
    else
        echo "Data directory preserved: $YADGAR_DIR"
        echo "  (run with --purge to remove it)"
    fi

    echo "Yadgar uninstalled."
    exit 0
fi

# ── Linux: systemd path ───────────────────────────────────────────────────────

# DELIBERATE ASYMMETRY (v5.169): install enables systemd lingering
# (scripts/install/enable_linger.sh) but uninstall does NOT disable it.
# Lingering is user-session policy, not yadgar-owned state — it may well be
# keeping somebody's unrelated user services alive, so turning it off here could
# silently break something yadgar never installed. Users who want it gone run
# `loginctl disable-linger $USER` themselves. Asymmetric on purpose, not an
# oversight.

# Stop and disable systemd units (skip in test mode)
if [[ "$TEST_MODE" != "1" ]]; then
    if command -v systemctl &>/dev/null; then
        systemctl --user stop yadgar.target 2>/dev/null || true
        systemctl --user disable yadgar.target yadgar.service yadgar-backend.service 2>/dev/null || true
        systemctl --user daemon-reload 2>/dev/null || true
    fi
fi

# Remove unit files from output dir
if [[ -d "$SYSTEMD_OUTPUT_DIR" ]]; then
    for unit in yadgar.service yadgar-backend.service yadgar.target; do
        unit_path="${SYSTEMD_OUTPUT_DIR}/${unit}"
        if [[ -e "$unit_path" || -L "$unit_path" ]]; then
            rm -f "$unit_path"
            echo "Removed: $unit_path"
        fi
    done
fi

# Remove yadgar data directory only on --purge
if [[ "$PURGE" == "1" ]]; then
    if [[ -d "$YADGAR_DIR" ]]; then
        rm -rf "$YADGAR_DIR"
        echo "Removed data directory: $YADGAR_DIR"
    fi
else
    echo "Data directory preserved: $YADGAR_DIR"
    echo "  (run with --purge to remove it)"
fi

echo "Yadgar uninstalled."
