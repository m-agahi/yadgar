#!/bin/bash
# Core container entrypoint: MCP server only
set -e

# ---------------------------------------------------------------------------
# v5.6.7 PR-M: resolve log directory (YADGAR_LOG_DIR env knob)
# Default inside containers: /data/logs (bind-mounted by compose or systemd).
# Operators on Linux hosts with an external log shipper (e.g. Alloy) can set
# YADGAR_LOG_DIR=/var/log/yadgar (world-readable path) to avoid home-dir 700
# traversal issues. On macOS / bare-metal dev: leave unset → ~/.yadgar/logs.
# ---------------------------------------------------------------------------
YADGAR_LOG_DIR="${YADGAR_LOG_DIR:-/data/logs}"
export YADGAR_LOG_DIR
echo "yadgar-core: log dir = ${YADGAR_LOG_DIR}" >&2
if ! mkdir -p "${YADGAR_LOG_DIR}" && chmod 0750 "${YADGAR_LOG_DIR}" 2>/dev/null; then
    echo "WARNING: could not create ${YADGAR_LOG_DIR}; falling back to /tmp/yadgar-logs" >&2
    YADGAR_LOG_DIR="/tmp/yadgar-logs"
    export YADGAR_LOG_DIR
    mkdir -p "${YADGAR_LOG_DIR}" || true
fi

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
