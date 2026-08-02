# PR #32 Fix Plan

Based on review of `feat/spine-knob-mariadb` against `docs/plans/task-table-refactor-2026-07-29.md`.

---

## Fix 1: Drop D31 allocation — use AUTO_INCREMENT as the number

**Problem:** `_next_number()` does `SELECT MAX+1 FOR UPDATE` in its own transaction, then `create_*_row()` does INSERT in a separate transaction. The lock is released between them — race condition. The whole allocation mechanism is unnecessary.

**Decision:** `number` column = `AUTO_INCREMENT`. INSERT returns the generated number. No allocation step, no race, no separate transaction.

**Changes:**

1. **`alembic/versions/002_ledger_tables.py`** — change `number` columns from plain `Integer` to `Integer, autoincrement=True` (or just make `number` the PK and drop the surrogate `id` column — simpler: one column, not two). Actually: keep `id` as surrogate PK (D6a), make `number` AUTO_INCREMENT. On MariaDB only one AUTO_INCREMENT per table, so `id` stays AUTO_INCREMENT and `number` becomes a plain column set from `id` at INSERT time. **Simpler:** just use `id` AS the number. Drop the `number` column entirely. `id` IS the number. One column, no duplication.

   Wait — three tables (task, adr, agent_prompt) each have their own AUTO_INCREMENT. Task #5 and ADR #5 are different rows in different tables. That's fine — the type is implicit in which tool you call.

2. **`alembic_models.py`** — remove `number` column from all three models. `id` is the number.

3. **`ledger.py`** — delete `_next_number()`, `allocate_task_number()`, `allocate_adr_number()`, `allocate_agent_prompt_number()`. Remove `number` parameter from `create_task_row()`, `create_adr_row()`, `save_agent_prompt()`. INSERT returns `id` as the number.

4. **`adr.py`** — remove `allocate_adr_number()` call. `create_adr_row()` returns the row with `id` → use that as the number. Format as `ADR-{id:04d}`.

5. **`adr_ledger.py`** — same.

6. **`task.py`** — remove `allocate_task_number()` call. Use returned `id` as number.

7. **`agent_prompts_ledger.py`** — same.

8. **`backend/admin_exec/ledger.py`** — fix method name mismatches (see Fix 4).

9. **Tests** — update all tests that mock `allocate_*_number`. Update `test_ledger_d31_allocation.py` → test that INSERT returns sequential ids.

10. **Plan doc** — update D6/D8/D31 in the plan to reflect the simplification.

---

## Fix 2: `adr_add` must write the wiki page body and store its slug

**Problem:** `adr_add` in both `adr.py` and `adr_ledger.py` only creates the ledger row. No wiki page body is written to SurrealDB. `body_slug` is not passed to `create_adr_row`. The ADR model has `body_slug` as `nullable=False` — the INSERT will fail at the DB level.

**Decision:** After creating the ledger row (and getting the `id`/number back), write the wiki page body to SurrealDB using the existing canonical write path. Slug = `{project_id}_adr-{number}` per D32. Pass the resulting slug as `body_slug` to `create_adr_row`.

**Changes:**

1. **`adr.py`** — after `create_adr_row()`, call `_wiki_write_canonical()` (or equivalent) to write the body page. Pass `body_slug` to `create_adr_row`.

2. **`adr_ledger.py`** — same.

3. **`ledger.py`** — `create_adr_row` already accepts `body_slug` parameter. No change needed there.

4. **`adr_render.py`** — `_build_adr_body()` already exists and builds the markdown body from ADR fields. Reuse it.

---

## Fix 3: Re-point callers of deleted `parse_index_rows` / `_build_index_content`

**Problem:** `adr_index.py` deleted `parse_index_rows`, `_build_index_content`, `_next_adr_id`, `_next_adr_id_from_index`, `_index_max_id`, `_committed_page_max_id`, `_render_index_row`. But these are still imported in `adr_render.py:179`, `project.py:1880`, and 5 test files. All will fail at import time.

**Decision:** Re-point the two production callers to use the ledger. Update/delete affected tests.

**Changes:**

