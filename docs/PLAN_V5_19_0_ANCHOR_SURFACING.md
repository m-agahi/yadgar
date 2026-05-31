# PLAN — v5.19.0: Anchor Unconditional Surfacing

> **STATUS: SHIPPED v5.19.0 (2026-05-30)**

**Renumbered:** v5.14.0 → v5.19.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — odd-only minors for sequential features, even slots reserved for hotfix patches between them.

**Status:** plan-only — 2026-05-30.
**Version slot:** v5.19.0 — slots after v5.17.0 (write-time contradiction); v5.18.x reserved for hotfixes.
**Effort:** ~1.5 days (implementation + TDD).
**Design source:** wiki page `yadgar-anchor-memory-design-scopes-and-surfacing` (captured 2026-05-18 after repeat "I forgot anchor X" incidents).

---

## Problem

`restore()` calls `storage.get_anchored_memories(limit=20)` which issues:

```sql
SELECT * FROM memory
WHERE is_protected = true AND heat > 0 AND '_anchor' INSIDE tags
AND (valid_until IS NONE OR valid_until > $now)
ORDER BY created_at DESC LIMIT $lim
```

No `directory_context` filter — so global anchors ARE included in theory. However, `restore()` in `yadgar/restoration.py:263` feeds the entire result into a flat `anchored` list which is then deduplicated against `hot_memories`. The problem is that the cap (`REPLAY_MAX_RESTORE_MEMORIES`, typically 20) is shared between global + project anchors and all other ranked content: hot memories and predicted memories compete for the same total slot budget, and `get_anchored_memories` is ordered by `created_at DESC`, meaning a freshly-written project anchor can push old global anchors out.

In `session-start-context.py`, the hook calls `project_brief(directory, mode="catalog")`. The catalog mode calls `_build_anchor_rows_catalog(storage, resolved)` which already does the right two-query split (global + project) and caps each at 20. However, `mode="catalog"` is **deprecated since v5.7.12** — the session-start HTTP endpoint (`hook_session_context`) defaults to `mode="catalog"` and passes the result to `_render_project_brief()`. So anchor surfacing in the hook currently works *only* because the deprecated catalog path happens to do the right thing.

The real failure path is `restore()` (the MCP tool called after `/clear` or `/compact`):

- `restoration.py::restore()` uses `get_anchored_memories()` with no scope split.
- `get_anchored_memories()` is ordered by `created_at DESC`, not by scope priority.
- Global anchors written 6 months ago rank below a project anchor written yesterday.
- With `limit=20`, any project that has 20+ anchors causes global anchors to disappear.

Additionally, the `project_brief(mode="restore")` path used by `restore()` in
`project.py::_project_brief_restore()` correctly calls `_build_anchor_rows_restore()` which
does the two-query split — but `restoration.py::restore()` does NOT use `project_brief`; it
calls `_format_restoration()` with the raw `anchored` list from `get_anchored_memories()`.
These two code paths are inconsistent.

**Root cause:** Two separate restore implementations exist:
1. `project_brief(mode="restore")` — correct, uses two-query scope split.
2. `restoration.py::HippocampalReplay.restore()` — broken, uses flat `get_anchored_memories()`.

