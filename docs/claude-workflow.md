# Yadgar v5 Integration Model

Runbook for the long-lived feature-branch workflow used to ship v5.0.
Captured 2026-05-15 after v4.9 surfaced compounding pain from N parallel
PRs (rebase cascades + long-running CI on each intermediate push).

---

## When to use this model

Whenever a release bundles N items that:
- Logically ship together (one release, not N)
- Touch overlapping files (rebase cost between PRs is real)
- Can be parallelised by agents (independent enough to develop concurrently)

For small isolated changes (one CVE bump, one docstring fix): direct PR
to master is still correct. This model is for cohesive releases like v5.

---

## Branch topology

```
master
   │
   └── feat/vX.Y  (long-lived integration branch)
            │
            ├── feat/vX.Y/01-<topic>   (agent A, no PR)
            ├── feat/vX.Y/02-<topic>   (agent B, no PR)
            ├── feat/vX.Y/03-<topic>   (agent C, no PR)
            └── ...
```

- `feat/vX.Y` is created once at start of release work.
- Sub-branches are numbered in dependency / integration order.
- Sub-branches push to origin (for backup / cross-machine visibility) but
  do NOT open PRs against master.
- Final PR is one PR: `feat/vX.Y → master`.

---

## Workflow

### 0. One-time setup (per release)

```bash
git checkout master
git pull --ff-only
git checkout -b feat/vX.Y
git push origin feat/vX.Y
```

Optional: extend `.forgejo/workflows/*.yaml` `on:` clause to skip CI on
the feature branch except when commit message contains `[ci]` — keeps
intermediate pushes cheap.

### 1. Dispatching an agent

Every agent prompt MUST include the base-branch instruction:

> **Branch from `origin/feat/vX.Y` not `origin/master`.** Push to
> `feat/vX.Y/NN-<topic>` (use the next available NN). Do NOT open a PR
> against master — main thread handles integration.

Use isolated worktrees per agent (`isolation: "worktree"` in Agent tool)
when running multiple agents concurrently.

### 2. Integration cycle (main thread, after each agent completes)

```bash
git checkout feat/vX.Y
git pull --ff-only
git fetch origin feat/vX.Y/NN-<topic>

# Optionally light audit on sub-branch diff first
git diff feat/vX.Y..origin/feat/vX.Y/NN-<topic>

# Merge (preserves sub-branch history) or rebase (linear)
git merge --no-ff origin/feat/vX.Y/NN-<topic> \
  -m "merge: NN-<topic> into feat/vX.Y"

# Conflicts? Resolve inline OR dispatch fix agent on feat/vX.Y directly.

# Local sanity check
YADGAR_TEST=1 .venv/bin/python -m pytest yadgar/tests/ -x --timeout=60 -q

git push origin feat/vX.Y
```

Once merged, the sub-branch can be deleted from origin or retained for
historical reference.

### 3. Periodic master sync

Every ~3 days OR when master moves significantly (>10 commits):

```bash
git checkout feat/vX.Y
git pull --ff-only
git fetch origin master
git rebase origin/master
git push --force-with-lease origin feat/vX.Y
```

Prevents end-of-release big-bang divergence.

### 4. Final master PR

When all sub-branches are integrated:

```bash
# Final rebase onto latest master
git fetch origin master
git rebase origin/master
git push --force-with-lease origin feat/vX.Y

# Open PR feat/vX.Y → master
gh pr create --base master --head feat/vX.Y --title "feat: vX.Y" --body "..."
```

This is the ONE point where full CI fires. After merge:
- Tag `vX.Y.0`
- Bump `nix/modules/home/yadgar.nix`
- `home-manager switch`

---

## CI on the feature branch

Three options. Recommendation: **B** for most releases.

| Mode | Description | When |
|------|-------------|------|
| A — Off | No CI on feature branch; only final PR runs CI | Small releases where regressions are unlikely |
| **B — Manual / opt-in** | CI runs only when commit message contains `[ci]` or operator clicks "Re-run" | **Default for v5-class releases** |
| C — Scheduled | Nightly cron run on `feat/vX.Y` | Long releases (>2 weeks) where regression-bisect cost matters |

