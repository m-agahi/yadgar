# Task-list restore cost: root-cause fix

Status: DRAFT — not approved. Seven review items still open (see bottom).

## Context

Restoring the task list into a session costs ~22.3k input tokens (`task_list` 12,786 + `restore()` 9,563, measured) plus 16,705 output tokens if the session mirrors the ledger into the harness task list. The MariaDB ledger migration was expected to remove this cost. It did not, and the storage engine is not the reason.

Measured 2026-08-16 on the live corpus (79 open tasks): `task_list` returns 24,889 chars for 79 rows — **315 chars/row**, where `id + title + status` is ~90. Real tokenisation density for these JSON payloads is 2.0-2.3 chars/token, not the chars/3.6 rule of thumb.

### Defect 1 — no projection (the main cost)

`TASK_COLUMNS` (`ledger_columns.py:45-48`) is a fixed 11-column string hard-coded into all three task readers:

```
id, project_id, title, status, state, active_form,
plan_path, body_slug, completed_at, created_at, updated_at
```

There is no projection mechanism anywhere — no caller-supplied column list, no `fields=` parameter. When listing tasks to sync into the harness, only `id`, `title` and `status` are used. `project_id` is constant across the whole result, and `plan_path` / `body_slug` / `state` / `active_form` / the three timestamps are never read from a list result.

### Defect 2 — the L10 ROW-constructor bug

`mariadb.py:365` writes `" AND status IN (:status)"`, then `:372` applies `bindparam("status", expanding=True)`. The expanding bindparam emits its own parenthesised placeholder list when it compiles, so the literal parens in the SQL text produce a double wrap. Confirmed live — MariaDB echoed its own statement back:

```
SELECT id, project_id, title, status, state, active_form, plan_path,
       body_slug, completed_at, created_at, updated_at
FROM task WHERE project_id = %s AND status IN ((%s, %s)) ORDER BY id ASC
parameters: ('m-agahi/yadgar', 'pending', 'in_progress')
(asyncmy.errors.OperationalError) (4078, "Illegal parameter data types varchar and row for operation '='")
```

`(a, b)` is a row constructor in MariaDB, so the clause becomes `status = ROW('pending','in_progress')`. One status works because `(x)` ≡ `x`. Fix is deleting two characters: `AND status IN :status`. Duplicated at `:397`/`:404` in `list_task_rows_all_projects`.

Severity note: `_resolve_list_status` (`task.py:393-403`) correctly defaults to `("pending", "in_progress")` — that default is right and is not a defect. It does mean the breakage is not opt-in: the bare `task_list(project_id=...)` call fails without the caller passing anything unusual.

### Defect 3 — the regression test locks the bug in

`test_task_list_bindparam.py:71-80` and `:111-114` assert `"status IN (:status)" in src` — the buggy literal is asserted as a *requirement*. The test string-matches source via `ast.unparse` and never compiles or executes the statement, so it passed for the bug's entire life. Any correct fix fails it.

### Defect 4 — `limit`/`offset` are decorative

`task.py:478-481` adds them to the payload only when non-default; `ledger.py:83-86` never reads them; `mariadb.py:340-345` accepts no such kwargs and emits no `LIMIT` clause. Confirmed live: `limit=5` returned all 77 pending rows.

This is a truthfulness defect, not a cost defect. The intended behaviour for `task_list` is to return **all** open tasks — the caller listing tasks needs the complete set. Paging is not the fix and must not be used as one.

### The interlock — why ordering is not negotiable

`_task_list_restore_nudge` (`http.py:1260`, prepended at `:1533`) fires on every non-compact SessionStart and renders:

```
[yadgar] ACTION REQUIRED — restore your task list BEFORE any other work.
{N} open task(s) … Call TaskCreate for EACH one now:
```

It queries `status=["pending", "in_progress"]` (`http.py:1309-1313`), hits Defect 2, and fails open at `http.py:1314-1316` to `rows=[]`. **The nudge is dead by exception, not by design.** Sessions currently look clean because an error is being swallowed.

Fixing Defect 2 in isolation therefore resurrects the nudge and makes every future session mirror all 79 open tasks — the 16,705-output-token operation, automatically, on every start. Defect 2 is presently queued as a standalone fix (ledger task 10). **It must not ship alone.**

Intended outcome: session-start task cost ~300 tokens; a full 79-row `task_list` ~3k instead of 12.8k; no instruction anywhere telling a session to mirror the whole ledger.

