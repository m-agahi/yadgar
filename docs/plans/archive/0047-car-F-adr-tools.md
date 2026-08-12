# Car F — ADR tools re-pointed (adr_list / adr_get / adr_add → ledger)

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: shipped in 0047 spine train (Car F integration commit on `car/F-adr-tools`)
> Depends on: B, C
> Lifecycle: ADR-0081/0082 — archived as the first commit of the completing branch.

## 1. Scope

Re-point the three ADR MCP tools — `adr_list`, `adr_get`, `adr_add` — from the
wiki-index-parse read/write path onto the ledger-backed path built by Cars A/B
(`_LedgerMixin.list_adr_rows()` / `create_adr_row()` / `set_adr_body_slug()`,
reachable from core over the §15 core-PTC → backend-PTC → DB chain). Bodies stay
wiki pages in SurrealDB (D4 — NON-NEGOTIABLE); ONLY the metadata/index rows move
to MariaDB. The load-bearing acceptance gate is a **characterization test** that
pins `adr_list` / `adr_get` / `adr_add` return shapes **pre-migration** (today's
wiki-index path) and asserts them **green post-migration** (ledger path), so the
live consumers cited in the §7 row keep working unchanged across the re-point.

This car touches ONLY the three ADR tools' internals. Re-pointing the
`project_brief` consumers (`_build_adr_log`, `_get_adr_log_updated_at`) is Car G.
Deleting the old parser/serializer/lock (`parse_index_rows`,
`_build_index_content`, `_render_index_row`, `_adr_log_lock`) is Car G. The ADR
seed (backfilling the ledger from existing pages) is Car G. Car F re-points the
tools; Car G populates the ledger and removes the dead code.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/core/server/tools/adr.py:143` | `adr_add` — replace the read-assign-write index-page sequence (lines 211-291: `_adr_log_lock` ctx, `wiki_read(index_slug)`, `_next_adr_id`, `_wiki_write_canonical` for the per-ADR page, `_assemble_index_rows`, `_build_index_content`, second `_wiki_write_canonical` for the index, `_flip_superseded_target`) with: write per-ADR body page via `_wiki_write_canonical` (UNCHANGED — D4), then `create_adr_row(project_id, title, status, decided_on, body_slug, …)` over the core PTC → backend path, then supersede-status flip via the ledger join (`adr_supersedes` row insert) instead of the index-row rewrite. Signature unchanged (§4: `adr_add` EXISTING — signatures unchanged). | `def adr_add` confirmed at `adr.py:143` via grep |
| `yadgar/core/server/tools/adr.py:295` | `adr_get` — today returns the raw `wiki_read(slug)` page dict (`adr.py:315`). Per D5: must MERGE the ledger row's metadata into its response, or `date`/`rationale`/`alternatives`/`revisit_trigger`/`supersedes` vanish from its output (the row owns ALL metadata after the split; the page owns ONLY prose). Return shape GAINS `baseline_hash`/`content_hash` per ADR-0209 (§14.3, §4 table). Read path: core PTC fetches the ledger row by `(project_id, adr_id)` + backend fetches the body page from SurrealDB, merged in core. | `def adr_get` confirmed at `adr.py:295`; current `return wiki_read(slug, directory=resolved)` at `adr.py:315` |
| `yadgar/core/server/tools/adr.py:319` | `adr_list` — today reads `<project>-adr-index` via `wiki_read` + `parse_index_rows` (`adr.py:349-353`), filters by status, paginates in-memory. Re-point to `list_adr_rows(project_id, status=…, limit=…, offset=…)` over the core PTC → backend path (indexed scan, §3.5; `adr_list(status=…)` stays a plain indexed scan per D22/§3.5). Return shape UNCHANGED: `{"adrs": [{adr_id, status, date, title, supersedes, superseded_by, slug}, …], "count": N}` plus `total`/`truncated`/`next_offset` when sliced (adr.py:361-366). | `def adr_list` confirmed at `adr.py:319`; current index-read at `adr.py:349-353`; row shape (7 keys) confirmed at `adr_index.py:78-97` docstring + `_INDEX_ROW_RE` |
| `yadgar/tests/core/test_adr_tools_car_f.py` (NEW) | Characterization test — pins pre-migration return shapes for `adr_list`/`adr_get`/`adr_add` against the current wiki-index path, then asserts the same shapes against the re-pointed ledger path. RED first, GREEN after re-point. This is the §7 Car F acceptance gate. | new file; existing test infra at `yadgar/tests/core/test_adr.py` (engine fixture pattern `server.init_engines` at `test_adr.py:44-49`) reused |
| `pyproject.toml:7` + `server.json:10` | core `version` bump per WORKFLOW RULE (ADR tools live in `core/server/tools/`, NOT `backend/` → core bump, NOT `backend_version`; §16.11 Car M confirms `backend-bump: NO` for `core/server/tools/` edits). `scripts/check_versions.py` enforces consistency across pyproject/server.json/docker-compose/uv.lock/flake.nix. | `version = "5.181.0"` at `pyproject.toml:7`, `server.json:10` confirmed; `scripts/check_versions.py` + `scripts/check_version_bump.py` exist |

**NOT touched in this car (Car G scope):** `yadgar/core/server/tools/project.py:1787` (`_build_adr_log`, the `r["adr_id"]` consumer at `project.py:1813`), `yadgar/core/server/tools/adr_render.py:168` (`_assemble_index_rows`, the `parse_index_rows` call at `adr_render.py:181`), `yadgar/core/server/tools/adr_index.py` (`parse_index_rows`/`_build_index_content`/`_render_index_row`/`_next_adr_id`/`_committed_page_max_id`/`_adr_log_lock`). These are cited in the §7 row as LIVE CONSUMERS whose contracts the characterization test protects — not as Car F edit targets. They are deleted/re-pointed in Car G.

## 3. Functions / symbols

**Existing signatures (verified, UNCHANGED per §4):**

```python
# yadgar/core/server/tools/adr.py:143
@_tool(power=True)
def adr_add(
    directory: str, title: str, status: str, date: str,
    context: str, decision: str, rationale: str, alternatives: str,
    consequences: str, revisit_trigger: str, supersedes: str,
) -> dict: ...