1. **`adr_render.py:179`** — `_assemble_index_rows` calls `parse_index_rows`. This function is part of the old supersede path. Since `adr_add` no longer writes an index page, `_assemble_index_rows` is dead. Delete it and its callers (`_flip_superseded_target`'s index-write path). The supersede logic moves to the ledger (update `supersedes`/`superseded_by` columns on the target row).

2. **`project.py:1880`** — `_build_adr_log` calls `parse_index_rows` to build the ADR log for `project_brief`. Re-point to `storage.list_adr_rows(project_id=...)`.

3. **`adr.py` `__all__`** — remove `_assemble_index_rows`, `_build_index_content`, `parse_index_rows` (they no longer exist).

4. **Tests** — update `test_adr.py`, `test_project_brief_adr_log.py`, `test_recall_output_cap.py` to use ledger mocks instead of the deleted functions.

---

## Fix 4: Delete `adr_ledger.py` — dead code, never wired in

**Problem:** `adr_ledger.py` was added as a parallel implementation but never imported in `tools/__init__.py`. Only `adr.py`'s tools are registered. Dead code.

**Decision:** Delete the file. Move `_should_regenerate_rollup` into `adr.py` (one test imports it). Standardize on `list[dict]` return shape for `adr_list` (the characterization test already validates this).

**Changes:**

1. Delete `yadgar/core/server/tools/adr_ledger.py` — DONE.
2. Move `_should_regenerate_rollup` into `adr.py`.
3. Update `test_adr_tier_subsystem_car_h.py:50` import to point at `adr.py`.

---

## Fix 5: Fix backend method name mismatches in `admin_exec/ledger.py`

**Problem:** `backend/admin_exec/ledger.py` calls methods that don't exist on `_LedgerMixin`:
- `storage.create_task(payload)` → should be `create_task_row(**kwargs)`
- `storage.add_adr(payload)` → should be `create_adr_row(**kwargs)`
- `storage.save_agent_prompt(payload)` → signature mismatch (takes kwargs, not dict)
- `storage.set_config_row(...)` → does not exist
- `storage.delete_config_row(...)` → does not exist

**Decision:** Rename calls to match existing methods. Add `set_config_row`/`delete_config_row` to `_LedgerMixin` (runtime config store moved to MariaDB per task #0119).

**Changes:**

1. **`backend/admin_exec/ledger.py`** — rename `create_task` → `create_task_row`, `add_adr` → `create_adr_row`, `save_agent_prompt` → unpack payload dict into kwargs.
2. **`ledger.py`** — add `set_config_row(key, value, directory)` and `delete_config_row(key, directory)` methods.

---

## Fix 6: Auto-invoke Alembic migrations from `_init_ledger()`

**Problem:** `StorageEngine.__init__` calls `_init_ledger()` but never runs `alembic upgrade head`. Tables don't exist until migrations are run manually. D34 requires auto-invocation from the same gate as `_run_migrations`.

**Decision:** Call `alembic.command.upgrade(alembic_cfg, "head")` from `_init_ledger()`, gated the same way as `_run_migrations` (server mode only, under the migration lock).

**Changes:**

1. **`ledger.py` `_init_ledger()`** — after creating the engine, run Alembic migrations. Gate on `self._db_url` being set (server mode only, same as `_run_migrations`). Use the same `fcntl.flock` on `STATE_DIR/.migration.lock`.

---

## Fix 7: Extend chokepoint guard to catch ORM queries

**Problem:** `check_ledger_chokepoint.py` only catches raw SQL strings (`.execute()`, `.executemany()`). SQLAlchemy ORM queries like `session.query(Task).filter(...)` from outside `_LedgerMixin` would bypass the guard. D20 requires every row access through the mixin.

**Decision:** Extend the AST scanner to flag `session.query(Task)` / `session.query(ADRModel)` / `session.query(AgentPrompt)` calls outside `_LedgerMixin` methods.

**Changes:**

1. **`scripts/check_ledger_chokepoint.py`** — add ORM query detection: scan for `session.query(` calls where the argument is one of the three ledger model classes, and the call site is not inside a `_LedgerMixin` method.

---

## Fix 8: `save_agent_prompt` — upsert, not insert-only

**Problem:** `ledger.py:402-439` always does `session.add()`. Called twice with the same title → UNIQUE constraint violation.

**Decision:** Check for existing row by title. Update if found, insert if not.

**Changes:**

1. **`ledger.py` `save_agent_prompt()`** — query by title first. If row exists, update fields. If not, insert new row.
2. **`alembic/versions/002_ledger_tables.py`** — add UNIQUE constraint on `agent_prompt.title`.
3. **`ledger.py` `save_agent_prompt()`** — before insert, run similarity check against existing titles. Reject if cosine similarity ≥ `AGENT_PROMPT_TITLE_SIMILARITY_THRESHOLD` (default 0.90). Configurable via runtime config knob.

---
