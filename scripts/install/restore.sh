#!/usr/bin/env bash
# Restore yadgar database from a .surql backup (advanced/optional flow).
#
# Usage:
#   YADGAR_RESTORE_DB=/path/to/backup.surql make restore
#   YADGAR_RESTORE_DB=/path/to/backup.surql YADGAR_RESTORE_ARCHIVE=/path/to/archive make restore
#
# Environment variables (required):
#   YADGAR_RESTORE_DB       Path to the .surql export file (required)
#
# Environment variables (optional):
#   YADGAR_RESTORE_ARCHIVE  Archive directory (memories/ + wiki/) to restore alongside
#   YADGAR_DIR              Yadgar data dir (default: ~/.yadgar)
#   YADGAR_RESTORE_DRY_RUN  Set to 1 for a no-op dry run (test/preview mode)
#
# What this does:
#   1. Stops yadgar.target (systemctl --user stop)
#   2. Optionally restores archive directory
#   3. Wipes surreal_db data dir
#   4. Starts yadgar-backend.service temporarily
#   5. Imports .surql via surrealdb import command
#   6. Stops yadgar-backend.service
#   7. Reloads + starts yadgar.target

set -euo pipefail

# ── Validate required env ─────────────────────────────────────────────────────

if [[ -z "${YADGAR_RESTORE_DB:-}" ]]; then
    echo "ERROR: YADGAR_RESTORE_DB is required." >&2
    echo "  Usage: YADGAR_RESTORE_DB=/path/to/backup.surql make restore" >&2
    echo "  Optionally also set: YADGAR_RESTORE_ARCHIVE=/path/to/archive" >&2
    exit 1
fi

DB_FILE="${YADGAR_RESTORE_DB}"
ARCHIVE_DIR="${YADGAR_RESTORE_ARCHIVE:-}"
YADGAR_DIR="${YADGAR_DIR:-${HOME}/.yadgar}"
DRY_RUN="${YADGAR_RESTORE_DRY_RUN:-0}"

# ── Detect container runtime ──────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "${SCRIPT_DIR}/detect_runtime.sh" ]]; then
    RUNTIME="$(bash "${SCRIPT_DIR}/detect_runtime.sh")" || {
        echo "ERROR: Could not detect container runtime." >&2
        exit 1
    }
elif command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
    RUNTIME="podman"
elif command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    RUNTIME="docker"
else
    echo "ERROR: No container runtime found. Install podman or docker." >&2
    exit 1
fi

# ── Load config from secrets.env ─────────────────────────────────────────────

SECRETS_ENV_FILE="${YADGAR_SECRETS_ENV_FILE:-${YADGAR_DIR}/secrets.env}"
if [[ ! -f "${SECRETS_ENV_FILE}" ]]; then
    SECRETS_ENV_FILE="/etc/yadgar/secrets.env"
fi
if [[ -f "${SECRETS_ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a; . "${SECRETS_ENV_FILE}"; set +a
fi

BACKEND_IMAGE="${YADGAR_BACKEND_IMAGE:-openfantasy/yadgar-backend:latest}"
BACKEND_CONTAINER="yadgar-backend-restore-$$"
NETWORK="yadgar-net"
ROOT_USER="${SURREAL_USER:-root}"
ROOT_PASS="${SURREAL_PASS:?SURREAL_PASS is required — set it in ${SECRETS_ENV_FILE}}"

# ── Helpers ───────────────────────────────────────────────────────────────────

info()  { echo "  $*"; }
ok()    { echo "==> $*"; }
fail()  { echo "ERROR: $*" >&2; exit 1; }

# ── Dry run check ─────────────────────────────────────────────────────────────

if [[ "${DRY_RUN}" == "1" ]]; then
    echo "==> Dry run — would restore:"
    echo "    DB:      ${DB_FILE}"
    [[ -n "${ARCHIVE_DIR}" ]] && echo "    Archive: ${ARCHIVE_DIR}"
    echo "    Runtime: ${RUNTIME}"
    echo "    Data:    ${YADGAR_DIR}"
    exit 0
fi

# ── Validate inputs ───────────────────────────────────────────────────────────

[[ -f "${DB_FILE}" ]] || fail ".surql file not found: ${DB_FILE}"
[[ -n "${ARCHIVE_DIR}" && ! -d "${ARCHIVE_DIR}" ]] && fail "Archive directory not found: ${ARCHIVE_DIR}"

# ── Step 1: stop yadgar.target ────────────────────────────────────────────────

ok "Stopping yadgar.target..."
systemctl --user stop yadgar.target 2>/dev/null || true
info "Services stopped."

# ── Step 2: restore archive (if provided) ────────────────────────────────────

if [[ -n "${ARCHIVE_DIR}" ]]; then
    ok "Restoring archive..."
    mkdir -p "${YADGAR_DIR}/archive"
    cp -r "${ARCHIVE_DIR}/." "${YADGAR_DIR}/archive/"
    info "Archive restored → ${YADGAR_DIR}/archive/"
fi

# ── Step 3: wipe SurrealDB data ───────────────────────────────────────────────

ok "Wiping SurrealDB data..."
rm -rf "${YADGAR_DIR}/surreal_db"
info "Data wiped."

# ── Step 4: start a temporary backend container ───────────────────────────────

ok "Starting temporary backend for import..."
${RUNTIME} network inspect "${NETWORK}" &>/dev/null || \
    ${RUNTIME} network create --driver bridge "${NETWORK}"

${RUNTIME} rm -f "${BACKEND_CONTAINER}" &>/dev/null || true
${RUNTIME} run -d \
    --name "${BACKEND_CONTAINER}" \
    --network "${NETWORK}" \
    --user root \
    -v "${YADGAR_DIR}:/data" \
    -p "127.0.0.1:8000:8000" \
    -e "SURREAL_USER=${ROOT_USER}" \
    -e "SURREAL_PASS=${ROOT_PASS}" \
    "${BACKEND_IMAGE}"

# Wait for SurrealDB
info "Waiting for SurrealDB (up to 120s)..."
deadline=$(( $(date +%s) + 120 ))
while [[ $(date +%s) -lt $deadline ]]; do
    if curl -sf http://127.0.0.1:8000/health &>/dev/null; then
        break
    fi
    sleep 3
done
curl -sf http://127.0.0.1:8000/health &>/dev/null || fail "SurrealDB did not start in 120s"
info "SurrealDB ready."

# ── Step 5: import .surql ─────────────────────────────────────────────────────

ok "Importing $(basename "${DB_FILE}")..."
${RUNTIME} run --rm \
    --network "${NETWORK}" \
    -v "${DB_FILE}:/restore.surql:ro" \
    "surrealdb/surrealdb:v3.0.5" \
    import \
    --endpoint "http://${BACKEND_CONTAINER}:8000" \
    --username "${ROOT_USER}" \
    --password "${ROOT_PASS}" \
    --namespace yadgar \
    --database main \
    /restore.surql
info "Import complete."

# ── Step 6: stop temporary backend ───────────────────────────────────────────

ok "Stopping temporary backend..."
${RUNTIME} stop "${BACKEND_CONTAINER}" || true
${RUNTIME} rm -f "${BACKEND_CONTAINER}" || true

# ── Step 7: reload + start yadgar.target ─────────────────────────────────────

ok "Reloading systemd and starting yadgar.target..."
systemctl --user daemon-reload
systemctl --user start yadgar.target

echo ""
echo "==> Restore complete."
echo "    Check service status: systemctl --user status yadgar.service"
