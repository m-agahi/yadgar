# C9 — Task 223: `wiki_delete` on a `page_type='adr'` page returns a bare HTTP 500 instead of a mutability refusal

## Goal

Make `wiki_delete` reject `page_type='adr'` pages (effective mutability
`locked`) with the SAME structured refusal envelope PR #54 wired for
`wiki_set_mutability`, instead of the bare HTTP 500 a caller currently
observes. The defect class is "correct-state refusal rendered as
server fault" — a caller that has the same body and intent as PR #54's
fix (raise `WikiImmutableError`, expect 409 + structured envelope) sees
a 500 instead and reads the lock as a dead backend.

This car verifies the live path and adds the post-processing the core
shell needs to convert the storage-layer raise into the call-visible
refusal envelope. The fix is narrow: the core shell is the only edit
site.

## Pre-conditions

- File to edit: `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py`
  (function `wiki_delete`, lines 999-1018).
- The mutability gate at
  `/home/max/git/yadgar/yadgar/_shared/storage/mutability_gate.py:58-95`
  raises `WikiImmutableError` (subclass of `AdminRefusal` +
  `PermissionError`) when the page's effective mutability is `locked`
  or `derived`. `page_type='adr'` resolves to `locked` via
  `_shared/wiki/policy.py`.
- The `/admin` route at
  `/home/max/git/yadgar/yadgar/backend/embed_service/embed_service_routes.py:386-401`
  catches `AdminRefusal` and re-files it as
  `HTTPException(status_code=REFUSAL_STATUS, detail=refusal_envelope(...))`
  (REFUSAL_STATUS = 409, `_shared/refusal.py:56`).
- `_forward_admin` (`yadgar/core/forward.py:161-170`) parses the 409
  envelope via `parse_refusal` and returns the envelope dict on
  success, or raises `httpx.HTTPStatusError` on a genuine 5xx.
