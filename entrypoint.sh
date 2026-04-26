#!/bin/bash
set -e

cleanup() {
  kill "$SURREAL_PID" 2>/dev/null
  wait "$SURREAL_PID" 2>/dev/null
}
trap cleanup TERM INT

surreal start \
  --no-banner \
  --bind 127.0.0.1:8000 \
  --user root \
  --pass root \
  surrealkv:///data/surreal_db &
SURREAL_PID=$!

until python3 -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
  sleep 0.2
done

exec yadgar --transport streamable-http
