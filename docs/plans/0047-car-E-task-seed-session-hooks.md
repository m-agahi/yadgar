# Car E — task seed + SessionStart/stop-hook rewire + http.py matcher + D11 prefix

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: D (task tools — `task_write`/`task_list`/`task_get` + `_LedgerMixin.create_task_row`/`list_task_rows`)
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Per §7 row E (verbatim): "task seed + SessionStart/stop-hook rewire + `http.py:923` matcher + D11 prefix instruction". This car does four things, all after Car D's task tools and `task` ledger table have merged:

1. **Task seed** (D35a/D35b/D35c) — a one-shot admin op that reads all the `page_type='task_list'` wiki pages, parses each `## task:<id>` section, and inserts rows into the `task` ledger table per `project_id`. Idempotent; ships with an exact-equality verification gate (per-page `## task:<id>` section count must equal seeded rows per project). NOT a migration step. The inserted rows will have different id than the source. this is expected. we are abandoning the tracking of ids in favor of using built in row ids.
2. **SessionStart rewire** — `_task_list_restore_nudge` (`yadgar/core/server/http.py:829`) stops parsing the `{project}-task-list` wiki page and instead reads open tasks from the `task` ledger (via `task_list` / `list_task_rows`, D37 open-only default). The hook script `session-start-context.py` itself is unchanged (it already just calls `/hooks/session-context`); the rewire is server-side in http.py.
3. **`http.py:923` matcher** — the `_TASK_RE` regex (`^## task:(\d+)`, actually at `http.py:885` — see §9 drift) must change to accept Crockford base32 ids (D10) and an optional origin segment (D11). Forced by D10 regardless of the origin drop (§14.1).
4. **D11 prefix instruction** — the restore-nudge template (`http.py:920-933`) already emits `[{tid}]` but never instructs the model to *preserve* that prefix in the `TaskCreate` subject. Add that instruction.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/core/server/http.py:829` | `_task_list_restore_nudge` — rewire to read from `task` ledger instead of parsing wiki page; update `_TASK_RE` (http.py:885); add D11 prefix-preserve instruction to nudge template (http.py:920-933) | http.py:829 `async def _task_list_restore_nudge`, :885 `_TASK_RE`, :904 `_CAP=12`, :926 emits `[{_tid}]`, :1105 call site hoisted FIRST |
| `yadgar/core/hooks/session-start-context.py` | NO change (already calls `/hooks/session-context?directory=<cwd>`; rewire is server-side) | session-start-context.py:55 `_url = f"http://127.0.0.1:{_port}/hooks/session-context?..."` |
| `yadgar/core/hooks/templates/stop_checkpoint_prompt.md:105-181` | Rewire step 5 (TASK-LIST MIRROR): replace `wiki_write_task_list` with the new task tools (`task_list` to read, `task_write` to persist). The page-format schema block and `## task:<id>` parsing instructions are replaced by ledger calls. | stop_checkpoint_prompt.md:122 `wiki_write_task_list(project="{project}", ...)`, :137 same, :156-181 SCHEMA block |
| `yadgar/backend/admin_exec/seed_ledger.py` | ADD `seed_task_from_pages` (mirror of existing `seed_adr_from_pages` at seed_ledger.py:44). Parse the 6 `page_type='task_list'` wiki pages, insert rows into the `task` table per project. | seed_ledger.py:44 `def seed_adr_from_pages` exists; NO task seed function present (verified on `feat/spine-knob-mariadb`) |
| `yadgar/core/cli/seed.py` | [VERIFY: expose `seed-task-from-pages` as a CLI admin op, parallel to the ADR seed CLI. Car D/PR #32 added the ADR seed CLI path — confirm exact registration site] | seed.py exists on feature branch; exact task-seed CLI registration unverified on current branch |
| `yadgar/core/server/tools/task.py` | NO change in this car (Car D ships it). Car E consumes `task_list` (task.py:125, open-only D37) and `task_write` (task.py:81) from the SessionStart/stop-hook paths. | task.py:81 `def task_write`, :125 `def task_list`, :146 `def task_get`, :32 `_format_task_id` (verified on `feat/spine-knob-mariadb`) |
| `yadgar/_shared/storage/ledger.py:243,286` | NO change in this car (Car D ships). Car E's seed calls `create_task_row` (ledger.py:243); SessionStart reads via `list_task_rows` (ledger.py:286). | ledger.py:243 `def create_task_row`, :286 `def list_task_rows`, :318 `list_task_rows_all_projects`, :361 `get_task_row` (verified on `feat/spine-knob-mariadb`) |

## 3. Functions / symbols

### 3.1 Modify — `_task_list_restore_nudge` (`yadgar/core/server/http.py:829`)

Current signature (verified):
```python
async def _task_list_restore_nudge(directory: str) -> str:
```
Current body (http.py:855-942) reads the `{project}-task-list` wiki page via `storage.get_wiki_page_by_slug_directory` (http.py:860-863), then parses `## task:<id>` sections with `_TASK_RE = _re.compile(r"^## task:(\d+)", _re.MULTILINE)` (http.py:885) and inlines up to `_CAP = 12` (http.py:904) open tasks in a forcing nudge (http.py:920-933).

After rewire:
- Replace the wiki-page read + parse with a ledger read: call `list_task_rows(project_id=<derived>, status=["pending","in_progress"])` (D37 open-only) via the storage layer / HTTP forward to backend (§15 read path: core PTC → backend PTC → DB).
- Keep the forcing-nudge form (ADR-0137 Option B — imperative, hoisted FIRST at http.py:1105) and the `_CAP = 12` cap.
- Emit `[{number}]` via the existing `_format_task_id` (task.py:32) for D11 prefix consistency.
- Add the D11 instruction line to the nudge template: "Preserve the `[N]` prefix at the start of each `TaskCreate` subject so task ids reconcile across sessions."

### 3.2 Modify — `_TASK_RE` matcher (`yadgar/core/server/http.py:885`)

Current (verified): `_TASK_RE = _re.compile(r"^## task:(\d+)", _re.MULTILINE)`

D10 (base32, Crockford alphabet `0123456789abcdefghjkmnpqrstvwz`, no `I/L/O/U`) + D11 (optional origin segment) require:
```python
# D10: Crockford base32 (digits + a-z minus i,l,o,u). D11: optional origin/ prefix.
_TASK_RE = _re.compile(r"^## task:(?:([\w-]+/)?([0-9a-hj-np-tv-z]+))", _re.MULTILINE)
```
- The existing decimal pages (`## task:0003`) still match (digits are a subset of the Crockford set).
- `## task:4Y` (base32) matches.
- `## task:alice/4Y` (foreign origin) matches the optional group. Note: §14.1 dropped `origin` as a column, so the origin segment is partially mooted for NEW writes — but the regex must still tolerate it on legacy/seeded pages (D11's "unmatched → warn, never silently create").
- No `\d{4}` width assumption (D10 forbids it).

[VERIFY: whether the seed reads legacy decimal pages only, in which case a transitional regex accepting both decimal and base32 is needed; the Crockford-only class above already accepts both since `[0-9]` ⊂ Crockford set.]

### 3.3 Add — `seed_task_from_pages` (`yadgar/backend/admin_exec/seed_ledger.py`)

Mirror of `seed_adr_from_pages` (seed_ledger.py:44). Signature (proposed, matching the ADR seed's shape):
```python
@observe(tier="boundary", metric="backend.admin.seed_task_from_pages")
def seed_task_from_pages(*, directory: str, project_id: str, dry_run: bool = False) -> dict:
    """Seed the `task` table from existing {project}-task-list wiki pages (D35b).

    Reads page_type='task_list' pages, parses ## task:<id> sections, inserts
    rows into the task table. Idempotent (D35a). Verification gate: exact
    equality of per-page section count vs seeded rows per project (D35c).

    Returns: {seeded: N, skipped: M, dry_run: bool, candidates: K}
    """
```
- Source of truth: the `{project}-task-list` wiki page (D35b — pages, not an index). Parse `## task:<id>` sections with the updated `_TASK_RE` (or a local copy — the seed reads the page body, not the nudge path).
- Fields per section (from stop_checkpoint_prompt.md:156-181 schema): `subject`, `status` (pending/in_progress/completed), `active_form`, `description`, `context`, `blockedBy`, `blocks`, `modified`.
- Map to `create_task_row(project_id=, title=subject, status=, state=, active_form=, plan_path=, body_slug=)` — the exact kwargs of Car A's signature (`0047-car-A-ledger-tables.md:72-74`). NOT `origin=` (Car D §D-note: "§14.1 dropped `origin` as a column… do NOT carry it forward"), NOT `directory=` (the `task` table keys on `project_id`; there is no `directory` column — Car A:183). `status=` is REQUIRED here, not optional: it defaults to `"pending"`, so omitting it silently flattens every `in_progress`/`completed` page section to pending.
- `status` from the page section → `task.status` column (pending/in_progress/completed). `state` from the `[PLANNED]`/`[SPIKE]`/`[DECIDE]`/`[VERIFY]` subject prefix per §11.1/§16.10, defaulting to `open`.
- D35c gate: per-page `## task:<id>` section count == seeded rows per project (line 556-557 of master plan).

### 3.4 Modify — `stop_checkpoint_prompt.md` step 5 (`yadgar/core/hooks/templates/stop_checkpoint_prompt.md:105-181`)

Replace the `wiki_write_task_list` calls (stop_checkpoint_prompt.md:122, :137) and the SCHEMA block (:156-181) with ledger-backed task tool calls:
- Step 5a (reconcile own list via harness `TaskList`/`TaskUpdate`/`TaskCreate`) — unchanged.
- Step 5b — replace `wiki_read("{project}-task-list", ...)` with `task_list(directory="<directory>")` (D37 open-only default). NO `project_id=` — Car D's signature (`0047-car-D-task-tools.md:91-98`) takes `directory` and *derives* `project_id` from it via `identity.py` (Car A0); `project_id` is not a parameter of the tool surface.
- Step 5c — replace `wiki_write_task_list(project=..., content=..., directory=...)` with `task_write(directory=..., title=..., status=..., state=..., active_form=...)` per task. Same rule: `directory` in, `project_id` derived (`0047-car-D-task-tools.md:49-61`).
- Delete the page-format SCHEMA block (tasks now live in SQL, not a markdown page).

## 4. Build steps (TDD)

1. **RED** — test that `seed_task_from_pages` on a fixture `{project}-task-list` page with 3 open + 2 completed sections produces 5 rows in the `task` table (exact-equality gate, D35c) and is idempotent on re-run (second call → 0 new rows, 5 skipped).
2. **GREEN** — implement `seed_task_from_pages` in `seed_ledger.py`, mirroring `seed_adr_from_pages`. Parse sections with the updated `_TASK_RE`; map fields per §3.3.
3. **RED** — test that `_task_list_restore_nudge` with a warm ledger returns a nudge listing open tasks from the `task` table (not from a wiki page), capped at `_CAP=12`, and includes the D11 prefix-preserve instruction.
4. **GREEN** — rewire `_task_list_restore_nudge` (http.py:829) to call `list_task_rows` instead of `get_wiki_page_by_slug_directory`; update the nudge template (http.py:920-933) to add the prefix-preserve line.
5. **RED** — test that `_TASK_RE` matches `## task:0003` (decimal legacy), `## task:4Y` (base32), and `## task:alice/4Y` (foreign origin); and does NOT match `## task:00I` (invalid Crockford `I`).
6. **GREEN** — update `_TASK_RE` at http.py:885 per §3.2.
7. **RED** — test the stop-hook checkpoint protocol step 5 instructs `task_list` / `task_write` (not `wiki_write_task_list`) and contains no page-format SCHEMA block.
8. **GREEN** — edit `stop_checkpoint_prompt.md` step 5 per §3.4.
9. **REFACTOR** — extract the nudge-formatting (shared between the forcing-nudge path and any fallback) so the handler stays under the I13 complexity cap (http.py:1099 comment already flags this).
10. **D35c gate** — integration test: after seeding all 6 `page_type='task_list'` pages, assert `COUNT(task rows WHERE project_id=X) == COUNT(## task:<id> sections in page X)` for each project, exact equality (never `>=`).

## 5. Acceptance gates

- [ ] `seed_task_from_pages` is a one-shot admin op (CLI-invoked, idempotent, NOT a migration step) — D35a.
- [ ] Task-list seed verification gate (D35c): per-page `## task:<id>` section count == seeded rows per project, exact equality. 6 `page_type='task_list'` pages DB-wide (master plan line 556).
- [ ] `_TASK_RE` (http.py:885) accepts Crockford base32 + optional origin segment; no `\d{4}` width assumption (D10/D11).
- [ ] `_task_list_restore_nudge` (http.py:829) reads from the `task` ledger, not the wiki page; nudge includes the D11 prefix-preserve instruction.
- [ ] `stop_checkpoint_prompt.md` step 5 uses `task_list`/`task_write`; no `wiki_write_task_list`; no page-format SCHEMA block.
- [ ] Old `{project}-task-list` pages marked `superseded-by-ledger` and content replaced by a one-line pointer (D35d) — [VERIFY: whether this marker is applied by the seed op or a separate cutover step].
- [ ] core/backend version bumped per WORKFLOW RULE (new core/backend version) — [VERIFY exact bump mechanism; http.py + stop_checkpoint_prompt.md are core; seed_ledger.py is backend → both bump].
- [ ] pre-commit green.
- [ ] tests pass.

## 6. Sequencing

- **Must merge before E:** Car D (task tools + `task` ledger table + `create_task_row`/`list_task_rows`). E consumes D's tools and storage methods.
- **Must also be merged:** Car A (Alembic `002_ledger_tables` + `_LedgerMixin`), Car B (backend ops), Car C (identity gate — Car E's seed needs `project_id` derivation per D13/D14, which is Car A0's `identity.py`; C3 gates correctness).
- **E gates:** Car K (nightly archive sweep, policy-dispatched) depends on E. Car M (cross-project `project=` param) depends on E + D + F.
- Build order per §6.1: step 4 = "Task tools; seed; rewire SessionStart + stop-hook (incl. the D11 prefix instruction)" — E is step 4, after C lands (step 3).

## 7. ADRs / decisions

| # | gist |
|---|------|
| D10 | No zero-padding; display in Crockford base32. No code may regex `\d{4}` or assume width. Forces `_TASK_RE` change. |
| D11 | Harness reconcile keys on the id prefix — `[231]` local, `[alice/231]` foreign. Unmatched → warn, never silently create. `_TASK_RE` must accept optional origin segment; nudge must instruct the model to *preserve* `[{tid}]` in the `TaskCreate` subject. |
| D35a | Seed is a separate one-shot admin op, NOT a migration step. Idempotent. |
| D35b | ONE-SHOT, not dual-write. Source = the `{project}-task-list` PAGES, not an index. Cutover is a single atomic flip of the read path. |
| D35c | Verification gate: EXACT equality on a stated predicate. `>=` is not a gate (2026-06-16 vacuum destroyed 3,622 memories). |
| D35d | Old `{project}-task-list` pages KEPT-AND-IGNORED, not deleted, for one cycle. Marked `superseded-by-ledger`; content replaced by a one-line pointer preserving the slug. |
| D37 | `task_list` defaults to open-only (`status IN pending, in_progress`); closed/archived require an explicit filter. |
| D4 | `body_slug` retained; task bodies are the versioned artifact (NOT deleted on archive). |
| ADR-0137 | Task-list inbound seeding: forcing SessionStart nudge (Option B) shipped; mechanical file-writer (Option A) is fallback only. Car E keeps the forcing-nudge form. |

## 8. Out of scope

- Car D's task tool implementation (`task_write`/`task_list`/`task_get`) and the `task` table schema — shipped by Car D.
- Car A0's `identity.py` / `project_id` derivation — E consumes it, does not build it.
- Car G's ADR seed — separate one-shot, same pattern (`seed_adr_from_pages` already exists at seed_ledger.py:44).
- Car I's agent-prompt seed — separate one-shot.
- The mechanical file-writer (ADR-0137 Option A — writing `~/.claude/tasks/<session_id>/<N>.json` directly) — held as fallback only, built ONLY if the forcing nudge (Option B, retained by this car) measurably still leaves the harness TaskList empty after the ledger rewire.
- Car K's nightly archive sweep — depends on E but is separate scope.
- Cross-project `project=` param (Car M) — separate.

## 9. Risks / open questions

- **[VERIFY: line drift — `http.py:923`]** The §7 row E and D11 decision row both cite `http.py:923` for the `_TASK_RE` matcher. On the current branch (`docs/0047-per-car-plans`), `_TASK_RE` is at **http.py:885**, and line 923 is `"Call TaskCreate for EACH one now:",`. The plan's `http.py:1080` reference for the catalog-mode default is also stale — `mode = request.query_params.get("mode", "catalog")` is at **http.py:989**. Cite http.py:885 for the matcher, http.py:989 for catalog mode, http.py:1105 for the hoisted call site. Re-verify line numbers at build time (the file is 2607 lines and drifts).

- **[VERIFY: seed-vs-schema numbering gap]** §13.2 blocker 2 (master plan line 893-908): the seed reads `## task:0003` and needs a row with the historical number 3, but §14.1/ADR-0197 made the AUTO_INCREMENT `id` IS the number — the seed cannot control the id via `INSERT`. Two options: (a) accept renumbering (seed inserts get fresh AUTO_INCREMENT ids, historical numbers lost — acceptable only if nothing external references task numbers, which D7 says external refs must not retarget), or (b) revert to an explicit `number` column for the seed path (contradicts Fix 1 / §14.1). This is an OPEN design gap. The `create_task_row` signature on `feat/spine-knob-mariadb` (ledger.py:243) does NOT take a `number` kwarg. Resolve before building `seed_task_from_pages`.

- **[VERIFY: origin segment mooted by §14.1]** §14.1 dropped `origin` (hardcoded `"yadgar"` at all write sites; `origin` column dropped). D11's "optional origin segment" (`[alice/231]` foreign) was designed for federation. With `origin` gone, foreign-task headers do not arise on the write path — BUT legacy pages may carry foreign-origin sections from before the drop, and D11 says "unmatched → warn, never silently create," so the regex must still tolerate the segment. The base32 change (D10) is the load-bearing part; the origin-segment tolerance is defensive.

- **[VERIFY: `task_write` vs harness `TaskCreate`]** The yadgar MCP tool is `task_write` (task.py:81); the harness tool the nudge instructs is `TaskCreate` (Claude Code's own). The nudge (http.py:920-933) tells the model to call `TaskCreate` (harness), NOT `task_write` (yadgar). The D11 prefix-preserve instruction is about the harness `TaskCreate` subject containing `[N]` so the NEXT session's reconcile can match it. Do not conflate the two surfaces.

- **[VERIFY: D35d marker application]** D35d says old `{project}-task-list` pages are marked `superseded-by-ledger` with content replaced by a one-line pointer. [VERIFY: whether this is applied by `seed_task_from_pages` at seed time, or by a separate cutover step after the D35c gate passes. The master plan §6.2/§6.3 implies the marker is post-cutover, not at seed time — the seed is append-only and never mutates a wiki page (line 567-569).]

- **[VERIFY: core reads ledger via HTTP forward]** §15 mandates core never touches the DB; `_task_list_restore_nudge` (core, http.py) must read the ledger via HTTP forward to backend, not a direct `create_task_row`/`list_task_rows` call. [VERIFY: the backend PTC (§15.1) exists by the time E starts; if not, E must forward over HTTP through whatever backend op Car B shipped. The `task_list` tool (task.py:125) already calls `_get_storage().list_task_rows` directly — that is a Car D pattern that §15 says must be re-routed; confirm whether E re-routes it or that is Car B scope.]
