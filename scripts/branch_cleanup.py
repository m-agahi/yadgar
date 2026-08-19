#!/usr/bin/env python3
"""scripts/branch_cleanup.py — squash-safe branch classifier + worktree-age sweep.

WHY THIS EXISTS (task 221 / ADR-0333)
--------------------------------------
`scripts/cleanup-merged-branches.sh` classified branches with
`git diff --quiet <default>..<branch>` — two-dot, which `git diff` treats as
a full-tree comparison, not a range. Any branch whose fork point predates the
current default-branch tip reports "unmerged" purely because the default
branch advanced, regardless of whether the branch's content actually landed.
Measured 2026-08-19 on this repo: deleted=0 unmerged=170. The sweep had never
deleted a branch.

Three straightforward repairs were tried and documented as failing against
real squash-merged fixtures in this repo (ADR-0333):

  - three-dot `default...branch`: still shows the whole delta after a squash
    merge, because the merge-base stays pre-merge.
  - `git merge-tree --write-tree`: produces a conflicted tree on old
    squash-merged branches (30+ CONFLICT entries), unusable as a boolean.
  - `git merge-base --is-ancestor`: false for every squash merge, and this
    repo's history is entirely squash merges.

This module was validated (2026-08-19) against two more candidates that also
fail here, for a reason specific to this repo's branch topology: car
sub-branches (`car/A-ledger-tables`) merge via real `git merge` into a
*local train-integration branch* (`feat/spine-0047-train`), and only the
integration branch gets a PR, which is then squash-merged. So:

  - exact current-tree-content-equality on just the branch's touched files:
    fails once the default branch's *later, unrelated* commits touch the
    same files (they will, on a live repo) — the comparison has no way to
    tell "superseded by later edits" from "never landed".
  - `git patch-id` equality (whole-commit or scoped-to-touched-files) against
    the squash-merge commit: fails when the squash commit combines *multiple
    branches'* changes to the same file — the file's final diff is the union
    of several sources, not any single branch's diff verbatim.

WHAT THIS MODULE DOES INSTEAD
-------------------------------
Two-layer classification, using the one source of truth that survives squash
merges: the code-forge's own PR records, not git tree diffing.

  1. Direct: ask `gh pr list --state all` once for every branch's PR state.
     A branch with a MERGED PR is merged. A branch with an OPEN PR is
     unmerged (never touch it). This covers every branch that got its own
     PR — which is every *train-integration* branch in this repo's
     multi-car-train convention (see `multi-car-train-single-push` house
     rule: cars merge locally into ONE branch, which gets ONE PR).

  2. Transitive: a branch with no PR record of its own (every car
     sub-branch) is merged if it is a `git merge-base --is-ancestor` of any
     branch already known to be merged (seeded from the default branch, plus
     every PR-confirmed-merged branch). This is real ancestry — car
     sub-branches are merged into the integration branch via an actual
     `git merge`, not a squash, so `is-ancestor` is exact here. Iterated to a
     fixed point (bounded rounds) to catch chains more than one level deep.

If `gh` is unavailable (offline, no auth) this degrades LOUDLY: a warning is
printed, `pr_states` is empty, and only layer 2 seeded from the default
branch runs (i.e. the tool behaves exactly as safely-inert as the old, buggy
script for anything not a true ancestor of default — but says why, instead
of silently misreporting "tree differs").

KNOWN LIMITATION — layer 2 trusts the integration branch's CURRENT tip
------------------------------------------------------------------------
Layer 2 checks `is-ancestor(sub_branch, integration_branch)` against
whatever commit `integration_branch` currently points at, not the commit
its PR actually merged. If a local branch keeps receiving commits AFTER
its own PR lands (someone reuses an already-merged branch ref instead of
starting a new one), a sub-branch merged into it post-landing would be
misclassified as merged even though that content never reached the
default branch. Validated 2026-08-19 against the six integration branches
this repo's real corpus actually used as ancestor-check targets
(`feat/spine-0047-train`, `train/2026-08-14-identity-corpus-rekey`,
`fix/adr-seed-failure-visibility`, `feat/branch-scoping-removal`,
`fix/fresh-install-engine2`, `docs/ettin-rust-investigation-2026-08-02`):
zero commits landed on any of them after their PR's `mergedAt`. Not
provably impossible in general — re-check this if a future sweep run
classifies something as merged that looks wrong; the diagnostic is
`git log <branch> --since=<pr_mergedAt>` returning anything.

PIECE 2 — WORKTREE-AGE SWEEP (independent of piece 1)
--------------------------------------------------------
Reclaims disk without needing ANY merge classification: a worktree whose
branch's last commit is older than `--max-age-days` gets its *working
directory* removed; the branch is always retained (branches are nearly free;
worktrees are ~500MB each, mostly `.venv`). A worktree with uncommitted
changes (ignoring `.venv`) is never force-removed blind — its `git diff
HEAD` and any untracked files are saved to `--patches-dir` first.

Safe by default: this phase only ACTS when `--apply` is passed; absent it,
every action is reported and nothing is removed (`ADR-0333` consequence: the
weekly sweep is unattended cron, and this is the first automated run of
piece 2 ever — it should not go live silently).

CLI
---
    branch_cleanup.py [--dry-run] [--apply] [--max-age-days N]
                       [--patches-dir PATH] [--skip-branches]
                       [--skip-worktree-sweep] [--repo PATH]

    --dry-run              Force BOTH phases to report-only (overrides --apply).
    --apply                Let the worktree-age phase (piece 2) actually
                            remove worktrees. Branch deletion (piece 1) keeps
                            its pre-existing contract: it acts unless
                            --dry-run is given (unchanged cron behaviour).
    --max-age-days N        Worktree-age threshold (default 14).
    --patches-dir PATH      Where dirty-worktree patches are saved
                            (default ~/.cache/yadgar/abandoned-worktree-patches).
    --skip-branches         Skip phase 1 entirely.
    --skip-worktree-sweep   Skip phase 2 entirely.
    --repo PATH             Repo root (default: discovered from this file's
                            location, matching the old script's convention).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PRESERVE_GLOB = "feat/v?.?-stage-*"
DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_PATCHES_DIR = Path.home() / ".cache" / "yadgar" / "abandoned-worktree-patches"
GH_TIMEOUT_SECONDS = 30
MAX_CLOSURE_ROUNDS = 5


# ── git/gh subprocess helpers ────────────────────────────────────────────────


def _run(args: list[str], cwd: Path, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def git(repo: Path, *args: str) -> str:
    result = _run(["git", *args], cwd=repo)
    return result.stdout.strip()


def get_default_branch(repo: Path) -> str:
    result = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    ref = result.stdout.strip()
    if ref.startswith("origin/"):
        ref = ref[len("origin/") :]
    return ref or "master"


def list_local_branches(repo: Path) -> list[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b for b in out.splitlines() if b]


def current_branch(repo: Path) -> str:
    result = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo)
    return result.stdout.strip()


def is_ancestor(repo: Path, branch_name: str, target: str) -> bool:
    if branch_name == target:
        return False
    result = _run(["git", "merge-base", "--is-ancestor", branch_name, target], cwd=repo)
    return result.returncode == 0


def branch_last_commit_epoch(repo: Path, branch_name: str) -> int | None:
    result = _run(["git", "log", "-1", "--format=%ct", branch_name], cwd=repo)
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def fetch_pr_states(repo: Path, gh_bin: str = "gh") -> dict[str, str]:
    """Return {headRefName: state} for every PR ("MERGED"/"OPEN"/"CLOSED").

    Empty dict + a stderr warning on any failure (gh missing, not authed,
    network down, timeout) — callers must treat that as "no PR data", not
    "no PRs exist".
    """
    try:
        result = subprocess.run(
            [
                gh_bin,
                "pr",
                "list",
                "--state",
                "all",
                "--json",
                "headRefName,state",
                "--limit",
                "500",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"[cleanup] WARNING: gh pr list failed to run ({exc}); PR data unavailable, "
            f"falling back to pure git ancestry (this classifies strictly fewer branches "
            f"as merged than a working gh connection would)",
            file=sys.stderr,
        )
        return {}
    if result.returncode != 0:
        print(
            f"[cleanup] WARNING: gh pr list exited {result.returncode}: "
            f"{result.stderr.strip()!r}; PR data unavailable, falling back to pure git "
            f"ancestry",
            file=sys.stderr,
        )
        return {}
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[cleanup] WARNING: gh pr list returned unparseable JSON ({exc}); PR data unavailable",
            file=sys.stderr,
        )
        return {}
    states: dict[str, str] = {}
    for row in rows:
        head = row.get("headRefName")
        state = row.get("state")
        if not head or not state:
            continue
        # A branch can have multiple PRs over time (reopened, re-created).
        # MERGED wins over anything else if ever seen merged.
        if states.get(head) == "MERGED":
            continue
        states[head] = state
    return states


# ── phase 1: branch classification ───────────────────────────────────────────


@dataclass
class ClassificationResult:
    merged: set[str] = field(default_factory=set)
    unmerged: set[str] = field(default_factory=set)
    preserved: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)


def classify_branches(
    repo: Path,
    branches: list[str],
    default_branch: str,
    pr_states: dict[str, str],
    preserve_glob: str = DEFAULT_PRESERVE_GLOB,
    skip: set[str] | None = None,
) -> ClassificationResult:
    """Classify every branch as merged / unmerged / preserved.

    Layer 1 (direct): branch has its own PR — MERGED -> merged,
    OPEN/CLOSED(unmerged) -> unmerged, trusted outright.
    Layer 2 (transitive): no PR of its own — merged if it is an ancestor of
    the default branch or of any branch already known merged (fixed-point
    iteration, bounded rounds — catches multi-level car-branch chains).
    Anything left unresolved after that stays unmerged (conservative
    default — matches the old script's fail-safe direction).
    """
    import fnmatch

    skip = skip or set()
    result = ClassificationResult()

    candidates = [b for b in branches if b != default_branch and b not in skip]
    for b in list(candidates):
        if fnmatch.fnmatch(b, preserve_glob):
            result.preserved.add(b)
            result.reasons[b] = f"matches preserve glob {preserve_glob!r}"
    candidates = [b for b in candidates if b not in result.preserved]

    # Layer 1: direct PR state.
    undetermined: list[str] = []
    for b in candidates:
        state = pr_states.get(b)
        if state == "MERGED":
            result.merged.add(b)
            result.reasons[b] = "PR state MERGED"
        elif state in ("OPEN", "CLOSED"):
            result.unmerged.add(b)
            result.reasons[b] = f"PR state {state}"
        else:
            undetermined.append(b)

    # Layer 2: transitive ancestry, fixed-point over bounded rounds.
    known_merged_targets = {default_branch} | result.merged
    for _round in range(MAX_CLOSURE_ROUNDS):
        newly_merged = []
        for b in undetermined:
            for target in known_merged_targets:
                if is_ancestor(repo, b, target):
                    newly_merged.append((b, target))
                    break
        if not newly_merged:
            break
        for b, target in newly_merged:
            result.merged.add(b)
            result.reasons[b] = f"ancestor of {target!r} (no direct PR)"
            known_merged_targets.add(b)
        undetermined = [b for b in undetermined if b not in result.merged]

    for b in undetermined:
        result.unmerged.add(b)
        result.reasons.setdefault(b, "no PR record, not an ancestor of any known-merged branch")

    return result


def worktrees_for_branch(repo: Path, branch_name: str) -> list[str]:
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    paths: list[str] = []
    current_path = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            if ref == f"refs/heads/{branch_name}" and current_path:
                paths.append(current_path)
    return paths


def delete_branches(
    repo: Path,
    classification: ClassificationResult,
    dry_run: bool,
    patches_dir: Path | None = None,
) -> tuple[int, int]:
    """Delete merged branches (unlocking + removing any attached worktree first).

    A worktree attached to a merged branch is force-removed just like the
    age sweep does — which means it gets the SAME dirty-worktree protection:
    a worktree with uncommitted changes (ignoring .venv) never gets force-
    removed blind. Its diff + untracked files are saved to `patches_dir`
    first. Without this, "the branch actually got classified merged" (this
    car's whole point) makes force-removal reachable for the first time —
    the old, permanently-broken classifier never got here.

    Returns (deleted_count, skipped_worktree_attached_count).
    """
    patches_dir = patches_dir if patches_dir is not None else DEFAULT_PATCHES_DIR
    deleted = 0
    for branch in sorted(classification.merged):
        wt_paths = worktrees_for_branch(repo, branch)
        for wt_path in wt_paths:
            wt_path_obj = Path(wt_path)
            dirty = False
            if not dry_run and wt_path_obj.is_dir():
                dirty, _ = is_worktree_dirty(wt_path_obj)
            if dry_run:
                if dirty:
                    print(
                        f"[cleanup] DRY-RUN: would save patch + remove dirty worktree "
                        f"{wt_path} (for branch {branch})"
                    )
                else:
                    print(
                        f"[cleanup] DRY-RUN: worktree unlock+remove: {wt_path} "
                        f"(for branch {branch})"
                    )
            else:
                if dirty:
                    patch_path = save_worktree_patch(wt_path_obj, branch, patches_dir)
                    if patch_path:
                        print(f"[cleanup] saved dirty-worktree patch: {patch_path}")
                print(f"[cleanup] worktree unlock+remove: {wt_path} (for branch {branch})")
                _run(["git", "worktree", "unlock", wt_path], cwd=repo)
                _run(["git", "worktree", "remove", "--force", wt_path], cwd=repo)
        reason = classification.reasons.get(branch, "")
        if dry_run:
            print(f"[cleanup] DRY-RUN: merged ({reason}) -> delete: {branch}")
        else:
            print(f"[cleanup] merged ({reason}) -> delete: {branch}")
            _run(["git", "branch", "-D", branch], cwd=repo)
        deleted += 1
    return deleted, 0


# ── phase 2: worktree-age sweep ──────────────────────────────────────────────


@dataclass
class Worktree:
    path: str
    branch_name: str | None
    locked: bool


def list_worktrees(repo: Path) -> list[Worktree]:
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    entries: list[Worktree] = []
    path: str | None = None
    current_branch_name: str | None = None
    locked = False
    for line in result.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            if path is not None:
                entries.append(Worktree(path=path, branch_name=current_branch_name, locked=locked))
            path = line[len("worktree ") :]
            current_branch_name = None
            locked = False
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            current_branch_name = (
                ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
            )
        elif line.strip() == "locked" or line.startswith("locked "):
            locked = True
    if path is not None:
        entries.append(Worktree(path=path, branch_name=current_branch_name, locked=locked))
    return entries


def is_worktree_dirty(path: Path) -> tuple[bool, list[str]]:
    """Return (dirty, untracked_files). Ignores .venv/ noise."""
    result = _run(["git", "status", "--porcelain", "--ignore-submodules"], cwd=path)
    lines = [line for line in result.stdout.splitlines() if not line[3:].startswith(".venv/")]
    untracked = [line[3:] for line in lines if line.startswith("??")]
    return (len(lines) > 0, untracked)


def save_worktree_patch(repo_worktree: Path, branch_name: str, patches_dir: Path) -> Path | None:
    """Save `git diff HEAD` + copies of untracked files for a dirty worktree.

    Returns the patch file path, or None if there was nothing to save
    (e.g. only .venv noise made it look dirty).
    """
    diff = _run(["git", "diff", "HEAD"], cwd=repo_worktree)
    _dirty, untracked = is_worktree_dirty(repo_worktree)
    safe_branch = branch_name.replace("/", "_")
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    patch_path = patches_dir / f"{safe_branch}-{timestamp}.patch"

    if not diff.stdout.strip() and not untracked:
        return None

    patches_dir.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff.stdout, encoding="utf-8")

    if untracked:
        untracked_dir = patches_dir / f"{safe_branch}-{timestamp}-untracked"
        for rel in untracked:
            src = repo_worktree / rel
            if not src.is_file():
                continue
            dest = untracked_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())

    return patch_path


def sweep_worktree_age(
    repo: Path,
    max_age_days: int,
    dry_run: bool,
    patches_dir: Path,
    now: float | None = None,
) -> list[dict]:
    """Remove worktrees whose branch's last commit is older than max_age_days.

    NEVER deletes the branch. Dirty worktrees (ignoring .venv) get their
    diff + untracked files saved to patches_dir before removal.
    Returns a list of action dicts for reporting/testing.
    """
    now = now if now is not None else time.time()
    cutoff = now - (max_age_days * 86400)
    actions: list[dict] = []

    main_worktree = list_worktrees(repo)[0].path if list_worktrees(repo) else None
    for wt in list_worktrees(repo):
        if wt.path == main_worktree:
            continue
        if wt.branch_name is None:
            continue
        epoch = branch_last_commit_epoch(repo, wt.branch_name)
        if epoch is None:
            continue
        age_days = (now - epoch) / 86400
        if epoch > cutoff:
            continue

        wt_path = Path(wt.path)
        action: dict = {
            "path": wt.path,
            "branch_name": wt.branch_name,
            "age_days": round(age_days, 1),
            "dirty": False,
            "patch_saved": None,
            "removed": False,
            "locked": wt.locked,
        }

        dirty = False
        if wt_path.is_dir():
            dirty, _ = is_worktree_dirty(wt_path)
        action["dirty"] = dirty

        if dry_run:
            if dirty:
                print(
                    f"[worktree-sweep] DRY-RUN: would save patch + remove dirty worktree "
                    f"{wt.path} (branch {wt.branch_name}, {action['age_days']}d old)"
                )
            else:
                print(
                    f"[worktree-sweep] DRY-RUN: would remove worktree {wt.path} "
                    f"(branch {wt.branch_name}, {action['age_days']}d old); branch retained"
                )
            actions.append(action)
            continue

        if dirty:
            patch_path = save_worktree_patch(wt_path, wt.branch_name, patches_dir)
            action["patch_saved"] = str(patch_path) if patch_path else None
            if patch_path:
                print(f"[worktree-sweep] saved dirty-worktree patch: {patch_path}")

        if wt.locked:
            _run(["git", "worktree", "unlock", wt.path], cwd=repo)
        remove_result = _run(["git", "worktree", "remove", "--force", wt.path], cwd=repo)
        if remove_result.returncode == 0:
            action["removed"] = True
            print(
                f"[worktree-sweep] removed worktree {wt.path} (branch {wt.branch_name} retained, "
                f"{action['age_days']}d old)"
            )
        else:
            print(
                f"[worktree-sweep] WARNING: failed to remove {wt.path}: "
                f"{remove_result.stderr.strip()}",
                file=sys.stderr,
            )
        actions.append(action)

    return actions


# ── CLI ───────────────────────────────────────────────────────────────────────


def _discover_repo(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Force both phases report-only.")
    parser.add_argument(
        "--apply", action="store_true", help="Let the worktree-age phase actually remove worktrees."
    )
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--patches-dir", type=str, default=str(DEFAULT_PATCHES_DIR))
    parser.add_argument("--skip-branches", action="store_true")
    parser.add_argument("--skip-worktree-sweep", action="store_true")
    parser.add_argument("--repo", type=str, default=None)
    parser.add_argument("--gh-bin", type=str, default="gh")
    parser.add_argument(
        "--preserve-glob",
        type=str,
        default=os.environ.get("YADGAR_CLEANUP_PRESERVE_GLOB", DEFAULT_PRESERVE_GLOB),
        help="Branch name glob to never classify/delete (env "
        "YADGAR_CLEANUP_PRESERVE_GLOB, default %(default)r).",
    )
    args = parser.parse_args(argv)

    repo = _discover_repo(args.repo)
    default_branch = get_default_branch(repo)
    cur_branch = current_branch(repo)

    branch_dry_run = args.dry_run
    worktree_dry_run = args.dry_run or not args.apply

    if not args.skip_branches:
        pr_states = fetch_pr_states(repo, gh_bin=args.gh_bin)
        branches = list_local_branches(repo)
        classification = classify_branches(
            repo,
            branches,
            default_branch,
            pr_states,
            preserve_glob=args.preserve_glob,
            skip={cur_branch} if cur_branch else set(),
        )
        for b in sorted(classification.unmerged):
            print(f"[cleanup] unmerged ({classification.reasons.get(b, '')}): {b}")
        for b in sorted(classification.preserved):
            print(f"[cleanup] preserved: {b}")
        deleted, _ = delete_branches(
            repo, classification, dry_run=branch_dry_run, patches_dir=Path(args.patches_dir)
        )
        print(
            f"[cleanup] summary: deleted={deleted} unmerged={len(classification.unmerged)} "
            f"preserved={len(classification.preserved)} dry_run={int(branch_dry_run)}"
        )

    if not args.skip_worktree_sweep:
        actions = sweep_worktree_age(
            repo,
            max_age_days=args.max_age_days,
            dry_run=worktree_dry_run,
            patches_dir=Path(args.patches_dir),
        )
        removed = sum(1 for a in actions if a.get("removed"))
        print(
            f"[worktree-sweep] summary: candidates={len(actions)} removed={removed} "
            f"dry_run={int(worktree_dry_run)} max_age_days={args.max_age_days}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