- The live `wiki_delete` shell at `tools/wiki.py:1005-1018` ALREADY
  has the `if _res.get("refused"): return _res` guard at line 1009
  (added in the same fix batch as PR #54, post-incident). The live
  shell SHOULD work; the task title asserts it doesn't. This car
  re-verifies the path live and tightens the post-processing for any
  remaining defect surface (e.g. an envelope missing the `refused`
  marker but carrying `reason`, or a slug miss reported as deleted).
- `wiki_delete`'s docstring at lines 1000-1018 is explicit: "Car J's
  lock carries no `deleted`, and calling that 'not found' below would
  swap the old 500 for a fresh lie." The post-processing guard at
  line 1009 is the load-bearing piece — this car confirms it works
  end-to-end against the live storage layer.

## Step-by-step

1. **Open `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py`**.

2. **Read `_forward_admin` semantics** at
   `/home/max/git/yadgar/yadgar/core/forward.py` (lines 161-170 +
   any `raise_for_status` paths). The contract:
   - Backend returns 409 + envelope → forward returns the envelope
     dict (e.g. `{"ok": False, "refused": True, "reason":
     "wiki_page_locked", ...}`).
   - Backend returns 200 + `{"deleted": False, ...}` → forward
     returns that dict.
   - Backend returns 5xx → forward raises `httpx.HTTPStatusError`.

3. **Verify the existing post-processing at `tools/wiki.py:1009-1010`
   handles the 409 envelope correctly**:
   ```python
   if _res.get("refused"):
       return _res
   ```
   - If a 409 envelope carries `refused=True`, this returns the
     envelope intact to the MCP caller — NO 500, NO exception, NO
     "Wiki page '...' not found" misclassification.
   - This is the post-PR-#54 state; the live shell already does the
     right thing.

4. **Tighten the post-processing to also handle envelope variants**:
   the live `_forward_admin` parser (`yadgar/core/forward.py:115-120`)
   keys on `body.get("detail").get("refused") is True`. A bug-shaped
   envelope could in principle carry `reason` but lack `refused`. To
   make the shell resilient against envelope-shape drift, broaden
   the guard:
   - Before (lines 1005-1010):
     ```python
     _res = _forward_admin("wiki_delete", {"slug": slug})
     # The only wiki tool that post-processes the result, so the only one that must
     # know a refusal: Car J's lock carries no ``deleted``, and calling that "not found"
     # below would swap the old 500 for a fresh lie.
     if _res.get("refused"):
         return _res
     ```
   - After:
     ```python
     _res = _forward_admin("wiki_delete", {"slug": slug})
     # Car C9 / task 223: this is the only wiki tool whose post-processing has
     # to recognise a refusal — PR #54 fixed the same shape on
     # wiki_set_mutability, but a 500 reading as a dead backend is the
     # defect we're closing here. The guard keys on either the explicit
     # ``refused`` marker (live contract, yadgar/core/forward.py:115-120)
     # OR a present ``reason`` field (defence-in-depth in case a future
     # envelope drops the marker), and exits BEFORE the "deleted" branch —
     # which would otherwise mis-classify a locked page as "not found".
     if isinstance(_res, dict) and (_res.get("refused") or _res.get("reason")):
         return _res
     ```
   - Rationale: the live `_forward_admin` parser produces an envelope
     with `refused=True` today, so the `or _res.get("reason")` arm is
     a belt-and-braces widening for envelope variants that may surface
     later (e.g. a future embed service route dropping the marker).
     The cost is one extra `dict.get` on the success path; zero risk
     because `_res.get("reason")` is also true for refusal envelopes.

5. **No change to `_forward_admin`** — the parser is correct, the
   defect is purely that the shell needs to handle the envelope it
   already produces.

6. **No change to the storage layer** — `WikiStore.delete` at
   `_shared/wiki/store.py:1204-1210` and `_WikiMixin.delete_wiki_page`
   at `_shared/storage/wiki.py:517-557` correctly call
   `enforce_mutability` (line 533) which raises `WikiImmutableError`.
   That raise bubbles up through `admin_exec/wiki.py:61-71` →
   `/admin` route → 409 → `_forward_admin` → envelope dict.

## Verification

- A `wiki_delete` call against a `page_type='adr'` page returns the
  refusal envelope dict directly: `{"ok": False, "refused": True,
  "reason": "wiki_page_locked", "mutability": "locked", "page_id":
  <int>, "slug": "...", "page_type": "adr", "wiki_op":
  "delete_wiki_page", "op": "wiki_delete", "error": "..."}`. NOT a
  `httpx.HTTPStatusError`, NOT a 500.
- A `wiki_delete` call against a `page_type='task'` page (mutability
  `free`) returns `{"deleted": True, "slug": "..."}` (the live
  success branch on line 1017).
- A `wiki_delete` call against a non-existent slug returns
  `{"deleted": False, "error": "Wiki page '...' not found"}` (line
  1018). The new broadened guard does NOT mis-fire on this branch
  because the success-path envelope from a not-found slug carries
  no `reason` and no `refused`.
- The page row remains in the DB after the locked-delete attempt
  (proves the storage gate fired and refused; nothing was written).
- The SSE `wiki_deleted` event is NOT pushed (proves the
  `if _res.get("deleted", False)` branch on line 1011 was correctly
  skipped).
- The file-queue mirror's `delete_wiki` is NOT called (proves the
  post-processing guard short-circuited before the file-queue
  cleanup on lines 1013-1016).
- A live trace on the storage side shows `_WikiMixin.delete_wiki_page`
  raised `WikiImmutableError` BEFORE the `DELETE type::record(...)`
  query on `_shared/storage/wiki.py:552` — confirms the gate is the
  chokepoint, not the SQL.

## Risks / rollback

