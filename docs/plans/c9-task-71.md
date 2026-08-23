# C9 — Task 71: Cap `wiki_append_section` content size

## Goal

Reject `wiki_append_section` calls whose `content` exceeds a per-payload
ceiling, mirroring the `wiki_add` 65 536-byte gate at
`core/server/tools/wiki.py:225-226`. Today `wiki_append_section`
(`tools/wiki.py:1451-1527`) accepts an unbounded `content` string and
forwards it to the storage layer
(`_shared/wiki/store.py:1879-1954`), where `size_after` is computed in
bytes but only echoed back as telemetry — no cap fires. A caller that
appends a 5 MB blob succeeds at the API surface, gets the page record
ballooned, and there is no symmetric refusal to enforce the budget.

This car adds a `content_too_large` refusal on the `content` parameter
ONLY (not on the resulting total page size — that's task 70's concern
on the read side; task 71 is purely the write-side payload cap).

## Pre-conditions

- File to edit: `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py`
  (function `wiki_append_section`, lines 1451-1527).
- Cap value: 8 192 bytes (matches task 70's read-side cap). Documented
  in a shared module-level constant so future bumps stay lockstep.
  The write-side cap MUST be >= the read-side cap (else a page can be
  read fully but not re-built); task 71 keeps them equal.
- Existing pattern to mirror: `wiki_add`'s `content_too_large` check at
  `tools/wiki.py:225-226` (line numbers in the wiki_add body, NOT this
  function):
  ```python
  if len(content) > 65_536:
      return {"stored": False, "reason": "content_too_large", "max_bytes": 65_536}
  ```
- `wiki_append_section` is called via `_forward_admin` (line 1517), so
  the cap lives CORE-side, BEFORE the forward — same shape as
  `wiki_add`'s cap. The storage-side storage layer does NOT need a
  matching cap; core is the chokepoint.
- The secret gate (`gate_or_reject` on line 1507) is already a core-side
  pre-forward check; the cap slots in the same shape, before `_pid` is
  resolved and before `_resolve_page_id_by_slug` is called.

## Step-by-step

1. **Open `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py`**.

2. **Reuse the constant added by task 70**. Both cars share a single
   module-level constant:
   ```python
   _WIKI_APPEND_SECTION_CAP_BYTES = 8_192  # task 71; pairs with task 70
   ```
   Place it next to task 70's `_WIKI_READ_CONTENT_CAP_BYTES` constant
   (added at the same site, around line 70). Comment cross-references
   task 70 so future bumps stay paired.

3. **Edit `wiki_append_section` (lines 1451-1527)** to insert the cap
   check between the secret gate (line 1507-1509) and the slug
   resolution (line 1513):
   - Before (lines 1506-1515):
     ```python
     # I26: secret-gate on written content (STAYS core)
     _gate = gate_or_reject(content, tags=[])
     if _gate is not None:
         return _gate

     # R3 Car 3c: slug→page_id resolution stays core (backend has no git/cwd); the
     # section write forwards keyed by page_id.
     page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
     if page_id is None:
         return {"error": f"Wiki page '{slug}' not found"}
     ```
   - After:
     ```python
     # I26: secret-gate on written content (STAYS core)
     _gate = gate_or_reject(content, tags=[])
     if _gate is not None:
         return _gate

     # Car C9 / task 71: cap the per-call content payload (write side).
     # Mirrors wiki_add's `content_too_large` gate (tools/wiki.py:225-226),
     # halved because section patches are smaller than full pages and the
     # task 70 read cap is paired to this value (same module-level
     # constants block — keep them lockstep). Core is the chokepoint; the
     # storage layer stays unchanged.
     if isinstance(content, str) and len(content.encode("utf-8")) > _WIKI_APPEND_SECTION_CAP_BYTES:
         return {
             "stored": False,
             "reason": "content_too_large",
             "max_bytes": _WIKI_APPEND_SECTION_CAP_BYTES,
         }

     # R3 Car 3c: slug→page_id resolution stays core (backend has no git/cwd); the
     # section write forwards keyed by page_id.
     page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, project_id=_pid)
     if page_id is None:
         return {"error": f"Wiki page '{slug}' not found"}
     ```
   - The cap fires BEFORE the slug resolution, so an oversized call
     doesn't even hit the storage layer. Same byte-length math
     (`encode("utf-8")`) as task 70 — keeps the two ends of the
     read/write contract comparable.
   - Return shape mirrors `wiki_add`: `{"stored": False, "reason":
     "content_too_large", "max_bytes": N}`. NOT a `WikiImmutableError`
     (this is a payload-budget gate, not a mutability lock).

4. **No change to the storage layer**. The storage path at
   `_shared/wiki/store.py:1879-1954` (`append_section` on the
   `WikiStore`) keeps its existing `size_before` / `size_after`
   telemetry; the cap is a CORE-side pre-forward check and storage
   does not need a second one.

5. **No change to the `/admin` op wrapper** at
   `backend/admin_exec/wiki.py:137-152`. The wrapper takes whatever
   core forwards; the refusal already lives upstream of `_forward_admin`.

## Verification

- A `wiki_append_section` call with `content` <= 8 192 bytes succeeds
  (or fails for an unrelated reason — slug not found, etc.); the cap
  does NOT fire.
- A `wiki_append_section` call with `content` > 8 192 bytes returns
  `{"stored": False, "reason": "content_too_large", "max_bytes": 8192}`
  WITHOUT touching the slug resolver or the storage layer. Confirmed
  by:
  - The response shape matches the documented `stored=False` family.
  - The page's `updated_at` is unchanged (proves storage didn't write).
  - The storage-layer `_append_section` span does NOT fire (proves the
    forward was short-circuited).
