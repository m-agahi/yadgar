# C9 — Task 272: `wiki_append_section` cannot resolve `agent-prompt-pr-review` — the page lacks the `global` reach tag siblings carry

## Goal

Make `wiki_append_section` (and the wider `_resolve_page_id_by_slug`
family — `wiki_restore`, `wiki_history`, `wiki_diff`, `wiki_read_version`)
find cross-project `agent-prompt-*` pages the same way `wiki_read` does.
Today three behaviours diverge for the same slug:

| Tool | Resolver | Path taken | Outcome on `agent-prompt-pr-review` |
|---|---|---|---|
| `wiki_read` | `_resolve_wiki_read_project` (strict) → `read_by_project` | always project-keyed; rung 2 = `$reach IN tags` | FOUND |
| `wiki_restore` | `_resolve_slug_scope_project` (tolerant) → `_resolve_page_id_by_slug` → `read_by_project` (if `_pid` resolves) else `read_by_directory` | project-keyed when `_pid` resolves; directory-keyed otherwise | FOUND when caller passes `project=`; MISS otherwise |
| `wiki_append_section` | `_resolve_slug_scope_project` → `_resolve_page_id_by_slug` → same shape as `wiki_restore` | same | FOUND when caller passes `project=`; MISS otherwise |

The `agent-prompt-pr-review` page lacks the `GLOBAL_REACH_TAG = "global"`
sibling pages carry (the Car C7 reach tag, added by
`_backfill_global_reach` at `_shared/wiki/store.py:66-70` only when
`policy.storage_scope == "global"` AND the page already has `global`
in its tags). Three tools miss the row when the caller doesn't pass
`project=`. The viz browser counterparts (`api_wiki_history`,
`api_wiki_read_version`, `api_wiki_diff`, `api_wiki_restore` in
`http_wiki_versioning.py`) call positionally with no project, so they
hit the directory-keyed rung 2 — which requires
`directory_context = 'global'`, not `$reach IN tags`.

This car backfills the missing `global` tag on `agent-prompt-pr-review`
(the source-of-record fix) AND closes the resolution gap in
`_resolve_page_id_by_slug` so the directory-keyed fallback ALSO
consults the Car C7 reach tag (the structural fix). Both fixes are
needed: the backfill repairs the live row, the resolution change
prevents the next page in the same shape from re-introducing the
defect.

## Pre-conditions

- Files to edit:
  - `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py` —
    `_resolve_page_id_by_slug` at lines 1235-1279 (resolution fix).
  - `/home/max/git/yadgar/yadgar/_shared/storage/wiki.py` —
    `get_wiki_page_by_slug_directory` at lines 442-464 (resolution
    fix, rung 2 widening).
  - A live data fix to add the `global` tag to the
    `agent-prompt-pr-review` row. Documented but NOT executed in
    this car (data fix is an operator step; this car is the plan).
- `GLOBAL_REACH_TAG = "global"` defined at
  `/home/max/git/yadgar/yadgar/_shared/storage/directory.py:102`.
  Imported into `get_wiki_page_by_slug_directory`'s module by
  `_shared/wiki/store.py:21`.
- The Car C7 reach ladder is at
  `_shared/wiki/store.py:66-70`: `storage_scope == "global"` →
  inject `GLOBAL_REACH_TAG` into the row's tags at write time.
  The backfill is a one-shot data fix; the resolution change makes
  the next row written under the same conditions auto-findable from
  every tool.
- `_resolve_page_id_by_slug` (tools/wiki.py:1235-1279) is the shared
  resolver behind `wiki_restore`, `wiki_history`, `wiki_read_version`,
  `wiki_diff`, `wiki_append_section`, and every `*_text` /
  `*_at` / `*_block` tool. The resolution change touches every
  one of them; that's the POINT (closing the divergence on a single
  rung).

## Step-by-step

### Source-of-record fix (data)

1. **Identify the row**:
   - `wiki_page` row with `slug = "agent-prompt-pr-review"` AND
     `tags` array does NOT contain `"global"`.
   - The row's `project_id` is `local/aws-work` (test fixture in
     `/home/max/git/yadgar/yadgar/tests/core/test_wiki_read_car_w2_project_scope.py:230-249`
     records the expected row shape). `directory_context` may be
     `"local/aws-work"` or `"global"` depending on how the row was
     seeded.

2. **Backfill the tag** via the data-fix operator path (NOT in this
   car; documented as the operator step):
   ```
   UPDATE wiki_page SET tags = array::append(tags, 'global')
     WHERE slug = 'agent-prompt-pr-review'
       AND 'global' NOT IN tags
   ```
   - Mirrors `_backfill_global_reach` at
     `_shared/wiki/store.py:66-70` (which only runs on writes).
   - Run after the resolution fix lands so the fix lands first.

### Structural fix (resolution)

3. **Open
   `/home/max/git/yadgar/yadgar/_shared/storage/wiki.py`**.

