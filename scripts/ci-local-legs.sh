#!/usr/bin/env bash
# scripts/ci-local-legs.sh — `make ci-local`'s leg runner (Car F10, PR #40 remediation;
# concurrency added Car G3).
#
# WHY THIS EXISTS
# ----------------
# `make ci-local` used to run ONE pytest invocation over the union of all the
# CI test dirs. `.github/workflows/ci-pr.yml` never does that — it runs the
# same union as SEVERAL SEPARATE processes (one per `tests` matrix group, each
# in its own container). The lumped local invocation accumulates
# memory no single CI job ever does: measured OOM-killed at 20G/MemoryMax
# 1h32m in even with TEST_TIMEOUT=18000. This script restores the process
# boundary CI actually has: one `run_leg` call per CI subsystem job.
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
# invocation, or left uncovering a CI job ci-pr.yml added later. That guard
# discovers legs by grepping literal `run_leg <name> ...` call lines, so the
# CONCURRENCY added below changes what `run_leg` does internally but keeps
# every call site textually unchanged — the parity check needs no update.
#
# CAR G3 — BOUNDED CONCURRENCY
# ----------------------------
# F10's sequential fix bounded peak memory but left the box ~85% idle: legs
# are separate PROCESSES with no data dependency between them, so they can
# run concurrently. `CI_LOCAL_PARALLEL` (default 2, override with
# `PARALLEL=N make ci-local`, clamped to the number of legs actually running)
# bounds how many run at once. `run_leg` now backgrounds its leg and the
# caller throttles with a `jobs -rp` / `wait -n` queue instead of waiting
# inline, so `CI_LOCAL_PARALLEL=1` reproduces the exact old sequential
# behaviour.
#
# MEMORY ARITHMETIC — the actual point of F10's fix, preserved
# --------------------------------------------------------------
# F10 bounded the WORST CASE to one leg's `TEST_MEM_MAX` ceiling (ambient or
# test-capped.sh's own 20G default) — never the sum of all legs at once.
# Running legs concurrently under an UNCHANGED per-leg ceiling would let the
# worst case become ceiling × parallelism, exactly what F10 existed to avoid.
# So each leg's systemd MemoryMax is `TEST_MEM_MAX / CI_LOCAL_PARALLEL`
# (floored at 4G — see `_leg_mem_max`), which keeps the WORST-CASE total
# ceiling at the same value F10 already had (ambient TEST_MEM_MAX, default
# 20G) no matter how many legs run at once. Typical-case usage stays far
# below that ceiling either way (measured: see MIGRATION_NOTES.md / the car's
# report for the concurrent shared+backend measurement) — the division is
# what keeps the CEILING honest, not what the tests are expected to use.
#
# ATTRIBUTION — per-leg logs, not interleaved stdout
# -----------------------------------------------------
# Concurrent legs writing pytest's own multi-line, non-line-buffered output
# to one shared stdout would interleave into noise. Each leg's full output
# goes to its own log under `$(dirname "$TEST_LOCK")/ci-local-logs/<name>.log`
# instead; the console only gets a queued line, and the final per-leg summary
# names each leg's PASS/FAIL plus its log path.
#
# Required env (all set by the Makefile's `ci-local:` recipe):
#   CI_LOCAL_MARKER          pytest -m expression, shared by every leg
#   TEST_LOCK                flock path serializing against test/test-ci/e2e
#   CI_LOCAL_DIRS_override   non-empty ONLY for `make ci-local DIRS=...`
#   CI_LOCAL_DIRS_<leg>      one leg's dirs each (full run). The authoritative
#                            leg list comes from ci-pr.yml's `tests` matrix:
#                            `python scripts/ci_group_manifest.py --legs`.
#   CI_LOCAL_PARALLEL        max concurrent legs (Makefile default 2)
# Ambient env inherited straight through to every leg's test-capped.sh call,
# unmodified except TEST_MEM_MAX (divided, see above): TEST_TIMEOUT,
# TEST_CPU_QUOTA, PYTEST_ARGS.
set -euo pipefail