- A `wiki_append_section` call where the cap fires AFTER the secret
  gate also still returns the cap's shape — the cap check runs only
  when the gate passes, but the gate runs only when no secret-gate
  refusal fires. Order: secret-gate → size-cap → slug-resolution →
  forward.
- The `content_too_large` reason string matches `wiki_add`'s so a
  caller that already special-cases the reason keeps working.

## Risks / rollback

- **Cap value mismatch with task 70**. Task 70 caps the READ-side
  content at the same 8 192 bytes. Today both are equal; if a future
  car moves one, the other must move too. Documented in the constant
  comments AND in the cap-block inline comment. (Locked pair, no drift
  allowed.)
- **Refusal shape differs from `wiki_append_section`'s other
  refusals**. The function's existing refusal family uses
  `{"error": "section_not_found", ...}` (no `stored` key), while the
  cap returns `{"stored": False, "reason": "content_too_large", ...}`
  (no `error` key). This is intentional: the cap is a PAYLOAD refusal
  (same shape as `wiki_add`'s cap), not a slug-resolution refusal.
  Documented in the inline comment. Callers that branch on `stored`
  vs `error` keep working — the two are mutually exclusive in this
  function.
- **No escape hatch (`allow_truncation`-style flag)**. Task 71's cap
  is unconditional — a caller that NEEDS a larger section must use
  `wiki_update` (which has the 65 536-byte ceiling and the
  `allow_truncation` flag from task 271). Mirrors the existing
  `wiki_add` / `wiki_update` split: small section → `append_section`,
  large full-page write → `update`.
- **Cap fires on `content`, not on resulting page size**. A caller
  could append many small sections and balloon a page beyond any
  cap. This is task 70's read-side concern (which truncates the
  read); task 71 deliberately scopes to the per-call payload. A
  "total page size" cap is a different gate and not in scope.
- **Rollback**: delete the 7-line cap block + the constant. Trivially
  safe; the cap is purely additive.

## Approx LOC + risk class

- LOC: +9 (constant + cap block + comment).
- Risk class: **low** (mirrors an existing pattern; refusal shape is
  identical to `wiki_add`'s; no caller should be relying on unbounded
  `content` since the storage layer was already byte-counting).
- Time cost: <10 min for the edit + one smoke test on the live
  corpus.

## Source evidence

- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1451-1527` —
  `wiki_append_section` function. Line 1507-1509 is the secret gate;
  the cap inserts immediately after it. Line 1513 is the slug
  resolution; the cap is before it.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:225-226` —
  `wiki_add`'s `content_too_large` gate. The pattern + shape + reason
  string are mirrored verbatim.
- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:1517-1527` —
  `_forward_admin` call. Untouched; the cap is upstream of the forward.
- `/home/max/git/yadgar/yadgar/_shared/wiki/store.py:1879-1954` —
  `WikiStore.append_section`. Untouched by task 71; storage-side
  telemetry (`size_before` / `size_after`) stays as-is.
- `/home/max/git/yadgar/yadgar/backend/admin_exec/wiki.py:137-152` —
  the `/admin` op wrapper. Untouched; the cap is upstream of the
  forward so the wrapper never sees oversized payloads.
- Task 70 plan (`docs/plans/c9-task-70.md`) — paired cap value;
  both cars' inline comments cross-reference each other.