The session-start hook calls the broken path indirectly (via catalog mode's `_render`). But the
MCP `restore()` tool calls `HippocampalReplay.restore()` directly.

---

## Design decision (from wiki, 2026-05-18)

Two anchor scopes, both surfaced unconditionally on top of ranked content:

**Scope 1 — Global anchors:** `directory_context IN ('', 'global', 'system')`. Cross-project facts. Hard cap 50. Surfaced ALWAYS regardless of current task or cwd.

**Scope 2 — Project anchors:** `directory_context = <repo-absolute-path>`. Surfaced ALWAYS when restoring from that directory. Hard cap 50.

Other content (hot/predicted/hot_memories): below both anchor buckets, ranked+capped as today.

---

## Files to touch

3 files:

| File | Change |
|---|---|
| `yadgar/storage/memory.py` | New method `get_anchored_memories_scoped(directory, limit)` — two queries, global first then project, merged with dedup |
| `yadgar/restoration.py` | Replace `get_anchored_memories(limit)` call with `get_anchored_memories_scoped(directory, limit)` |
| `yadgar/tests/test_anchor_surfacing.py` | New test file (TDD red-first) |

The session-start hook (`hook_session_context` in `http.py`) uses `project_brief(mode="catalog")` which already calls `_build_anchor_rows_catalog` — that path is correct. No change needed there, but mode should eventually be migrated off deprecated catalog. Tracked as a follow-on (see Open Questions).

---

## Concrete diff sketch

### `yadgar/storage/memory.py` — new method after line 728

```python
def get_anchored_memories_scoped(
    self,
    directory: str,
    limit: int = 20,
) -> list[dict]:
    """Return anchors in scope priority order: global first, then project.

    Two queries, hard cap `limit` each (safety cap 50 per design).
    Global = directory_context IN ('', 'global', 'system').
    Project = directory_context = directory (exact repo root match).
    Deduplicates by memory id. Returns global anchors first, then project.
    No rank-filter applied — anchors surface unconditionally (design §2).

    v5.10.12: replaces flat get_anchored_memories() in restore() path.
    """
    _now = self._now_iso()
    _cap = min(limit, 50)  # hard safety cap

    global_rows = self._q(
        "SELECT * FROM memory "
        "WHERE '_anchor' INSIDE tags AND is_protected = true "
        "AND (directory_context = '' OR directory_context = 'global' "
        "     OR directory_context = 'system') "
        "AND (valid_until IS NONE OR valid_until > $now) "
        "ORDER BY heat DESC LIMIT $lim",
        {"now": _now, "lim": _cap},
    )
    project_rows = self._q(
        "SELECT * FROM memory "
        "WHERE '_anchor' INSIDE tags AND is_protected = true "
        "AND directory_context = $dir "
        "AND (valid_until IS NONE OR valid_until > $now) "
        "ORDER BY heat DESC LIMIT $lim",
        {"dir": directory, "now": _now, "lim": _cap},
    )

    seen: set[int] = set()
    merged: list = []
    for row in global_rows + project_rows:
        mid = self._extract_id(row.get("id"))
        if mid in seen:
            continue
        seen.add(mid)
        merged.append(row)

    return self._rows_to_dicts(merged)
```

### `yadgar/restoration.py` — line 263 change

**Before:**
```python
# 2. Get anchored memories (always included)
anchored = self._storage.get_anchored_memories(limit=max_memories)
```

**After:**
```python
# 2. Get anchored memories (always included, scope-split: global first then project)
anchored = self._storage.get_anchored_memories_scoped(
    directory=directory, limit=max_memories
)
```

No other changes needed in `restoration.py` — the rest of the function (dedup against hot/predicted, format) works correctly once the anchor list is properly scoped.

---

## TDD test plan

New file: `yadgar/tests/test_anchor_surfacing.py`

Tests must be **red-first** — write assertions before implementation, verify they fail, then implement.

```python
# test_anchor_surfacing.py outline

class TestGetAnchoredMemoriesScoped:
    """Tests for storage.get_anchored_memories_scoped()."""

    def test_global_anchor_surfaces_in_unrelated_project(storage):
        """Global anchor (directory_context='global') appears when
        restore() is called with a completely unrelated directory.
        This is the core regression test for the 2026-05-18 incident."""
        # seed: 1 global anchor + 20 project-B anchors (to fill the limit)
        # call: get_anchored_memories_scoped(directory=project_A_path, limit=20)
        # assert: global anchor is in result despite project_A having no anchors

    def test_project_anchor_does_not_surface_in_other_project(storage):
        """Project-scoped anchor for repo-A does NOT appear when
        restoring from repo-B context."""
        # seed: 1 project anchor for /repos/A
        # call: get_anchored_memories_scoped(directory='/repos/B', limit=20)
        # assert: anchor NOT in result

    def test_global_anchors_appear_before_project_anchors(storage):
        """Global anchors are returned first in the merged list."""
        # seed: 1 global + 1 project anchor, project has higher heat
        # assert: global anchor is index 0 despite lower heat

    def test_deduplication_when_anchor_matches_both_scopes(storage):
        """An anchor row matching global AND project scopes appears once."""
        # seed: anchor with directory_context='' and another with resolved path
        # having same content (edge case: same id won't happen, but two rows)
        # assert: no duplicates in result list

    def test_hard_cap_50_enforced(storage):
        """Result never exceeds 50 entries even when limit > 50."""
        # seed: 60 global anchors
        # call: get_anchored_memories_scoped(directory='...', limit=100)
        # assert: len(result) <= 50

    def test_expired_anchors_excluded(storage):
        """Anchors with valid_until in the past are excluded."""
        # seed: 1 global anchor with valid_until = yesterday
        # assert: not in result


class TestRestoreUsesScope:
    """Integration: HippocampalReplay.restore() surfaces global anchors."""

    def test_restore_includes_global_anchor_from_different_project(replay, storage):
        """After fix: restore(directory=project_A) returns global anchor
        that was written while working on project_B."""
        # This test FAILS before fix (get_anchored_memories returns [] or
        # omits global when 20 project anchors fill the cap).
        # seed: global anchor + checkpoint for project_A + 20 project_A anchors
        # call: replay.restore(directory=project_A)
        # assert: 'anchored_memories' count includes global anchor
        # assert: formatted text contains global anchor content

    def test_restore_does_not_include_other_project_anchors(replay, storage):
        """restore(directory=project_A) excludes project_B anchors."""
        # seed: 1 anchor for project_B
        # call: replay.restore(directory=project_A)
        # assert: project_B anchor content NOT in formatted text
```

---

## Acceptance criteria

1. `pytest yadgar/tests/test_anchor_surfacing.py` — all tests green.
2. `pytest yadgar/tests/test_anchor_hygiene_schema.py` — no regressions.
3. Existing `test_session_start_context_hook.py` suite unchanged and green (hook path not touched).
4. Manual smoke: seed a global anchor in one project, switch directory, call `restore()` — global anchor appears in returned `formatted` markdown.
5. No schema migration required — `directory_context` field exists. No new DB columns.
6. Invariant lints pass: `python scripts/check_invariants.py` (or equivalent).

---

## Migration story

**No schema migration.** `directory_context` already exists on all anchor rows.

Existing anchors with `directory_context = ''` (empty string): the new query includes them in the global bucket via `directory_context IN ('', 'global', 'system')` — same as `_build_anchor_rows_catalog`/`_build_anchor_rows_restore` in `project.py`. Consistent.

Existing anchors with `directory_context = None`: these are edge cases from pre-v5.8 rows. The new query does NOT include `NULL` in the global bucket (intentional — NULL means "unscoped, unintentional" not "global"). They will no longer surface. This is correct behavior — anchors should have an explicit scope. The `check_invariants` / `audit_anchors` tooling should surface them as hygiene items.

**Optional follow-on (not v5.10.12):** one-time SQL to normalize legacy `directory_context IN ('', 'system')` rows to `'global'` (as specified in the wiki design). Tracked as open question below.

---

## Heat / TTL interaction

Anchors are `is_protected=True` which prevents heat decay in consolidation passes (`get_memories_by_heat` filters `is_protected = false`). No change needed. The `valid_until` field is already checked in both the old and new queries.

The new `ORDER BY heat DESC` on global/project queries (vs old `ORDER BY created_at DESC`) is a deliberate improvement: within each scope, most-accessed anchors surface first. The old `created_at DESC` was arbitrary.

---

## Effort estimate

| Task | Hours |
|---|---|
| Write TDD tests (red-first, verify failure) | 2 |
| Implement `get_anchored_memories_scoped` | 1 |
| Wire into `restoration.py` | 0.5 |
| Run tests green + lint | 1 |
| Manual smoke test | 0.5 |
| Commit + version bump notes | 0.5 |
| **Total** | **~5.5h (~1 day)** |

Buffer for hook-path audit and unexpected edge cases: +0.5 day. Total: **~1.5 days**.

---

## What is explicitly OUT of scope

- Session-start hook migration from deprecated `mode="catalog"` to `mode="restore"` — tracked separately. Hook path works correctly today via catalog; no regression.
- `directory_context` normalization SQL ('' → 'global') — safe follow-on, not blocking.
- Cross-project dedup detection — covered by `PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md`.
- `is_protected` repurpose — also v5.11.

---

## Open questions

1. **`mode="catalog"` deprecation in session hook** — `hook_session_context` passes `mode="catalog"` (deprecated v5.7.12, removal targeted v5.8). Should the hook be moved to `mode="restore"` as part of this PR? Lean: **no** — the catalog path is a superset of restore and the hook-side rendering (`_render_project_brief`) is only wired to catalog/full. Migrate in a dedicated PR that also updates `_render` to handle `mode="restore"`. Not v5.10.12 scope.

2. **`directory_context = NULL` rows** — should the new `get_anchored_memories_scoped` treat `NULL` as global (include in global bucket)? Current lean: **no** — `NULL` means misconfigured, not global. Surface via `audit_anchors` instead. Revisit if real-world NULL rows are found at non-trivial count.

3. **`REPLAY_MAX_RESTORE_MEMORIES` still shared cap** — post-fix, the anchor budget is `limit` per scope (global + project), up to 50 each, for a possible 100-anchor payload before hot/predicted memories even start. This exceeds the `<800 tokens` target for restore mode. Should the restore call pass a lower limit to `get_anchored_memories_scoped`? Lean: default limit=20 is safe (50-cap is only a safety rail). The design doc says >50 anchors = signal to consolidate; that's a user problem, not a code problem. Log a warning if total anchor count > 40 at restore time.

4. **Two restore implementations co-existing** — `restoration.py::HippocampalReplay.restore()` and `project_brief(mode="restore")` in `project.py` now both have correct scope logic but are separate code paths. Long-term, one should delegate to the other. Lean: have `HippocampalReplay.restore()` call `_build_anchor_rows_restore()` from `project.py` instead of maintaining a parallel query. Deferred to post-v5.10.12 refactor.

---

## Sequencing

v5.10.11 (security hotfix) → **v5.19.0 (this plan)** → v5.21.0 (cross-project dedup).

v5.19.0 is a prerequisite for v5.21.0 to be meaningful: cross-project dedup works on anchors that actually surface. If global anchors don't surface, cross-project dedup recommendations are never acted on.

---

## Related

- Wiki: `yadgar-anchor-memory-design-scopes-and-surfacing`
- `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` — next milestone
- `yadgar/storage/memory.py:714` — `get_anchored_memories` (to be supplemented)
- `yadgar/restoration.py:263` — call site (to be changed)
- `yadgar/server/tools/project.py:510` — `_build_anchor_rows_restore` (reference impl, already correct)