: "${CI_LOCAL_MARKER:?CI_LOCAL_MARKER not set — invoke via 'make ci-local', not directly}"
: "${TEST_LOCK:?TEST_LOCK not set — invoke via 'make ci-local', not directly}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_LOCAL_PARALLEL="${CI_LOCAL_PARALLEL:-2}"
if ! [[ "$CI_LOCAL_PARALLEL" =~ ^[0-9]+$ ]] || [ "$CI_LOCAL_PARALLEL" -lt 1 ]; then
  echo "ci-local-legs: CI_LOCAL_PARALLEL must be a positive integer, got '${CI_LOCAL_PARALLEL}'" >&2
  exit 2
fi

# Clamp to the number of legs actually about to run, so the memory division
# below reflects REAL concurrency, not a requested ceiling nobody hits (the
# DIRS= override path always runs exactly one leg).
if [ -n "${CI_LOCAL_DIRS_override:-}" ]; then
  _num_legs_this_run=1
else
  # Keep in step with the run_leg calls at the bottom of this file. This number
  # feeds _leg_mem_max's division, so an understated value would hand each leg a
  # LARGER share than the concurrency actually warrants — i.e. quietly raise the
  # worst-case total ceiling F10 exists to bound. scripts/check_ci_local_parity.py
  # pins the run_leg SET against ci-pr.yml; this count is the one number it does
  # not check, so it is verified against this file's own call sites HERE —
  # before any leg starts, since the division has already been applied by then.
  _num_legs_this_run=6
  # Excludes `run_leg override` by NAME, not by first letter — a future leg
  # whose name merely begins with "o" must not be silently uncounted.
  _declared_legs=$(grep -E '^[[:space:]]*run_leg[[:space:]]+' "${BASH_SOURCE[0]}" \
    | grep -cvE '^[[:space:]]*run_leg[[:space:]]+override([[:space:]]|$)' || true)
  if [ "$_declared_legs" -ne "$_num_legs_this_run" ]; then
    echo "ci-local-legs: _num_legs_this_run=${_num_legs_this_run} but this file makes" \
         "${_declared_legs} non-override run_leg calls. That number divides TEST_MEM_MAX" \
         "across concurrent legs — a stale value silently raises the worst-case memory" \
         "ceiling F10 exists to bound. Fix _num_legs_this_run." >&2
    exit 2
  fi
fi
if [ "$CI_LOCAL_PARALLEL" -gt "$_num_legs_this_run" ]; then
  CI_LOCAL_PARALLEL="$_num_legs_this_run"
fi

log_dir="$(dirname "$TEST_LOCK")/ci-local-logs"
mkdir -p "$log_dir"
declare -a LEG_NAMES=()

# Divide the ambient (or test-capped.sh's default) memory ceiling across the
# concurrent legs so the WORST-CASE total never exceeds what a single
# sequential leg was already bounded to. Only handles the plain "<N>G" shape
# every TEST_MEM_MAX in this repo uses (Makefile: 8G/12G/16G/20G) — anything
# else is left alone rather than guessed at.
_leg_mem_max() {
  local ambient="${TEST_MEM_MAX:-20G}"
  local num="${ambient%G}"
  if [ "$num" = "$ambient" ] || ! [[ "$num" =~ ^[0-9]+$ ]]; then
    echo "$ambient"
    return
  fi
  local per=$(( num / CI_LOCAL_PARALLEL ))
  if [ "$per" -lt 4 ]; then
    per=4
  fi
  echo "${per}G"
}