Configure mode B in Forgejo workflow:

```yaml
on:
  pull_request:
  push:
    branches: [master]
    paths-ignore: ['feat/**']
```

Or check commit message:

```yaml
jobs:
  test:
    if: contains(github.event.head_commit.message, '[ci]') || github.ref == 'refs/heads/master'
```

---

## Safeguards

1. **Local test gate before each integration.** Run `pytest` on the
   merged feature branch before pushing. Catches integration breaks
   immediately.
2. **Light audit at integration point.** Spawn ONE `cavecrew-reviewer`
   pass on the sub-branch diff. Skip full 3-pass — save that for final PR.
3. **Full 3-pass audit before master PR.** Security + quality + cavecrew
   on `master..feat/vX.Y` diff.
4. **Periodic master rebase.** ~3 days OR significant master movement.
5. **Numbered sub-branches.** `01-`, `02-`, etc. — resolve conflicts in
   numeric order, predictable conflict surface.
6. **No --force-push to sub-branches once merged.** History matters.
7. **Push sub-branches to origin.** Even without PR — backup + visibility.

---

## Anti-patterns (what NOT to do)

- ❌ Branching sub-work directly from master while a feature branch exists
- ❌ Opening intermediate PRs against master for items destined for the
  feature branch
- ❌ Force-pushing the feature branch on every minor change (history matters)
- ❌ Letting the feature branch diverge >30 commits from master without rebasing
- ❌ Skipping the final 3-pass audit because "intermediate audits looked fine"

---

## Lessons from v4.9 (why this exists)

v4.9 shipped via 4 parallel PRs (#57, #58, #59, #60), each branched from
the same master. As each merged:

1. Remaining PRs needed rebase + force-push
2. Each force-push fired full CI (~34 min)
3. Audit agents ran against stale diffs (saw plan-doc changes "reversed")
4. Cumulative CI time across iterations: many hours

Each pain point above maps to a fix in this model:

| v4.9 pain | This model |
|-----------|-----------|
| Rebase cascade after merges | All sub-branches branched off stable feature branch |
| CI burn on every intermediate push | CI only on `[ci]` commits + final PR |
| Stale-diff audits | All diffs computed vs feature-branch tip |
| Hard-to-track integration state | Single integration branch, one history |

## Doc-update gate (before opening final PR)

Run before `gh pr create` / API equivalent:

1. Diff README + every canonical doc (`architecture`, `configuration`,
   `memory-lifecycle`, `retrieval`) vs the actual feature / behaviour
   change list. If the PR touches `yadgar/**` but no doc changed,
   PR is incomplete.
2. Verify `docs/CHANGELOG.md` has an entry for the new version.
3. Update `docs/roadmap/v*.md` for the shipping version with
   "shipped" status if it carries a plan doc.
4. Reviewers fail PRs that ship features without doc updates.

## Branch cleanup (after PR is open)

AFTER the PR exists on origin (review window provided), delete each
sub-branch that fed it:

    git push origin --delete feat/vX.Y-NN-<topic>

Skip the PR's own head branch until merged + deployed + verified —
that branch is your rollback path. Pre-existing
`feat/v5.0-NN-<topic>` branches from v5 integration may be cleaned
in batch after v5.0.1 ships.

## Benchmarks (LoCoMo / LongMemEval)

`benchmarks/` holds reproducible scripts that drove yadgar's initial
performance claims:

- `run_locomo_jscore.py` — LoCoMo with Jaccard scoring
- `run_locomo_ablation.py` — LoCoMo ablation study
- `run_longmemeval.py` — LongMemEval suite
- `run_benchmark_gpu.py` — GPU-accelerated runner
- `test_e_locomo.py` — LoCoMo end-to-end test

Run on:
- Every major version bump (5.0 → 5.1 etc) — compare against the
  prior baseline, commit the JSON output to `benchmarks/results/`.
- After any retrieval-pipeline change (Stage 11-style decomposition,
  branch-tagging, rerank threshold tuning).

See `benchmarks/README.md` for setup and current baseline numbers.
