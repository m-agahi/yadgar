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
set -euo pipefail

TEST_TIMEOUT="${TEST_TIMEOUT:-5400}"      # 90 min hard ceiling
TEST_CPU_QUOTA="${TEST_CPU_QUOTA:-300%}"  # ~3 cores
TEST_MEM_MAX="${TEST_MEM_MAX:-20G}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <command...>" >&2; exit 2
fi

# systemd-run --user --scope requires a user manager; fall back to plain
# timeout (still bounded) if it is unavailable.
if systemd-run --user --scope --quiet true 2>/dev/null; then
  exec timeout --signal=KILL "$TEST_TIMEOUT" \
    systemd-run --user --scope --quiet \
      -p "CPUQuota=${TEST_CPU_QUOTA}" -p "MemoryMax=${TEST_MEM_MAX}" \
      -- "$@"
else
  echo "test-capped: systemd-run --scope unavailable, running timeout-only" >&2
  exec timeout --signal=KILL "$TEST_TIMEOUT" "$@"
fi
