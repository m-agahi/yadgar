# C2 — Task 301: Restore `edited:` to `.forgejo/workflows/validate.yaml`

## Goal

Fix `validate.yaml`'s `on.pull_request.types:` list so a PR body edit
re-fires the workflow. Today the list is `opened, synchronize, reopened`
(`.forgejo/workflows/validate.yaml:6`) — `edited` is missing, so a workflow
run that completed against an EMPTY (or stale) PR body and then fails its
`check_pr_metadata.py` gate stays RED permanently. Forgejo's rerun-on-edit
behavior is gated by the `edited` type, not by Rerun-Workflow. Rerun replays
the frozen payload (the body at the time of the original trigger) — a fix
that requires changing the body itself needs a NEW trigger event, which only
`edited:` delivers.

This is a "correct state rendered as failure" defect, mirroring the Car 11
class — a missing type filter turns a green fix into a permanently red check.

## Pre-conditions

- File to edit: `/home/max/git/yadgar/.forgejo/workflows/validate.yaml`
  (47 lines total).
- The workflow's body validation step is on line 28-32 (`Validate PR title
  and body`) — it reads `${{ github.event.pull_request.body }}` and pipes to
  `python3 scripts/check_pr_metadata.py`.
- `scripts/check_pr_metadata.py` is the body validator. If a PR's body is
  empty/malformed at PR-open time, the workflow fails with the body
  validation error. Re-opening the PR (`reopened`) does not change the body
  — only `edited` does.
- The list already has `synchronize` (push), `reopened` (re-open), and
  `opened` (initial open). The missing type is `edited` (body edit without
  push).
- Car 11 (mirror class): the previously fixed defect was a required check
  rendering correct state as failure due to a stale trigger condition. Same
  shape, different surface — the fix here is the same shape: re-add the
  missing trigger condition.

## Step-by-step

1. **Open `/home/max/git/yadgar/.forgejo/workflows/validate.yaml`**.

2. **Edit line 6** to add `edited` to the `types:` list. After:
   ```yaml
   on:
     pull_request:
       branches: [master]
       types: [opened, synchronize, reopened, edited]
   ```
   - Position rationale: `edited` is conventionally placed after `reopened`
     in Forgejo / GitHub Actions trigger lists (matches the published
     Actions schema docs ordering: `opened, edited, synchronize, reopened,
     closed, ...`).
   - Indentation matches the existing two-space nested indent under
     `pull_request:`.

3. **No other change to this file**:
   - The `concurrency:` block (lines 9-11) is fine: `cancel-in-progress:
     true` correctly cancels the prior red run when the new `edited` event
     fires.
   - The `jobs.validate` block (lines 14-46) needs no edit — it already
     reads `pull_request.body` correctly on every trigger.

4. **Confirm `scripts/check_pr_metadata.py` is idempotent under rerun**:
   - The script is invoked as `python3 scripts/check_pr_metadata.py` on
     line 32. It must produce the same exit code for the same body content
     regardless of how many times it has been invoked. If it has any
     once-only side effects (e.g. writing to a state file), the `edited`
     rerun could compound them. Read the script top to confirm; if any
     state writes exist, this car does NOT touch them — flag for a
     follow-up.

5. **Sanity test on a real PR**:
   - Open a throwaway PR with an empty body → validate run fails (red).
   - Edit the PR body to a valid template → `edited` event fires, new
     validate run completes (green). Check the PR checks panel for a new
     run entry timestamped to the edit.
   - Click "Rerun" on the green run → second green run, body unchanged.
     This proves Rerun still works for the standard use case (re-running
     a green run to refresh a flaky job) — the `edited` fix is additive,
     not a replacement for Rerun.

## Verification

- `cat /home/max/git/yadgar/.forgejo/workflows/validate.yaml` line 6 reads
  `types: [opened, synchronize, reopened, edited]`.
- A PR with a body fix produces a new workflow run entry with the
  `pull_request` event of type `edited` (visible in the run header in
  Forgejo Actions UI).
- The previously-failed run is cancelled by the `concurrency:
  cancel-in-progress: true` block — the PR's check status transitions
  from RED → yellow (cancelled) → GREEN on the new run.
- Forgejo's "Required" status check for `validate` (if configured at repo
  level) reflects green on the new run.
- No new runs are produced for pushes that don't change the body — the
  `synchronize` type still triggers on pushes, but those pushes already
  carry an `edited` payload only if the body changed during the push
  (which it doesn't on `git push`). Verify by pushing to an open PR
  with `gh pr edit --body "..."` left untouched: expect NO new validate
  run beyond the `synchronize` one.

## Risks / rollback

- **Spurious reruns on cosmetic edits**: every body edit (even trivial
  whitespace) triggers a new validate run. With `concurrency:
  cancel-in-progress: true` (line 11), the prior run is cancelled, so the
  wallclock cost is bounded to one active run. Net cost: ~1 extra cold-
  cache run per real body edit.
- **PR-author notification spam**: Forgejo sends a notification per
  workflow run. For an author who edits the body multiple times during a
  review cycle, the volume goes up by one notification per edit. Worth
  flagging to the user if they care.
- **Webhook rate-limit pressure**: `edited` events fire on every body save
  in the Forgejo web editor. A user typing-then-saving repeatedly can
  generate 5-10 `edited` events per minute. Mitigated by `concurrency:
  cancel-in-progress: true`.
- **Rollback**: revert the one-line `types:` change. Trivially safe.
- **Car 11 parallel**: this fix has the same shape (restore missing
  trigger). If Car 11's fix has unforeseen consequences, this one likely
  does too — pair-review recommended.

## Approx LOC + risk class

- LOC: +1 token (`edited` appended to the `types:` list).
- Risk class: **low** (one-line config change, fully reversible,
  concurrency block absorbs cost).
- Time cost: <5 min for the edit + one throwaway PR to verify.

## Source evidence

- `/home/max/git/yadgar/.forgejo/workflows/validate.yaml:5-6` — the
  `on.pull_request` block. Line 6 is the `types:` list that this car
  edits.
- `/home/max/git/yadgar/.forgejo/workflows/validate.yaml:9-11` —
  `concurrency:` block (already correct; absorbs the extra `edited`
  triggers).
- `/home/max/git/yadgar/.forgejo/workflows/validate.yaml:17` —
  `container.image: python:3.14-slim` (unaffected).
- `/home/max/git/yadgar/.forgejo/workflows/validate.yaml:28-32` — the
  `Validate PR title and body` step that reads
  `github.event.pull_request.body`. This is the consumer of the trigger
  fix; it was already correct, only the trigger was wrong.
- `/home/max/git/yadgar/.forgejo/workflows/validate.yaml:46` — the
  `Run pre-commit` step. Unaffected by this car; pre-commit runs are
  triggered by `synchronize` (push) and re-trigger correctly.
- `/home/max/git/yadgar/scripts/check_pr_metadata.py` — body validator
  referenced on line 32. Read top-to-bottom for idempotency before
  flagging as the only blocker; not edited by this car.
- Car 11 reference (from memory `completed-notification-is-not-agent-finished.md`):
  same defect class, different trigger surface. Pattern parallel.
