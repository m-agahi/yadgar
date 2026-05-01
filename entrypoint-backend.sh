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

# Start embedding service
python3 -m uvicorn yadgar.embed_service:app \
  --host 0.0.0.0 \
  --port 8001 \
  --no-access-log &
EMBED_PID=$!

# Backup cron: first backup immediately, then every 6 hours, keep 7 days
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
  _do_backup
  while true; do
    sleep 21600
    _do_backup
  done
) &

wait -n "$SURREAL_PID" "$EMBED_PID"