# run_leg <name> <dir>...  — one leg == one pytest process, launched in the
# BACKGROUND and throttled to CI_LOCAL_PARALLEL concurrent legs. Each leg's
# full output goes to its own log file; PASS/FAIL is written to a status file
# rather than returned as this function's exit code (which would abort the
# `set -e` script once the leg itself no longer gates on an `if`).
run_leg() {
  local name="$1"
  shift
  local log="$log_dir/$name.log"
  local status_file="$log_dir/$name.status"
  rm -f "$status_file"
  LEG_NAMES+=("$name")
  local leg_mem
  leg_mem="$(_leg_mem_max)"
  echo "==> [$name leg] queued (mem cap ${leg_mem}, log: $log): $*"
  (
    # --extra sql is NOT optional decoration (task 380): the yadgar-ci image
    # bakes `--extra test --extra ml --extra sql` (Dockerfile.ci), so a CI group
    # RUNS the 15 test modules that open with
    # `pytest.importorskip("sqlalchemy")` while this leg — which exists to
    # reproduce that group locally — silently SKIPPED every one of them. Nothing
    # reported the difference: `sqlalchemy not installed (sql extra)` is a
    # sanctioned reason in yadgar/tests/skip_inventory.json, so the skip gate
    # stays green either way. Local/CI parity is the whole point of this runner;
    # a leg that installs a smaller dependency set than the group it mirrors is
    # not mirroring it. (`make test` deliberately keeps the smaller set —
    # asyncmy is a compiled driver and that is the everyday target.)
    #
    # --extra analytics (task 390) is the same defect with a second dependency:
    # all 26 tests in yadgar/tests/core/test_export_duckdb.py open with
    # `pytest.importorskip("duckdb")`, and NO extra in pyproject.toml provided
    # duckdb at all, so unlike the sqlalchemy set they had never executed
    # anywhere — not here, not in CI, not on a developer's machine. The extra is
    # restored; this leg installs it. duckdb ships prebuilt wheels, so the
    # compiled-driver argument that keeps `sql` out of `make test` does not apply.
    #
    # The extras receipt (task 392) leads the log, exactly as it does in the CI
    # jobs: it MEASURES, inside the same `uv run` environment this leg's pytest
    # uses, which optional dependencies are importable. `check_skip_inventory.py`
    # reads it back out of this file and refuses to let e.g. `sqlalchemy not
    # installed (sql extra)` sanction a skip on a leg that installed sqlalchemy.
    # Piping this log into the gate is therefore meaningful locally too:
    #   cat <log> | python scripts/check_skip_inventory.py --require-receipt
    uv run --extra test --extra ml --extra sql --extra analytics \
      python "$script_dir/check_skip_inventory.py" --emit-receipt > "$log" 2>/dev/null || : > "$log"
    if TEST_TIMEOUT="${TEST_TIMEOUT:-5400}" TEST_MEM_MAX="$leg_mem" \
        "$script_dir/test-capped.sh" \
        uv run --extra test --extra ml --extra sql --extra analytics python -m pytest "$@" \
          -q -rs --tb=short -n 4 --dist loadgroup --reruns 2 --reruns-delay 2 \
          -m "$CI_LOCAL_MARKER" ${PYTEST_ARGS:-} >> "$log" 2>&1; then
      echo pass > "$status_file"
    else
      echo fail > "$status_file"
    fi
  ) &

  # Throttle: block here (after launch, before returning) once at the
  # concurrency cap, so CI_LOCAL_PARALLEL=1 reproduces the exact old
  # sequential behaviour (each leg finishes before the next is queued).
  while [ "$(jobs -rp | wc -l)" -ge "$CI_LOCAL_PARALLEL" ]; do
    wait -n
  done
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
  # Full run: one leg per ci-pr.yml `tests` matrix group, sharded groups
  # collapsed (core-1 + core-2 -> one `core` leg). Order is QUEUEING order only
  # — up to CI_LOCAL_PARALLEL of these run at the same time, and since Car J1
  # there is no ordering between the CI groups either (the `needs:` chain that
  # used to imply one was a RAM throttle, now expressed as max-parallel).
  # Cheapest legs first so a fast failure surfaces early.
  run_leg repo_guards      $CI_LOCAL_DIRS_repo_guards
  run_leg server_api       $CI_LOCAL_DIRS_server_api
  run_leg client_surfaces  $CI_LOCAL_DIRS_client_surfaces
  run_leg shared_storage   $CI_LOCAL_DIRS_shared_storage
  run_leg backend_services $CI_LOCAL_DIRS_backend_services
  run_leg core             $CI_LOCAL_DIRS_core
fi

# Drain whatever is still running past the last run_leg call's throttle.
wait

echo ""
echo "==> ci-local per-leg summary (parallel=$CI_LOCAL_PARALLEL):"
overall=0
for name in "${LEG_NAMES[@]}"; do
  status_file="$log_dir/$name.status"
  status="fail"
  if [ -f "$status_file" ]; then
    status="$(cat "$status_file")"
  fi
  if [ "$status" = "pass" ]; then
    echo "  $name: PASS  (log: $log_dir/$name.log)"
  else
    echo "  $name: FAIL  (log: $log_dir/$name.log)"
    overall=1
  fi
done
exit "$overall"
