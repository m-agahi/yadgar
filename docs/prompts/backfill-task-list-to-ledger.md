# Prompt — backfill a project's task-list wiki page into the `task` ledger

**Human-invoked, one project per run. Nothing here runs automatically.** There is no
script and no admin op behind this — deliberately. A machine backfill on a live corpus
is the thing that produced duplicate rows before (see "Why no script" at the bottom).

Copy this whole file into an instance that has the project's task-list page, fill in the
three parameters, and let it work. Every STOP below is a hard stop: report and wait, do
not improvise past it.

## Parameters

| Name | Example | How to get it |
|---|---|---|
| `PROJECT_ID` | `m-agahi/yadgar` | the SessionStart banner prints it |
| `PROJECT_DIR` | `/home/max/git/yadgar` | repo root |
| `PAGE_SLUG` | `yadgar-task-list` | usually `{project-name}-task-list` |

`SLUG_PREFIX` is `PROJECT_ID` with `/` → `_` — e.g. `m-agahi_yadgar`. Used for body-page
slugs (plan D32 ③).

Every yadgar MCP call in this run passes `project="PROJECT_ID"`.

---

## Step 0 — preconditions

```
task_list(project_id=PROJECT_ID, project=PROJECT_ID, include_closed=True, limit=500)
```

Rows may legitimately be present — a session writes tasks straight to the ledger between
backfills. Task ids carry no meaning (unlike ADR ids, see the ADR procedure), so
pre-existing rows are not a blocker on their own. What you are checking for is a
**previous backfill**.

**STOP if the ledger already holds rows that correspond to the page's `## task:` sections**
— match on title, not count. That means this project was backfilled before, and re-running
would duplicate every row: nothing in the schema prevents it (`UNIQUE(project_id, body_slug)`
does not bind while `body_slug` is NULL, and it is NULL until Step 7).

Otherwise record the existing rows and carry on. In Step 6, skip any page section whose
subject already matches one of them, and say so in the report.

If this call fails with `Illegal parameter data types varchar and row` — a known defect in
the open-only status filter — **STOP**. Do not work around it with `include_closed=True`
and proceed; a precondition you cannot read is not a precondition you have checked.

Then confirm the page exists:

```
wiki_read(slug=PAGE_SLUG, directory=PROJECT_DIR, project=PROJECT_ID)
```

**STOP if it is missing** — nothing to back up, and a delete later would be a no-op on
the wrong thing.

## Step 1 — sweep the harness task list

```
TaskList
```

If the harness holds tasks that are not on the wiki page, they exist only in this
session and die with it. Append each as an extra `## task:` section to the backup file
in Step 2, marked `- source: harness`, using a `h1`, `h2`, … id so it cannot collide
with a page id. Then clear them from the harness (`TaskUpdate(taskId, status="deleted")`).

If `TaskList` is empty, skip.

## Step 2 — write the backup file

Path: `~/yadgar-private-backups/{SLUG_PREFIX}-task-list-{YYYY-MM-DD}.md`

**Outside the repo, never inside it.** A task list is personal working data — internal
hostnames, infrastructure paths, unreleased plans — and this repo is PUBLIC. `docs/prompts/backups/`
is gitignored precisely so a future run cannot put it back. Do not commit the backup,
do not move it into the tree "temporarily", do not paste its contents into a commit
message or a PR body.

Contents: the page's `content` field **verbatim**, byte for byte, with a provenance
comment prepended:

```
<!-- VERBATIM BACKUP of wiki page `{PAGE_SLUG}` (page id N, page_type=task_list,
     project_id={PROJECT_ID}, updated_at ...). Captured {DATE} before ledger
     backfill + page deletion. -->
```

Do not reformat, re-wrap, sort or "tidy" anything. This file is the only copy once
Step 12 runs.

## Step 3 — verify the file

```
grep -c '^## task:' <backup file>
```

Must equal the number of `## task:` sections in the page content you read in Step 0.
**STOP on any mismatch.**

