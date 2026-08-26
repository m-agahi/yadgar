# Train bug-bag-2 — 2026-08-23

**Branch:** `train/bug-bag-2-2026-08-23`
**Head of train (as of 2026-08-26):** Car N, commit `650ba791` ("Merge train
tip 391c9c9f into car/N-bookmark-cascade"). Superseded the original "Head of
train: Car C1" line below — see "Second wave" for everything that landed
after the C1–C11 + DC1–DC4 wave this doc originally tracked. Car
`O-version-and-contract` is in flight on its own branch, not yet merged into
the train.
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

## Second wave (undocumented until now — cars A through N)

The C1–C11 + DC1–DC4 wave above landed on `origin/master` through its own PR
(#68) before this branch's own history shows it: merge commit `16c895b3`
("train: merge origin/master — C1-C10 #65 + C5 #67 + plan-archive #66,
resolves CONFLICTING on PR #68") is where C1, C5, C6, C9, and C10 enter this
branch's log — they never appear as standalone commits here because they
arrived pre-merged from master. C2, C3, C4, C7a, C7b, C7c, C7c-revert-1,
C8-3, and the two C11 splits (#88, #89, plus a later C11-cross-cutting) do
appear as individual commits on this branch, each wrapped in its own
`train: merge car ...` commit.

After `16c895b3`, a second, previously undocumented wave of lettered cars
landed directly on this branch (oldest → newest; task/ledger IDs are the
ones the commit messages name — not verified against the ledger beyond
that):

| Car | Commit(s) | Title | Task/ledger IDs |
|-----|-----------|-------|------------------|
| A | `302cd661` | cascade wiki_delete to wiki_bookmark | #341 |
| B | `35fb0676`, `05492d44`, `3d61490b` | landscape-mode tags filter; seed_adr_tier_subsystem D20 routing; chokepoint excludes information_schema | #204, #202, #201 |
| — | `35d139c4` | bump backend_version 5.84.0 → 5.84.1, closing car A + car B-tags + a master merge | — |
| C | `e09c6167` (test-only), `c291bb28` (source) | docstring corpus numbers + admin_exec retype + noqa cleanup | #345, #346 |
| — | `1d0ea125` | ci-followup: SECTION_TO_CATEGORY self-map for 'ops' | #354 (closes #356 as a side effect) |
| — | `009afe39` | ci-followup: alembic 005 migration filename/revision shortened to fit version_num(32) | #357 (closes #358) |
| — | `97cd54cb` | c10g-followup: restore() SR bucket accepts legacy directory_context paths | #308 |
| E | `70c92e85` | drain-local docstring describes defensive flag accurately | #342 |
| D | `21a927c8` | verify-hooks execution probe: run each hook, classify hang/crash/binary-missing, flip ok on failure | #322 |
| F | `31a8f597` | task-296 NULL-OUT floor gets unit + integration coverage | #296 |
| G | `b1a493be` | train-tip CI pass: stdlib-only errors module, sentinel rename, recent-memories fixture patch-path fix | (no task ID in commit message) |
| — | `9c7d7aec` | empty commit, message "test-empty", zero files changed | — |
| H | `c5df33fc` | ledger_columns docstring refresh, pre-compact async, validate.yml edited trigger | #345, #36, #301 |
| I | `0d62d7c8`, `6cec7f75` | retire two C10 #319 sibling swallows in adr.py; ship dispatch_prelude marker audit script | #346, #347 |
| J | `1c50c24a` | backend-startup backoff cap holds at poll_max_sec; AdminRefusal class identity restored (car G had inlined a copy) | #367, #368 |
| K | `ab417b6e` | test_abstract_empty_cluster assertion — see this train's Car O for the ADR-0430 follow-up | #376 |
| L | `0310af47` | audit marker only — task #94 resync already shipped, no code change | #94, #296 |
| M | `391c9c9f` | cross-project gate wired into 10 page_id-keyed + 2 slug-keyed wiki write tools | #50, #364 |
| N | `4ed9f107` | wiki_delete cascades to wiki_bookmark + lint surfaces orphans | #341 |

Notes on the table above:

- **Car A and Car N both cite task #341** ("wiki_delete cascades to
  wiki_bookmark"). They touch different modules — car A edits
  `yadgar/_shared/storage/wiki.py`; car N edits `yadgar/_shared/wiki/store.py`,
  `yadgar/_shared/storage/bookmarks.py`, and `yadgar/core/server/tools/wiki.py`
  — but the relationship between the two (car N supersedes car A, extends it,
  or the module layout changed underneath both) has not been verified here.
  Flagged, not resolved.
- Rows without a car letter (`35d139c4`, the two `ci-followup` commits,
  `c10g-followup`) are follow-up fixes that never got a car letter of their
  own in their commit messages — listed for completeness, not renamed into
  the lettered scheme.
- `9c7d7aec` changed no files; it is not a car.
- Car O (`car/O-version-and-contract`, this branch) is not in the table above
  — it is not merged into the train.

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
