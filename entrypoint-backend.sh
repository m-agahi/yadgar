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

# Fail fast if required credentials are missing.
# Use YADGAR_ALLOW_ROOT=1 in test/dev environments to bypass.
if [[ "${YADGAR_ALLOW_ROOT:-0}" != "1" ]]; then
    : "${SURREAL_USER:?SURREAL_USER is required — set via EnvironmentFile or docker -e}"
    : "${SURREAL_PASS:?SURREAL_PASS is required — set via EnvironmentFile or docker -e}"
fi

# Log level configuration — shared across SurrealDB and the embed service.
# YADGAR_BACKEND_LOG_LEVEL uses the SurrealDB convention (warn/info/debug/error).
# uvicorn uses "warning" instead of "warn", so we remap before passing it.
_LOG_LEVEL="${YADGAR_BACKEND_LOG_LEVEL:-warn}"
export SURREAL_LOG="${_LOG_LEVEL}"
_UVICORN_LOG_LEVEL="${_LOG_LEVEL}"
[ "$_UVICORN_LOG_LEVEL" = "warn" ] && _UVICORN_LOG_LEVEL="warning"

cleanup() {
  kill "$SURREAL_PID" "$EMBED_PID" "${WIKI_BACKUP_PID:-}" 2>/dev/null
  wait "$SURREAL_PID" "$EMBED_PID" 2>/dev/null
}
trap cleanup TERM INT

# Worker-thread stack size. Default tokio stack (~2 MiB) overflows on deep
# queries (large transactions, long expression chains, deeply nested values),
# aborting the whole process. 32 MiB gives headroom. Overridable via env.
export SURREAL_RUNTIME_STACK_SIZE="${SURREAL_RUNTIME_STACK_SIZE:-33554432}"
export RUST_MIN_STACK="${RUST_MIN_STACK:-33554432}"

# Start SurrealDB — bind to all interfaces so the core container can reach it
# across the docker network. Security: the docker network is internal; the
# host-side port is only published to 127.0.0.1 via -p 127.0.0.1:8000:8000.
surreal start \
  --no-banner \
  --bind 0.0.0.0:8000 \
  --user "${SURREAL_USER}" \
  --pass "${SURREAL_PASS}" \
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
    # Use Authorization header to avoid credentials leaking via /proc/<pid>/cmdline
    _b64_creds="$(printf '%s:%s' "${SURREAL_USER}" "${SURREAL_PASS}" | base64 -w0)"
    _bootstrap_sql="DEFINE USER IF NOT EXISTS \$rw_user ON ROOT PASSWORD \$rw_pass ROLES OWNER; DEFINE USER IF NOT EXISTS \$ro_user ON ROOT PASSWORD \$ro_pass ROLES VIEWER;"
    if curl -sf \
        -H "Authorization: Basic ${_b64_creds}" \
        -H "Surreal-NS: yadgar" -H "Surreal-DB: main" \
        -H "Content-Type: text/plain" \
        -X POST --data "${_bootstrap_sql}" \
        http://127.0.0.1:8000/sql >/dev/null; then
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

# §16 Wiki backup loop — every 6 hours alongside main services.
#
# NOTE: We do NOT use SurrealDB's /export endpoint — it can trigger a
# stack overflow in surrealdb-worker on large datasets (the recursive
# value serialiser blows the default tokio stack). Instead we do a
# targeted SELECT * FROM wiki_page via /sql. wiki_page is small and
# bounded so this query is safe.
#
# Authorization uses a base64-encoded Basic auth header instead of
# -u / --netrc-file so credentials do NOT appear in /proc/<pid>/cmdline.
_wiki_backup_loop() {
    while true; do
        sleep 21600  # 6 hours
        if [[ "${YADGAR_ALLOW_ROOT:-0}" == "1" ]] || \
           { [[ -n "${SURREAL_USER}" ]] && [[ -n "${SURREAL_PASS}" ]]; }; then
            _b64_creds="$(printf '%s:%s' "${SURREAL_USER:-root}" "${SURREAL_PASS:-root}" | base64 -w0)"
            _snap_file="/data/wiki_$(date +%Y%m%d_%H%M%S).jsonl"
            if curl -sf \
                -H "Authorization: Basic ${_b64_creds}" \
                -H "Surreal-NS: yadgar" -H "Surreal-DB: main" \
                -H "Content-Type: text/plain" \
                -X POST --data "SELECT * FROM wiki_page;" \
                -o "${_snap_file}" \
                http://127.0.0.1:8000/sql; then
                echo "wiki_snapshot: saved ${_snap_file}"
            else
                echo "WARNING: wiki snapshot failed" >&2
                rm -f "${_snap_file}"
            fi
            # Retention: prune snapshots older than 14 days
            find /data -name 'wiki_*.jsonl' -mtime +14 -delete
        fi
    done
}
_wiki_backup_loop &
WIKI_BACKUP_PID=$!

wait -n "$SURREAL_PID" "$EMBED_PID"
