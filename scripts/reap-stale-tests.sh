#!/usr/bin/env bash
# reap-stale-tests.sh — safety net for unattended test runs.
#
# Kills test processes that have run longer than MAX_AGE_SEC (default 90min) —
# a hung/deadlocked pytest run can otherwise peg every core for hours (one ran
# 9.7h overnight). Intended to be fired every ~10min by reap-stale-tests.timer.
#
# NEVER touches the production yadgar daemon: it only matches test-suite
# patterns (pytest under yadgar/tests, uv test runs, and SurrealDB instances
# whose data dir is under /tmp/pytest). The production surreal binds
# surrealkv:///data/surreal_db and is explicitly skipped.
set -uo pipefail

MAX_AGE_SEC="${MAX_AGE_SEC:-5400}"   # 90 minutes
killed=0

_reap() {  # $1 = pgrep pattern
  local pid et
  for pid in $(pgrep -f "$1" 2>/dev/null || true); do
    # never kill the production surreal (binds /data/surreal_db)
    if grep -qa '/data/surreal_db' "/proc/$pid/cmdline" 2>/dev/null; then continue; fi
    et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ') || continue
    [ -n "$et" ] || continue
    if [ "$et" -gt "$MAX_AGE_SEC" ]; then
      kill -9 "$pid" 2>/dev/null && killed=$((killed + 1))
    fi
  done
}

_reap 'pytest yadgar/tests'
_reap 'uv run --extra test pytest'
_reap 'surreal start.*/tmp/pytest'

if [ "$killed" -gt 0 ]; then
  command -v logger >/dev/null 2>&1 && logger -t reap-stale-tests "killed $killed stale test proc(s) (>${MAX_AGE_SEC}s)"
fi
echo "reap-stale-tests: killed $killed stale proc(s)"
