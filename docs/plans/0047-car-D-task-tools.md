# Car D — task tools

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: B (backend ops + cache), C (C1 tag-override, C2 downweight, C3 identity gate)
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Per §7 row D (verbatim): "task tools". Per §4 (line 431) the tool inventory is `task_list / task_get / task_write  NEW`. This car builds the three task MCP tools that sit on top of the `task` ledger table (Car A) and the backend ops + PTC cache (Car B), and that respect the recall/mutability disposition settled by Car C.

The task tools are the SQL-backed replacement for the markdown `{project}-task-list` wiki page as the **source of truth** for task tracking. The wiki page becomes a derived mirror (ADR-0133: harness task list = source of truth; yadgar `{project}-task-list` wiki = stop-hook-derived mirror via `wiki_write_task_list`). After this car, task reads/writes go through the ledger, not through page parsing.

Three tools, all in a NEW file `yadgar/core/server/tools/task.py` (does not exist on `master`; verified — `ls yadgar/core/server/tools/task*` → no matches):

- `task_write` — create or update a task row. `id` is `AUTO_INCREMENT` and IS the semantic number (ADR-0197, §14.1 — no `number` column, no allocation step). Manages `task_blocked_by` join edges (D39).
- `task_list` — list tasks for a project; defaults to **open-only** `status IN (pending, in_progress)` (D37).
- `task_get` — fetch one task by `(project_id, id)`.

