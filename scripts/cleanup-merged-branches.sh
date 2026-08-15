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
#     classify via `git diff --quiet <default>..<branch> -- . ':!*.md'`
#       (tree-based; squash merges defeat `git cherry`, see Car H train 2026-08-14)
#     if worktree attached to branch: unlock + worktree remove --force FIRST,
#       then delete the branch
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
  [[ "$branch" == "$(git symbolic-ref --short HEAD 2>/dev/null || true)" ]] && { : $((skipped++)); continue; }
  # Skip preserved patterns.
  # shellcheck disable=SC2053
  if [[ $branch == $PRESERVE_GLOB ]]; then
    log "preserved (matches glob): $branch"
    : $((skipped++))
    continue
  fi
  # Effectively-merged check via tree diff (squash merges defeat `git cherry`).
  # "merged" = branch's source-tree content already matches default. We exclude
  # *.md because plan/notes commits land on master via squash without a code
  # delta; counting those as "unmerged" would strand car-trains.
  diff_exit=0
  git diff --quiet "$DEFAULT_BRANCH..$branch" -- . ':!*.md' 2>/dev/null || diff_exit=$?
  # Exit 0 = no source diff (= effectively merged). Exit 1 = has source diff.
  # Exit 2 = error (pathspec with new file vs HEAD, etc.) — treat as unmerged.
  if [[ "$diff_exit" -eq 0 ]]; then
    # Branch may still be referenced by a worktree — unlock + remove the worktree
    # BEFORE deleting the branch (post-Car-H rule, worktree-aware sweep).
    wt_paths="$(git worktree list --porcelain | awk -v b="$branch" '
      /^worktree / { path=$2 }
      /^branch /   { if ($2 == "refs/heads/"b) print path }
    ')"
    if [[ -n "$wt_paths" ]]; then
      while IFS= read -r wt_path; do
        [[ -z "$wt_path" ]] && continue
        log "worktree unlock+remove: $wt_path (for branch $branch)"
        run "git worktree unlock '$wt_path'"
        run "git worktree remove --force '$wt_path'"
      done <<< "$wt_paths"
    fi
    log "merged → delete: $branch"
    run "git branch -D '$branch'"
    : $((deleted++))
  else
    log "unmerged (source-tree ahead of $DEFAULT_BRANCH): $branch"
    : $((unmerged++))
  fi
done < <(git for-each-ref --format='%(refname:short)' refs/heads/)

log "summary: deleted=$deleted unmerged=$unmerged preserved/skipped=$skipped dry_run=$DRY_RUN"
