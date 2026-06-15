# PLAN — v5.41.1: Wiki versioning transactional atomicity (hotfix)

**Status:** drafted 2026-06-02. Hotfix patch.

**Origin:** v5.41.0 (committed 2026-06-01 night) shipped versioning with best-effort try/except wrapping the version-row INSERT. Tech debt flagged by agent in commit `a1e0e96`: page update can succeed without a version row if the version INSERT silently fails. Violates the plan's "atomic version + write" promise (PLAN_V5_41_0 §S3 — "emit version row inside same SurrealQL transaction as the wiki_page mutation").

**Why now:** the whole point of v5.41 is corruption hardening. A versioning mechanism that silently drops version rows IS a corruption class — exactly what we built v5.41 to prevent. Ship the fix before v5.43/v5.45 etc accumulate on top of the broken pattern.

**Effort estimate:** 0.5-1 calendar day.

**Branch:** `fix/v5.41.1-versioning-transactional` off master.

---

## Problem

Current flow in `yadgar/storage/wiki.py` (post-v5.41.0):

```python
def update_wiki_page(slug, content, ...):
    try:
        snapshot_version_row(slug, current_content)  # best-effort
    except Exception:
        log.warning(...)  # CONTINUES
    db.update(wiki_page, ...)  # PROCEEDS REGARDLESS
```

Failure mode:
1. SurrealKV write fails on the version row (transient I/O, lock contention, schema drift, OOM).
2. Exception swallowed.
3. `wiki_page` row mutates anyway.
4. Version chain has a HOLE.
5. `wiki_history(slug)` returns a non-contiguous timeline.
6. `wiki_restore(slug, version=N)` can't restore between N and N+2 if N+1 was the missing snapshot.

User impact: silent data loss on the audit-trail mechanism that v5.41.0 was supposed to provide.

## Fix

Wrap `(version snapshot INSERT, wiki_page mutation)` in a single SurrealQL transaction. Either both succeed or both roll back.

Reference pattern (already in repo): `upsert_project_init` in `yadgar/storage/project.py` — uses BEGIN / COMMIT for the same atomicity guarantee on `_project_init` writes.

### Changes

**`yadgar/storage/wiki.py`:**

```python
async def insert_wiki_page(self, page_dict):
    async with self._db.txn() as tx:
        version_row = self._build_version_row(page_dict, version=1)
        await tx.query("CREATE wiki_page_version CONTENT $v", {"v": version_row})
        await tx.query("CREATE wiki_page CONTENT $p", {"p": page_dict})
    # If either fails, both rolled back.

async def update_wiki_page(self, slug, fields):
    async with self._db.txn() as tx:
        existing = await tx.query("SELECT * FROM wiki_page WHERE slug = $slug", {"slug": slug})
        if not existing:
            raise WikiPageNotFound(slug)
        prev_version = existing[0].get("current_version", 0)
        version_row = self._build_version_row(existing[0], version=prev_version + 1)
        await tx.query("CREATE wiki_page_version CONTENT $v", {"v": version_row})
        await tx.query("UPDATE wiki_page SET ... WHERE slug = $slug", {...})
```

**Remove the try/except wrapping the snapshot call.** Let exceptions propagate.

**`yadgar/wiki.py`:** caller-side `WikiStore` helpers — already inside the same call chain. No change needed beyond removing any local try/except that masked the version write.

### Embedded mode caveats

- SurrealKV embedded transactions: confirm BEGIN/COMMIT works in same-process embedded mode (`upsert_project_init` does this — verify pattern still holds).
- HTTP transport (separate daemon): standard SurrealQL transaction semantics apply.
- Server-mode and embedded should both succeed in tests (use `_surreal_runner.py` spawn helper for server mode).

## Tests

### Atomicity tests (new)

`yadgar/tests/test_wiki_versioning_atomicity.py`:

1. `test_insert_rollback_on_version_failure` — patch the version INSERT to raise; assert wiki_page row NOT created.
2. `test_update_rollback_on_version_failure` — pre-create page; patch version INSERT to raise; call update; assert wiki_page unchanged (content + updated_at).
3. `test_update_rollback_preserves_version_chain` — pre-create page + 2 versions; patch version INSERT for v3 to raise; call update; assert wiki_page unchanged AND version chain still has v1, v2 only (not v3 partial).
4. `test_concurrent_updates_serialize` — two concurrent updates: assert both versions land OR one fails cleanly (not partial). Tests transaction isolation.
5. `test_happy_path_both_succeed` — baseline: both rows land together.

### Regression tests

- All 38 v5.41.0 tests must still pass.
- Performance: `test_update_under_5ms_p50` — benchmark with 100 updates; assert p50 ≤5ms (I9 budget). Transaction overhead expected <1ms on embedded.

### Failure-injection pattern

Use `unittest.mock.patch.object` on the storage layer to inject a `RuntimeError` at the version-INSERT call site. Must restore mocks after each test.

## Acceptance criteria

1. `insert_wiki_page` + `update_wiki_page` use transaction wrapping.
2. Version INSERT failure → entire write rolls back (no orphan wiki_page mutation, no orphan version row).
3. 5 new atomicity tests + benchmark pass.
4. All 38 v5.41.0 tests still pass.
5. Try/except wrappers around version snapshot REMOVED (let exceptions propagate to caller).
6. Code comments updated — remove "best-effort" / "documented technical debt" notes.
7. Version bumped 5.41.0 → 5.41.1.
8. CHANGELOG + MIGRATION_NOTES updated.

## Non-goals

- No new MCP tools.
- No schema change (table 013 unchanged).
- No retroactive backfill of any missing version rows from v5.41.0 production. If user prod had a version-write failure between v5.41.0 ship and v5.41.1 ship, the gap stays. Document this in MIGRATION_NOTES (tiny window, low risk).
- No transaction wrapping for `wiki_crossref` writes (separate scope; orthogonal).

## Risks

- Transaction overhead on embedded mode. Mitigation: benchmark test enforces ≤5ms p50.
- Embedded BEGIN/COMMIT semantics differ from server. Mitigation: test both modes via `_surreal_runner.py` spawn pattern.
- Existing `wiki_restore` path may have its own try/except that needs removal too. Audit during impl.
- Removing try/except changes failure surface — callers that swallowed version errors silently will now see them. Acceptable per "fail loud not quiet" principle; document in MIGRATION_NOTES.

## Dependencies

- v5.41.0 must be live (✓ shipped 2026-06-01 night).
- No other dependencies.

## Phases (for agent dispatch)

1. **Atomicity tests** — write failing tests first (atomicity #1-5).
   → COMMIT `test(wiki): atomicity regression tests for versioning`
2. **Transactional wrap** — refactor `insert_wiki_page` + `update_wiki_page` to use `db.txn()`. Remove try/except.
   → COMMIT `fix(wiki): wrap version snapshot + page mutation in single transaction`
3. **Audit + cleanup** — remove "best-effort" comments + tech-debt notes from a1e0e96/a1e0e96 commits. Verify no other version writes use try/except masking.
   → COMMIT `chore(wiki): remove best-effort version-write workaround comments`
4. **Version bump + docs** — 5.41.0 → 5.41.1 + CHANGELOG (hotfix style) + MIGRATION_NOTES (note the failure-surface change).
   → COMMIT `chore: bump version 5.41.0 → 5.41.1 + docs`

## References

- v5.41.0 commit `a1e0e96` — introduces the best-effort pattern (debt)
- v5.41.0 task report (2026-06-01 night) — explicit "remaining gap" note
- `yadgar/storage/project.py:upsert_project_init` — canonical transaction pattern
- `yadgar/_surreal_runner.py` — server-mode spawn helper for integration tests
- Plan v5.41.0 §S3 — original intent ("inside same SurrealQL transaction")

## Coordination notes

Single agent dispatch. Sonnet. NO-isolation mode recommended (v5.41 worktree-isolation bug hit b6696b5 base twice; main-worktree dispatch worked). Main thread stays on master while agent works on `fix/v5.41.1-versioning-transactional`.

After ship: roadmap wiki "Recently shipped" + pipeline rows updated.
