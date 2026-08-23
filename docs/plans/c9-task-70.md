# C9 — Task 70: Cap `wiki_read` / `wiki_get` output size

## Goal

Stop `wiki_read` from returning the FULL uncut page dict (potentially 10s of
KB of `content`) when callers only need metadata. Today every read returns
the whole `content` blob — the only thing stripped is `embedding`
(`core/server/tools/wiki.py:991`). A 50 KB ADR rollup page on a hot
recall/inspect path therefore drags 50 KB over the MCP boundary on every
call, with no opt-out. The shape mirrors the v5.7.x "uncapped search-result
return" defect class (no max-payload shape), but on the single-page read
path instead of the ranked-search path.

This car adds a per-call cap, applied core-side BEFORE the cache put so a
truncated hit is cached as truncated (consistent across hits/misses).

## Pre-conditions

- File to edit: `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py`
  (function `wiki_read`, lines 914-995).
- The only strip in the current read path is `page.pop("embedding", None)`
  on line 991. `content` returns full.
- The shape is parallel to task 71's `wiki_append_section` cap (same file,
  lines 1451-1527). Two non-overlapping edits, same file, different
  functions — keep them in lockstep when picking the cap value.
- Cap value: 8 192 bytes on `content` (matches the existing
  `wiki_find_similar_pages` window cap at `_shared/wiki/store.py:1239`,
  which slices `content[:4000]` — task 70 doubles it for the read path
  where the caller asked for the page). Cite ADR-style: lower than
  `wiki_update`'s `content_too_large` ceiling of 65 536 bytes
  (`core/server/tools/wiki.py:225-226`), higher than the search window so
  full-page reads stay useful.
- `_wiki_read_cache` (`tools/wiki.py:662-680`) is a byte-bounded LRU
  (`max_bytes=budget`) keyed on `(slug, _current_wiki_epoch(),
  _effective_project_id)` — the cache key already accounts for project
  scoping (Car 2 + Car M). The truncated page stores cleanly because
  `deep_copy=True` is the default and the truncated payload is smaller
  than the uncut.
- `wiki_read_version` (tools/wiki.py:1320) returns the historical VERSION
  row (full content), used by the viz diff/restore surfaces — NOT in
  scope for this car. `wiki_query` returns a RANKED-LIST path with its
  own scoring + budget guard (`tools/wiki.py:683-696`,
  `_namespace_budget_bytes`) — NOT in scope. `wiki_read` is the
  single-page fetch and is the only car 70 target.

## Step-by-step

1. **Open `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py`**.

2. **Insert a constant near the top of the file** (after the imports,
   around line 70 — same module-scope block as the existing
   `_CONTENT_TOO_LARGE` style constants):
   ```python
   _WIKI_READ_CONTENT_CAP_BYTES = 8_192  # task 70; pairs with task 71
   ```
   Comment carries the rationale + cross-reference to task 71 so future
   bumps stay lockstep.

3. **Edit `wiki_read` (lines 914-995)** to cap `content` AFTER the cache
   miss but BEFORE the cache put. The cap lives between `page.pop(...)`
   on line 991 and `_wiki_read_cache.put(_r_key, page)` on line 994:
   - Before (lines 989-995):
     ```python
     if page is None:
         return {"error": f"Wiki page '{slug}' not found"}
     page.pop("embedding", None)
     # Car 2: store the resolved page. deep_copy=True → callers cannot corrupt the
     # cached value, and each hit returns its own isolated copy.
     _wiki_read_cache.put(_r_key, page)
     return page
     ```
   - After:
     ```python
     if page is None:
         return {"error": f"Wiki page '{slug}' not found"}
     page.pop("embedding", None)
     # Car C9 / task 70: cap the returned `content` to a single-payload window
     # (task 71 doubles this for the write side). Mirrors the v5.7.x
     # uncapped-return class but on the single-page read path; keeps hot
     # MCP reads under the byte budget so a 50 KB rollup doesn't drag 50 KB
     # over the boundary. `truncated=True` lets callers re-fetch with a
     # version-pinned path if they need the full body.
     content = page.get("content") or ""
     if isinstance(content, str) and len(content.encode("utf-8")) > _WIKI_READ_CONTENT_CAP_BYTES:
         page["content"] = content[:_WIKI_READ_CONTENT_CAP_BYTES]
         page["content_truncated"] = True
         page["content_total_bytes"] = len(content.encode("utf-8"))
     # Car 2: store the resolved page. deep_copy=True → callers cannot corrupt the
     # cached value, and each hit returns its own isolated copy.
     _wiki_read_cache.put(_r_key, page)
     return page
     ```
   - The cap is applied to the dict in place — same shape returned, with
     three new keys when truncation fires (`content_truncated=True`,
     `content_total_bytes=<int>`). Cached as truncated → consistent
     across hits and misses (no race where one call sees full and the
     next sees truncated).
   - `content_total_bytes` uses `encode("utf-8")` byte length to match
     what the cache's byte budget tracks; char length would be
     incomparable with the byte budget elsewhere in the file.

