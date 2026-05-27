#!/usr/bin/env bash
# scripts/cleanup-merged-branches.sh
# Weekly worktree + merged-branch sweep for the yadgar repo.
#
# Operates on the repo containing this script. Safe to run with no args.
# Use --dry-run to see what would be removed without deleting anything.
#
# - git worktree prune --expire 1.day
# - For each local branch != current default branch:
#     skip if matches PROTECTED patterns below
#     classify via `git cherry <default> <branch>` — zero "+" lines = effectively merged
#     delete merged local branches via `git branch -D`
# - For each remote-tracking branch (refs/remotes/origin/*) that no longer
#   exists upstream: `git fetch --prune` cleans automatically (one-shot).
#
# DESIGN: read-only by default in protected paths. Active train branches
# matching `feat/vX.Y-stage-*` or env YADGAR_CLEANUP_PRESERVE_GLOB stay.

set -euo pipefail

DRY_RUN=0
case "${1:-}" in
  --dry-run|-n) DRY_RUN=1 ;;
  --help|-h)
    sed -n '2,/^$/p' "$0" | sed 's/^# //; s/^#//'
    exit 0 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DEFAULT_BRANCH="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-master}"

PRESERVE_GLOB="${YADGAR_CLEANUP_PRESERVE_GLOB:-feat/v?.?-stage-*}"

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
log "preserve glob: $PRESERVE_GLOB"
log "dry-run: $DRY_RUN"

# 1. Fetch + prune remote-tracking branches deleted upstream.
log "git fetch --prune"
run "git fetch --prune --quiet"

# 2. Prune stale worktrees (lock files >1d old).
log "git worktree prune --expire 1.day"
run "git worktree prune --expire 1.day"

# 3. Walk local branches; delete merged ones.
deleted=0
skipped=0
unmerged=0
while IFS= read -r branch; do
  branch="${branch## }"
  [[ -z "$branch" ]] && continue
  [[ "$branch" == "$DEFAULT_BRANCH" ]] && continue
  # Skip current HEAD.
  [[ "$branch" == "$(git symbolic-ref --short HEAD 2>/dev/null || true)" ]] && { ((skipped++)); continue; }
  # Skip preserved patterns.
  # shellcheck disable=SC2053
  if [[ $branch == $PRESERVE_GLOB ]]; then
    log "preserved (matches glob): $branch"
    ((skipped++))
    continue
  fi
  # Effectively-merged check via cherry.
  diff_count="$(git cherry "$DEFAULT_BRANCH" "$branch" 2>/dev/null | grep -c '^+' || true)"
  if [[ "$diff_count" -eq 0 ]]; then
    log "merged → delete: $branch"
    run "git branch -D '$branch'"
    ((deleted++))
  else
    log "unmerged ($diff_count commit-equiv ahead of $DEFAULT_BRANCH): $branch"
    ((unmerged++))
  fi
done < <(git for-each-ref --format='%(refname:short)' refs/heads/)

log "summary: deleted=$deleted unmerged=$unmerged preserved/skipped=$skipped dry_run=$DRY_RUN"