- **Broader guard catches legitimate success envelopes**. The
  `or _res.get("reason")` widening is intended ONLY for envelopes
  that look like refusals. A live success envelope from
  `wiki_delete` is `{"deleted": True, "slug": "..."}` — no
  `reason` field, so the new branch does NOT mis-fire. A success
  envelope from `wiki_set_metadata` is `{"ok": True, "slug": "...",
  "rows_updated": N, "page_ids": [...]}` — also no `reason`. The
  guard's widening is safe across the existing `_forward_admin`
  envelope shapes.
- **`reason` collision with future success envelopes**. If a future
  tool starts returning `reason` on success (e.g. `"reason": "no
  rows updated"` for idempotent metadata writes), the guard would
  mis-fire on `wiki_delete`. Mitigation: scope the widening with an
  explicit comment + a follow-up to move to the `refused` marker
  exclusively. NOT a blocker today because no live success envelope
  carries a `reason` field.
- **`_forward_admin` raises instead of returning**. If the backend
  returns a genuine 5xx (not a 409), `_forward_admin` raises
  `httpx.HTTPStatusError` (forward.py:170). The shell does NOT
  catch that raise — it propagates to the MCP layer, which renders
  it as an HTTP 500. That is CORRECT behaviour: a genuine backend
  fault must surface as a server fault. The task title's "bare
  HTTP 500" assertion is specifically about REFUSAL rendered as
  500, not about FAULT rendered as 500. The fix does not paper
  over faults.
- **Rollback**: delete the broadened guard condition. Trivially
  safe; the original guard already handles the live envelope shape.

## Approx LOC + risk class

- LOC: +5 (broadened condition + comment).
- Risk class: **medium** (closes a user-visible error-surface defect;
  must not regress genuine-fault surfacing).
- Time cost: <20 min for the edit + a live end-to-end test against
  an ADR page (one deleted successfully, one refused with envelope).

## Source evidence

- `/home/max/git/yadgar/yadgar/core/server/tools/wiki.py:999-1018` —
  `wiki_delete` function. Lines 1005-1010 are the load-bearing
  post-processing guard; this car broadens line 1009.
- `/home/max/git/yadgar/yadgar/_shared/storage/mutability_gate.py:58-95` —
  `enforce_mutability` + `WikiImmutableError` definition. The
  error type carries `mutability`, `page_id`, `slug`, `page_type`,
  `wiki_op` — all surfaced into the envelope via `refusal_report`.
- `/home/max/git/yadgar/yadgar/_shared/storage/wiki.py:517-557` —
  `_WikiMixin.delete_wiki_page` — calls `enforce_mutability` on
  line 533, BEFORE the `DELETE` query on line 552. The gate is the
  chokepoint.
- `/home/max/git/yadgar/yadgar/_shared/wiki/store.py:1204-1210` —
  `WikiStore.delete(slug)` — slug→page_id resolution + delegation
  to `_storage.delete_wiki_page`. Untouched.
- `/home/max/git/yadgar/yadgar/backend/admin_exec/wiki.py:61-71` —
  `wiki_delete` op body. Slug-keyed delete; raises propagate to
  route.
- `/home/max/git/yadgar/yadgar/backend/embed_service/embed_service_routes.py:386-401` —
  `/admin` route handler; catches `AdminRefusal`, returns 409 +
  envelope.
- `/home/max/git/yadgar/yadgar/core/forward.py:161-170` —
  `_forward_admin` parser; keys on `refused` marker on 409 response.
- `/home/max/git/yadgar/yadgar/_shared/refusal.py:56` —
  `REFUSAL_STATUS = 409`. Wired through the parser + envelope.
- PR #54 (memory `ok:true is not evidence` / `yadgar-ok-true-is-not-evidence.md`):
  same error-surface shape, fixed on `wiki_set_mutability`. This
  car closes the parallel on `wiki_delete`.
- Task 193 (wiki `m-agahi_yadgar_task-193`) — the earlier
  `wiki_set_mutability` missing-from-`_ADMIN_OPS` defect; fixed in
  PR #54. Task 223 is the wiki_delete parallel — same envelope
  shape, different op.