4. **Edit `get_wiki_page_by_slug_directory` (lines 442-464)** so the
   directory-keyed ladder ALSO consults the Car C7 reach tag on rung 2:
   - Before (lines 442-464):
     ```python
     if caller_directory is None:
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug LIMIT 1",
             {"slug": slug},
         )
         return self._row_to_dict(rows[0]) if rows else None

         caller_dir = caller_directory.rstrip("/")

         # Step 1: project-scoped
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug AND directory_context = $dir LIMIT 1",
             {"slug": slug, "dir": caller_dir},
         )
         if rows:
             return self._row_to_dict(rows[0])

         # Step 2: global fallback
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug AND directory_context = 'global' LIMIT 1",
             {"slug": slug},
         )
         return self._row_to_dict(rows[0]) if rows else None
     ```
   - After:
     ```python
     if caller_directory is None:
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug LIMIT 1",
             {"slug": slug},
         )
         return self._row_to_dict(rows[0]) if rows else None

         caller_dir = caller_directory.rstrip("/")

         # Step 1: project-scoped
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug AND directory_context = $dir LIMIT 1",
             {"slug": slug, "dir": caller_dir},
         )
         if rows:
             return self._row_to_dict(rows[0])

         # Step 2: directory='global' fallback (legacy reach shape, ADR-0171 era)
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug AND directory_context = 'global' LIMIT 1",
             {"slug": slug},
         )
         if rows:
             return self._row_to_dict(rows[0])

         # Step 3 (Car C9 / task 272): Car C7 tag-based reach (post-ADR-0171).
         # The directory-keyed ladder used to STOP at rung 2, so a row whose
         # reach rides on the `global` tag but whose directory_context is
         # something else (e.g. 'local/aws-work') was unreachable from any
         # tool that fell through to this resolver — `wiki_append_section`,
         # `wiki_restore`, `wiki_history`, `wiki_diff`, `wiki_read_version`,
         # and the entire `*_text` / `*_at` family. Mirror the project-keyed
         # ladder's reach rung here so the directory-keyed fallback finds
         # the same rows `wiki_read` does.
         rows = self._q(
             "SELECT * FROM wiki_page WHERE slug = $slug AND $reach IN tags LIMIT 1",
             {"slug": slug, "reach": GLOBAL_REACH_TAG},
         )
         return self._row_to_dict(rows[0]) if rows else None
     ```
   - The rung-3 query mirrors the project-keyed ladder's rung 2 at
     lines 509-513 (`get_wiki_page_by_slug_project`). Two queries, NOT
     one `OR` — same shape ADR-0233 car W4 argued for the project
     side; with `LIMIT 1` an `OR` would pick arbitrarily between
     the rung-2 `directory_context='global'` row and a rung-3
     tag-reach row sharing the slug.
   - `GLOBAL_REACH_TAG` is already imported into the module's import
     chain via `_shared/storage/directory.py:102` (re-exported by
     `_shared/wiki/store.py:21`); verify the import is present in
     `_shared/storage/wiki.py` before relying on it. If not, add
     `from yadgar._shared.storage.directory import GLOBAL_REACH_TAG`
     to the imports block at the top of the file.

5. **No change to `_resolve_page_id_by_slug`** (tools/wiki.py:1235-1279).
   The function is correct: it prefers `read_by_project` when `_pid`
   resolves, falls back to `read_by_directory` otherwise. The fix is
   in `read_by_directory`'s SQL — adding rung 3 there propagates to
   every caller without further edits.

6. **No change to `wiki_read`**. The project-keyed path was already
   correct (Car W2 / Car M).

### Verification (source-of-record)

7. After the resolution fix lands AND the backfill runs, verify:
   - `wiki_read("agent-prompt-pr-review")` returns the page (live:
     already does).
   - `wiki_append_section("agent-prompt-pr-review", "## New heading",
     "content", project=None, directory=None)` returns the
     append result (FIXED — was returning
     `{"error": "Wiki page 'agent-prompt-pr-review' not found"}`).
   - `wiki_restore("agent-prompt-pr-review", version=1)` returns the
     restore result (FIXED).
   - `wiki_history("agent-prompt-pr-review")` returns the version list
     (FIXED).
   - All of the above also work with `project="m-agahi/yadgar"`
     supplied — the project-keyed path was already correct.

## Verification (regression)

- A `wiki_read` of a page whose `directory_context='global'` and
  tags does NOT contain `"global"` STILL finds it (rung 2 catches
  it via the directory column; rung 3 is additive).
- A `wiki_append_section` against a slug that exists in two projects
  (e.g. one row in `m-agahi/yadgar` with tags without `global`, one
  row in `local/aws-work` with `global` tag) resolves the
  caller's-own-project row first (rung 1 hits). No `LIMIT 1`
  arbitrariness introduced by rung 3.
- A `wiki_append_section` against a slug that does NOT exist
  anywhere returns `{"error": "Wiki page '...' not found"}` (all
  three rungs miss).
