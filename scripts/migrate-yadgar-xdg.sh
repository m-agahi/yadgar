#!/usr/bin/env bash
# migrate-yadgar-xdg.sh — one-shot migration from legacy ~/.yadgar/ to XDG paths.
#
# Usage:
#   1. systemctl --user stop yadgar.target
#   2. bash scripts/migrate-yadgar-xdg.sh
#   3. pipx upgrade yadgar  (or home-manager switch for nix users)
#   4. yadgar-setup  (re-runs unit generation + restarts daemon)
#
# Idempotent: safe to re-run. Each mv is best-effort; missing source = no-op.
# Single-user script — no daemon-running preflight, no cross-filesystem check.

set -e

mkdir -p ~/.config/yadgar ~/.local/share/yadgar ~/.local/state/yadgar
chmod 700 ~/.config/yadgar ~/.local/state/yadgar

mv ~/.yadgar/config.yaml                    ~/.config/yadgar/         2>/dev/null || true
mv ~/.yadgar/secrets.env                    ~/.config/yadgar/         2>/dev/null || true
mv ~/.yadgar/secret-gate-allowlist.yaml     ~/.config/yadgar/         2>/dev/null || true

mv ~/.yadgar/surreal_db                     ~/.local/share/yadgar/    2>/dev/null || true
mv ~/.yadgar/logs                           ~/.local/share/yadgar/    2>/dev/null || true
mv ~/.yadgar/cache                          ~/.local/share/yadgar/    2>/dev/null || true
mv ~/.yadgar/archive                        ~/.local/share/yadgar/    2>/dev/null || true
mv ~/.yadgar/dlq                            ~/.local/share/yadgar/    2>/dev/null || true
mv ~/.yadgar/queue                          ~/.local/share/yadgar/    2>/dev/null || true
mv ~/.yadgar/scans                          ~/.local/share/yadgar/    2>/dev/null || true

mv ~/.yadgar/stop-hook-state.json           ~/.local/state/yadgar/    2>/dev/null || true
mv ~/.yadgar/session-ends                   ~/.local/state/yadgar/    2>/dev/null || true
mv ~/.yadgar/active-work-tracked            ~/.local/state/yadgar/    2>/dev/null || true
mv ~/.yadgar/quarantine                     ~/.local/state/yadgar/    2>/dev/null || true
mv ~/.yadgar/triggers                       ~/.local/state/yadgar/    2>/dev/null || true
mv ~/.yadgar/secret-gate-audit              ~/.local/state/yadgar/    2>/dev/null || true

rmdir ~/.yadgar 2>/dev/null || true

echo "Migration done. Now: pipx upgrade yadgar (or home-manager switch), then yadgar-setup."
