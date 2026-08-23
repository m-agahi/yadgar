#!/usr/bin/env bash
# Bootstrap yadgar credential secrets env file.
#
# Usage:
#   bash scripts/install/bootstrap_secrets.sh
#   bash scripts/install/bootstrap_secrets.sh --system    # write to /etc/yadgar/secrets.env via sudo
#
# Environment variables:
#   YADGAR_TEST_DRYRUN=1            Write to YADGAR_TEST_SECRETS_PATH (tests only; no prompts)
#   YADGAR_TEST_SECRETS_PATH        Target path when YADGAR_TEST_DRYRUN=1 is set
#   INSTALL_NONINTERACTIVE=1        Skip prompts; fail-closed if creds missing and no file exists
#
# Output file (default): ~/.config/yadgar/secrets.env  (mode 600)
# With --system flag:    /etc/yadgar/secrets.env (mode 600 via sudo)

set -euo pipefail

SYSTEM_INSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --system) SYSTEM_INSTALL=1; shift ;;
        --help|-h)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Test dryrun path ──────────────────────────────────────────────────────────

if [[ "${YADGAR_TEST_DRYRUN:-0}" == "1" ]]; then
    SECRETS_ENV_FILE="${YADGAR_TEST_SECRETS_PATH:-/tmp/yadgar-test-secrets.env}"
    mkdir -p "$(dirname "${SECRETS_ENV_FILE}")"

    if [[ -f "${SECRETS_ENV_FILE}" ]]; then
        # Check if all required keys are present
        all_present=1
        for key in SURREAL_USER SURREAL_PASS YADGAR_RW_USER YADGAR_RW_PASS YADGAR_RO_USER YADGAR_RO_PASS; do
            if ! grep -q "^${key}=" "${SECRETS_ENV_FILE}" 2>/dev/null; then
                all_present=0
                break
            fi
        done
        if [[ "${all_present}" == "1" ]]; then
            echo "==> Secrets file already exists with all required keys — skipping"
            echo "    ${SECRETS_ENV_FILE}"
            exit 0
        fi
    fi

    # Generate dummy credentials for test dryrun.
    # YADGAR_RW_USER/PASS is the canonical credential name (post-rename).
    # Legacy DB_USER/PASS aliases are NOT written on new installs —
    # runtime consumers read RW first and fall back to the legacy name for old hosts.
    _gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
    _gen32() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
    umask 177
    cat > "${SECRETS_ENV_FILE}" <<SECRETS
# Yadgar secrets — chmod 600 — never commit or log
SURREAL_USER=root
SURREAL_PASS=$(_gen)
YADGAR_RW_USER=yadgar-rw
YADGAR_RW_PASS=$(_gen)
YADGAR_RO_USER=yadgar-ro
YADGAR_RO_PASS=$(_gen)
YADGAR_MCP_AUTH_TOKEN=$(_gen32)
SECRETS
    umask 022
    chmod 600 "${SECRETS_ENV_FILE}"
    echo "==> Secrets written (test dryrun) → ${SECRETS_ENV_FILE}"
    exit 0
fi

# ── Determine target secrets file path ───────────────────────────────────────

if [[ "${SYSTEM_INSTALL}" == "1" ]]; then
    SECRETS_ENV_FILE="/etc/yadgar/secrets.env"
else
    SECRETS_ENV_FILE="${HOME}/.config/yadgar/secrets.env"
fi

mkdir -p "$(dirname "${SECRETS_ENV_FILE}")" 2>/dev/null || {
    if sudo mkdir -p "$(dirname "${SECRETS_ENV_FILE}")" 2>/dev/null; then
        : # sudo mkdir succeeded
    else
        echo "Cannot create $(dirname "${SECRETS_ENV_FILE}") — check permissions on ${HOME}/.config/yadgar/" >&2
        exit 1
    fi
}
chmod 700 "$(dirname "${SECRETS_ENV_FILE}")" 2>/dev/null || true

# ── Idempotency check ─────────────────────────────────────────────────────────

REQUIRED_KEYS=(SURREAL_USER SURREAL_PASS YADGAR_RW_USER YADGAR_RW_PASS YADGAR_RO_USER YADGAR_RO_PASS YADGAR_MCP_AUTH_TOKEN)

if [[ -f "${SECRETS_ENV_FILE}" ]]; then
    all_present=1
    for key in "${REQUIRED_KEYS[@]}"; do
        if ! grep -q "^${key}=" "${SECRETS_ENV_FILE}" 2>/dev/null; then
            all_present=0
            break
        fi
    done
    if [[ "${all_present}" == "1" ]]; then
        echo "==> Secrets file already exists with all required keys — skipping"
        echo "    ${SECRETS_ENV_FILE}"
        exit 0
    fi
fi

# ── Non-interactive mode ──────────────────────────────────────────────────────

_gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
_gen32() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

