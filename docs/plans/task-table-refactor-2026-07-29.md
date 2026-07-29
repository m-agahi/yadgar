# Plan: Task-table refactor — dedicated `task` table, reuse `recall`/`memorize`

**Date:** 2026-07-29
**Task:** #0047
**Status:** design locked, not yet built

## Problem

The monolithic `{project}-task-list` wiki page (~14k chars / ~4k tokens) is read in
full on every stop-hook checkpoint and every session start. Task 0080 measured the
session-start rehydration cost at **~24k tokens** (wiki_read + 49 TaskCreate calls +
harness re-injection). This is the single biggest per-session cost.

The old 0047 design (2-TOC wiki pages + per-task detail pages) cuts the cost but
still routes structured data through the embedding/recall pipeline — the wrong
abstraction for a task list.

## Design

### Core idea

A dedicated `task` table in SurrealDB. No new MCP tools — reuse `recall` and
`memorize` with type-aware routing. The TOC is a query result, not a page that
needs constant reconciliation.

### Schema

```
task {
  id,              // SurrealDB record ID
  project,         // directory_context (e.g. "/home/max/git/yadgar")
  number,          // task number (0047, 0080, ...) — server-assigned, atomic
  subject,         // one-line title
  status,          // pending | in_progress | completed
  active_form,     // optional: what the task looks like when in_progress
  description,     // full description (markdown)
  context,         // cross-refs, related files, ADRs
  blocked_by,      // list of task numbers this task is blocked by
  blocks,          // list of task numbers this task blocks
  wiki_page_slug,  // link to rich detail page ({project}-task-NNNN)
  created_at,
  modified_at
}
```

All tables are SCHEMALESS (existing convention). Fields added via idempotent
`DEFINE FIELD IF NOT EXISTS` migration.

### API: `recall(type="task")`

```
recall(type="task", query="", directory="/home/max/git/yadgar")
```

- `type="task"` routes to a new `TaskProvider` (or direct backend query) that
  skips embedding entirely — plain SurrealQL `SELECT`.
- `query` is a **filter string**, not semantic search. Syntax: `key:value`
  pairs, space-separated. Supported keys: `status`, `blockedBy` (`empty`/`any`).
- **Default filter:** `status != 'completed'` — all active tasks, blocked
  included. The harness can gray out blocked tasks client-side using the
  `blocked_by` field returned in every result.
- **Override examples:** `query="status:pending"`, `query="blockedBy:empty"`,
  `query="status:in_progress blockedBy:empty"`.
- Result includes `blocked_by` and `blocks` fields so the harness can render
  grayed-out blocked tasks without a second query.

### API: `memorize(type="task")`

```
memorize(type="task", content=<structured>, context="/home/max/git/yadgar", tags=["task-list"])
```

- `type="task"` routes to a different drainer path: direct INSERT/UPDATE on the
  `task` table, no embedding.
- Task number assignment is **server-side, atomic** — mirroring `adr_add`'s
  `max+1` pattern. The backend assigns the number in the same transaction as the
  write, so two parallel sessions cannot collide.
- On completion: status → `completed`, wiki detail page deleted (or kept as
  archive — TBD).

### Cache

Core-side pull-through cache, piggybacking on the existing `Cache` class
(`yadgar/backend/cache/cache.py`) with `ScopeVersions` invalidation.

- **Key:** `(project_dir, "task")`
- **Version:** task epoch — bumped on every write
- **Flow:** `recall(type="task")` → cache check → miss → `_forward_admin("task_list", ...)` → backend `SELECT` → cache fill → return
- **Write-through:** `memorize(type="task")` → optimistic cache update → file queue enqueue → backend drainer → INSERT/UPDATE → bump task epoch → all sessions' caches invalidate on next read

### Nightly cleanup

Consolidation cycle deletes completed task rows + their wiki detail pages.
Keeps the table lean without manual intervention.

### Client differences

| | Claude Code | OpenCode |
|---|---|---|
| **Harness task store** | Persistent, per-session files on disk | None — `todowrite` is in-memory only |
| **Yadgar → harness** | Mechanical: SessionStart hook writes via `TaskCreate` | Read-only context injection; model decides whether to call `todowrite` |
| **Harness → yadgar** | Stop-hook reads harness list, writes to yadgar | No stop-hook task mirroring (no persistent store to read from) |

The design works for both — Claude Code keeps its bidirectional sync against the
new table; OpenCode gets read-only context injection.

## Architecture fit

All cross-layer communication via `_forward_admin` (HTTP) — no import-linter
violations.

| Layer | What | Files |
|---|---|---|
| `_shared/storage/` | DDL migration + `TaskStore` mixin (CRUD) | `migrations.py` (new migration entry), `task_store.py` (new) |
| `backend/admin_exec/` | Backend impl: `task_list`, `task_write` ops | `tasks.py` (new) |
| `backend/admin_exec/__init__.py` | Register ops in `_ADMIN_OPS` | edit existing |
| `core/server/tools/` | Type-aware routing in `recall`/`memorize` handlers | `recall.py`, `memorize.py` (edit existing) |
| `core/server/tools/_forward.py` | `_forward_admin("task_list", ...)` / `_forward_admin("task_write", ...)` | edit existing |
| `backend/retrieval/` | `TaskProvider` (or direct backend query, no embedding) | `providers/task.py` (new) or inline in recall_pipeline |
| `core/hooks/` | SessionStart nudge: read task table instead of wiki page | `session-start-context.py` (edit existing) |
| `core/hooks/templates/` | Stop-hook step 5: reconcile against task table | `stop_checkpoint_prompt.md` (edit existing) |
| `CAPABILITY_REGISTRY.md` | New entry (I32) | edit existing |

### What we avoid

- No new MCP tools (reuse `recall`/`memorize`)
- No embedding cost on task reads
- No TOC drift (the TOC *is* the query result)
- No markdown parsing on read
- No task-number collision (server-side atomic assignment)

### What we add

- 1 new table + migration (~20 lines)
- 1 new `_ADMIN_OPS` entry (`task_list`, `task_write`)
- 1 new drainer op type
- Type-aware routing in `recall`/`memorize` core handlers
- ~3 new files (`backend/admin_exec/tasks.py`, `_shared/storage/task_store.py`, migration entry)
- Cache integration (piggyback on existing `Cache` class)

## Migration path

1. Add `task` table + migration (no data migration — the wiki page stays as
   source of truth during transition).
2. Add backend ops (`task_list`, `task_write`).
3. Add type-aware routing in `recall`/`memorize`.
4. Add cache integration.
5. Rewire SessionStart nudge + stop-hook step 5 to use the new table.
6. One-time seed: read the existing `{project}-task-list` wiki page, parse
   markdown, INSERT into `task` table.
7. Delete the old `{project}-task-list` wiki page (or mark it deprecated).
8. Add nightly cleanup of completed rows.

## Estimated impact

| Metric | Current (monolithic wiki) | Old 0047 (2-TOC wiki) | New (task table) |
|---|---|---|---|
| Session-start rehydration | ~24k tok | ~8-10k tok | ~2-4k tok |
| Stop-hook checkpoint read | ~4k tok | ~1.6k tok | ~200-400 tok |
| Embedding cost per read | Full pipeline | Full pipeline (on ~50 pages) | None |
| TOC drift risk | None (single page) | High (separate TOC page) | None (query result) |
| Task number collision | Implicit (single-page lock) | High (parallel sessions) | None (server-side atomic) |
