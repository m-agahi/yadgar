#!/usr/bin/env bash
# uninstall.sh — Remove yadgar systemd units. Preserves data dir unless --purge.
# Usage: uninstall.sh [--purge]
#
# Environment variables:
#   YADGAR_DIR                  Data directory (default: $HOME/.yadgar)
#   YADGAR_TEST_MODE            Set to 1 to skip systemctl calls
#   YADGAR_SYSTEMD_OUTPUT_DIR   Override systemd unit directory (default: $YADGAR_DIR/systemd_user)
set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

YADGAR_DIR="${YADGAR_DIR:-${HOME}/.yadgar}"
SYSTEMD_OUTPUT_DIR="${YADGAR_SYSTEMD_OUTPUT_DIR:-${YADGAR_DIR}/systemd_user}"
TEST_MODE="${YADGAR_TEST_MODE:-0}"

echo "Uninstalling yadgar..."

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
