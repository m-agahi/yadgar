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
    # Every plist generate_launchd.sh renders. The list previously covered only
    # the two daemon plists, so an uninstall left the four maintenance
    # LaunchAgents loaded and still firing against a removed install. Guarded by
    # test_uninstall_removes_every_launchd_plist_the_generator_renders, which
    # derives its expectation from an actual render rather than a second list.
    MACOS_PLISTS=(
        com.openfantasy.yadgar.plist
        com.openfantasy.yadgar-backend.plist
        com.openfantasy.yadgar-vacuum.plist
        com.openfantasy.yadgar-nightly-cycle.plist
        com.openfantasy.yadgar-vacuum-trigger.plist
        com.openfantasy.yadgar-worktree-sweep.plist
    )

    # Unload launchd jobs (skip in test mode)
    if [[ "$TEST_MODE" != "1" ]]; then
        for plist_name in "${MACOS_PLISTS[@]}"; do
            launchctl unload "${LAUNCHD_OUTPUT_DIR}/${plist_name}" 2>/dev/null || true
        done
    fi

    # Remove plist files
    for plist_name in "${MACOS_PLISTS[@]}"; do
        plist="${LAUNCHD_OUTPUT_DIR}/${plist_name}"
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

# Every unit generate_systemd.sh renders. The list previously covered only the
# three daemon units, so an uninstall left the timers and the trigger watcher
# behind — still scheduled, now pointing at a removed install. Guarded by
# test_uninstall_removes_every_systemd_unit_the_generator_renders, which derives
# its expectation from an actual render rather than a second hardcoded list.
SYSTEMD_UNITS=(
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
# Timers carrying Persistent=true leave a last-fire stamp under
# ~/.local/share/systemd/timers/ that OUTLIVES the unit file, so a later
# reinstall inherits a stale timestamp and may immediately catch up (or not fire
# when expected). `man systemd.timer` recommends clearing it explicitly.
PERSISTENT_TIMERS=(yadgar-vacuum.timer yadgar-nightly-cycle.timer)

# Stop and disable systemd units (skip in test mode)
if [[ "$TEST_MODE" != "1" ]]; then
    if command -v systemctl &>/dev/null; then
        systemctl --user stop yadgar.target 2>/dev/null || true
        # Timers and the .path watcher are pulled in by yadgar.target's Wants=,
        # which does NOT stop them when the target stops — stop them by name.
        systemctl --user disable --now \
            yadgar-vacuum.timer yadgar-nightly-cycle.timer yadgar-vacuum-trigger.path \
            2>/dev/null || true
        systemctl --user disable yadgar.target yadgar.service yadgar-backend.service 2>/dev/null || true
        for timer in "${PERSISTENT_TIMERS[@]}"; do
            systemctl --user clean --what=state "${timer}" 2>/dev/null || true
        done
        systemctl --user daemon-reload 2>/dev/null || true
    fi
fi

# Remove unit files from output dir
if [[ -d "$SYSTEMD_OUTPUT_DIR" ]]; then
    for unit in "${SYSTEMD_UNITS[@]}"; do
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
