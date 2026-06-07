#!/usr/bin/env bash
# yadgar-secrets-activation.sh — install-time secrets activation via 1Password CLI.
#
# Mirrors the home-manager 'yadgarSecrets' activation block from yadgar.nix.
# Runs interactively during setup so biometric/Touch ID prompt is available.
# The resolved secrets.env is read statically at runtime by launchd wrapper scripts.
#
# Called from yadgar-setup.sh after _step_bootstrap_secrets, gated on 'op' availability.
# NOT a launchd plist — runs once at install/rotate time, not at agent load time.

set -euo pipefail

SECRETS_DIR="${HOME}/.config/yadgar"
SECRETS_ENV="${SECRETS_DIR}/secrets.env"
TEMPLATE="${SECRETS_ENV}.tpl"

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

if [ ! -f "$TEMPLATE" ]; then
    echo "INFO: No secrets template at ${TEMPLATE} — skipping op inject."
    echo "      To enable: place a 1Password-annotated template at ${TEMPLATE}"
    echo "      and re-run: yadgar-setup --rotate-secrets"
    exit 0
fi

if ! command -v op &>/dev/null; then
    echo "ERROR: 1Password CLI (op) not found. Install from https://1password.com/downloads/command-line/" >&2
    exit 1
fi

echo "Injecting secrets from 1Password template: ${TEMPLATE}"
op inject -i "$TEMPLATE" -o "$SECRETS_ENV"
chmod 600 "$SECRETS_ENV"
echo "Secrets written to ${SECRETS_ENV} (mode 600)."
echo "Note: re-run this script after rotating credentials in 1Password."