## Not in scope

- The harness `<system-reminder>` that re-injects the whole harness task list (~4k, observed 4x in one session). Claude Code behaviour, not this repo. The only lever is keeping the harness list small, which the user has declined.
- `restore()`'s 9,563 tokens. Verified to carry zero task-ledger content (`checkpoint_restore.py:549`) — it reads checkpoint, anchors, hot memories, SR predictions, gaps. Separate lever, ledger task 49.
- `project_brief`. Verified to carry no task rows in any of its four modes.

## Cars

### Car A — column projection (the fix)

- `ledger_columns.py:45-48`: add `TASK_COLUMNS_SUMMARY = "id, title, status"` alongside `TASK_COLUMNS`. Do not modify `TASK_COLUMNS` — `task_get` and `task_write` need the full set.
- `mariadb.py` `list_task_rows` and `list_task_rows_all_projects`: accept `summary`, defaulting to **`False` (full)** — CORRECTED 2026-08-16 during build. An earlier draft of this plan said `summary: bool = True` at storage. That would have broken the nightly sweep, which calls both readers with no `summary` kwarg (`nightly_sweep.py:377, 426, 508, 512`) and reads `body_slug`, `completed_at` and `project_id` off the results — leaving it archiving nothing while reporting success, the exact silent degradation `ledger_columns`' docstring exists to prevent. Lean-by-default lives **only at the MCP surface**: `task_list` sends `"summary": not verbose` on every call, and no `nightly_sweep` call site needed editing.
- Thread through `ledger.py:76-90` and `task.py:406-501`. Expose on the MCP tool as `verbose: bool = False`; `verbose=True` restores the 11-column shape.
- `task_get` unchanged — the single-row read stays full.
- Row count is **not** capped. `task_list` returns every open task; only the width changes.
- **Second consumer, not Python.** `yadgar/core/hooks/templates/stop_checkpoint_prompt.md:133-156` is a prompt template — it instructs the *model* to call `task_list` at every session-end checkpoint, then reads `updated_at` (`:134`, plus a 14-day staleness guard at `:148`) and `state` / `active_form` for merge write-back (`:142`, `:152-156`). The docstring's "one production caller" claim is false; this consumer fires more often than the SessionStart nudge.
- **It does not need `verbose=True`.** `task_write` is already partial-update — `status`, `state`, `active_form`, `plan_path` and `body_slug` are left unchanged when passed `None`. An instance updating a task it worked on therefore never needs to read `state`/`active_form` in order to preserve them; the storage layer preserves them. The merge-write-back at `:142`/`:152-156` duplicates work the backend already does. The 3-column default serves this consumer once that redundant step is removed (Car C).
- **The `updated_at` 14-day guard at `:148` stays — RESOLVED 2026-08-16.** Its rationale was never promoted to an ADR; it lives in `docs/plans/archive/task-list-mirror-2026-07-14.md` (shipped in `da94237e`, 2026-07-14). It does NOT guard against adopting completed tasks — the status filter does that. It guards the case the status filter *cannot* see: a task that was finished but never marked completed, i.e. a row that simply went quiet. `updated_at` is the only last-touched signal in the row set; `created_at` measures age-since-creation, which the sibling 90-day retirement clock (`ledger_columns.py:69-93`) already documents as the wrong signal for this. There is no code-side backstop — the prompt text is the entire implementation, so removing the read removes the guard.
- **It fires in exactly one branch**: harness list empty AND ledger has rows (catch-up sync). That branch calls `task_list` with `verbose=True`; every other path keeps the 3-column default. `task.updated_at` is engine-auto-bumped (`server_default=CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP`), so the "automatic and can't be forgotten" premise the guard relies on still holds post-ledger-migration.
- Note: a reliable mechanical seeder largely retires this branch — if SessionStart seeds, the harness is never empty at session end, so catch-up only covers a concurrent session's work. Do not delete the branch in this train; it is the fallback when seeding is guarded off.
- `yadgar/tests/hooks/test_stop_hook_template.py` pins the guard: `test_task_list_mirror_step_present` asserts on the "14 days" wording and "never adopt a completed task", and `_EXPECTED_TEMPLATE` is a verbatim byte-pin that needs re-syncing on any edit to this template.
- Do **not** modify the shared `TASK_COLUMNS` constant. `get_task_row` and the nightly sweep both depend on it — the sweep reads `body_slug`, `completed_at` and `project_id`, and its own docstring warns the failure mode is "archives nothing, reports success". Add `TASK_COLUMNS_SUMMARY` alongside; never narrow the shared one.
- Note: no shared row-serialisation helper exists — `dict(row._mapping)` is inlined at `mariadb.py:375`, `:407`, `:418`. Projection changes the SELECT so no helper is needed, but one edit does not cover all readers.

