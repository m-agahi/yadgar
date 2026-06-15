# PLAN — v5.41.0: Wiki Versioning + Section-Patching (Phase 1+2 toolset)

**Status:** DRAFT — 2026-05-31. Plan-first per I27.

**Slot:** v5.41.0 (renumbered after viz overhaul moved v5.41 → v5.50). No prior plan content; this is a fresh slot.

**Depends on:** v5.26.0 ships first (master moves before this plan's implementer branches off). No code-level coupling, only branch ordering.

**Supersedes:** nothing.

**Two-mitigation framing:**
- Wiki *versioning* (snapshot per update + `wiki_history` / `wiki_read_version` / `wiki_diff` / `wiki_restore`) is the **recovery** path — turns any future corruption into a `wiki_restore(slug, N-1)` one-liner instead of a multi-hour archive dig.
- `wiki_append_section` is the **prevention** path — gives callers a section-atomic write primitive so they stop reaching for `wiki_update(content=<short patch>)` (the failure mode that caused the 2026-05-31 incident).

Bundling both in one ship because they share the same TDD scaffolding, schema migration window, and write-path audit.

---

## Background — the 2026-05-31 wiki corruption incident

On 2026-05-31, the roadmap wiki page (id `6163`, slug `yadgar-roadmap-future-improvements`) — historically ~250 KB of curated multi-section roadmap content — was discovered truncated to a 4-entry stub.

Forensic reconstruction (from `~/.yadgar/archive/wiki/2026-05-30/` daily snapshots):

1. The full page existed at 2026-05-30T00:00:00Z snapshot — ~250 KB, dozens of sections.
2. Multiple ship-agents called `wiki_update(page_id=6163, fields={"content": "<short patch covering just their section>"})` over the day.
3. Each call replaced the entire `content` field with the agent's short patch — there is no read-modify-write enforced by the tool surface, and agents did not consistently do it manually.
4. The last agent's short patch became the new "current" content. Prior versions existed only on the daily archive disk.

Recovery: hand-dug the most-recent-pre-corruption snapshot out of `~/.yadgar/archive/wiki/2026-05-30/yadgar-roadmap-future-improvements.md`, merged remembered edits manually, re-wrote via `wiki_update`. Took ~90 minutes. Final restored content lost some intermediate-day edits that never landed in the daily archive (window: between archive run and corruption).

**Why this class of incident keeps being possible today:**

| Surface | Behaviour |
|---|---|
| `wiki_read(slug)` | returns latest content only |
| `wiki_update(page_id, fields={"content": X})` | replaces entire content field, no merge, no warn-on-shrink, no version history |
| `~/.yadgar/archive/wiki/YYYY-MM-DD/` | daily snapshot — at-best 24-hour recovery granularity, file-system-bound |
| no in-DB history | recovery requires shell + disk + manual diff |

**Why versioning prevents the next one:**

- Every `wiki_update` writes a new `wiki_page_version` row containing the full pre-update content. The `wiki_page` "current pointer" is updated atomically.
- A corrupting short-content overwrite still happens (we can't tell at write time whether a 200-byte patch is intentional or a bug), but the prior 250 KB version is one `wiki_restore(slug, N-1)` away. Mean-time-to-recovery: seconds.
- Daily disk archive stays as belt-and-braces (defense in depth — what if the SurrealDB file itself corrupts?).

**Why `wiki_append_section` prevents it more directly:** if callers wanting to "add a follow-up to the Pipeline section" had a section-atomic primitive, they would never write a full-content patch. The corruption pattern doesn't fire at all.

---

## Goal

Add full version history to every wiki page (storage-level snapshots, no breaking API change to `wiki_read`), expose 4 read/restore MCP tools for navigating it, and ship a 5th section-atomic write tool (`wiki_append_section`) that prevents the overwrite-corruption pattern at the source.

After v5.41.0 ships, recovery from any future wiki corruption is one MCP call. Prevention of the most common cause is one new primitive.

---

## Non-goals (explicit)

- **No wiki branching / merge semantics.** Versioning is linear append-only per page. Multi-writer reconciliation is out of scope. (Single-user yadgar; no need.)
- **No conflict detection across concurrent edits.** Two `wiki_update` calls landing inside one tick: both produce versions, last write wins on the pointer. Optimistic-lock `expected_version` parameter is offered as a defense-in-depth option (Open Question 4), not the default.
- **No version garbage collection.** Keep every version forever. ~250 KB × N pages × N versions is negligible at single-user scale (estimate <100 MB/year even at heavy use). Add GC later only if measured-cause.
- **No distributed multi-writer / replication.** Single-DB-node assumption preserved.
- **No version pruning UI / archival policy.** All historical versions remain queryable via `wiki_read_version`. If a sensitive value ever lands in wiki content, manual deletion via storage-level SQL is the (rare) escape hatch — not via MCP.
- **No automatic `change_summary` LLM-generation.** Summary is rule-based diff-stats only (see Scope §change-summary). LLM-summarised commit messages can be added in a later slot.
- **No `wiki_blame` / per-line authorship attribution.** Different problem, different design. Defer.

---

## Current state (verified from code, 2026-05-31)

**`wiki_page` table** (SCHEMALESS, defined in `yadgar/storage/migrations.py:366`):

Columns observed via `yadgar/storage/wiki.py:insert_wiki_page`:
- `id` (int, table-record PK)
- `title`, `slug`, `content` (str)
- `category` (str), `tags` (list[str]), `links` (list[str])
- `confidence` (str), `embedding` (vector bytes)
- `source_memory_ids` (list[int])
- `created_at`, `updated_at` (ISO-8601 str)
- `branch` (optional str — added by migration 004)

Indexes: `wiki_content_idx` (FTS BM25), `wiki_embedding_idx` (HNSW/MTREE), `wiki_slug_idx`.

**Write paths today** (all of these must be hooked by versioning):

| Caller | File | Eventually calls |
|---|---|---|
| `wiki_add` (insert) | `yadgar/server/tools/wiki.py:18` | `wiki.add()` → `storage.insert_wiki_page` |
| `wiki_add` (overwrite branch) | same | `wiki.add()` → `storage.update_wiki_page` |
| `wiki_add(append=True)` | same | `wiki.ingest()` → `storage.update_wiki_page` |
| `wiki_update` | `yadgar/server/tools/admin_other.py:459` | direct `storage.update_wiki_page` |
| `wiki_approve` | `yadgar/server/tools/wiki.py:310` | `wiki.add()` → `storage.update_wiki_page` |
| **new** `wiki_append_section` | this plan | `storage.update_wiki_page` |

**Implication for hook placement:** version-row insertion lives at the storage layer (`update_wiki_page` + `insert_wiki_page`). MCP-tool-level hooks would miss `wiki_approve` and any future caller. Storage-layer hook covers all.

**`wiki_read` behaviour today** (`yadgar/server/tools/wiki.py:200`, calls `wiki.read_by_branch` → `storage.get_wiki_page_by_slug_and_branch`):

- §25 branch resolution: current_branch → default_branch → branch IS NONE.
- Returns latest content for the resolved branch row.

**Async file-queue write path** (`yadgar/server/tools/wiki.py:80-96`):

`wiki_add` enqueues to the file queue and returns immediately. The actual `storage.update_wiki_page` happens later at drain time. Consequence: a `wiki_history(slug)` call immediately after `wiki_add` may not see the new version row until the drain runs. Document this in the tool docstring.

**Daily archive** at `~/.yadgar/archive/wiki/YYYY-MM-DD/<slug>.md` — produced by `yadgar/scripts/wiki_snapshot.py`. Survives DB corruption; stays in place as defense-in-depth alongside in-DB versions.

**Schema migrations** are append-only entries in `_MIGRATIONS` (`yadgar/storage/migrations.py:251`). Migration runs **only in server mode** (`_run_migrations` returns early if `not self._db_url`). Embedded mode skips the seed — embedded mode is dev/test only, so this is acceptable; tests run against server mode.

---

## Scope (concrete file changes)

### S1. New table `wiki_page_version`

Defined in `_init_schema` table list + new migration `_migration_010_wiki_page_version`.

| Column | Type | Description |
|---|---|---|
| `id` | int (record PK) | yadgar `_next_id("wiki_page_version")` |
| `page_id` | int | FK to `wiki_page.id` — keyed on page_id, NOT slug. Slug-with-branch can resolve to different page_ids; versioning is per-row. |
| `version` | int | Monotonic per `page_id`. `version=1` is seeded from initial `wiki_page` row; subsequent updates increment by 1. |
| `title` | str | Snapshot of `wiki_page.title` at version time. |
| `content` | str | Full snapshot. ~250 KB worst-case per row. |
| `category` | str | Snapshot. |
| `tags` | list[str] | Snapshot. |
| `confidence` | str | Snapshot. |
| `source_memory_ids` | list[int] | Snapshot. |
| `branch` | option<str> | Snapshot of branch field (mirrors `wiki_page.branch`). |
| `change_summary` | str | Auto-generated. See §change-summary below. |
| `created_at` | ISO-8601 str | When THIS version was written. |
| `provenance_agent` | option<str> | Snapshot of caller agent id when available (matches memory.provenance_agent semantic). |

**Indexes:**
```
DEFINE INDEX IF NOT EXISTS wiki_page_version_page_idx
    ON wiki_page_version FIELDS page_id;
DEFINE INDEX IF NOT EXISTS wiki_page_version_page_version_idx
    ON wiki_page_version FIELDS page_id, version UNIQUE;
DEFINE INDEX IF NOT EXISTS wiki_page_version_created_idx
    ON wiki_page_version FIELDS created_at;
```

Unique `(page_id, version)` enforces correct increment ordering.

**Embedding column intentionally excluded.** Versioning the embedding doubles storage (vector blobs are the heavy part). On `wiki_restore`, recompute embedding from restored title+content rather than store snapshots.

### S2. Migration `_migration_010_wiki_page_version`

Idempotent. Three-stage:

1. **DDL** — `DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;` plus the three indexes above.
2. **Seed** — for every existing `wiki_page` row, create a `version=1` row in `wiki_page_version` copying current fields. Skip pages that already have any version rows (idempotency for partial-failure replay).
3. **No-op on subsequent calls** via `schema_version` table guard (existing pattern).

Migration runs only in server mode. Embedded mode is dev/test only — tests must set `db_url` to exercise the migration.

### S3. `yadgar/storage/wiki.py` changes

Add to `_WikiMixin`:

- `insert_wiki_page_version(page_id: int, snapshot: dict, change_summary: str) -> int` — write one version row, returns version number assigned.
- `get_max_version_for_page(page_id: int) -> int` — returns 0 if no versions yet.
- `list_wiki_page_versions(page_id: int, limit: int = 20) -> list[dict]` — newest first, no `content` field (light payload for `wiki_history`).
- `get_wiki_page_version(page_id: int, version: int) -> dict | None` — single version with full content.

**Modify** `insert_wiki_page` and `update_wiki_page` to ALSO write a version row inside the same transaction:

```sql
BEGIN TRANSACTION;
UPDATE wiki_page:<id> SET <new_fields>;
CREATE type::record('wiki_page_version', $vid) SET
  page_id = <id>, version = <next>, title = ..., content = ..., ...,
  change_summary = $summary, created_at = $now;
COMMIT TRANSACTION;
```

Atomicity: pointer update + version insert succeed-or-fail together. Reader never sees a pointer that lacks a corresponding version row.

**Auto change_summary generation** (in `update_wiki_page`):

```
+12 -3 lines | sections touched: "Currently deployed", "Pipeline" | size: 4291 → 5102 bytes
```

Algorithm:
- Compare old `content` vs new `content` line-by-line via `difflib.unified_diff` (Python stdlib).
- Count `+`-prefixed and `-`-prefixed lines.
- Extract `## ` and `### ` headings whose surrounding lines changed (regex over diff hunk windows).
- Format the summary as a single line, ≤300 chars (truncate with `…`).
- On `insert_wiki_page` (version=1 seed): summary = `"initial version"`.

### S4. `yadgar/wiki.py` (`WikiStore`) helper additions

Add wrapper methods used by the new MCP tools:

- `history(page_id, limit=20)` — calls storage.
- `read_version(page_id, version)`.
- `diff(page_id, v1, v2, fmt="unified")`.
- `restore_version(page_id, version)` — fetches old snapshot, calls `add()` to write as new latest. Crossrefs rebuild from restored content's `[[wikilinks]]` (consistent-with-content semantic; see Open Question 5).
- `append_section(page_id, section_heading, content, position)` — section-atomic; see §wiki_append_section semantics below.

### S5. 5 new MCP tool registrations

All in `yadgar/server/tools/wiki.py` (next to `wiki_read`, `wiki_list`, etc.):

- `wiki_history(slug, limit=20)` — branch-resolves slug → page_id, calls history.
- `wiki_read_version(slug, version)` — branch-resolves, fetches version.
- `wiki_diff(slug, v1, v2, fmt="unified")` — branch-resolves, diffs.
- `wiki_restore(slug, version)` — branch-resolves, restores. `@_tool(power=True)`.
- `wiki_append_section(slug, section_heading, content, position="end_of_section")` — branch-resolves, patches. `@_tool(power=True)`.

All tools branch-resolve slug → page_id using existing `wiki.read_by_branch` so versioning honors the §25 branch model. Versioning is keyed on page_id (stable across the resolution), not slug.

### S6. Tests

- **New** `yadgar/tests/test_wiki_versioning.py` — ~15 tests, see TDD list below.
- **Extend** `yadgar/tests/test_memory_update_wiki_update.py` — assert every `wiki_update` call produces a new version row.
- **Extend** `yadgar/tests/test_wiki.py` (or nearest existing wiki-add test file) — assert `wiki_add` produces version=1.

### S7. Docs / changelog

- `CHANGELOG.md` v5.41.0 entry.
- `MIGRATION_NOTES.md` v5.41.0 section — schema migration notes, retention disclaimer ("we keep every version forever"), one-line restore command example.
- `docs/architecture.md` brief mention of `wiki_page_version` table.

---

## MCP tool signatures + return shapes

### `wiki_history(slug, limit=20)`

```json
{
  "slug": "yadgar-roadmap-future-improvements",
  "page_id": 6163,
  "versions": [
    {"version": 17, "created_at": "2026-05-31T14:22:01Z",
     "change_summary": "+12 -3 lines | sections: 'Pipeline'",
     "size_bytes": 256892, "provenance_agent": "ship-agent-a"},
    {"version": 16, "created_at": "2026-05-31T13:08:44Z",
     "change_summary": "+2 -184 lines | full content replaced",
     "size_bytes": 412, "provenance_agent": "ship-agent-b"},
    {"version": 15, "created_at": "2026-05-30T22:41:09Z",
     "change_summary": "+8 -0 lines | sections: 'Currently deployed'",
     "size_bytes": 252104, "provenance_agent": "default"}
  ],
  "total_versions": 17
}
```

Newest first. No `content` field (use `wiki_read_version` for content). `change_summary` highlights the corruption candidate (line 2: −184 lines, size dropped to 412 bytes).

### `wiki_read_version(slug, version)`

```json
{
  "slug": "yadgar-roadmap-future-improvements", "page_id": 6163,
  "version": 15, "title": "Roadmap — Future Improvements",
  "content": "<full snapshot>", "category": "architecture",
  "tags": ["roadmap", "v5"], "confidence": "high",
  "source_memory_ids": [12,34], "branch": null,
  "change_summary": "...", "created_at": "2026-05-30T22:41:09Z"
}
```

Error: `{"error": "version 99 not found for slug 'foo' (page_id=6163, max_version=17)"}`.

### `wiki_diff(slug, v1, v2, fmt="unified")`

`fmt="unified"` (default):

```json
{
  "slug": "...", "page_id": 6163, "v1": 15, "v2": 16,
  "fmt": "unified",
  "diff": "--- v15 (2026-05-30T22:41:09Z)\n+++ v16 (2026-05-31T13:08:44Z)\n@@ -1,184 +1,8 @@\n-...\n+..."
}
```

`fmt="json"`:

```json
{
  "slug": "...", "page_id": 6163, "v1": 15, "v2": 16, "fmt": "json",
  "hunks": [
    {"old_start": 1, "old_count": 184, "new_start": 1, "new_count": 8,
     "removed": ["...", "..."], "added": ["..."]}
  ],
  "added_lines": 8, "removed_lines": 184,
  "sections_changed": ["Currently deployed", "Pipeline"]
}
```

### `wiki_restore(slug, version)`

```json
{
  "slug": "...", "page_id": 6163, "restored_from_version": 15,
  "new_version": 18,
  "note": "version 18 created from snapshot of version 15"
}
```

Restore creates a NEW version (does not delete intervening 16, 17). Crossrefs rebuilt from restored content's `[[links]]`. Tags / title / category fields also restored to snapshot values (see Open Question 2).

### `wiki_append_section(slug, section_heading, content, position="end_of_section")`

```json
{
  "slug": "...", "page_id": 6163, "new_version": 19,
  "section_heading": "Pipeline",
  "action": "appended",
  "size_before": 256892, "size_after": 257214
}
```

Positions:
- `end_of_section` (default) — append `content` to the end of the named section (before the next `## ` heading at the same level).
- `start_of_section` — insert immediately after the heading line.
- `replace_section` — replace section body (heading line preserved).
- `new_section_top` — create section at top of page (error if heading already exists).
- `new_section_bottom` — create section at end of page (error if heading already exists).

Heading detection: matches `## <heading>` or `### <heading>` (markdown levels 2 + 3 only; level 1 is the page title). Comparison is case-insensitive on the heading text, ignoring leading/trailing whitespace.

Error cases:
- `section_not_found` — heading not in current content + position requires existing section.
- `section_exists` — heading already present + position is `new_section_top` / `new_section_bottom`.
- `ambiguous_section` — heading text appears more than once + position is not `replace_section` (caller must disambiguate by index — `section_heading="Pipeline#2"` for second occurrence).

`@_tool(power=True)` — write tool.

---

## Migration plan

1. Migration `_migration_010_wiki_page_version` defines the table + indexes.
2. Same migration seeds `version=1` row from every existing `wiki_page` row. Idempotent guard: skip pages with any existing version rows.
3. After migration, every subsequent `wiki_add` or `wiki_update` writes a new version row (version=N+1) inside the same transaction as the pointer update.
4. **Embedded mode skips migration** (existing `_run_migrations` early-return). Embedded mode is dev/test only; tests must use server mode to exercise versioning.
5. Daily `~/.yadgar/archive/wiki/YYYY-MM-DD/` snapshots **stay in place** as defense-in-depth (recovery path when SurrealDB file itself corrupts).
6. **Backfill of historical archive snapshots is out of scope.** Pre-v5.41 history exists on disk only. After v5.41 ships, all new edits get version history; pre-existing pages get a single `version=1` representing their current state.

**Production DB scale check:** at 2026-05-31, prod has ~2,054 `wiki_page` rows. Seed produces 2,054 version rows, one transaction each, batched. Expected wall-clock: <30 s. Migration runs under `~/.yadgar/.migration.lock` so a parallel daemon start blocks until done.

---

## Mockups (concrete examples)

### `wiki_history(slug="yadgar-roadmap-future-improvements")` — the corruption-trace use case

```json
{
  "slug": "yadgar-roadmap-future-improvements", "page_id": 6163,
  "versions": [
    {"version": 17, "change_summary": "+1 -0 lines | sections: 'Plan v5.41 committed'",
     "size_bytes": 257201, "created_at": "2026-05-31T16:00:00Z"},
    {"version": 16, "change_summary": "+2 -184 lines | full content replaced ⚠",
     "size_bytes": 412, "created_at": "2026-05-31T13:08:44Z"},
    {"version": 15, "change_summary": "+8 -0 lines | sections: 'Currently deployed'",
     "size_bytes": 252104, "created_at": "2026-05-30T22:41:09Z"}
  ], "total_versions": 17
}
```

The `size_bytes: 412` jump-down is the corruption marker. `wiki_restore(slug, version=15)` would recover.

### `wiki_diff` — confirming corruption

```
$ wiki_diff slug=yadgar-roadmap-future-improvements v1=15 v2=16 fmt=unified
--- v15 (2026-05-30T22:41:09Z)
+++ v16 (2026-05-31T13:08:44Z)
@@ -1,184 +1,8 @@
-# Yadgar Roadmap — Future Improvements
-
-## Currently deployed
-...
-## Pipeline
-... (180+ lines)
+# Roadmap update
+
+- v5.41 plan committed
+- (rest TBD)
```

Operator sees -184 lines, decision is obvious.

### `wiki_append_section` — what should have been used

```python
wiki_append_section(
    slug="yadgar-roadmap-future-improvements",
    section_heading="Pipeline",
    content="- v5.41 wiki versioning plan committed 2026-05-31\n",
    position="end_of_section",
)
```

**Before:**
```markdown
## Pipeline

- v5.26 Sonnet rerun in flight
- v5.27 duckdb export

## Open questions
```

**After:**
```markdown
## Pipeline

- v5.26 Sonnet rerun in flight
- v5.27 duckdb export
- v5.41 wiki versioning plan committed 2026-05-31

## Open questions
```

No global content replacement. Rest of the page untouched. Corruption pattern cannot fire.

---

## Open questions

1. **`wiki_diff` default format** — `fmt="unified"` (human-readable text) or `fmt="json"` (structured)? Lean unified for default (matches developer expectations; LLM still parses fine). JSON available as opt-in for tools that need structured diffs.

2. **`wiki_restore` field scope** — does restore overwrite tags / title / category / confidence to snapshot values, or only `content`? Lean **all-snapshot-fields** (consistent semantic; "restore" means "page looks as it did at that version"). If caller wants content-only, they read the version and manually `wiki_update`.

3. **`change_summary` content scope** — diff stats only, or include touched-section headings + size delta? Lean **all three** (stats + sections + size). Single-line cap at 300 chars. Sections detected from markdown `## ` / `### ` headings adjacent to changed lines. No LLM call.

4. **Concurrent writes** — two `wiki_update` calls racing on the same page. Current SurrealDB transaction wraps pointer-update + version-insert, so atomicity is preserved (no orphan version row). But "last write wins" on pointer means one caller's intent silently disappears.
   - **Lean (default v5.41):** accept last-write-wins. Both versions are in history; recovery is one `wiki_restore`.
   - **Optional (v5.41 hardening):** add `version_number` column to `wiki_page` + `expected_version` parameter to `wiki_update`. Mismatch → reject with `wiki_version_conflict`. Cleaner than SurrealDB-TX-isolation-across-server-and-embedded. Implementer decides at TDD time based on perceived race likelihood.

5. **`wiki_restore` crossrefs** — restoring content with `[[other-slug]]` links: rebuild crossrefs from the restored content's links, or preserve current crossrefs? Lean **rebuild from restored content** — "restore" should produce consistent state.

6. **Per-version `provenance_agent`** — capture from caller context, or default to `"default"`? Lean: capture from `_state` if available, fall back to `"default"`. Matches `memory.provenance_agent` semantic.

7. **Section-heading disambiguation in `wiki_append_section`** — if `## Pipeline` appears twice in the same page, how to address? Lean: `section_heading="Pipeline#2"` syntax for nth occurrence. Index is 1-based. `Pipeline` alone matches first. Error `ambiguous_section` raised if caller used bare name with non-replace position.

8. **Version retention sanity-check** — at current write rate (~5 wiki_updates/day across all pages), 365 days × 5 = ~1825 version rows/year + ~250 KB average content → ~450 MB/year worst case. Add a `memory_stats`-style surface: total version row count + total bytes. No GC until measured-cause.

---

## Step plan (TDD-driven, 3-4 calendar days)

### Day 1 — Storage layer + migration (TDD red phase)

1. Write `yadgar/tests/test_wiki_versioning.py` covering: insert produces version=1; update produces version=N+1; version rows survive page deletion (or not — decide at this step); `get_wiki_page_version` returns correct snapshot; max-version query correct; idempotent migration seed; `expected_version` mismatch (if Open Q 4 picks the hardening option).
2. Run tests — all red.
3. Add `wiki_page_version` table to `_init_schema`.
4. Write `_migration_010_wiki_page_version` (DDL + seed). Tests for seed idempotency.
5. Add storage-mixin methods: `insert_wiki_page_version`, `get_max_version_for_page`, `list_wiki_page_versions`, `get_wiki_page_version`.
6. Modify `insert_wiki_page` + `update_wiki_page` to write version rows inside the same TX. Implement `_compute_change_summary` helper.
7. Run tests — green.

### Day 2 — WikiStore wrapper + 4 read tools

1. Tests in same file for `WikiStore.history`, `read_version`, `diff`, `restore_version`.
2. Implement WikiStore wrappers.
3. Add MCP tools `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore` in `yadgar/server/tools/wiki.py`. Branch-resolve slug → page_id at the tool boundary.
4. Run tests — green.

### Day 3 — `wiki_append_section` (the prevention primitive)

1. Tests covering all positions (`end_of_section`, `start_of_section`, `replace_section`, `new_section_top`, `new_section_bottom`), error cases (`section_not_found`, `section_exists`, `ambiguous_section`), and the disambiguation syntax (`Pipeline#2`).
2. Implement `WikiStore.append_section` — pure-Python markdown section parsing (regex on `^##+ `).
3. Add MCP tool `wiki_append_section`. Power tool.
4. Tests for the corruption-prevention scenario: simulate a ship-agent flow that calls `wiki_append_section` instead of `wiki_update(content=)`; assert no content loss.

### Day 4 — Polish + ship gates

1. Extend `test_memory_update_wiki_update.py` — every `wiki_update` produces version.
2. Extend `test_wiki.py` — every `wiki_add` produces version=1 (insert) or N+1 (update).
3. CHANGELOG + MIGRATION_NOTES entry.
4. `architecture.md` brief mention.
5. Run full suite. `python scripts/check_versions.py` exit 0.
6. Manual smoke: start daemon against a sandbox SurrealDB, run migration, verify seed, call all 5 new tools.
7. Tag + ship.

---

## Effort estimate

3-4 calendar days, single agent.

| Component | Days |
|---|---:|
| Storage migration + version-write hook | 0.5 |
| `_compute_change_summary` helper | 0.25 |
| 4 read tools (`history`, `read_version`, `diff`, `restore`) | 1 |
| `wiki_append_section` parser + tool | 1 |
| Test extension + regression | 0.5 |
| CHANGELOG / docs / migration notes | 0.25 |
| **Total** | **3.5 days** |

---

## Acceptance criteria

v5.41.0 ships when ALL of the following are true:

- [ ] `pytest yadgar/tests/test_wiki_versioning.py` green — ≥15 tests cover migration, seed, version-write-on-insert/update, 5 new tools.
- [ ] `pytest yadgar/tests/test_memory_update_wiki_update.py` green — every `wiki_update` produces a new version row.
- [ ] Migration `_migration_010_wiki_page_version` ships in `_MIGRATIONS` list; seeds `version=1` for every existing `wiki_page` row idempotently.
- [ ] `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_append_section` registered as MCP tools; each appears in `yadgar/server/tools/__init__.py` exports.
- [ ] `wiki_read(slug)` behaviour unchanged — returns latest content, same return shape as v5.40.x.
- [ ] `wiki_append_section` round-trip: add a section, verify content / version / size deltas all consistent.
- [ ] Corruption-prevention scenario test: simulate the 2026-05-31 pattern (short overwrite via `wiki_update`); assert prior content recoverable via `wiki_restore`; assert `change_summary` flags the size delta.
- [ ] `MIGRATION_NOTES.md` v5.41.0 section documents: schema migration, seed behaviour, retention policy.
- [ ] `CHANGELOG.md` v5.41.0 entry references this plan + the 2026-05-31 incident.
- [ ] `python scripts/check_versions.py` exit 0.
- [ ] Production migration smoke: seed 2,054 prod rows in <60 s wall-clock (target <30 s).

---

## Risks

| Risk | Mitigation |
|---|---|
| Migration seed crashes mid-loop on prod 2,054 rows | Migration runs in idempotent batches with per-page guard (skip pages with existing versions); failed run can be re-run. Lock at `~/.yadgar/.migration.lock` prevents concurrent retry. |
| Atomicity: pointer update committed without version row | Both writes inside one SurrealDB transaction. Verified by test that injects mid-TX failure (mock storage layer). |
| Write throughput regression — 2x rows per update | Negligible at single-user yadgar scale (<10 writes/sec peak). Benchmark in Day 4 smoke: measure before/after `wiki_update` p50 / p99 latency. Acceptable if <2x increase. |
| Storage growth — 250 KB × N versions × M pages | Measured-only concern. Add `memory_stats`-style surface in v5.42+ if needed. ~450 MB/year worst case at current rates is acceptable. |
| Markdown section parser edge cases (code blocks containing `## `, HTML headings, indented headings) | Test suite covers ≥5 edge cases. Conservative: only match `^##` and `^###` at column 0, ignore inside fenced code blocks (track ```` ``` ```` state). |
| Async file-queue path: caller sees stale `wiki_history` immediately after `wiki_add` | Documented in tool docstring. Optional: add `wait_for_drain=False` flag to `wiki_history` for callers that need fresh state. |
| `wiki_restore` of content with `[[broken-link]]` | Crossref sync handles dangling links the same as `wiki_add` does today (no crash; orphans flagged by `wiki_lint`). Existing semantic. |
| `change_summary` mis-attribution for surrounding-section detection | Algorithm is conservative — only marks a section as touched if a `## ` heading appears within 5 lines above an inserted line. False negatives acceptable; false positives undesirable. |

---

## TDD test list (target: ≥15 tests)

1. `test_migration_010_creates_table` — table + indexes defined after migration.
2. `test_migration_010_seeds_existing_pages` — pre-existing page produces version=1 row matching its current content.
3. `test_migration_010_idempotent` — re-running migration does not duplicate version rows.
4. `test_insert_wiki_page_writes_version_1` — `insert_wiki_page` produces version=1 inside the same TX.
5. `test_update_wiki_page_increments_version` — successive `update_wiki_page` produces 2, 3, 4… versions.
6. `test_update_writes_change_summary` — summary contains line delta + at least one touched section heading when applicable.
7. `test_wiki_history_newest_first` — returns versions descending by `created_at`.
8. `test_wiki_history_no_content_field` — payload light; no `content` per entry.
9. `test_wiki_read_version_full_snapshot` — returns full content + all snapshot fields.
10. `test_wiki_read_version_missing` — error dict with `max_version` hint.
11. `test_wiki_diff_unified_format` — text diff parses with stdlib unified-diff parser.
12. `test_wiki_diff_json_format` — JSON shape contains `hunks`, `added_lines`, `removed_lines`, `sections_changed`.
13. `test_wiki_restore_creates_new_version_pointing_back` — `wiki_restore(slug, 5)` produces new version=N+1 whose content matches version=5.
14. `test_wiki_restore_rebuilds_crossrefs` — restored content's `[[links]]` reflected in `wiki_crossref` table.
15. `test_wiki_append_section_end_of_section` — content correctly inserted before next heading.
16. `test_wiki_append_section_replace_section` — section body replaced, heading preserved.
17. `test_wiki_append_section_ambiguous_heading_raises` — heading appearing twice + non-replace position → error.
18. `test_wiki_append_section_index_disambiguation` — `Pipeline#2` syntax targets second occurrence.
19. `test_wiki_append_section_section_not_found` — error with available-sections list in payload.
20. `test_wiki_append_section_inside_code_block_ignored` — `## Foo` inside ```` ``` ```` fenced block does not count as a section heading.
21. `test_corruption_prevention_scenario` — simulate the 2026-05-31 pattern: page at v=1 has 250 KB content; agent calls `wiki_update(content="short patch")` → v=2 is the patch; assert `wiki_restore(slug, 1)` recovers full content.
22. `test_wiki_update_inside_transaction_atomicity` — mock storage to fail version-insert; assert pointer not updated (or vice-versa).
23. `test_wiki_read_unchanged_returns_latest` — `wiki_read(slug)` after 10 updates returns version-10 content. Regression guard.
24. `test_branch_resolution_keys_versioning_on_page_id` — same slug on `master` and `feat/x` branches produce separate version chains keyed on distinct page_ids.

(Tests 1-24 above; target ≥15. Extras for safety margin.)

---

## Dependencies & blockers

- **None blocking start.** Storage migration pattern proven (10 prior migrations). No file overlap with v5.26.0 Sonnet branch.
- **v5.26.0 must ship to master first** so this plan's implementer branches off a clean master. Plan-only doc (this file) goes to master directly per the anchored rule.
- **No new env knobs required.** All behaviour controlled by storage-layer code paths.
- **No new external dependencies.** `difflib` is stdlib.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (slug `yadgar-workflow-plan-commits-direct-to-master`). No feature branch for this doc.
- Implementation requires a feature branch — `feat/v5.41.0-wiki-versioning` is the obvious name. Branch from latest master after v5.26.0 has merged.
- Implementer must read this plan, `MIGRATION_NOTES.md` migration discipline section, and `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md` (most recent wiki-area ship — pattern reference) before TDD.
- Roadmap wiki page should be updated (via read-modify-write — the very pattern this plan exists to prevent the bypass of) to mark v5.41 plan committed and add the new follow-ups.