# yadgar/core/server/tools/adr.py:295
@_tool(power=True)
def adr_get(directory: str, adr_id: str) -> dict: ...

# yadgar/core/server/tools/adr.py:319
@_tool(power=True)
def adr_list(directory: str, status: str | None = None, limit: int = 50, offset: int = 0) -> dict: ...
```

**Car A/B dependencies (NOT on master today — built by Cars A/B, verified absent):**

```python
# _LedgerMixin (Car A) — backend; core reaches it over HTTP per §15
def list_adr_rows(project_id: str, status: str | None = None,
                  limit: int = 50, offset: int = 0) -> list[dict]: ...
   # each row dict: {id, project_id, title, status, decided_on, subsystem, tier,
   #                  body_slug, created_at, updated_at}  (§3.5 schema)
def create_adr_row(project_id: str, title: str, status: str,
                   decided_on: str | None, body_slug: str | None) -> dict: ...
   # returns {"id": <int>, ...}  — AUTO_INCREMENT id IS the ADR number (ADR-0197)
def set_adr_body_slug(adr_id: int, body_slug: str) -> None: ...
```

[VERIFY: `list_adr_rows` / `create_adr_row` / `set_adr_body_slug` exact signatures
— not on master; defined on the unmerged PR #32 branch. Car A/B may have adjusted
them since §13's review. Confirm against Car A/B's final shape when those cars
merge, and update this doc's §3 before building.]

**New (this car):**

```python
# yadgar/tests/core/test_adr_tools_car_f.py  (NEW — characterization test)
class TestAdrListReturnShape:
    # pins {"adrs": [...7-key rows...], "count": N} + optional total/truncated/next_offset
