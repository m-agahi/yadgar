#!/bin/bash
# Backend container entrypoint: SurrealDB + embedding service.
#
# NOTE: no in-container backup loop. The previous `GET /export` cron triggered
# a stack overflow in a `surrealdb-worker` thread (the export's recursive value
# serializer blows the default ~2 MiB tokio stack on this dataset), aborting the
# whole process and putting the container in a restart loop. DB snapshots are
# handled outside the container by the systemd ExecStartPre `cp -r` of the
# surrealkv data dir. If a logical (.surql) backup is needed again, run
# `surreal export` from a one-off client against a *quiesced* DB, not on a hot
# server, and only after the upstream export-recursion issue is resolved.
set -e

# Log level configuration — shared across SurrealDB and the embed service.
# YADGAR_BACKEND_LOG_LEVEL uses the SurrealDB convention (warn/info/debug/error).
# uvicorn uses "warning" instead of "warn", so we remap before passing it.
_LOG_LEVEL="${YADGAR_BACKEND_LOG_LEVEL:-warn}"
export SURREAL_LOG="${_LOG_LEVEL}"
_UVICORN_LOG_LEVEL="${_LOG_LEVEL}"
[ "$_UVICORN_LOG_LEVEL" = "warn" ] && _UVICORN_LOG_LEVEL="warning"

cleanup() {
  kill "$SURREAL_PID" "$EMBED_PID" 2>/dev/null
  wait "$SURREAL_PID" "$EMBED_PID" 2>/dev/null
}
trap cleanup TERM INT

# Worker-thread stack size. Default tokio stack (~2 MiB) overflows on deep
# queries (large transactions, long expression chains, deeply nested values),
# aborting the whole process. 32 MiB gives headroom. Overridable via env.
export SURREAL_RUNTIME_STACK_SIZE="${SURREAL_RUNTIME_STACK_SIZE:-33554432}"
export RUST_MIN_STACK="${RUST_MIN_STACK:-33554432}"

# Start SurrealDB
surreal start \
  --no-banner \
  --bind 0.0.0.0:8000 \
  --user "${SURREAL_USER:-root}" \
  --pass "${SURREAL_PASS:-root}" \
  --log "${SURREAL_LOG}" \
  surrealkv:///data/surreal_db &
SURREAL_PID=$!

# Wait for SurrealDB to be ready
until python3 - <<'PYEOF' 2>/dev/null
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1)
except Exception:
    sys.exit(1)
PYEOF
do
  sleep 0.2
done

# Bootstrap database users (idempotent — IF NOT EXISTS).
# Required env vars: YADGAR_RW_USER, YADGAR_RW_PASS, YADGAR_RO_USER, YADGAR_RO_PASS.
# If any are missing, log a warning and skip (legacy mode — only ROOT user exists).
#
# Users are defined ON ROOT (not ON DATABASE) because SurrealDB v3 only supports
# HTTP Basic auth for ON ROOT and ON NAMESPACE users. ON DATABASE users must use
# the JWT /signin flow, which yadgar's StorageEngine does not implement. The
# tradeoff: these users have full-server access rather than DB-scoped access.
# If finer-grained isolation is needed, migrate StorageEngine to JWT auth.
if [[ -n "${YADGAR_RW_USER}" && -n "${YADGAR_RW_PASS}" && -n "${YADGAR_RO_USER}" && -n "${YADGAR_RO_PASS}" ]]; then
    echo "Bootstrapping yadgar-rw and yadgar-ro users..."
    _creds="${SURREAL_USER:-root}:${SURREAL_PASS:-root}"  # gitleaks:allow
    _bootstrap_sql="DEFINE USER IF NOT EXISTS \"${YADGAR_RW_USER}\" ON ROOT PASSWORD '${YADGAR_RW_PASS}' ROLES OWNER; DEFINE USER IF NOT EXISTS \"${YADGAR_RO_USER}\" ON ROOT PASSWORD '${YADGAR_RO_PASS}' ROLES VIEWER;"
    if curl -sf -u "${_creds}" -H "Surreal-NS: yadgar" -H "Surreal-DB: main" -H "Content-Type: text/plain" -X POST --data "${_bootstrap_sql}" http://127.0.0.1:8000/sql >/dev/null; then
        echo "User bootstrap complete (yadgar-rw ROOT OWNER, yadgar-ro ROOT VIEWER)"
    else
        echo "WARNING: user bootstrap failed; backend may be running with only ROOT user" >&2
    fi
else
    echo "WARNING: YADGAR_RW_USER/PASS or YADGAR_RO_USER/PASS not set — skipping user bootstrap (legacy ROOT-only mode)" >&2
fi

# Start embedding service
python3 -m uvicorn yadgar.embed_service:app \
  --host 0.0.0.0 \
  --port 8001 \
  --no-access-log \
  --log-level "${_UVICORN_LOG_LEVEL}" &
EMBED_PID=$!

wait -n "$SURREAL_PID" "$EMBED_PID"