### Car B — L10 SQL fix + a test that actually tests

- `mariadb.py:365`: `" AND status IN (:status)"` → `" AND status IN :status"`. Same at `:397`.
- `test_task_list_bindparam.py:71-80`, `:111-114`: delete the assertions requiring the buggy literal. Replace with a **live test that executes the statement against real MariaDB with two statuses** — it either returns rows or throws 4078, and cannot be fooled. Do not substitute a compiled-SQL string assertion: with `expanding=True` SQLAlchemy defers rendering the placeholder list until bind values are supplied, so compiling without values may inspect an artifact that never reaches the database — the same class of error as the test being replaced.
- Also fix the CI visibility gap in this train (ledger task 77): the skip-gate is blind to DESELECTED tests, so the engine-#2 live suite has never run in CI. Without this, the new live test is as invisible as the vacuous one it replaces — green locally, never executed on a PR. Scope note: this pulls task 77 into the train.
- Record the old test on the existing wiki page `The vacuous-pass family: guards in this repo that reported OK while checking nothing`.

### Car C — make the mirror free (mechanical harness seeder)

The car that stops Car B from regressing the session. Must land with or before Car B.

**Requirement, restated 2026-08-16:** the harness task list MUST be populated — without it there is no visible list of what to work on. The mirror is not the problem, its *cost* is. Today it costs 16,705 output tokens because each `TaskCreate` carried a subject, a description, an `activeForm` and a metadata blob. None of that was required.

Primary mechanism — **mechanical seeder**. Promotes ledger task 20, held since ADR-0137 as "fallback if forcing-nudge B fails". B has been failing by exception this entire time, so that condition is met.

- The SessionStart hook writes `~/.claude/tasks/<session_id>/<N>.json` directly. Zero model tokens; the list is present when the session opens.
- On-disk schema (verified 2026-07-17, anchored memory 532566): `{"id", "subject", "description", "activeForm", "status" (pending|in_progress|completed), "blocks": [], "blockedBy": []}`, plus a `.lock` file in the directory. The hook already receives `session_id` and `cwd` on stdin.
- Must `mkdir -p` (the harness creates the dir lazily on first `TaskCreate`), write atomically via temp+rename, respect `.lock`, and dedupe idempotently.
- **Subject format: `41: title`** — bare number, no `[L]` wrapper, and it MUST be the exact ledger SQL id. Never an instance-chosen or re-sequenced number.
- **Id scheme — RESOLVED empirically 2026-08-16, seeding is safe.** Write `41.json` with `id: "41"` so the ledger id *is* the harness id; no prefix needed at all. Measured: allocation does not reuse gaps (created 80, deleted it — the file is physically removed — created again and got 81). `.highwatermark` is written on DELETE, never on create; it appeared with value `80` at the second task 80 was deleted, which is why it trails max id in older dirs. Allocation behaves as `max(file ids, .highwatermark) + 1`, existing so that deleting the top task cannot cause id reuse. A dir seeded with ledger ids 10..92 therefore yields 93 on the next `TaskCreate`. Writing `.highwatermark = max seeded id` is optional belt-and-braces. `.lock` is a 0-byte sentinel — presence is the signal, no PID or flock bytes. The schema tolerates sparse keys (`activeForm` absent entirely in some files) and persists a custom `metadata` object verbatim. Full detail in anchored memory 532566, updated with these measurements.
- Format is undocumented and internal, so version-fragile. Guard on schema mismatch and fall back to the lean nudge.

**Direction split — decided 2026-08-16, and the reason is concurrency.**

Downstream (ledger → harness) is mechanical. The harness task dir is per-session, so seeding into it is isolated and cannot collide.

Upstream (harness → ledger) stays **model-mediated** and must not be mechanised. The ledger is shared across concurrently running instances. A mechanical differ reading this session's files would see another instance's ledger changes as local divergence and clobber them. Only the instance knows *which tasks it actually touched*, and that knowledge is what makes the upstream write safe.

