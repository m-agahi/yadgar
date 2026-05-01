#!/bin/bash
# Core container entrypoint: MCP server only
set -e

# Wait for backend embed service to be ready (max 120s)
echo "Waiting for backend embed service..."
_deadline=$(( $(date +%s) + 120 ))
until python3 - <<'PYEOF' 2>/dev/null
import urllib.request, sys, os
url = os.environ.get("YADGAR_EMBED_URL", "http://yadgar-backend:8001")
try:
    urllib.request.urlopen(url + "/health", timeout=2)
except Exception:
    sys.exit(1)
PYEOF
do
  if [ "$(date +%s)" -ge "$_deadline" ]; then
    echo "Backend not ready after 120s — starting in degraded mode"
    break
  fi
  sleep 1
done

exec yadgar --transport streamable-http
