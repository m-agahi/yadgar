#!/usr/bin/env bash
# scripts/cleanup-merged-branches.sh
# Weekly worktree + merged-branch sweep for the yadgar repo.
#
# Operates on the repo containing this script. Safe to run with no args.
# Use --dry-run to see what would be removed without deleting anything.
#
# CLASSIFIER (task 221 / ADR-0333): branch merge-state is no longer decided
# by a git tree diff against the default branch — that approach (two-dot,
# three-dot, merge-tree --write-tree, is-ancestor, content-equality,
# patch-id) was tried and fails against this repo's squash-merge-heavy,
# multi-car-train history (see scripts/branch_cleanup.py module docstring
# for the full record of what was tried and why each failed). The working
# classifier asks the code forge (`gh pr list`) which branches have a
# MERGED PR, then closes the transitive `git merge-base --is-ancestor` set
# for car sub-branches merged locally into a train-integration branch.
# All of that logic lives in scripts/branch_cleanup.py — this file is now a
# thin wrapper that also runs `git fetch --prune` / `git worktree prune`
# first, and additionally drives the worktree-age sweep (task 221 piece 2,
# ADR-0333): removes a worktree whose branch has had no commit in
# --max-age-days, retaining the branch always. That phase defaults to
# report-only (dry-run) regardless of this script's own --dry-run/no-flag
# convention — pass --apply-worktree-sweep to let it actually remove
# anything. See scripts/branch_cleanup.py --help for full flag semantics.
#
# DESIGN: read-only by default in protected paths. Active train branches
# matching `feat/vX.Y-stage-*` or env YADGAR_CLEANUP_PRESERVE_GLOB stay.

set -euo pipefail

DRY_RUN=0
EXTRA_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    --apply-worktree-sweep) EXTRA_ARGS+=(--apply) ;;
    --help|-h)
      sed -n '2,/^$/p' "$0" | sed 's/^# //; s/^#//'
      exit 0 ;;
    *) EXTRA_ARGS+=("$arg") ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DEFAULT_BRANCH="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-master}"

log() { printf '[cleanup] %s\n' "$*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '[cleanup] DRY-RUN: %s\n' "$*"
  else
    eval "$@"
  fi
}

log "repo: $REPO_ROOT"
log "default branch: $DEFAULT_BRANCH"
log "dry-run: $DRY_RUN"

# 1. Fetch + prune remote-tracking branches deleted upstream.
log "git fetch --prune"
run "git fetch --prune --quiet"

# 2. Prune stale worktrees (lock files >1d old).
log "git worktree prune --expire 1.day"
run "git worktree prune --expire 1.day"

# 3. Branch classification + deletion, and worktree-age sweep — both handled
#    by branch_cleanup.py (see its module docstring for the classifier design).
PYTHON_BIN="${YADGAR_CLEANUP_PYTHON:-python3}"
if [[ -x "$REPO_ROOT/.venv/bin/python3" ]]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python3"
fi

CLEANUP_PY_ARGS=(--repo "$REPO_ROOT")
if [[ $DRY_RUN -eq 1 ]]; then
  CLEANUP_PY_ARGS+=(--dry-run)
fi
CLEANUP_PY_ARGS+=("${EXTRA_ARGS[@]}")

"$PYTHON_BIN" "$REPO_ROOT/scripts/branch_cleanup.py" "${CLEANUP_PY_ARGS[@]}"