§15 read path is binding: **core NEVER touches the database** (ADR-0078). The tools forward over HTTP to the backend PTC (Car B), which reads/writes MariaDB via `_LedgerMixin` (Car A). The PR #32 reference implementation called `_get_storage().create_task_row()` directly from core (`task.py:109` on `feat/spine-knob-mariadb`) — §15 explicitly names that as a violation (`core/server/tools/task.py:107-159 calling _get_storage().create_task_row()`). This car must NOT repeat it: forward, do not call storage.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/core/server/tools/task.py` | NEW. Three MCP tools (`task_write`, `task_list`, `task_get`) + `_format_task_id` (D11 prefix) + `_validate_title` (D12, 200 chars) + `_validate_project_id` (strict string). Forward over HTTP to backend (§15), do NOT call `_get_storage()` directly. | no `task.py` on `master` (`ls yadgar/core/server/tools/task*` → no matches); reference impl on `feat/spine-knob-mariadb` at `task.py:81 def task_write`, `:125 def task_list`, `:146 def task_get`, `:32 def _format_task_id` — but that impl is STALE (uses dropped `origin=` kwarg at `:111`, calls storage directly at `:109` violating §15) |
| `yadgar/core/server/tools/__init__.py:66-67` | Add `task_write, task_list, task_get` to the `from yadgar.core.server.tools.task import (...)` block (new import block, alongside the existing `from ...wiki import (..., wiki_write_task_list, ...)` at `:66`). | `__init__.py:66-67` `from yadgar.core.server.tools.wiki import (wiki_add, wiki_write_task_list, ...)`; `:158-159` `__all__` has `"wiki_write_task_list"` — task tools slot in beside it |
| `yadgar/core/server/tools/__init__.py:158-165` | Add `"task_write"`, `"task_list"`, `"task_get"` to `__all__`. | `:159 "wiki_write_task_list"` is the adjacent entry |
| `yadgar/core/server/__init__.py:177` | Add `task_write, task_list, task_get` to the top-level re-import block (alongside `wiki_write_task_list` at `:177`). | `:177 wiki_write_task_list,` verified in the re-import block |
| `yadgar/core/server/tools/project.py:143` | Update the task-tracking nudge text (`"*To track TODOs/tasks use the harness task list (TaskCreate)"`) to point at the new `task_write` tool as the durable store, not (only) the harness list. | `project.py:143` verified — the nudge line exists |
| `yadgar/tests/core/test_task_tools.py` | NEW. TDD tests (red-first) per §4. | `yadgar/tests/core/` exists; `test_adr.py` is the sibling pattern; no `test_task_tools.py` on `master` |
| `server.json:10` | Bump core `version` (5.181.0 → next). Car D touches `yadgar/core/server/tools/` (core) — core bump required. | `server.json:10 "version": "5.181.0"`, `:11 "backend_version": "5.71.0"` at repo root (NOT `yadgar/core/server.json` — that path does not exist; Car A doc's `yadgar/core/server.json` citation is wrong) |
| `pyproject.toml:7` | Bump `version` to match `server.json` (`check_versions.py:13-22` asserts they match). | `pyproject.toml:7 version = "5.181.0"`; `scripts/check_versions.py:14,21` reads both |

**NOT touched by this car (owned upstream):**
- `yadgar/_shared/storage/ledger.py` — `create_task_row` / `list_task_rows` / `get_task_row` / `update_task_status` live in `_LedgerMixin` (Car A). Car D calls them via the backend HTTP forward, not directly.
- `yadgar/backend/admin_exec/` task handler + PTC cache — Car B ("backend ops + cache"). [VERIFY: whether the backend admin_exec task CRUD handler is Car B's deliverable or whether Car D must add a thin `backend/admin_exec/task.py` — the §7 row B "backend ops" is generic; the §13 PR #32 had a single `backend/admin_exec/ledger.py` handling all three entities. Assume Car B ships the backend task handler; if not, Car D adds it and bumps `backend_version` too — see §5.]
- `yadgar/core/server/http.py:829` `_task_list_restore_nudge` rewire + `_TASK_RE` matcher — Car E (depends on D).
- `yadgar/backend/admin_exec/seed_ledger.py` task seed — Car E.

## 3. Functions / symbols

All signatures take `project_id` as an explicit caller parameter. **No tool derives it, and no internal core/backend component derives it.** ADR-0202 is binding here: project_id "is derived ONCE per session — by the startup hook, or from `.yadgar/project-id` found by walking up from cwd — and thereafter travels as an explicit caller parameter on each write, with an override for cross-project work." ADR-0202 explicitly REJECTS the alternative: "Re-deriving project_id per write — rejected: pays the git-remote parse and its traps on every call for a value that cannot change mid-session."

This corrects an earlier draft of this car, which said the tools take `directory` and derive internally via `yadgar/core/identity.py`, citing §16 as superseding PR #32's `project_id` shape. That was a misreading: §16 says the registry check on write REJECTS an unknown `project_id` with a structured error, which only makes sense if the caller supplies it. Car A0's `derive_project_id()` exists to run ONCE at session start — it is not a per-call helper.

The cross-project case needs no separate mechanism at the tool layer: passing a different `project_id` IS the override. Car M (§16.6) covers the surrounding plumbing, not a distinct `project=` kwarg here.

### 3.1 Add — `task_write` (`yadgar/core/server/tools/task.py`)

```python
@_tool(power=True)
def task_write(
    project_id: str,                # session-resolved, caller-supplied (ADR-0202)
    title: str,
    *,
    id: int | None = None,          # None → create; int → update existing row
    status: str | None = None,      # pending | in_progress | completed | archived
    state: str = "open",            # open | planned | spike | needs_decision | built_unverified (D36)
    active_form: str | None = None,
    plan_path: str | None = None,
    body_slug: str | None = None,
    blocked_by: list[int] | None = None,  # D39 — task_blocked_by join edges
    blocks: list[int] | None = None,      # D39 — same join table read the other way
) -> dict:
    """Create or update a task row.

    Create (id is None): INSERT; id is AUTO_INCREMENT and IS the number
    (ADR-0197, §14.1 — no number column, no allocation step). Returns the
    generated id. Update (id given): UPDATE the row; status/state/title
    fields are partial-update (None = leave unchanged).

    D12: title <= 200 chars, reject-on-write.
    D36: state is NULLABLE; cleared (set to NULL) when status transitions
    to completed/archived (§16.10).
    D39: blocked_by/blocks manage task_blocked_by join rows (both FK → task.id
    CASCADE). One table serves both directions (§3.4).
    D26: task mutability = free (no lock).

    Error model: {ok: False, error: "..."} — never raise.
    """
```

Notes:
- **No `origin` parameter.** §14.1 dropped `origin` as a column (hardcoded `"yadgar"` at all write sites, discriminated nothing). The PR #32 reference passed `origin="yadgar"` (`task.py:111` on the branch) — do NOT carry it forward.
- **No `number` parameter / no allocation.** §14.1 / ADR-0197 retired D31; `id` IS the number.
- **`completed_at`** is set by the backend when `status` transitions to `completed` (§3.4, §14.2 — "archive sweep must not age off last-touched"). Car D passes `status`; the backend writes `completed_at`. [VERIFY: whether `completed_at` is set backend-side in `update_task_status` or whether the tool must pass it — `ledger.py:343 update_task_status` on the feature branch takes only `project_id, number, status`; confirm Car A's implementation sets `completed_at` on the transition.]
- **State clearing on completion (§16.10).** When `status` is set to `completed`/`archived`, `state` MUST be set to NULL. [VERIFY: enforce in the tool (task_write clears state before forwarding) or in the backend update path. The §14.3 open question — whether a `CHECK` constraint rejects `status='completed' AND state='planned'` — is NOT decided; this car tolerates the combination in the schema but clears `state` at the application layer per §16.10.]
- **Forward over HTTP (§15).** The body does NOT call `_get_storage().create_task_row()`. It calls `_forward_admin(...)` (or the Car B backend PTC forward helper) to reach the backend admin_exec task handler. [VERIFY: the exact forward helper signature Car B exposes — `_forward_admin` exists for wiki; Car B may add a ledger-specific forward. Confirm against Car B's doc when it lands.]

### 3.2 Add — `task_list` (`yadgar/core/server/tools/task.py`)

```python
@_tool()
def task_list(
    project_id: str,                # session-resolved, caller-supplied (ADR-0202)
    *,
    include_closed: bool = False,   # D37 — default open-only
    status: list[str] | None = None,  # explicit override; None → D37 default
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List tasks for the derived project_id.

    D37: default to status IN (pending, in_progress). include_closed=True
    returns all rows (completed/archived). An explicit status list overrides
    both. Closed/archived rows never appear unless requested — this is the
    mechanism that makes D7 (archive-never-delete) survivable (§11.2).
    """
```

- `project_id` arrives from the caller (session-resolved). Validate it against the `project` registry — §16 makes that check LOAD-BEARING on write: an unknown `project_id` is REJECTED with a structured error, never auto-created, or a typo mints a phantom namespace.
- Forward to backend `list_task_rows` (Car A `_LedgerMixin`, `ledger.py:286` on the feature branch).
- Return shape: list of dicts keyed on `id` (NOT `number` — §13.2 blocker 2: the seed/archive callers read `r["number"]` but the ledger returns `{"id": ...}`). Car D's tool surface MUST use `id` consistently.

### 3.3 Add — `task_get` (`yadgar/core/server/tools/task.py`)

```python
@_tool()
def task_get(
    project_id: str,                # session-resolved, caller-supplied (ADR-0202)
    id: int,                        # AUTO_INCREMENT PK — the semantic number
) -> dict:
    """Fetch one task by (project_id, id)."""
```

- Forward to backend `get_task_row` (`ledger.py:361` on the feature branch).
- Parameter named `id`, not `number` (§14.1 / §13.2 blocker 2).

### 3.4 Add — `_format_task_id` (`yadgar/core/server/tools/task.py`)

```python
def _format_task_id(id: int) -> str:
    """D11 — format a task id as the harness-readable [id] prefix.

    The harness renders tasks as "[status] [id] subject". D11 says the
    [id] must be the prefix-reconciled task id, not a fresh session handle.
    Foreign projects get the [owner/repo/id] form (Car M adds the origin
    segment; here it is local-only). D10 (Crockford base32 display) is
    applied at render time, not stored — the id is stored as an integer.
    """
    return f"[{id}]"
```

[VERIFY: D10 says "Display in Crockford base32, default on." Whether `_format_task_id` renders decimal or base32 here is a Car E concern (the restore-nudge template renders the prefix). Car D provides the integer; rendering is the caller's job. Car E §3.2 handles the base32 regex. Keep this helper decimal-only unless Car E's nudge needs base32 at the `_format_task_id` call site — confirm against Car E when it ships.]

### 3.5 Add — `_validate_title` / `_validate_project_id`

Mirrors the PR #32 reference (`task.py:43 _validate_title`, `:68 _validate_project_id`): strict-type checks (reject non-string, empty, > 200 chars per D12). `_validate_project_id` is a strict string check here; the registry FAIL-LOUD check (ADR-0202 amendment, unknown `project_id` → REJECTED) is enforced at the backend write path (Car A0 + Car B), not in the core tool — the core tool only rejects non-string/empty before forwarding.

## 4. Build steps (TDD)

1. **RED** — `yadgar/tests/core/test_task_tools.py::test_task_write_creates_row_and_returns_id` — call `task_write(project_id=..., title="...")` against a stubbed backend forward; assert it forwards a create op (no `id`), returns `{"ok": True, "id": <int>}`, and does NOT call `_get_storage()` directly (assert the storage accessor is never touched — this is the §15 invariant).
2. **RED** — `test_task_write_update_clears_state_on_completion` — call `task_write(project_id=..., id=231, status="completed", state="planned")`; assert the forwarded update op sets `state=NULL` alongside `status="completed"` (§16.10).
3. **RED** — `test_task_write_rejects_title_over_200_chars` — D12.
4. **RED** — `test_task_write_rejects_non_string_project_id` — strict types.
5. **RED** — `test_task_list_defaults_open_only` — `task_list(project_id=...)` forwards with `status=["pending","in_progress"]` (D37); `include_closed=True` forwards with `status=None`.
6. **RED** — `test_task_list_explicit_status_overrides_default` — `status=["archived"]` forwards with exactly that, ignoring `include_closed`.
7. **RED** — `test_task_get_forwards_by_id_not_number` — `task_get(project_id=..., id=231)` forwards `id=231`; the forwarded payload key is `id`, never `number` (§13.2 blocker 2).
8. **RED** — `test_task_write_manages_blocked_by_edges` — `blocked_by=[5, 7]` forwards a join-edge sync op for `task_blocked_by` (D39).
9. **RED** — `test_task_write_does_not_pass_origin` — assert the forwarded payload has NO `origin` key (§14.1 dropped it).
10. **GREEN** — implement `task.py` with the three tools + helpers, forwarding over HTTP to the Car B backend. Wire into `tools/__init__.py:66` and `:158`, and `core/server/__init__.py:177`.
11. **GREEN** — update `project.py:143` nudge to reference `task_write`.
12. **REFACTOR** — extract the forward-helper call shape shared by all three tools (one `_forward_task_op` helper) if repetition warrants it; keep under the I13 complexity cap.
13. **GATE** — ruff, import-linter contracts (§ `backend must not import core` at `pyproject.toml:360`; core may import backend only via HTTP, not directly), `check_ledger_chokepoint.py` (D20 — task.py must NOT touch `_LedgerMixin` directly; it goes through the backend forward, so it is outside the chokepoint's allowlist concern), `check_versions.py` (server.json `version` == pyproject `version`), `check_backend_bump.py` (only if `backend/` touched).

## 5. Acceptance gates

- [ ] `task_write` / `task_list` / `task_get` registered as MCP tools (appear in the tool list; callable).
- [ ] `task_list` defaults to open-only `status IN (pending, in_progress)` (D37) — verified by `test_task_list_defaults_open_only`.
- [ ] `task_write` create returns the AUTO_INCREMENT `id`; no `number` column / no allocation step (ADR-0197, §14.1).
- [ ] `task_write` update clears `state` to NULL when `status` → `completed`/`archived` (§16.10).
- [ ] No `origin` parameter anywhere in the task tool surface (§14.1).
- [ ] Core never calls `_get_storage().create_task_row()` / `list_task_rows` / `get_task_row` directly — forwards over HTTP (§15, ADR-0078). Verified by test asserting the storage accessor is not touched.
- [ ] Forwarded payload keys use `id`, not `number` (§13.2 blocker 2).
- [ ] `blocked_by` / `blocks` manage `task_blocked_by` join edges (D39).
- [ ] Title > 200 chars rejected (D12).
- [ ] The yadgar `{project}-task-list` wiki page is noted as the **derived mirror** (ADR-0133) — `wiki_write_task_list` (`yadgar/core/server/tools/wiki.py:130`) remains the outbound mirror writer; the ledger is source of truth. (The stop-hook rewire that stops writing the mirror is Car E, not D.)
- [ ] core `version` bumped in `server.json:10` AND `pyproject.toml:7` (they must match per `scripts/check_versions.py:14,21`). Current: `5.181.0`.
- [ ] `backend_version` bumped in `server.json:11` IF and only if Car D touches `yadgar/backend/` (`BACKEND_BUILD_DIRS=("backend",)` per `scripts/check_backend_bump.py:44`). [VERIFY: if the backend admin_exec task handler is Car D's deliverable (not Car B's), `backend_version` (currently `5.71.0`) must bump too.]
- [ ] pre-commit green (ruff, import-linter, `check_ledger_chokepoint.py`, `check_versions.py`, `check_backend_bump.py`).
- [ ] tests pass (`yadgar/tests/core/test_task_tools.py` + no regressions in `yadgar/tests/_shared/test_task_list_schema.py`).

## 6. Sequencing

**Must merge before D:** A0 (identity.py — `project_id` derivation), A (`task` table + `_LedgerMixin` + `create_task_row`/`list_task_rows`/`get_task_row`/`update_task_status`), B (backend admin_exec task handler + PTC cache + the HTTP forward surface core calls), C (C2 `downweight` recall disposition for task bodies — D22; C3 identity gate — D21 — for `body_slug` derivation).

**Waits on D:** E (task seed + SessionStart/stop-hook rewire + `_TASK_RE` matcher + D11 prefix — E consumes `task_list`/`task_get`/`task_write` from the hook paths; E's `seed_task_from_pages` calls `create_task_row` which D's tools also exercise), M (cross-project `project=` param on the task tools — M adds the override to `task_write`/`task_list`/`task_get` signatures built here).

D ∥ F (ADR tools) ∥ I (agent_prompt tools) after B. **C gates D and F.**

## 7. ADRs / decisions

| # | gist |
|---|------|
| D6 | Row PK is `AUTO_INCREMENT id`; it IS the semantic number (ADR-0197). No `number` column. |
| D7 | Never reused. Archive, never hard-delete. Closed rows cost nothing because `task_list` SELECTs open ones (D37). |
| D10 | No zero-padding. Crockford base32 display, default on. (Render-time; stored as integer.) |
| D11 | Harness reconcile keys on the id prefix `[231]` local / `[alice/231]` foreign. `_format_task_id` emits the prefix; Car E teaches the nudge to preserve it. |
| D12 | Title capped at 200 chars, reject-on-write. |
| D20 | Every row access through `_LedgerMixin` — lint-enforced. Core reaches it via HTTP forward, not direct call. |
| D22 | `recall_disposition` status-driven; `task` → downweight. (Car C2 ships the disposition; Car D's body pages inherit it.) |
| D26 | `task` mutability = free (no lock). |
| D36 | `state` STORED as a real nullable column, enum `open \| planned \| spike \| needs_decision \| built_unverified`. |
| D37 | `task_list()` defaults to `status IN (pending, in_progress)`; closed/archived require an explicit filter. |
| D38 | On archive, the task body page is retained, never deleted, and excluded from recall. (Car D writes `body_slug`; the retention/exclusion is Car C2 + Car K.) |
| D39 | `blocked_by[]` and `blocks[]` as the `task_blocked_by` join table (D30 retired scalar arrays → join tables, §14.1). |
| §14.1 | `origin`, `number`, `directory`, `branch` columns DROPPED. `id` IS the number. |
| §15 | Core PTC → backend PTC → DB. Core never touches the DB. Forward over HTTP. |
| §16.6 | Tools take `project_id` as an explicit caller parameter (ADR-0202); passing a different value IS the cross-project override. Car M covers the surrounding plumbing, not a separate `project=` kwarg. |
| §16.10 | `state` is NULL once `status` is `completed`/`archived`. CHECK-constraint question open (§14.3) — this car clears at the application layer. |
| ADR-0133 | Harness task list = source of truth; yadgar `{project}-task-list` wiki = derived mirror via `wiki_write_task_list`. This car makes the ledger the source of truth; the wiki mirror becomes second-class. |
| ADR-0197 | `id` IS the number; no allocation step. |

## 8. Out of scope

- **Task seed** (`seed_task_from_pages`) — Car E (D35a/D35b/D35c).
- **SessionStart / stop-hook rewire** (`_task_list_restore_nudge` at `http.py:829`, `_TASK_RE` at `http.py:885`, `stop_checkpoint_prompt.md`) — Car E.
- **`_TASK_RE` base32 + origin-segment matcher** (D10/D11) — Car E.
- **Cross-project `project=` override** on the task tools — Car M (§16.6).
- **`task` table schema, `_LedgerMixin` CRUD, Alembic revision** — Car A.
- **Backend admin_exec task handler + PTC cache** — Car B. [VERIFY boundary — see §2 note.]
- **Nightly archive sweep** (policy-dispatched, ages off `completed_at`) — Car K.
- **`check` constraint rejecting `status='completed' AND state='planned'`** — §14.3 open question, not decided in this car.
- **`body_slug` page write for task bodies** — the task body wiki page write (parallel to ADR's `adr_add` body write, §13 Fix 2) is [VERIFY: whether Car D writes the body page on create, or whether task bodies stay in the description column / are deferred. §3.4 has `body_slug VARCHAR(255) YES NULL` ("NULL until the body page exists", D4). The PR #32 `task_write` accepted `body_slug` as a passthrough param but did NOT write a wiki body page (unlike `adr_add` which does per §13 Fix 2). Decide: does Car D write a task body page, or only stamp a `body_slug` if one is supplied? Default: stamp only — task bodies are optional, the description lives in `title`/`active_form`/`plan_path`; full-body wiki pages are not the common case for tasks.]

## 9. Risks / open questions

- **[VERIFY] Backend task handler ownership.** §7 row B ("backend ops + cache") is generic. The §13 PR #32 had a single `backend/admin_exec/ledger.py` handling task/adr/agent_prompt CRUD. If Car B ships only the PTC cache + the generic forward surface and NOT the task-specific admin_exec handler, Car D must add `yadgar/backend/admin_exec/task.py` (or extend the ledger handler) — which triggers a `backend_version` bump (§5). Confirm against Car B's doc.
- **[VERIFY] Forward helper signature.** §15 specifies core→backend HTTP forwarding but the exact helper Car B exposes for ledger ops (`_forward_admin` vs a new `_forward_ledger_op`) is not in the merged tree. The existing `_forward_admin` serves wiki admin ops; Car B may add a ledger-specific forward with scope-version piggyback (§15.2). Car D's task.py depends on that surface.
- **[VERIFY] `completed_at` write site.** `ledger.py:343 update_task_status` on the feature branch takes only `(project_id, number, status)` — it does not take `completed_at`. §14.2 says `completed_at` is "new — archive sweep must not age off last-touched." Confirm Car A's `update_task_status` (or a successor) sets `completed_at` when `status` flips to `completed`; if not, Car D must pass it and the backend must accept it.
- **[VERIFY] `state` clearing enforcement point.** §16.10 says state is NULL once completed. Enforce in the tool (clear before forwarding) or in the backend update path? The tool-layer enforcement is safer (one place, visible) but duplicates if the backend also enforces. Recommend: tool clears `state=None` when `status` in `{completed, archived}` before forwarding; backend treats `state=None` as "leave unchanged" vs "set to NULL" ambiguity — [VERIFY the backend's None semantics].
- **[VERIFY] D10 base32 in `_format_task_id`.** Car E §3.2 handles the base32 regex for the nudge. Whether `_format_task_id` itself should render base32 or decimal is unclear — the PR #32 reference renders decimal (`f"[{number}]"`). Keep decimal here unless Car E requires base32 at this call site.
- **[VERIFY] `blocked_by` / `blocks` join-edge sync semantics.** D39 says one join table serves both directions (`blocks` is the same rows read the other way). Does `task_write` accept both `blocked_by` and `blocks` and reconcile, or only `blocked_by` (with `blocks` derived)? §3.4 says "Storing both is how they drift" — so the tool should accept `blocked_by` only and derive `blocks` on read. [Confirm the read API for `blocks` — likely `task_get` returns a `blocks: [...]` field computed from the join table.]
- **[VERIFY] Stale line numbers in the parent plan.** The plan cites `http.py:923` for `_TASK_RE` but the actual line is `http.py:885` (verified). The plan cites `core/server/tools/task.py:107-159` and `:68` — `task.py` does not exist on `master` (those lines are from `feat/spine-knob-mariadb`). Car D's own `task.py` line numbers will differ from the PR #32 reference once rebuilt post-audit (dropped `origin`, HTTP forward instead of direct storage call).
- **[VERIFY] `server.json` path.** Car A's doc cites `yadgar/core/server.json`; the actual file is at repo root `./server.json` (`scripts/check_versions.py:20` reads `root / "server.json"`). Bump the root file.
- **PR #32 `task.py:11` docstring cites "D31: SELECT MAX(number)+1 FOR UPDATE"** — D31 was dropped (§14.1, §13 Fix 1). Car D's docstring must cite ADR-0197 ("id IS the number"), not D31.
