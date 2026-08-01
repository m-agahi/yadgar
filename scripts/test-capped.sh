#!/usr/bin/env bash
# test-capped.sh — run a test command CPU/mem-capped and timeout-bounded.
#
# Two guarantees so an unattended test run can never roast the machine or
# OOM-kill the production yadgar daemon (both have happened):
#   1. timeout --signal=KILL: the whole run is killed after TEST_TIMEOUT
#      (default 90min). A hang dies; it cannot run for hours.
#   2. systemd-run --scope CPUQuota/MemoryMax: even while running, the tests
#      are capped to TEST_CPU_QUOTA cores and TEST_MEM_MAX RAM, so a runaway
#      leaves headroom for the production daemon.
#
# Usage:  scripts/test-capped.sh uv run --extra test pytest yadgar/tests/ -q
# Env:    TEST_TIMEOUT=5400  TEST_CPU_QUOTA=300%  TEST_MEM_MAX=20G
#         TEST_ALLOW_UNCAPPED=1  — explicit opt-out for hosts with no user-scope
#         systemd (CI containers). Drops guarantee 2 ONLY; the timeout stands.
set -euo pipefail

TEST_TIMEOUT="${TEST_TIMEOUT:-5400}"      # 90 min hard ceiling
TEST_CPU_QUOTA="${TEST_CPU_QUOTA:-300%}"  # ~3 cores
TEST_MEM_MAX="${TEST_MEM_MAX:-20G}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <command...>" >&2; exit 2
fi

# systemd-run --user --scope requires a user manager. FAIL CLOSED when it is
# unavailable: silently degrading to timeout-only drops guarantee #2 entirely
# (no CPUQuota, no MemoryMax) while still looking like a capped run — which is
# exactly the shape that let an unattended sweep saturate the box.
# Hosts that genuinely have no user-scope systemd (CI containers, minimal
# images) opt out EXPLICITLY with TEST_ALLOW_UNCAPPED=1; the timeout ceiling
# still applies there, so an uncapped run can never become an unbounded one.
if systemd-run --user --scope --quiet true 2>/dev/null; then
  exec timeout --signal=KILL "$TEST_TIMEOUT" \
    systemd-run --user --scope --quiet \
      -p "CPUQuota=${TEST_CPU_QUOTA}" -p "MemoryMax=${TEST_MEM_MAX}" \
      -- "$@"
elif [ "${TEST_ALLOW_UNCAPPED:-0}" = "1" ]; then
  echo "test-capped: systemd-run --user --scope unavailable; TEST_ALLOW_UNCAPPED=1 set," \
       "running WITHOUT CPUQuota/MemoryMax (timeout ${TEST_TIMEOUT}s still enforced)" >&2
  exec timeout --signal=KILL "$TEST_TIMEOUT" "$@"
else
  cat >&2 <<EOF
test-capped: REFUSING TO RUN — systemd-run --user --scope unavailable.
  Without it the run gets no CPUQuota=${TEST_CPU_QUOTA} and no MemoryMax=${TEST_MEM_MAX},
  so a runaway test can peg every core or OOM the production yadgar daemon.
  Missing capability: a systemd USER manager for uid $(id -u)
  (check: systemctl --user status; on a headless/SSH host you likely need
   'loginctl enable-linger $(id -un)', and DBUS_SESSION_BUS_ADDRESS /
   XDG_RUNTIME_DIR must be set).
  On a host that genuinely has no user-scope systemd (CI container, minimal
  image), opt out explicitly:  TEST_ALLOW_UNCAPPED=1 $0 $*
EOF
  exit 3
fi