# Backstop (task 64, car C3 / bug-bag-2 train 2026-08-23): if stdin is not a
# TTY AND INSTALL_NONINTERACTIVE is not set, the interactive `read` below
# would block forever or, with `set -e`, terminate with an opaque error at
# the FIRST credential prompt. Detect that scenario explicitly and tell the
# caller what to do — yadgar-setup was dying silently at step 3/12 of
# pipeline runs because of exactly this.
if [[ "${INSTALL_NONINTERACTIVE:-0}" != "1" ]] && [[ ! -t 0 ]]; then
    echo "ERROR: bootstrap_secrets.sh requires a TTY (or INSTALL_NONINTERACTIVE=1)." >&2
    echo "  stdin is not a terminal and INSTALL_NONINTERACTIVE is unset." >&2
    echo "  Re-run with INSTALL_NONINTERACTIVE=1 (e.g. yadgar-setup --noninteractive" >&2
    echo "  sets it for you) or attach a TTY for the credential prompts." >&2
    exit 64  # EX_USAGE
fi

if [[ "${INSTALL_NONINTERACTIVE:-0}" == "1" ]]; then
    echo "==> Non-interactive mode: generating credentials automatically..."
    ROOT_USER="root"
    ROOT_PASS="$(_gen)"
    RW_USER="yadgar-rw"
    RW_PASS="$(_gen)"
    RO_USER="yadgar-ro"
    RO_PASS="$(_gen)"
else

# ── Interactive mode ──────────────────────────────────────────────────────────

    echo "==> Yadgar credential setup"
    echo "    Prompting for database credentials."
    echo "    Press Enter to accept the default (shown in brackets)."
    echo ""

    read -r -p "    SurrealDB ROOT username [root]: " ROOT_USER
    ROOT_USER="${ROOT_USER:-root}"

    while true; do
        read -r -s -p "    SurrealDB ROOT password [auto-generate]: " ROOT_PASS
        echo ""
        if [[ -z "${ROOT_PASS}" ]]; then
            ROOT_PASS="$(_gen)"
            echo "    Generated ROOT password."
            break
        else
            read -r -s -p "    Confirm ROOT password: " ROOT_PASS_CONFIRM
            echo ""
            if [[ "${ROOT_PASS}" == "${ROOT_PASS_CONFIRM}" ]]; then
                break
            fi
            echo "    Passwords do not match. Try again."
        fi
    done

    read -r -p "    RW DB username [yadgar-rw]: " RW_USER
    RW_USER="${RW_USER:-yadgar-rw}"

    while true; do
        read -r -s -p "    RW DB password [auto-generate]: " RW_PASS
        echo ""
        if [[ -z "${RW_PASS}" ]]; then
            RW_PASS="$(_gen)"
            echo "    Generated RW password."
            break
        else
            read -r -s -p "    Confirm RW password: " RW_PASS_CONFIRM
            echo ""
            if [[ "${RW_PASS}" == "${RW_PASS_CONFIRM}" ]]; then
                break
            fi
            echo "    Passwords do not match. Try again."
        fi
    done

    read -r -p "    RO DB username [yadgar-ro]: " RO_USER
    RO_USER="${RO_USER:-yadgar-ro}"

    while true; do
        read -r -s -p "    RO DB password [auto-generate]: " RO_PASS
        echo ""
        if [[ -z "${RO_PASS}" ]]; then
            RO_PASS="$(_gen)"
            echo "    Generated RO password."
            break
        else
            read -r -s -p "    Confirm RO password: " RO_PASS_CONFIRM
            echo ""
            if [[ "${RO_PASS}" == "${RO_PASS_CONFIRM}" ]]; then
                break
            fi
            echo "    Passwords do not match. Try again."
        fi
    done

fi  # end interactive/non-interactive

# MCP bearer token — always auto-generated (no user prompt; not a DB credential)
MCP_TOKEN="$(_gen32)"

# ── Write secrets file ────────────────────────────────────────────────────────
# YADGAR_RW_USER/PASS is the canonical credential name (post v5.46.17 rename).
# Legacy DB_USER/PASS aliases are NOT written on new installs.
# Runtime consumers (daemon.py, vacuum) read YADGAR_RW_USER first and fall back
# to the legacy alias name for existing hosts that haven't re-bootstrapped.

umask 177
cat > "${SECRETS_ENV_FILE}" <<SECRETS
# Yadgar secrets — chmod 600 — never commit or log
SURREAL_USER=${ROOT_USER}
SURREAL_PASS=${ROOT_PASS}
YADGAR_RW_USER=${RW_USER}
YADGAR_RW_PASS=${RW_PASS}
YADGAR_RO_USER=${RO_USER}
YADGAR_RO_PASS=${RO_PASS}
YADGAR_MCP_AUTH_TOKEN=${MCP_TOKEN}
SECRETS
umask 022
chmod 600 "${SECRETS_ENV_FILE}"
echo "==> Secrets written → ${SECRETS_ENV_FILE} (mode 600)"