4. **No change to `wiki_read_version`** (lines 1320-1358). The version
   fetch is a forensic / restore path; truncating it would defeat the
   `wiki_diff` → `wiki_restore` flow. Out of scope per the task title
   (only `wiki_read` / `wiki_get` are named).

5. **No change to `_wiki_read_cache` semantics**. The cap is a payload
   transform, not a key transform — the same `(slug, epoch, project_id)`
   key resolves the same page, and the cache stores the truncated view.
   A wiki write that bumps the epoch (Car 2) still invalidates the entry.

## Verification

- A read of a small page (< 8 KB content) returns the page dict unchanged
  — no `content_truncated` key, `content` byte-identical to the stored
  row.
- A read of a large page (>= 8 KB content) returns `content` truncated
  to 8 192 bytes, plus `content_truncated=True` and
  `content_total_bytes=<actual>`. `content_total_bytes` > 8 192 on every
  truncated hit.
- Two consecutive reads of the same large page return byte-identical
  payloads (proves the truncation is applied to the cache put, not just
  to the first hit).
- A read that goes through `_r_hit` (warm cache) returns the truncated
  view — proves the cache put stores the truncated dict, not the uncut
  one.
- The cache's `_namespace_budget_bytes("wiki_read", total)` budget is
  unaffected: the truncated payload is smaller, so the cache actually
  gets MORE headroom, not less.
- `wiki_read_version` of the same large page returns the full content
  (proves task 70's blast radius is `wiki_read` only).

## Risks / rollback

- **Truncation hides content from callers that wanted the full page**.
  Today the only such caller is the viz's wiki page renderer (loads the
  page body directly via `wiki_read`). The truncated view breaks page
  rendering on > 8 KB pages. Mitigation: the viz re-fetches with a
  version-pinned path (`wiki_read_version`) for full-body needs, OR a
  follow-up car adds a `full=True` opt-out to `wiki_read`. Flagged for
  a follow-up; NOT addressed in this car because the task title scopes
  it to the read path and adding the opt-out is the wrong-shape fix for
  task 70's "uncapped return" defect class.
- **Cap value mismatch with task 71**. Task 71 caps the WRITE-side
  content at the same 8 192 bytes. A page that exceeds 8 192 total
  after an append would be impossible to build, but `wiki_update`'s
  existing 65 536 ceiling means legacy pages can already be > 8 192.
  Document in the cap constant's comment that the read cap MUST stay
  <= the write cap; today both are 8 192.
- **Key collision with `content_truncated` / `content_total_bytes`**.
  These are new top-level keys on the page dict; no other wiki tool
  emits them. If a future tool needs to render a "truncated" badge in
  viz, those keys are the source. Documented inline.
- **Cache-staleness class unchanged**. Car 2's wiki-write-busts-read
  guarantee still holds — the cache is keyed on the epoch, and a write
  bumps the epoch.
- **Rollback**: delete the 4 lines added inside `wiki_read` + the
  constant declaration. Trivially safe; the cap is purely additive.

## Approx LOC + risk class

- LOC: +12 (constant + cap block + comment).
- Risk class: **medium** (changes the read-shape contract for any caller
  that was relying on uncapped content; viz is the named consumer).
- Time cost: <15 min for the edit + a single-page smoke test on the
  live corpus (read a known-large ADR rollup, confirm truncation).

## Source evidence

- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:914-995` —
  `wiki_read` function. Line 991 is the only existing strip; the cap
  inserts between it and the cache put on line 994.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:225-226` —
  `wiki_add`'s `content_too_large` check (65 536 byte ceiling on the
  WRITE side). Task 70's 8 192 is intentionally below this ceiling.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:662-680` —
  `_make_wiki_read_cache` — the byte-bounded LRU. Truncated payloads
  fit more easily, not less.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1235-1279` —
  `_resolve_page_id_by_slug` — slug resolution used by the
  section-patch family. Untouched by task 70 (separate car 272).
- `/home/max/git/yadgar/yadgar/_shared/wiki/store.py:1239` — the
  `content[:4000]` window in `wiki_find_similar_pages`. Doubled by
  task 70 for the read-side cap; both choices documented in the
  constant's comment so a future bump has both anchors.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1451-1527` —
  `wiki_append_section`. Task 71's edit target; paired with task 70 to
  stay lockstep on the cap value.
