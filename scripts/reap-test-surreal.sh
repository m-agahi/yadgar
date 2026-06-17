#!/usr/bin/env bash
# Reap orphaned TEST surreal procs left by crashed pytest runs.
#
# Filters by process NAME (comm == "surreal") AND data path (/tmp/pytest) so it:
#   - NEVER matches its own wrapper shell / make / pre-commit (comm = sh/make/...),
#     which is the bug that made `pkill -9 -f 'surreal start.*/tmp/pytest'`
#     SIGKILL its own `sh -c` (the pattern appears in the wrapper's own cmdline),
#     killing `make e2e` and breaking the pre-push gate.
#   - NEVER touches the production daemon (its surrealkv data lives under /data,
#     not /tmp/pytest).
#
# Always exits 0 — reaping is best-effort cleanup, never a failure.
set -u
ps -eo pid,comm,args 2>/dev/null \
  | awk '$2 == "surreal" && /\/tmp\/pytest/ { print $1 }' \
  | xargs -r kill -9 2>/dev/null || true
exit 0