## Step 4 — check inbound crossrefs

Find pages that link `[[PAGE_SLUG]]`. Record the list in your report. Deletion in
Step 12 leaves these dangling; the user has accepted that, but they must know which.

## Step 5 — prove the backup is durable (GATE)

The backup lives outside the repo and is therefore **not version-controlled**. That is a
deliberate privacy trade, and it costs you the safety net a commit would have given, so
verify it by hand:

```bash
ls -l  ~/yadgar-private-backups/<file>
grep -c '^## task:'  ~/yadgar-private-backups/<file>
```

**STOP unless the file exists, is non-empty, and its section count matches Step 3.**

Because there is no commit to fall back on, the ordering in this procedure is what keeps
you safe: the page is not deleted until Step 12, long after the rows are written and
verified. Never reorder deletion earlier to "save a step".

## Step 6 — pass 1: create the rows

Walk `## task:<oldid>` sections **in page order**. For each, call:

```
task_write(project_id=PROJECT_ID, project=PROJECT_ID,
           title=..., status=..., state=..., active_form=..., plan_path=...)
```

Record the returned `id` against the section's old id. That old→new map is written down
in Step 9 and is the only way the dependency edges survive — build it as you go.

**title** ← the section's `subject`.
- Cap is 200 chars, enforced server-side (raises `ValueError`). If a subject is longer,
  truncate at a word boundary and put the full text at the top of the body page.
- Strip a leading bracket prefix **only if this task will get a body page** (Step 7).
  A task with no body page keeps its prefix in the title — otherwise the information is
  simply lost. Prefixes seen in practice: `[HIGH]` `[MED]` `[LOW]` `[POST-V6]`
  `[DEFERRED]` `[client-port]` `[REVISIT — user]` `[PLANNED]` `[SPIKE]` `[DECIDE]`
  `[VERIFY]`.

**status** ← the section's `status`, one of `pending` · `in_progress` · `completed`.

**state** ← derived from the title prefix, per plan §11.1 / D36. These four map; anything
else leaves `state` at `open`:

| prefix | state |
|---|---|
| `[PLANNED]` | `planned` |
| `[SPIKE]` | `spike` |
| `[DECIDE]` | `needs_decision` |
| `[VERIFY]` | `built_unverified` |

Leave `state` unset for `completed` rows (plan §16.10 — it is cleared on completion).

**active_form** ← the section's `active_form` if present, else omit.

**plan_path** ← the single `docs/plans/…` path found in the section's `context`, if any.
The page writes this under `context:`, never under `plan_path:` — read `context:`.
If `context` holds several references, take only the plan path; the rest goes in the
body page. No plan path → omit the field.

## Step 7 — pass 2: body pages

A task gets a body page if it has **either**: a `description`, or a `context` beyond the
plan path already captured. Otherwise `body_slug` stays NULL and there is no page — do
not manufacture one.

**A title prefix is not on its own a reason to make a page.** A task whose only extra
content is its prefix keeps the prefix in the title and gets no body page. (Reading the
prefix as a trigger makes Steps 6 and 7 circular — strip the prefix only if a body page
exists, qualify for a body page because a prefix was stripped — and it would manufacture
a page whose entire content is one word.)

A page is written for its own sake; the prefix just rides along in `## Tags` when one
happens to exist.

**A `completed` task never gets a body page.** Its detail is in the backup file and in
git; writing one now costs tokens on every future read for a row that readers skip by
default (D37).

Per qualifying task, in this order — the id does not exist until the row does:

1. `wiki_add(title="task {newid}: {subject}", slug="{SLUG_PREFIX}_task-{newid}", content=..., directory=PROJECT_DIR, project=PROJECT_ID, page_type="task_body", category="reference", wait=True)`
2. `task_write(project_id=PROJECT_ID, project=PROJECT_ID, id={newid}, body_slug="{SLUG_PREFIX}_task-{newid}")`

Body content — only what does not fit a column:

```markdown
## Context
<the section's context, minus the plan path already in plan_path>

## Description
<the section's description, verbatim>

## Tags
<priority / category prefixes stripped from the title, e.g. HIGH, POST-V6>

## Provenance
Backfilled {DATE} from wiki page `{PAGE_SLUG}` section `## task:{oldid}`.
```

Omit any section that would be empty.

**Expect the wiki similarity gate to fire once or twice on a backfill this size.** A task
body that restates the decision an ADR already records scores high against it — measured
0.838 against an ADR page on the 2026-08-15 run. They are distinct by construction: the
ADR is the decision, the task body is the work item. Pass `force=True` for that page and
name it in your report. Do not pass `force=True` pre-emptively on every page.

## Step 8 — pass 3: dependencies

Only now, with every row created and the old→new map complete.

Collect edges from the page. Two fields carry them and **they point opposite ways**:

- `blockedBy: A, B` on task X → X is blocked by A and B.
- `blocks: C, D` on task X → C and D are blocked by X. **Invert it.**

Inversion is mandatory: `task_write` accepts a `blocks` argument and silently discards it
(`yadgar/backend/admin_exec/ledger.py` strips it from the column payload and nothing
reconciles it). Passing `blocks` writes nothing and reports success.

Translate every old id through the map, then write, one call per blocked task:

```
task_write(project_id=PROJECT_ID, project=PROJECT_ID, id={newid}, blocked_by=[{newids}])
```

`blocked_by` is full-replace on update — send the complete list for that task in one
call, not one call per edge.

**STOP if any referenced old id has no entry in the map** (a dangling edge on the page).
Report it; do not guess.

## Step 9 — verify, and record the map

```
task_list(project_id=PROJECT_ID, project=PROJECT_ID, include_closed=True, limit=500)
```

The count that must match is **rows you created**, not total rows:

```
total rows  −  the pre-existing rows recorded in Step 0  ==  section count from Step 3
```

**STOP on mismatch** — the page is still intact at this point, which is the whole reason
deletion comes last. Do not compare raw row count against section count: Step 0 permits
pre-existing rows, so a raw comparison fires a false hard-stop *after* every irreversible
write has already happened.

Append the old→new id map to the backup file as a final `## Backfill id map` section
(`0047 → 12` per line). Without it, nothing can ever be traced back. Same rule as Step 2 —
it goes in the file under `~/yadgar-private-backups/`, never into the repo.

## Step 10 — the `## Meta` block

The lines above the first `## task:` section are not tasks — merged-train history,
backup paths, throughput rules. **Drop them.** They are decisions or git history wearing
a task list's clothes, and neither belongs in a task ledger. The backup file from Step 2
keeps them if anyone ever wants them back.

Do not relocate them to a wiki page. Do not turn them into tasks.

## Step 11 — report before deleting

Report: rows created, body pages written, dependency edges wired, Meta lines relocated,
inbound crossrefs that will dangle. **Wait for the user.** Step 12 is irreversible.

## Step 12 — delete the page

```
wiki_delete(slug=PAGE_SLUG)
```

There is no undo. The backup file is committed (Step 5), the rows are verified (Step 9),
the map is committed (Step 9). If any of those three is not true, do not run this.

---

## Why no script

An admin op for this existed — `seed_task_from_pages` — and was deleted along with its
CLI flag, daemon route and tests. Two reasons, both worth keeping in mind while running
this by hand:

- It was **not idempotent** despite saying so in two docstrings. Dedup rode
  `UNIQUE(project_id, body_slug)`, but it inserted `body_slug=NULL` and MySQL permits
  unlimited NULLs in a unique index. A second run duplicated every row. That is what
  Step 0's stop-check exists to catch.
- It was called a *seed*. A seed targets an empty system, so its author had no reason to
  write a collision check. This is a **backfill** — a one-shot migration over a live
  corpus — and the name is what hid the bug.

It also silently dropped `description`, `context`, `blockedBy`, `blocks` and the old id,
which is most of the page's content. Steps 6-8 exist because a column-only copy loses it.