class TestAdrGetReturnShape:
    # pins the merged row+body dict; pre-migration = raw wiki_read page,
    # post-migration = page dict GAINING row metadata + baseline_hash/content_hash
class TestAdrAddReturnShape:
    # pins {"adr_id": "ADR-NNNN", "slug": "<project>-adr-NNNN"} on success;
    # {"ok": False, "error": "..."} on validation/storage failure
```

## 4. Build steps (TDD)

1. **RED — characterization test (`yadgar/tests/core/test_adr_tools_car_f.py`).**
   Write tests that capture the CURRENT return shapes by exercising the live
   wiki-index path against the embedded engine fixture (reuse the
   `server.init_engines` + `_migration_013_wiki_page_version` pattern from
   `yadgar/tests/core/test_adr.py:40-49`). Pin:
   - `adr_add` success → `set(out) == {"adr_id", "slug"}` and
     `adr_id` matches `ADR-NNNN`; validation failure → `{"ok": False, "error": str}`
     (existing `test_adr.py:73-89` covers validation — extend, do not duplicate).
   - `adr_list` success → `set(out) == {"adrs", "count"}` when untruncated;
     each row `set(row) == {"adr_id","status","date","title","supersedes","superseded_by","slug"}`;
     truncated → adds `total`, `truncated: True`, `next_offset` (existing
     `test_recall_output_cap.py::TestAdrListPagination` at lines 408-475 already
     pins this exactly — reference it, add the row-key assertion).
   - `adr_get` success → returns a wiki-page dict with `content` key (current
     shape); post-migration the SAME dict GAINS row-metadata keys
     (`date`/`rationale`/`alternatives`/`revisit_trigger`/`supersedes` +
     `baseline_hash`/`content_hash` per ADR-0209) — assert the pre-migration keys
     are a SUBSET of the post-migration keys (additive-only contract).
   These tests must FAIL against the re-pointed path (or pass against current and
   pin the shape) — the gate is: same assertions green both before and after.

2. **GREEN — re-point `adr_list` (`adr.py:319-367`).**
   Replace `wiki_read(index_slug)` + `parse_index_rows` + in-memory filter/slice
   (`adr.py:349-359`) with a call to `list_adr_rows(project_id, status, limit,
   offset)` over the core PTC → backend HTTP path (§15). Map the ledger row dict
   (§3.5 keys: `id, project_id, title, status, decided_on, subsystem, tier,
   body_slug, …`) onto the 7-key consumer shape: `adr_id = f"ADR-{id:04d}"`,
   `date = decided_on`, `slug = body_slug`, `supersedes`/`superseded_by` from the
   `adr_supersedes` join (read backwards per §3.5). Preserve the
   `total`/`truncated`/`next_offset` envelope semantics exactly. Derive
   `project_id` via `identity.derive_project_id()` (Car A0) from the `directory`
   arg.

3. **GREEN — re-point `adr_get` (`adr.py:295-315`).**
   Keep the `wiki_read(slug)` body fetch (D4 — body stays in SurrealDB). ADD a
   ledger-row fetch (`list_adr_rows` filtered by id, or a dedicated
   `get_adr_row(project_id, adr_id)` if Car B ships one) and MERGE the row's
   metadata into the response dict per D5. Add `baseline_hash`/`content_hash`
   (ADR-0209). The pre-migration keys (`content`, `slug`, `directory_context`,
   etc. from `wiki_read`) must remain a subset.

4. **GREEN — re-point `adr_add` (`adr.py:143-291`).**
   Keep the per-ADR body-page write (`_wiki_write_canonical(page_payload, wait=True)`,
   adr.py:245) — D4. Replace the index-page write sequence (adr.py:211-285:
   `_adr_log_lock` ctx, `wiki_read(index_slug)`, `_next_adr_id`,
   `_assemble_index_rows`, `_build_index_content`, second `_wiki_write_canonical`)
   with: `row = create_adr_row(project_id, title, status, decided_on=date,
   body_slug=None)` → `adr_id = f"ADR-{row['id']:04d}"` → write body page with
   slug `{project_id}_adr-{id:04d}` (D32 ③ slug scheme, Car L re-slug) →
   `set_adr_body_slug(row['id'], page_slug)`. Supersede targets: insert
   `adr_supersedes` join rows + flip `status='superseded'` on the target rows in
   the same transaction (replaces `_flip_superseded_target`'s best-effort tag
   write; D23 retype stays Car G). Return `{"adr_id": adr_id, "slug": page_slug}`
   (unchanged). Delete the `_adr_log_lock` / `_next_adr_id` /
   `_committed_page_max_id` calls from `adr_add` — these are Car G's deletion
   targets, but `adr_add` must stop calling them in THIS car (it no longer reads
   the index).

5. **REFACTOR — extract the row→consumer-shape mapping** into a helper
   (`_row_to_adr_list_entry(row: dict) -> dict`) so Car G's `_build_adr_log`
   re-point and Car I's analogous agent-prompt mapping reuse it. Keep the helper
   in `adr.py` or `adr_index.py` (NOT `project.py` — avoids the circular import
   noted at `project.py:1799-1800`).

## 5. Acceptance gates

- [ ] Characterization test `yadgar/tests/core/test_adr_tools_car_f.py` GREEN
      against BOTH the pre-migration wiki-index path AND the post-migration
      ledger path (same assertions, same shapes). This is the §7 Car F gate.
- [ ] `adr_list` return shape unchanged: `{"adrs": [7-key rows], "count": N}` +
      optional `total`/`truncated`/`next_offset`; existing
      `yadgar/tests/core/test_recall_output_cap.py::TestAdrListPagination`
      (lines 408-475) stays GREEN without modification.
- [ ] `adr_get` return shape is additive-only: pre-migration keys ⊆
      post-migration keys (D5 merge + ADR-0209 hashes).
- [ ] `adr_add` return shape unchanged on success: `{"adr_id", "slug"}`;
      validation-error shape unchanged: `{"ok": False, "error": str}`.
- [ ] Live consumer `project.py:1813` `r["adr_id"]` (in `_build_adr_log`) keeps
      working — it reads `parse_index_rows` (Car G re-points it, not F), but F's
      re-pointed `adr_list` must not break it. [VERIFY: `_build_adr_log` does NOT
      call `adr_list` — it calls `parse_index_rows` directly (project.py:1802,
      1811). So F's `adr_list` re-point does not affect it. The characterization
      test pins the shape so a future Car G re-point of `_build_adr_log` onto
      `list_adr_rows` inherits the same contract.]
- [ ] Live consumer `adr_render.py:181` (`parse_index_rows` call inside
      `_assemble_index_rows`, called by `adr_add` at adr.py:268) — Car F's
      re-pointed `adr_add` no longer calls `_assemble_index_rows`, so this
      consumer goes DORMANT in F and is DELETED in G. [VERIFY: no other caller of
      `_assemble_index_rows` — grep confirms only `adr.py:268` calls it.]
- [ ] core `version` bumped in `pyproject.toml:7` + `server.json:10`
      (`scripts/check_versions.py` green); backend_version NOT bumped (ADR tools
      are core, not backend — §16.11 Car M reasoning).
- [ ] pre-commit green (ruff, import-linter I32/I33, `check_versions`,
      `check_version_bump`, `check_ledger_chokepoint`).
- [ ] tests pass (`yadgar/tests/core/test_adr.py`, `test_adr_tools_car_f.py`,
      `test_recall_output_cap.py`).

## 6. Sequencing

**Must merge before this car:** B (backend ops + cache — the core PTC → backend
HTTP path and `list_adr_rows`/`create_adr_row`/`set_adr_body_slug` are Car A/B
deliverables), C (identity gate + tag-override + downweight — `adr_list`'s
status filter depends on C2's downweight design and C3's identity gate).

**Waits on this car:** G (ADR seed + retype + `project_brief` re-point — G's seed
populates the ledger F reads from; G deletes the parser/serializer/lock F stops
calling; G re-points `_build_adr_log` onto `list_adr_rows` using F's row→shape
mapping), M (cross-project `project=` param on `adr_add`/`adr_list` — §16.11
Car M names Car F as the owner of that param on the ADR tools).

**Intermediate-state risk:** between F merge and G seed, `adr_list` reads from
an EMPTY ledger (the seed is Car G). Pre-existing ADRs are invisible to
`adr_list` in that window. `adr_get` still works (it reads the body page by
slug, which is unchanged in SurrealDB). This is acceptable in a train but MUST
be noted in MIGRATION_NOTES.md: run Car G's seed immediately after F merges.

## 7. ADRs / decisions

- **D1** — Per-entity tools over a shared `_LedgerMixin`; `adr_*` keep their
  signatures. Load-bearing claim is RETURN-SHAPE stability, not just signatures —
  §7 Car F acceptance is the characterization test that proves it.
- **D4** — Bodies stay wiki pages (SurrealDB). `adr_add`'s body-page write is
  UNCHANGED; `adr_get`'s body fetch is UNCHANGED. Only metadata/index rows move.
- **D5** — Row owns ALL metadata; page owns ONLY prose. `adr_get` MUST merge the
  row into its response or `date`/`rationale`/`alternatives`/`revisit_trigger`/
  `supersedes` vanish. (Also: `wiki_page_types.yaml:28-31` `required:` list +
  `wiki_meta.py:64-95` enforcement change is Car G, not F.)
- **D6 / ADR-0197** — `id` (AUTO_INCREMENT) IS the ADR number. `adr_add` uses
  `row["id"]` as the number; no `_next_adr_id` / `_committed_page_max_id` call.
- **D22** — `recall_disposition` status-driven; `adr_list(status=…)` stays a
  plain indexed scan on the ledger (§3.5).
- **D23** — Supersede = retype `adr` → `adr_superseded`, atomic with status flip.
  The retype mutator + `CANONICAL_PAGE_TYPES` addition is Car G; Car F inserts
  the `adr_supersedes` join row + flips `status` on the target row.
- **D32 ③** — `body_slug = {project_id}_adr-NNNN` (`/`→`_`). Car F's `adr_add`
  writes body pages with the new slug scheme; the 194-page re-slug of pre-existing
  ADRs is Car L.
- **ADR-0209 (§14.3)** — `adr_get` return shape GAINS `baseline_hash`/
  `content_hash` (row-side, changes only on seed/adopt) + `content_hash` (mirrored
  row+page, regenerated on every write — the desync signal).

## 8. Out of scope

- Re-pointing `_build_adr_log` (`project.py:1787`) and `_get_adr_log_updated_at`
  (`project.py:1378-1381`) — Car G.
- Deleting `parse_index_rows`, `_build_index_content`, `_render_index_row`,
  `_next_adr_id`, `_committed_page_max_id`, `_adr_log_lock`, `_assemble_index_rows`
  — Car G (F stops CALLING them from `adr_add`; G removes the definitions).
- ADR seed (backfilling the `adr` ledger table from the 194 existing wiki pages)
  — Car G, one-shot admin op seeded from PAGES not the index (D35a/D35b).
- `adr_superseded` page-type addition + retype mutator + retype of the 12 — Car G.
- Fixing the dead `{project}-adr-log` read at `stop_checkpoint_prompt.md:26-33`
  — Car G.
- Cross-project `project=` param on `adr_add`/`adr_list` — Car M.
- `tier`/`subsystem` fields on `adr` rows + rollup pages — Car H.
- The 194-page wiki re-slug — Car L.
- `wiki_page_types.yaml` / `wiki_meta.py` required-heading change for the
  row-owns-metadata split (D5) — Car G (the page-type/retype work owns it).

## 9. Risks / open questions

- **[VERIFY: Car A/B final `list_adr_rows`/`create_adr_row`/`set_adr_body_slug`
  signatures]** — these are NOT on master today (only on the unmerged, review-failed
  PR #32 branch per §13.2). §13.2 found a systematic `"number"` vs `"id"` key
  mismatch and mocked-shape-vs-real-shape test failures there. Car F MUST build
  against the final Car A/B shapes, not §13's draft. Confirm signatures + return
  dict keys against the merged Car A/B before writing the green-path code.
- **[VERIFY: the "7 test refs" count in the §7 row]** — grep for `adr_list`/
  `adr_get`/`adr_add` across `yadgar/tests/` finds **9** files, not 7:
  1. `yadgar/tests/core/test_adr.py` — DIRECT (tests `adr_add` validation + round-trip).
  2. `yadgar/tests/core/test_recall_output_cap.py` — DIRECT (`TestAdrListPagination` lines 408-475 pins `adr_list` return shape).
  3. `yadgar/tests/core/test_stop_memory_checkpoint_module.py:272` — INCIDENTAL (`"adr_add(" not in reason`).
  4. `yadgar/tests/core/test_tool_pool.py:4` — INCIDENTAL (docstring mention).
  5. `yadgar/tests/core/test_seed_disciplines.py:113` — INCIDENTAL (`"adr_list" in ac` string check).
  6. `yadgar/tests/_shared/test_models_module.py:626` — INCIDENTAL (docstring mention).
  7. `yadgar/tests/hooks/test_stop_hook_template.py:312,558` — INCIDENTAL (template-content assertions).
  8. `yadgar/tests/hooks/test_stop_hook_prompt.py:251,278,284` — INCIDENTAL (reason-exclusion assertions).
  9. `yadgar/tests/scripts/test_v5_53_1_curation_loop.py:447-448` — INCIDENTAL (template-content assertions).
  Only #1 and #2 actually pin return shapes. The §7 row's "7" may predate two
  incidental files being added, or may count only files with executable refs (not
  string mentions). The characterization test must cover #1 and #2's contracts;
  #3-9 are unaffected by the re-point (they assert on template strings, not tool
  outputs) and must stay GREEN unchanged.
- **[VERIFY: `_assemble_index_rows` has exactly one caller]** — grep shows
  `adr.py:268` is the only call site. Car F's re-pointed `adr_add` drops this
  call, leaving `_assemble_index_rows` dormant. Car G deletes it. Confirm no
  other caller exists at build time (the worktree copies under
  `.claude/worktrees/` are not real call sites).
- **[VERIFY: intermediate empty-ledger window]** — between F merge and G seed,
  `adr_list` returns `{"adrs": [], "count": 0}` for pre-existing ADRs. Is this
  acceptable, or should F ship WITH a read-fallback to `parse_index_rows` until G
  seeds? The audited plan splits them (F re-points, G seeds), implying the empty
  window is accepted. Confirm with the user if the window is a problem — the
  alternative is a temporary dual-read in `adr_list` that Car G removes, which
  adds complexity and a second removal pass.
- **[VERIFY: `adr_get` body-page slug under D32 ③]** — pre-migration `adr_get`
  reads slug `<project>-adr-NNNN` (basename-derived, `adr_page_slug` at
  `adr_index.py:54`). Post-migration the slug is `{project_id}_adr-NNNN`
  (project-id-derived). Car L does the 194-page re-slug. So between F and L,
  `adr_get`'s slug construction must handle BOTH schemes (legacy pages not yet
  re-slugged + new pages F writes with the new scheme). Confirm whether `adr_get`
  should try the new slug then fall back to the old, or whether Car L's re-slug
  must precede F. §7 ordering says L ∥ F (both after A0/B/C), so the fallback
  question is real.