- The `get_wiki_page_by_slug_project` path is unchanged — same
  ladder, same queries, same `LIMIT 1` semantics. Project-keyed
  callers see no behaviour change.

## Risks / rollback

- **Rung-3 widening exposes cross-project reads to caller
  misrouting**. A caller in tree A falling through to rung 3 might
  now find a tree-B row carrying the `global` tag that it would
  previously have missed. Mitigated by the existing ADR-0227 / ADR-0233
  guard at the TOOL boundary: tools raise / refuse when no project
  identity resolves (so rung 3 only fires when the caller has
  already passed identity). For `wiki_append_section` etc. the
  guard is `_resolve_slug_scope_project`'s
  `UnresolvedProjectError → None` fallback, which is the existing
  tolerant behaviour — rung 3 widens the TOLERANT path only,
  not the strict `wiki_read` path. Same blast radius as the existing
  rung 2 (`directory_context='global'`).
- **Rung 3 widens the `caller_directory is None` branch too**. A
  caller that passes NO directory lands on the unscoped `LIMIT 1`
  rung (lines 443-447); rung 3 is below the `caller_directory is None`
  return, so it only fires for callers that supplied a directory.
  The unscoped-`LIMIT 1` rung is unchanged.
- **Backfill is operator-driven, NOT in this car**. The data fix
  is a separate operational step (run the `UPDATE` against the live
  DB). The structural fix is the durable protection; the backfill
  is the immediate unblock.
- **Rollback**: revert the rung-3 SQL + import. Trivially safe;
  the directory ladder was unchanged for years and the only
  behavioural shift is "reach-tag-only rows are now findable from
  the directory-keyed fallback".

## Approx LOC + risk class

- LOC: +12 (rung-3 query + comment + import line if not already
  present).
- Risk class: **medium** (closes a cross-tool resolution divergence
  on the `agent-prompt-*` library; the rung-3 widening touches every
  tool that falls through to `read_by_directory`).
- Time cost: <30 min for the edit + a live smoke test against
  `agent-prompt-pr-review` + a regression check on a known
  directory-only row.

## Source evidence

- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1235-1279` —
  `_resolve_page_id_by_slug`. Untouched by this car; the fix lives
  one layer down in `read_by_directory`.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1451-1527` —
  `wiki_append_section` (task 71's edit site). The same function is
  the user-visible symptom of task 272; the fix is in the resolver
  it shares with `wiki_restore`, `wiki_history`, `wiki_diff`,
  `wiki_read_version`.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1198-1232` —
  `_resolve_slug_scope_project` — tolerant resolver. Unchanged;
  documents WHY the family is tolerant (the four `http_wiki_versioning.py`
  endpoints call positionally).
- `/home/max/git/yadgar/yadgar/_shared/storage/wiki.py:442-464` —
  `get_wiki_page_by_slug_directory`. THIS CAR'S edit site. Rung 3
  inserts below the existing rung 2.
- `/home/max/git/yadgar/yadgar/_shared/storage/wiki.py:466-514` —
  `get_wiki_page_by_slug_project` (the project-keyed twin). Its
  rung 2 (`$reach IN tags` on lines 509-513) is the model for this
  car's rung 3.
- `/home/max/git/yadgar/yadgar/_shared/storage/directory.py:102` —
  `GLOBAL_REACH_TAG = "global"`. The literal value used in rung 3's
  SQL.
- `/home/max/git/yadgar/yadgar/_shared/wiki/store.py:21` —
  imports `GLOBAL_REACH_TAG` from `directory.py`. Confirms the
  constant is reachable from the wiki module; verify the import
  is also present in `_shared/storage/wiki.py`.
- `/home/max/git/yadgar/yadgar/_shared/wiki/store.py:66-70` —
  `_backfill_global_reach` — the write-time tag-injection. The
  backfill operator step mirrors this logic.
- `/home/max/git/yadgar/yadgar/_shared/wiki/policy.py` — defines
  `get_effective_mutability` and `storage_scope` per page_type.
  Car C7's reach tag rides on `policy.storage_scope == "global"`,
  which is the trigger for tag injection.
- `/home/max/git/yadgar/yadgar/tests/core/test_wiki_read_car_w2_project_scope.py:230-249`
  — the test fixture recording the `agent-prompt-pr-review` row
  shape: `tags=[_REACH_TAG, "agent-prompt"]`. `_REACH_TAG` here IS
  `GLOBAL_REACH_TAG = "global"`; the fixture proves the sibling
  pages carry the tag and `agent-prompt-pr-review` does not (which
  is the source-of-record defect).
- ADR-0171 — the decision that agent-prompt pages are global-scoped
  by schema (`policy.storage_scope == "global"`), the foundation
  for the `GLOBAL_REACH_TAG` mechanism.
- ADR-0233 — the project-keyed re-key of §25 resolution; the
  foundation for `get_wiki_page_by_slug_project`. This car mirrors
  its rung-2 reach into the directory-keyed ladder.
