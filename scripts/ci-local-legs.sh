#!/usr/bin/env bash
# scripts/ci-local-legs.sh — `make ci-local`'s leg runner (Car F10, PR #40 remediation).
#
# WHY THIS EXISTS
# ----------------
# `make ci-local` used to run ONE pytest invocation over the union of all 8
# CI test dirs. `.github/workflows/ci-pr.yml` never does that — it runs the
# same union as FOUR SEPARATE processes (test-fast/test-shared/test-backend/
# test-core, one per container). The lumped local invocation accumulates
# memory no single CI job ever does: measured OOM-killed at 20G/MemoryMax
# 1h32m in even with TEST_TIMEOUT=18000. This script restores the process
# boundary CI actually has: one `run_leg` call per CI subsystem job,
# sequential, so peak per-process RSS stays bounded to one job's tests.
#
# Every leg runs to completion even if an earlier leg failed — one
# `make ci-local` gives the full picture, not a first-failure abort — and the
# script exits non-zero iff ANY leg failed (see the summary loop at the
# bottom). TEST_TIMEOUT (scripts/test-capped.sh's own env var) is inherited
# unmodified into each leg's OWN test-capped.sh call, so it applies PER LEG.
#
# Leg definitions live in the Makefile (CI_LOCAL_DIRS_<leg>), not here — this
# script trusts what it's handed via env and does not know about ci-pr.yml.
# scripts/check_ci_local_parity.py is the guard that keeps the Makefile's
# leg definitions AND this script's `run_leg` calls honest against ci-pr.yml
# — including that a leg here isn't silently collapsed back into one lumped
# invocation, or left uncovering a CI job ci-pr.yml added later.
#
# Required env (all set by the Makefile's `ci-local:` recipe):
#   CI_LOCAL_MARKER          pytest -m expression, shared by every leg
#   TEST_LOCK                flock path serializing against test/test-ci/e2e
#   CI_LOCAL_DIRS_override   non-empty ONLY for `make ci-local DIRS=...`
#   CI_LOCAL_DIRS_fast/shared/backend/core   one leg's dirs each (full run)
# Ambient env inherited straight through to every leg's test-capped.sh call,
# unmodified: TEST_TIMEOUT, TEST_CPU_QUOTA, TEST_MEM_MAX, PYTEST_ARGS.
set -euo pipefail

: "${CI_LOCAL_MARKER:?CI_LOCAL_MARKER not set — invoke via 'make ci-local', not directly}"
: "${TEST_LOCK:?TEST_LOCK not set — invoke via 'make ci-local', not directly}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
declare -a RESULTS=()

# run_leg <name> <dir>...  — one leg == one pytest process. Appends
# "<name>: PASS"/"<name>: FAIL" to RESULTS and always returns 0: a leg
# failure must not abort the loop that runs the remaining legs (aggregation
# happens once, at the very end, in main()).
run_leg() {
  local name="$1"
  shift
  echo ""
  echo "==> [$name leg] $*"
  if TEST_TIMEOUT="${TEST_TIMEOUT:-5400}" "$script_dir/test-capped.sh" \
      uv run --extra test --extra ml python -m pytest "$@" \
        -q -rs --tb=short -n 4 --dist loadgroup --reruns 2 --reruns-delay 2 \
        -m "$CI_LOCAL_MARKER" ${PYTEST_ARGS:-}; then
    RESULTS+=("$name: PASS")
  else
    RESULTS+=("$name: FAIL")
  fi
}

# Single flock held for the WHOLE run (all legs) — same mutual-exclusion
# contract the Makefile's other surreal-spawning targets (test/test-ci/e2e)
# use via the $(LOCKED) macro, just implemented without an external `flock
# <file> <command>` subprocess so run_leg's exit status isn't laundered
# through it.
mkdir -p "$(dirname "$TEST_LOCK")"
exec {ci_local_lock_fd}>>"$TEST_LOCK"
if ! flock -w 900 "$ci_local_lock_fd"; then
  echo "ci-local: could not acquire $TEST_LOCK within 900s — a concurrent" \
       "test/test-ci/e2e/ci-local run is in progress" >&2
  exit 1
fi

bash "$script_dir/reap-test-surreal.sh"
trap 'bash "$script_dir/reap-test-surreal.sh"' EXIT

if [ -n "${CI_LOCAL_DIRS_override:-}" ]; then
  # DIRS= override: mid-work subset, run as ONE leg — bypasses the four-leg
  # split entirely (see the Makefile's `ci-local` comment).
  run_leg override $CI_LOCAL_DIRS_override
else
  # Full run: CI's four subsystem legs, in the same order ci-pr.yml stages
  # them (test-core `needs: [test-fast, test-shared, test-backend]` — see
  # ci-pr.yml's own wave-staggering comments for why fast goes first).
  run_leg fast    $CI_LOCAL_DIRS_fast
  run_leg shared  $CI_LOCAL_DIRS_shared
  run_leg backend $CI_LOCAL_DIRS_backend
  run_leg core    $CI_LOCAL_DIRS_core
fi

echo ""
echo "==> ci-local per-leg summary:"
overall=0
for r in "${RESULTS[@]}"; do
  echo "  $r"
  case "$r" in *FAIL) overall=1 ;; esac
done
exit "$overall"
