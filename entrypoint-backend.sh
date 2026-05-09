#!/bin/bash
# Backend container entrypoint: SurrealDB + embedding service + backup cron
set -e

cleanup() {
  kill "$SURREAL_PID" "$EMBED_PID" 2>/dev/null
  wait "$SURREAL_PID" "$EMBED_PID" 2>/dev/null
}
trap cleanup TERM INT

# Start SurrealDB
surreal start \
  --no-banner \
  --bind 0.0.0.0:8000 \
  --user "${SURREAL_USER:-root}" \
  --pass "${SURREAL_PASS:-root}" \
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
  --no-access-log &
EMBED_PID=$!

# Backup cron: first backup immediately, then at 06:10, 12:10, 18:10, 21:10 local time.
# The 21:10 last slot avoids the previous 00:10 run that woke the laptop overnight.
(
  _do_backup() {
    STAMP=$(date +%Y%m%d_%H%M%S)
    _creds="${SURREAL_USER:-root}:${SURREAL_PASS:-root}"  # gitleaks:allow
    if curl -sf \
      -u "$_creds" \
      -H "Surreal-NS: yadgar" \
      -H "Surreal-DB: main" \
      -H "Accept: text/plain" \
      -o "/data/backup_${STAMP}.surql" \
      http://127.0.0.1:8000/export; then
      echo "Backup written: /data/backup_${STAMP}.surql"
    fi
    find /data -name 'backup_*.surql' -mtime +7 -delete
  }
  _sleep_until_next_backup() {
    python3 - <<'PYEOF'
import datetime
now = datetime.datetime.now()
for h in [6, 12, 18, 21]:
    t = now.replace(hour=h, minute=10, second=0, microsecond=0)
    if t > now:
        print(int((t - now).total_seconds()))
        break
else:
    t = (now + datetime.timedelta(days=1)).replace(hour=6, minute=10, second=0, microsecond=0)
    print(int((t - now).total_seconds()))
PYEOF
  }
  _do_backup
  while true; do
    # Fall back to 1h sleep if Python helper ever errors, so the loop never silently dies.
    sleep "$(_sleep_until_next_backup)" || sleep 3600
    _do_backup
  done
) &

wait -n "$SURREAL_PID" "$EMBED_PID"
