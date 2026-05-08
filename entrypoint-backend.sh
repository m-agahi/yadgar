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
