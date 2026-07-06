#!/usr/bin/env bash
# e2e master-baseline diff — the hard-won "never trust a 'pre-existing' label" rule.
#
# WHY: a branch e2e failure is only safe to dismiss as "pre-existing" if the SAME
# e2e also fails on master. Otherwise it is a branch-introduced regression = must-fix.
# This script runs the PRE-EXISTING e2e suite on a throwaway master worktree AND on
# the current branch, then diffs the failure sets.
#
# SCOPE: this baselines the pre-existing suite (test_recall_backend_contract_e2e,
# test_scope_filter_e2e, …). The backend cache-invalidation tests in
# test_backend_cache_invalidation_e2e.py are branch-only BY CONSTRUCTION — the Car 2
# cache seam does not exist on master, so they cannot run there and are excluded
# from the baseline comparison (only the branch side runs them).
#
# USAGE (from the repo root of the BRANCH worktree):
#   bash scripts/e2e_master_baseline.sh
#
# Requires: ~/.local/bin/surreal (same as `make e2e`).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
# BASELINE REF: the honest baseline for THIS branch's additive test files is the
# commit the branch sits on top of (the train tip), so the ONLY delta between the
# two sides is the new test file + this script. Default resolves that automatically
# (parent of HEAD); override with MASTER_REF=<ref> to diff against a different base
# (e.g. MASTER_REF=master for the true repo default branch). NOTE in this repo the
# code cars live on an unpushed branch NOT descended from master, so `master` alone
# lacks the Car seam — parent-of-HEAD is the like-for-like base.
MASTER_REF="${MASTER_REF:-$(git -C "$REPO_ROOT" rev-parse HEAD~1)}"
BASELINE_WT="$(mktemp -d /tmp/yadgar-e2e-baseline.XXXXXX)"
# Exclude the branch-only cache-invalidation file from the diffable set — it does
# not exist / cannot pass on master, so it is not a regression signal.
BRANCH_ONLY="yadgar/tests/e2e/test_backend_cache_invalidation_e2e.py"

cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$BASELINE_WT" 2>/dev/null || true
  rm -rf "$BASELINE_WT" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> Creating throwaway master worktree at $BASELINE_WT (ref=$MASTER_REF)"
git -C "$REPO_ROOT" worktree add --detach "$BASELINE_WT" "$MASTER_REF"

run_e2e() {
  # $1 = working dir, $2 = output file, $3... = extra pytest args
  local wd="$1" out="$2"
  shift 2
  ( cd "$wd" && \
    OTEL_SDK_DISABLED=true PATH="$HOME/.local/bin:$PATH" \
    uv run --extra test --extra ml python -m pytest yadgar/tests/e2e/ \
      -m e2e -p no:randomly -n0 --tb=no -q "$@" ) \
    > "$out" 2>&1 || true
}

MASTER_OUT="$BASELINE_WT/.e2e-master.txt"
BRANCH_OUT="$REPO_ROOT/.e2e-branch.txt"

echo "==> Running e2e on MASTER baseline (excluding branch-only file)"
run_e2e "$BASELINE_WT" "$MASTER_OUT" --deselect "$BRANCH_ONLY"

echo "==> Running e2e on BRANCH (excluding branch-only file for a like-for-like diff)"
run_e2e "$REPO_ROOT" "$BRANCH_OUT" --deselect "$BRANCH_ONLY"

# Extract FAILED/ERROR node ids from each run, sorted.
grep -oE '^(FAILED|ERROR) [^ ]+' "$MASTER_OUT" | sort -u > "$BASELINE_WT/.master-fails" || true
grep -oE '^(FAILED|ERROR) [^ ]+' "$BRANCH_OUT" | sort -u > "$REPO_ROOT/.branch-fails" || true

echo
echo "==== MASTER e2e failures (baseline) ===="
cat "$BASELINE_WT/.master-fails" || true
echo "==== BRANCH e2e failures ===="
cat "$REPO_ROOT/.branch-fails" || true
echo
echo "==== BRANCH-ONLY failures (in branch, NOT in master = MUST-FIX regressions) ===="
comm -13 "$BASELINE_WT/.master-fails" "$REPO_ROOT/.branch-fails" > "$REPO_ROOT/.branch-only-fails" || true
if [ -s "$REPO_ROOT/.branch-only-fails" ]; then
  cat "$REPO_ROOT/.branch-only-fails"
  echo
  echo "RESULT: branch-introduced e2e failures detected — DO NOT dismiss as pre-existing."
  exit 1
fi
echo "(none — every branch failure also fails on master, i.e. genuinely pre-existing)"
echo
echo "RESULT: no branch-introduced e2e regressions in the pre-existing suite."