Stop side — rewrite `stop_checkpoint_prompt.md:133-156` (it is a prompt template; the instructions in it are the deliverable):

- Call `task_list(project_id=..., status=["pending","in_progress"])` — 3 columns, cheap.
- Diff against the harness `TaskList`.
- For each task whose status *this instance* changed: `task_write(project_id, id, title, status)`. Send only what changed — omitted fields are left unchanged by the backend, so do not read or re-send `state`, `active_form`, `plan_path`, `body_slug`.
- For each task created in the harness this session: `task_write(project_id, title, status)` with no id; the returned id is the ledger id.
- **Never write a row that changed in the ledger but not in this instance's harness list** — that is another instance's work.
- Remove the merge-write-back at `:142`/`:152-156`: it re-sends fields the backend already preserves.
- Resolve the `:148` staleness guard's purpose before touching `updated_at`.

Stop side, second arm — **task context maintenance**. Status sync alone is not enough: a task row is a pointer, and its substance lives in the wiki body page named by `body_slug`. If the row moves and the body does not, the context rots. Live evidence: 57 of 79 open rows carry a `body_slug`, 22 carry none, and nothing in the current prompt ever revisits either that or `plan_path`. Add instructions covering, for tasks the instance actually worked on:

- Body page exists → append what changed the approach (root cause, decision, dead end, new dependency) via `wiki_append_section`. Append, never rewrite.
- `body_slug` is null and the task now has real substance → create the page (`{project_id with / as _}_task-{id}`) and set `body_slug`.
- A design doc was written for the task → set `plan_path`.
- The doc at `plan_path` was deleted, superseded, or no longer governs → clear it. Never leave a path pointing at a file that is not the plan.
- Title no longer describes the task → update it.
- Only tasks the instance touched. Do not audit rows it did not work on.

**Blocker for the "plan dropped" instruction — CONFIRMED 2026-08-16, needs a code fix in this train.** `plan_path` and `body_slug` cannot be cleared today:

- `plan_path=None` → `_build_update_payload` (`task.py:226-229`) drops the key; the column never reaches the SET clause and keeps its old value.
- `plan_path=""` → writes the literal empty string, not `NULL`.
- `body_slug=""` is actively unsafe: `uq_task_project_body_slug` (migration `002_ledger_tables.py:199`) permits repeated `NULL`s but `''` is a real value, so the second task cleared this way in the same project violates the unique index.

Both columns are nullable (`002_ledger_tables.py:187-199`) and `update_task_row` (`mariadb.py:421-459`) is already null-transparent — an explicit `None` reaching it emits `SET plan_path = NULL` correctly. The whole gap is the `if plan_path is not None:` guard, which cannot distinguish "caller didn't mention this field" from "caller wants it cleared".

Precedent for the fix is in the same function: `task.py:218-221` already writes `payload["state"] = None` unconditionally on a completed/archived transition, forcing a NULL through this exact stack. Sibling convention elsewhere: `wiki_set_mutability` / `wiki_set_metadata` (`wiki.py:1362-1460`) are single-field setters where `None` unambiguously *is* the new value; `wiki.py:342-346` documents this explicit-null-vs-omitted bug class directly.

**DECIDED 2026-08-16 — boolean clear flags, extending the existing block.** Add `clear_plan_path: bool = False` and `clear_body_slug: bool = False` to `task_write`, handled in `_build_update_payload`:

```python
if clear_plan_path:
    payload["plan_path"] = None
elif plan_path is not None:
    payload["plan_path"] = plan_path
```

Same shape for `body_slug`. No new MCP tools — the surface is already large enough that ledger task 76 wants it classified. Storage needs no change; it is already null-transparent. Reject the combination `clear_plan_path=True` with a non-None `plan_path` rather than silently picking one. Cover with a test that clears a populated `plan_path` and asserts the stored value is `NULL`, not `''`.

Fallback mechanism for the downstream direction — **lean nudge**:

- `http.py:1228-1256` `_format_task_list_nudge_rows`: keep the nudge, strip the cost. Instruct `TaskCreate(subject="{id}: {title}")` and nothing else — no description, no `activeForm`, no metadata. ~80 chars per call instead of ~400, roughly 3k output instead of 16.7k.
- Fix the id fallback: `_row.get("number") or _row.get("id") or "?"` renders a missing id as `?`. `number` is a dead fallback — no such column. Drop it and fail loudly rather than emitting an id that cannot reconcile.

Corpus:

- Supersede ADR-0137 (forcing nudge) and ADR-0127 (inline capped task list) with an ADR recording the ledger-era contract: the harness list is seeded mechanically; the model does not hand-mirror.
- Rewrite memory 532567 ("mirror them into the harness via TaskCreate FIRST THING before other work", `is_protected`, `useful_count` 84). Keep the requirement, change the mechanism — the seeder populates the list; a model that hand-mirrors uses bare `{id}: {title}` subjects only.
- `_task_list_legacy_wiki_nudge` (`http.py:1137-1217`) is unreachable — the `ImportError` guard at `http.py:1300-1305` never trips. Leave it; its docstring documents it as a rollback path.

### Car D — make `limit`/`offset` honest (demoted, optional)

Not load-bearing once Car A lands, since `task_list` returns the full open set by design. Kept only because the docstring currently claims a cap that does not exist and `_task_list_restore_nudge` fetches believing it is capped.

- `mariadb.py:339-375`: accept `limit` / `offset`, append `LIMIT`/`OFFSET` when set.
- `ledger.py:76-90`: read and forward them.
- `task.py:478-481`: always forward.
- Precedent: `list_agent_pattern_rows_uses_desc` (`ledger.py:264-281`) already threads a real `limit` through.
- Alternative if dropped: delete the `limit`/`offset` parameters and the docstring's claims instead, so the API stops lying.

### ~~Car E — corpus correction~~ — REMOVED, now ledger task 93

Split out 2026-08-16: these are MCP writes, not git changes, so they cannot be in the PR and must not gate the train. Filed as ledger task 93:

- Task 49's body (`m-agahi_yadgar_task-49`) says session-start cost "scales with N OPEN TASKS, not page size". Measurement contradicts this: the old nudge was capped at ~12 and near-flat; the expensive artifact was the full page read, driven by page bytes.
- Record the measured tokenisation density: 2.0-2.3 chars/token for this repo's JSON tool results.

## Verification

1. `task_list(project_id="m-agahi/yadgar", status=["pending","in_progress"])` returns rows instead of throwing 4078. This is the single call that fails today.
2. Same call, count response chars: **≥60% reduction from 24,889**. Then `verbose=True` returns the 11-column shape unchanged.
3. Row count is unchanged — all open tasks still returned, nothing truncated.
4. Restart the daemon, open a fresh session, read the SessionStart block. Must contain an open-task count and the `in_progress` rows; must NOT contain "Call TaskCreate for EACH one now".
5. `pytest yadgar/tests/core/test_task_list_bindparam.py yadgar/tests/core/test_task_list_restore_nudge.py`. Note `test_task_list_restore_nudge.py:110-127` pins that the primary path must not hit the legacy wiki parser — Car C must not break that.
6. Full suite + ruff + mypy before the PR. Use `env -u FORCE_COLOR` on commit/push (mypy colourises captured output and the type-ratchet hook reports "COULD NOT RUN").

## Risks

- **Car B without Car C is a regression, not a fix.** If split, Car C ships first or together, never after.
- `verbose=False` default changes the `task_list` response shape. Audit callers before merging.
- Backend and core both change, so both images bump. Do not apply nix until CI publishes, or the daemon crashloops on `manifest unknown`.

## Open review items (not applied)

Raised by advisor review 2026-08-16, awaiting per-item decision:

1. ~~Fold empirical proof into Context~~ — done above. The "broken by default" framing was wrong and has been recast as a severity note.
2. ~~Car B's replacement test form~~ — DECIDED 2026-08-16: live two-status execution, and fix the L77 CI-visibility gap in this same train. Folded into Car B above.
3. Car C: read `test_task_list_restore_nudge.py` in full and pull its ADR-0137 wording assertions into Car C's scope before starting.
4. ~~`verbose=False` default / caller audit~~ — DONE 2026-08-16. Grep complete. Decision: 3-column default (`id`, `title`, `status`, ~68% reduction) **and** update `stop_checkpoint_prompt.md` to call with `verbose=True`. Folded into Car A above. No consumer anywhere reads `plan_path` or `created_at` off a list result; no JS/TS consumer exists; no CLI caller outside tests.
5. ~~Verification gate too tight~~ — changed to a ratio above.
6. Car E is MCP writes, not code — split out of the PR.
7. Branch off master after PR #46 merges. Nix is already bumped locally to core 5.183.3 / backend 5.75.2, unpushed; this train bumps again and they would collide.
