# Standard Train Workflow (ADR-0088/0107)

Runbook for the standard train workflow used to ship cohesive multi-item
releases in parallel. Codified in ADR-0088 (one feat branch / one PR / one
version) and extended by ADR-0107 (stacked-rebase ff-only integration;
unconditional per-car audited plans; retiring independent-merge).

---

## When to use this model

Whenever a release bundles N items that:
- Logically ship together (one release, not N)
- Touch overlapping files (rebase cost between PRs is real)
- Can be parallelised by agents (independent enough to develop concurrently)

For small isolated changes (one CVE bump, one docstring fix): direct PR
to master is still correct. This model is for cohesive releases.

---

## Vocabulary

| Term | Meaning |
|------|---------|
| **train** | A cohesive release; one `feat/<train>` branch; one PR; one version. |
| **car** | A self-contained unit of work within the train; own worktree; own branch. |
| **stacked-rebase ff-only** | Integration method: each car rebases onto the latest feat tip, then is merged with `--ff-only`. |
| **AUDITED-ready** | A car plan that has passed an independent adversarial audit (BUILD / BUILD-WITH-CHANGES / DO-NOT-BUILD verdict). |

---

## Branch topology

```
master
   │
   └── feat/<train>   (integration branch; one PR at the end)
            │
            ├── car/<topic-A>   (agent A — isolated worktree)
            ├── car/<topic-B>   (agent B — isolated worktree)
            ├── car/<topic-C>   (agent C — isolated worktree)
            └── ...
```

- `feat/<train>` is created once at start of release work, off latest `origin/master`.
- Cars build in isolated worktrees on distinct branches; seams must be disjoint.
- Car branches push to origin (for backup / cross-machine visibility) but
  do NOT open PRs against master.
- Final PR is one PR: `feat/<train> → master`. ONE version claimed here.

---

## Workflow

### 0. One-time setup (per train)

```bash
git checkout master
git pull --ff-only
git checkout -b feat/<train>
git push origin feat/<train>
```

Optional: extend `.forgejo/workflows/*.yaml` `on:` clause to skip CI on
the feature branch except when commit message contains `[ci]` — keeps
intermediate pushes cheap.

### 1. Dispatching a car agent

**PRECONDITION (ADR-0107): every car MUST have its own plan doc that passes
an independent adversarial audit (verdict: BUILD / BUILD-WITH-CHANGES /
DO-NOT-BUILD) BEFORE its build dispatch — unconditional, regardless of blast
radius. Build starts only on an AUDITED-ready plan.**

See the `car-plan-audit-gate` agent-prompt pattern and
`agent-prompt-dispatch-plan-audit` / `agent-prompt-plan-audit` for the audit
HOW. Plan lifecycle follows ADR-0081/0082 (final car archives via first-commit
`git mv`).

Once a plan is AUDITED-ready, dispatch the car agent with:

> **Branch from `origin/feat/<train>`, not `origin/master`.** Push to
> `car/<topic>`. Do NOT open a PR against master — main thread handles
> integration via stacked-rebase ff-only.

Use `isolation: "worktree"` in the Agent tool when running multiple cars
concurrently.

### 2. Integration cycle — stacked-rebase ff-only (main thread, after each car)

**This is the canonical integration method (ADR-0107). Each car rebases onto
the latest feat tip, then is merged with `--ff-only`. This is NOT
`merge --no-ff`.**

```bash
git checkout feat/<train>
git pull --ff-only

# Fetch the finished car branch
git fetch origin car/<topic>

# Car rebases onto latest feat tip (front-loads conflict resolution)
git checkout car/<topic>
git rebase origin/feat/<train>

# Back on feat branch — ff-only merge (linear history, no merge commit)
git checkout feat/<train>
git merge --ff-only car/<topic>

# Local sanity check
YADGAR_TEST=1 .venv/bin/python -m pytest yadgar/tests/ -x --timeout=60 -q

git push origin feat/<train>
```

Conflicts surface during `rebase`, not during `merge`. Resolve inline OR
dispatch a fix agent that targets `car/<topic>` before the merge step.

Once merged, delete the car branch from origin:

```bash
git push origin --delete car/<topic>
```

### 3. Periodic master sync

Every ~3 days OR when master moves significantly (>10 commits):

```bash
git checkout feat/<train>
git pull --ff-only
git fetch origin master
git rebase origin/master
git push --force-with-lease origin feat/<train>
```

Prevents end-of-release big-bang divergence.

### 4. Final master PR

When all cars are integrated:

```bash
# Final rebase onto latest master
git fetch origin master
git rebase origin/master
git push --force-with-lease origin feat/<train>

# Open PR feat/<train> → master
gh pr create --base master --head feat/<train> --title "feat: <train>" --body "..."
```

This is the ONE point where full CI fires. After merge:
- Tag version
- Bump `nix/modules/home/yadgar.nix`
- `home-manager switch`

---

## CI on the feature branch

Three options. Recommendation: **B** for most trains.

| Mode | Description | When |
|------|-------------|------|
| A — Off | No CI on feature branch; only final PR runs CI | Small trains where regressions are unlikely |
| **B — Manual / opt-in** | CI runs only when commit message contains `[ci]` or operator clicks "Re-run" | **Default for standard trains** |
| C — Scheduled | Nightly cron run on `feat/<train>` | Long trains (>2 weeks) where regression-bisect cost matters |

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

1. **Per-car audited plan BEFORE build.** Every car must have an AUDITED-ready
   plan doc before its build is dispatched — unconditional, every car (ADR-0107).
   The audit tier may drop to a lighter check for purely-mechanical cars but is
   NEVER skipped.
2. **Stacked-rebase conflict front-loading.** Each car rebases onto the latest
   feat tip before the ff-only merge. Conflicts surface early, at merge-of-one,
   not as a big-bang at the end.
3. **ff-only merge gate.** `git merge --ff-only` on the feat branch will refuse
   if the car hasn't rebased cleanly — an automatic correctness check.
4. **Local test gate before each integration.** Run `pytest` on the feat branch
   after each car is merged. Catches integration breaks immediately.
5. **Full 3-pass audit before master PR.** Security + quality + cavecrew on
   `master..feat/<train>` diff.
6. **Periodic master rebase.** ~3 days OR significant master movement.
7. **Push car branches to origin.** Even without PR — backup + cross-machine visibility.

---

## Anti-patterns (what NOT to do)

- ❌ Dispatching a car build before its plan is AUDITED-ready
- ❌ Independent-merge (`merge --no-ff`) of parallel car branches — retired by ADR-0107
- ❌ Branching car work directly from master while a feat branch exists
- ❌ Opening intermediate PRs against master for cars destined for the feat branch
- ❌ Force-pushing the feat branch on every minor change (history matters)
- ❌ Letting the feat branch diverge >30 commits from master without rebasing
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
| Rebase cascade after merges | All cars branch off stable feat branch |
| CI burn on every intermediate push | CI only on `[ci]` commits + final PR |
| Stale-diff audits | All diffs computed vs feat-branch tip |
| Hard-to-track integration state | Single feat branch, one history |

Note: the model has since evolved further — ADR-0107 (2026-07-13) replaces
the original `merge --no-ff` integration with stacked-rebase ff-only and
adds the unconditional per-car audit gate.

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

## Branch cleanup (after cars merged, PR open)

AFTER the feat PR exists on origin (review window provided), delete each
car branch that fed it (if not already deleted post-integration):

    git push origin --delete car/<topic>

Skip the feat branch itself until merged + deployed + verified —
that branch is your rollback path.

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
