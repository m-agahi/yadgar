# Train bug-bag-2 — 2026-08-23

**Branch:** `train/bug-bag-2-2026-08-23`
**Head of train:** Car C1 (vacuum + service polish)
**Style:** ONE train branch. Each car branches off the train, merges BACK
to the train. **One final PR at the train tip** — no per-car PRs. Single push
of the merged tip per `multi-car-train-single-push` memory.

## Car list

11 code cars + 4 doc cars = 15 PRs.

### Code cars

| Car | Title | Tasks | Files | LOC |
|-----|-------|-------|-------|-----|
| C1 | vacuum + service + CHANGELOG polish | #62, #65, #68, #85, #233 | 5 | ~80 |
| C2 | ruff noqa liveness + complexity HARD ratchet | #313, #282 | 2 | ~50 |
| C3 | setup/doctor/onboarding cheap fixes | #63, #64, #66, #324 | 4 | ~90 |
| C4 | ADR seam — id sequence, get-fields, DLQ taxonomy | #177, #247, #316, #196, #217 | 5-6 | ~120 |
| C5 | agent-prompt library + seed backfill | #200, #268, #90, #206 | 4 | ~100 |
| C6 | precommit-bump guard + code-graph protocol | #281, #291, #292 | 2 | ~60 |
| C7 | anchor / dispatch / consolidation hygiene | #288, #339, #275 | 3 | ~80 |
| C8 | registry/create-gate + project-seed column + worktree project_id | #277, #241, #274 | 3 | ~80 |
| C9 | pre-existing RED + determinism | #263, #205, #323, #330 | 3-4 | ~80-100 |
| C10 | misc mechanical — queue, memory, auto-recall | #317, #318, #319, #340 | 4 | ~80 |
| C11 | cross-cutting correctness (split) | #21, #61, #79, #88, #89, #296, #300, #276 | 3 | ~80 |

### Doc cars

| Car | Title | Tasks | Files | LOC |
|-----|-------|-------|-------|-----|
| DC1 | stop_checkpoint_prompt.md step 4 discriminating test | #337 | 1 | ~30 |
| DC2 | audit session-exit hook — 4KB vs raw save | #35 | 1 | ~80 |
| DC3 | yadgar keyword cheat-sheet (viz + docs) | #38 | 2 | ~100 |
| DC4 | corpus corrections from 2026-08-16 cost measurement | #93 | 1 | ~40 |

## Out of scope (deferred)

- **#23** (multi-client nix provisioning — DUPLICATE) — closed as duplicate
- **#41** (SUPERSEDED by #310) — marked completed
- **#51** (IMPLEMENT project identity — DECIDED) — corpus re-key verified done; closed
- **#305** (yadgar.nix:367) — nix-repo change, out of yadgar train
- **#320** (PreCompact EMPTY ARRAY) — host-only settings.json, out of repo

## Staging: Car C4 + Car C7 vs #310 E4–E9

C4 (ADR seam: id sequence, get-fields, DLQ taxonomy) and C7 (anchor/dispatch/consolidation
hygiene) touch the same seams #310 task E4–E9 is still rewiring:

- C4 touches `tools/adr.py`, `tools/adr_seed.py`, `tools/admin_dlq.py` — #310 E6 (writers)
  has 56 unwritten sites including ADR seed paths.
- C7 touches `tools/dispatch_prelude.py`, `tools/consolidate.py`, `tools/de_anchor.py`
  — #310 E4 (wiki_page seam) and E5 (checkpoint) plus #288 anchor pollution are live.

**Order:** C4 and C7 land first in this train. The follow-up train for #310 E4–E9
integrates the now-stable seams. If a #310 E4–E9 car needs to land BEFORE C4 or C7,
it supersedes this train's order; cut a hot-fix branch off master instead of wedging
into the train.

**Conflict rule:** if a future PR in this train causes a #310 car to re-touch a file
that C4 or C7 already changed, the #310 car is the one that rebases. Train cars don't
rebase onto #310 work.

## Body slug standard

Every pending task in this train gets `body_slug="m-agahi_yadgar_task-<id>"` written to
the ledger AND the corresponding wiki body page written with the per-car fix plan.
No `.md` files on disk for these plans; the wiki body is the single source of truth.
Per the read-side rule (memory `[[wiki-read-cross-project-scoping-inconsistent]]`), the
body page must carry the `m-agahi/yadgar` tag siblings carry so `wiki_append_section`
can find it.

## Pre-flight checklist (each car)

1. Read task body via `wiki_read(slug=m-agahi_yadgar_task-<id>)`.
2. Write failing test FIRST (red).
3. Implement fix (green).
4. Refactor if obvious.
5. `task_write(body_slug=...)` on the ledger row.
6. PR title: `fix(<scope>): <car-name> closes #<id>, #<id>, ...`
7. PR body has all 5 template sections.
