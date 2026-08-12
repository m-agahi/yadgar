# Car G — ADR seed (pages→ledger) + retype mutator + delete parser/serializer/lock + re-point project_brief

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: shipped in 0047 spine train (Car G integration commit on `car/G-adr-seed`)
> Depends on: F
> Lifecycle: ADR-0081/0082 — archived as the first commit of the completing branch.

## 1. Scope

Car G is the dense ADR-spine cutover car. It (1) runs the one-shot ADR seed
that lifts the ~223 existing ADRs from per-ADR wiki PAGES into the `adr`
ledger table as rows (D35a: admin op, NOT a migration step; D35b: source =
PAGES not the index, so ADR-0124 — a page with no index row — is not silently
dropped); (2) adds `adr_superseded` to `CANONICAL_PAGE_TYPES` and builds the
server-side retype mutator that flips a page's `page_type` `adr`→`adr_superseded`
atomic with the status flip (D23 — today no tool can change `page_type`:
`_WIKI_UPDATE_ALLOWED` excludes it and `wiki_set_metadata` accepts only
`directory_context`); (3) retypes the ~12 superseded ADRs; (4) deletes the
dead ADR index parser / serializer / per-project lock machinery once the
ledger is the sole read/write path; (5) re-points `project_brief`'s two
hardcoded ADR-slug sites (`_build_adr_log`, `_get_adr_log_updated_at`) off the
`<project>-adr-index` wiki page onto the ledger; and (6) fixes the dead
`{project}-adr-log` read in the stop-checkpoint prompt template (that monolith
is deleted; every checkpoint runs a read that resolves a dead slug).

Car G depends on Car F (ADR tools `adr_list`/`adr_get` re-pointed to the
ledger, with a characterization test pinning return shapes) and transitively
on Car A (ledger tables + `MariaStorageEngine` CRUD) and Car B (backend ops +
cache). Car G must NOT start until F has merged: the seed writes rows through
the ledger API F re-points the readers onto, and the parser/serializer
deletion is only safe once `adr_list`/`adr_get`/`adr_add` no longer call them.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/core/server/tools/wiki.py:29` | Add `adr_superseded` to `CANONICAL_PAGE_TYPES = frozenset({"task_list", "adr"})` → `{"task_list", "adr", "adr_superseded"}`. | `wiki.py:29` read; frozenset literal confirmed |
| `yadgar/core/server/tools/wiki.py:109-113` | The `_wiki_write_canonical` allowlist assertion that raises on non-allowlisted `page_type` — adding `adr_superseded` to the frozenset lets the retype mutator (and only sanctioned server-side callers) write/rewrite superseded pages. (D23 cited `wiki.py:30,170-174`; observed raise is at `:109-113`, def at `:29`.) | `wiki.py:108-113` read |
| `yadgar/_shared/schemas/wiki_page_types.yaml` | Add an `adr_superseded` page type entry (mirror the `adr` shape: `required: [Context, Decision, Consequences]`) so `wiki_lint` format-checks superseded pages. [VERIFY: whether `adr_superseded` should alias `adr` or carry an extra `Superseded-by` required section — decide at build time.] | `wiki_page_types.yaml` read; `adr` entry at `:30-32` confirmed, no `adr_superseded` entry present |
| `yadgar/core/server/tools/admin_other.py:42` | Reference only — `_WIKI_UPDATE_ALLOWED = frozenset({"content", "tags", "category", "confidence"})` excludes `page_type`, which is WHY a new mutator is required rather than extending `admin_exec/wiki_update`. No edit here unless the mutator is wired through `admin_exec`. | `admin_other.py:42` read |
| `yadgar/core/server/tools/wiki.py:1056` | Reference only — `wiki_set_metadata` docstring: "field must be 'directory_context'. Other fields are rejected." Confirms it cannot retype. No edit. | `wiki.py:1056-1075` read |
| `yadgar/backend/admin_exec/` (NEW: retype mutator + seed op) | NEW backend admin op(s): (a) `retype_page_type(slug, from_type, to_type)` server-side mutator that flips `wiki_page.page_type` atomic with the `adr` row status flip — bypasses `_WIKI_UPDATE_ALLOWED` because it is a sanctioned server-side lifecycle transition, not an agent/tool edit (D26: `locked` blocks agent edits, NOT sanctioned lifecycle transitions); (b) ADR seed op — one-shot, idempotent, reads per-ADR PAGES, writes `adr` ledger rows via `MariaStorageEngine` ledger methods. | `backend/admin_exec/seed.py` exists as the seed_store precedent (T2 Car E1); `backend/admin_exec/` dir confirmed; [VERIFY: exact module/filename for the new ops — follow the `seed.py` / `ledger.py` precedent in that dir] |
| `yadgar/core/server/tools/adr_index.py` | DELETE the parser/serializer/index-render machinery after the seed + F's re-points land: `parse_index_rows` (`:78`), `parse_adr_ids` (`:64`), `_INDEX_ROW_RE` (`:21`), `_INDEX_HEADER` (`:28`), `_index_max_id` (`:100`), `_build_index_content` (`:177`), `_render_index_row` (`:156`), `_next_adr_id` (`:134`), `_next_adr_id_from_index` (`:146`), `_committed_page_max_id` (`:107`). Slug helpers `adr_log_slug` (`:40`), `adr_index_slug` (`:49`), `adr_page_slug` (`:54`) are RETIRED by Car L's D32③ re-slug (`body_slug = {project_id}_adr-{id}`) + ledger AUTO_INCREMENT id — [VERIFY: delete `adr_index.py` wholesale vs. retain `adr_page_slug` transiently until Car L merges; Car G → Car L ordering, see §6]. | `adr_index.py` full read; all symbols + line numbers confirmed |
| `yadgar/core/server/tools/adr.py:99-109` | DELETE the per-project lock: `_ADR_LOG_LOCKS` (`:99`), `_ADR_LOG_LOCKS_GUARD` (`:100`), `_adr_log_lock` (`:104-109`). The lock serializes the read-index→next-id→write-page→append-index-row sequence (`adr.py:95-98`); once id allocation is ledger AUTO_INCREMENT (§13 Fix 1 / D6b retired) and the index is gone, the lock has no sequence to protect. | `adr.py:95-109` read; lock used at `adr.py:212` |
| `yadgar/core/server/tools/adr.py:212-269` | Re-point `adr_add`'s body: remove `_adr_log_lock` context manager (`:212`), remove `_next_adr_id` call (`:221`), remove `_build_index_content` index append (`:269`) — id comes from ledger INSERT AUTO_INCREMENT, row comes from `create_adr_row`, body from `_wiki_write_canonical`. (Car F begins this re-point for `adr_list`/`adr_get`; Car G completes it for `adr_add` + deletes the helpers.) | `adr.py:208-269` read; [VERIFY: exact boundary between F's `adr_list`/`adr_get` re-point and G's `adr_add` re-point — coordinate with Car F] |
| `yadgar/core/server/tools/adr.py:60-71,382-393` | DELETE the backward-compat re-exports of `parse_index_rows`, `_build_index_content`, `_render_index_row`, `_INDEX_ROW_RE`, `_next_adr_id*`, `_committed_page_max_id`, `_index_max_id`, `adr_log_slug`, `parse_adr_ids` once the symbols are gone from `adr_index.py`. | `adr.py:60-71,382-393` read |
| `yadgar/core/server/tools/project.py:1287-1320` | Re-point `_get_adr_log_updated_at` (def `:1288`, call `:1373`) off the `<project>-adr-index` wiki-page `updated_at` query (`:1300-1305`) onto the ledger: `SELECT MAX(updated_at) FROM adr WHERE project_id = ?` (or the scope-version piggyback per §15.2). §7 row cited `project.py:1378-1381` — STALE; observed call site is `:1373`, def `:1288`. | `project.py:1287-1320,1373` read |
| `yadgar/core/server/tools/project.py:1786-1818` | Re-point `_build_adr_log` (def `:1787`, calls `:1865` restore + `:1919` catalog/full) off `wiki_read(<project>-adr-index)` + `parse_index_rows` (`:1802-1815`) onto the ledger: `list_adr_rows(project_id)` ordered by id desc, take 3. §7 row cited `project.py:1880,1889` — STALE; observed call sites are `:1865,1919`, def `:1787`. | `project.py:1786-1818,1865,1919` read |
| `yadgar/core/server/tools/adr_render.py:179,181` | Re-point `_assemble_index_rows` off `parse_index_rows(existing_index)` onto `storage.list_adr_rows(project_id)`. (§13 Fix 3 already names this re-point for the PR #32 branch; Car G lands it on master.) | `adr_render.py:170-185` read |
| `yadgar/core/hooks/templates/stop_checkpoint_prompt.md:26-31` | Fix the dead `{project}-adr-log` read. Line 26 names slug `{project}-adr-log`; lines 28-29 are the `wiki_read("{project}-adr-log", directory=...)` call. The monolith is deleted (per `_build_adr_log` docstring `project.py:1292`); every checkpoint resolves a dead slug. Re-point step 1's read-first-dedup to the ledger: `adr_list(directory=...)` against the seeded `adr` rows, not a `wiki_read` of a deleted slug. | `stop_checkpoint_prompt.md:1-33` read; dead read at `:26-31` confirmed |
| `yadgar/_shared/storage/sql/mariadb.py` | Reference only — Car A builds the `MariaStorageEngine` ledger methods (`create_adr_row`, `list_adr_rows`, `set_adr_body_slug`) Car G's seed + re-points call into. Car A doc establishes the engine is a CONCRETE class (no `_LedgerMixin`) — Car G must NOT re-introduce a mixin. | `mariadb.py:11-15` confirmed via Car A doc; [VERIFY: exact method signatures once Car A merges] |
| `server.json` | bump `backend_version` — Car G touches `yadgar/backend` (seed op + retype mutator). Mechanism: `scripts/check_backend_bump.py:44,51`, `BACKEND_BUILD_DIRS=("backend",)`. | `server.json` at repo root confirmed; mechanism per Car A doc `:60,193` |
| `pyproject.toml:7` | bump core `version` (5.181.0 today) — Car G touches core (`project.py`, `adr.py`, `adr_index.py`, `adr_render.py`, `wiki.py`, `stop_checkpoint_prompt.md`). [VERIFY: whether `check_version_bump.py` requires a core bump for these core-path edits — Car A doc `:290` flags the same uncertainty.] | `pyproject.toml:7` read |

## 3. Functions / symbols

**NEW — backend retype mutator** (sanctioned server-side lifecycle transition, D23/D26):
```python
# yadgar/backend/admin_exec/ (NEW module — [VERIFY exact filename])
def retype_page_type(*, slug: str, from_type: str, to_type: str, directory: str | None = None) -> dict:
    """Flip wiki_page.page_type from_type→to_type atomic with any row-side status flip.

    Bypasses _WIKI_UPDATE_ALLOWED (admin_other.py:42) because it is a sanctioned
    server-side lifecycle transition, not an agent/tool edit. D26: `locked`
    mutability blocks agent edits, NOT sanctioned transitions — otherwise the
    supersede retype deadlocks against its own guard. Asserts from_type matches
    the current page_type (refuses a cross-type retype that skips the guard).
    """
```

**NEW — ADR seed op** (one-shot, idempotent, D35a/D35b):
```python
# yadgar/backend/admin_exec/ (NEW module — [VERIFY exact filename])
def seed_adr_rows(*, project_id: str, directory: str) -> dict:
    """One-shot: lift existing per-ADR wiki PAGES into the `adr` ledger table.

    Source = per-ADR PAGES (D35b), NOT the index — enumerates pages via
    slug prefix `{project}-adr-` and parses each body, NOT parse_index_rows.
    Idempotent: keyed on body_slug; re-running converges, never duplicates.
    Metadata absent from the page body is recovered from the index row where
    one exists; where none exists (ADR-0124) it is filled from the page and
    flagged. Returns counts: pages_seen, rows_inserted, rows_skipped (idempotent
    re-run), flagged (metadata gap). D35c gate runs at the end.
    """
```

**MODIFY — `_get_adr_log_updated_at`** (`project.py:1288`):
```python
def _get_adr_log_updated_at(storage, resolved: str) -> float | None:
    """Re-pointed: MAX(updated_at) on the adr ledger table for this project_id,
    not the <project>-adr-index wiki-page updated_at."""
```

**MODIFY — `_build_adr_log`** (`project.py:1787`):
```python
def _build_adr_log(resolved: str) -> dict:
    """Re-pointed: list_adr_rows(project_id) ordered by id DESC, take 3.
    Drops the wiki_read + parse_index_rows path."""
```

**DELETE** — from `adr_index.py`: `parse_index_rows`, `parse_adr_ids`,
`_INDEX_ROW_RE`, `_INDEX_HEADER`, `_index_max_id`, `_build_index_content`,
`_render_index_row`, `_next_adr_id`, `_next_adr_id_from_index`,
`_committed_page_max_id`. From `adr.py`: `_ADR_LOG_LOCKS`,
`_ADR_LOG_LOCKS_GUARD`, `_adr_log_lock`, and the re-exports at `:60-71,382-393`.
[VERIFY: whether `adr_index_slug`/`adr_log_slug`/`adr_page_slug` survive until
Car L's re-slug or are deleted here — see §6.]

## 4. Build steps (TDD)

1. **RED** — test that `CANONICAL_PAGE_TYPES` accepts `adr_superseded`: a
   `_wiki_write_canonical(payload={"page_type": "adr_superseded", ...})` call
   raises `ValueError` today (`wiki.py:109`); test asserts it succeeds after
   the frozenset add. Also test the retype mutator refuses a `from_type`
   mismatch (guard against accidental cross-type retype).
2. **GREEN** — add `adr_superseded` to `CANONICAL_PAGE_TYPES` (`wiki.py:29`)
   and to `wiki_page_types.yaml`; build `retype_page_type` backend op; wire it
   through the admin_exec surface.
3. **RED** — test the seed op is idempotent: run `seed_adr_rows` twice on a
   fixture of 3 per-ADR pages; assert second run returns `rows_skipped=3,
   rows_inserted=0` and no duplicate rows. Assert ADR-0124 (page, no index
   row) is seeded with a `flagged` marker, not dropped.
4. **GREEN** — implement `seed_adr_rows` reading per-ADR PAGES (slug-prefix
   enumeration, body parse) not `parse_index_rows`; write rows via
   `MariaStorageEngine.create_adr_row`; set `body_slug` via
   `set_adr_body_slug`.
5. **RED** — test `_build_adr_log` and `_get_adr_log_updated_at` return data
   from the ledger, not the `<project>-adr-index` wiki page: mock the ledger
   rows, assert the functions never call `wiki_read` / never query
   `wiki_page WHERE slug = <project>-adr-index`.
6. **GREEN** — re-point `_build_adr_log` (`project.py:1787`) to
   `list_adr_rows`; re-point `_get_adr_log_updated_at` (`project.py:1288`) to
   `MAX(updated_at)` on the `adr` table; re-point `adr_render.py:179` to
   `list_adr_rows`.
7. **RED** — test that `adr_list`/`adr_get`/`adr_add` no longer import or call
   `parse_index_rows`/`_build_index_content`/`_adr_log_lock` (import-error
   test: `import adr_index` after deletion raises or the symbols are absent).
8. **GREEN** — delete the parser/serializer/lock symbols from `adr_index.py`
   and `adr.py`; re-point `adr_add` body off the lock + index append onto
   ledger INSERT; delete re-exports `adr.py:60-71,382-393`.
9. **RED** — test the stop-checkpoint prompt no longer issues a dead
   `wiki_read("{project}-adr-log")`: render the template, assert the
   read-first-dedup step calls `adr_list` against the ledger, not
   `wiki_read` of the deleted monolith slug.
10. **GREEN** — edit `stop_checkpoint_prompt.md:26-31` to re-point step 1's
    read-first-dedup to `adr_list(directory=...)`.
11. **REFACTOR** — run the D35c verification gate (see §5); mark old pages
    `superseded-by-ledger` (D35d); bump versions.

## 5. Acceptance gates

- [ ] `CANONICAL_PAGE_TYPES` includes `adr_superseded` (`wiki.py:29`); `_wiki_write_canonical` accepts it (`wiki.py:109`).
- [ ] `retype_page_type` backend op exists, is server-side only (not agent-callable), and refuses a `from_type` mismatch.
- [ ] ADR seed ran once: ~223 ADR rows exist in the `adr` table, each with a `body_slug` pointing at its per-ADR wiki page. Idempotent re-run inserts 0 rows.
- [ ] **D35c verification gate — EXACT equality on a stated predicate.** The three known counts are reconciled BEFORE cutover, never absorbed silently: `index_rows` vs `pages_seen` vs `page_type='adr'` rows. Per §1.5 the counts legitimately differ (193 index / 194 pages / 195 page_type='adr' at plan time; re-census at build time). The residue (pages with no index row, e.g. ADR-0124; extra page_type rows) is EXPLAINED, not tolerated. `>=` is not a gate (2026-06-16 vacuum destroyed 3,622 memories through a `>=` check — §6.3).
- [ ] The ~12 superseded ADRs are retyped `adr`→`adr_superseded` (page_type flipped, status flip atomic per D23). [VERIFY: exact superseded count at build time — plan §7 says "12"; recall/ADR-0206 context said "10 of ~181" as of 2026-08-06; re-census via `SELECT count(*) ... WHERE status='superseded'` against the seeded rows.]
- [ ] `parse_index_rows`, `_build_index_content`, `_render_index_row`, `_INDEX_ROW_RE`, `_adr_log_lock` and the `_ADR_LOG_LOCKS*` globals are DELETED; no import of them survives in non-test code.
- [ ] `_build_adr_log` (`project.py:1787`) and `_get_adr_log_updated_at` (`project.py:1288`) read from the ledger, not `<project>-adr-index`; `adr_render.py:179` likewise.
- [ ] `stop_checkpoint_prompt.md:26-31` no longer reads `{project}-adr-log`; step 1 dedups against `adr_list` (ledger).
- [ ] D35d: old `<project>-adr-index` page marked `superseded-by-ledger` (NOT deleted — rollback path for one release cycle); per-ADR BODY pages never deleted (D4).
- [ ] core/backend version bumped per WORKFLOW RULE — `backend_version` in `server.json` (Car G touches `yadgar/backend`: seed op + retype mutator; `BACKEND_BUILD_DIRS=("backend",)` per `scripts/check_backend_bump.py:44,51`). Core `pyproject.toml` version (5.181.0 today) bump if `check_version_bump.py` requires it for the core-path edits (`project.py`/`adr.py`/`adr_index.py`/`adr_render.py`/`wiki.py`/`stop_checkpoint_prompt.md`). [VERIFY the core-bump trigger.]
- [ ] pre-commit green (ruff, import-linter, I32, I33, `check_versions`, `check_ledger_chokepoint`).
- [ ] tests pass; Car F's characterization test (pinning `adr_list`/`adr_get` return shapes pre/post-migration) still green after G's deletions.

## 6. Sequencing

**Must merge before Car G:** F (ADR tools re-pointed to ledger — `adr_list`/`adr_get`
read from `MariaStorageEngine.list_adr_rows`; characterization test pins shapes);
transitively A (ledger tables + `MariaStorageEngine` CRUD + alembic chain), B
(backend ops + cache), A0 (project_id derivation — the seed stamps `project_id`
on every row), C1/C2/C3 (recall disposition + identity gate — C2's `downweight`
is what makes `adr_superseded` excluded/down-weighted in recall per D23/D22).

**Wait on Car G:** H (`tier` + `subsystem` + rollups — needs the seeded `adr`
rows); K (nightly archive sweep — policy-dispatched on `adr_superseded` page
type); L (194-page ADR re-slug to `{project_id}_adr-NNNN` — L's re-slug runs
against the seeded rows; Car G's slug-helper deletion must coordinate with L:
if L has not merged, `adr_page_slug` may need to survive transiently, [VERIFY]).

**Parallel:** I (agent_prompt table + TOC deletion) is independent of G after B
— D/E ∥ F/G ∥ I after B per §7.

## 7. ADRs / decisions

- **D35a** — The migration creates schema; the SEED is a separate one-shot admin op the migration merely enables, NOT a migration step. Ship as explicit admin op/CLI, run once by operator, idempotent. (Forced: `_run_migrations` runs under `fcntl.flock` in `StorageEngine.__init__`; reading 195 pages inside a constructor under a process-wide lock would stall every daemon start.)
- **D35b** — ONE-SHOT, not dual-write. Source of truth for the ADR seed = per-ADR PAGES, not the index. Cutover = single atomic flip of the read path. Pages-over-index because the page is the ID-bearing artifact (`_committed_page_max_id`'s docstring states the index "may lag"); §1.5 proves the index is already missing ADR-0124 — seeding from `parse_index_rows` would have silently dropped it.
- **D35c** — Verification gate: EXACT equality on a stated predicate, three known counts reconciled BEFORE cutover, never absorbed silently. `>=` is not a gate (2026-06-16 vacuum/3,622-memory incident).
- **D35d** — Old pages KEPT-AND-IGNORED, not deleted, for one full release cycle. Mark with `superseded-by-ledger` tag; the three index pages get content replaced by a one-line pointer preserving the slug. Per-ADR BODY pages never deleted (D4).
- **D23** — Supersede = retype `adr`→`adr_superseded`, atomic with the status flip. Never delete, never NULL `body_slug`. Two blockers Car G removes: (1) no tool can change `page_type` (`_WIKI_UPDATE_ALLOWED` excludes it; `wiki_set_metadata` takes only `directory_context`); (2) `CANONICAL_PAGE_TYPES` raises on any type outside `{task_list, adr}`. Car G adds the type AND builds the server-side retype mutator.
- **D26** — Per-type mutability: `adr`/`adr_superseded` → `locked`. `locked` blocks agent/tool edits, NOT sanctioned server-side lifecycle transitions — otherwise the supersede retype deadlocks against its own guard.
- **D22** — Superseded ADRs excluded/down-weighted in recall (C2's `downweight` implements this against `page_type='adr_superseded'`).

## 8. Out of scope

- The `agent_pattern`/`agent_discipline` page-type split (ADR-0209) — that is Car I.
- The 194-page ADR re-slug to `{project_id}_adr-NNNN` (D32③) — Car L.
- Memory + wiki `directory_context`→`project_id` backfill — Car L.
- The `tier`/`subsystem` columns + rollup pages — Car H.
- Nightly archive sweep policy dispatch on `adr_superseded` — Car K.
- Cross-project `project=` param on ADR tools — Car M.
- The knob-store MOVE to MariaDB (`runtime_config`) — Car A / the knob train (§16.9 item 2).
- Core-PTC→backend-PTC→DB read-path layering (§15) — Car B builds the backend PTC; Car G's re-points call the ledger directly and are re-routed onto HTTP forwards by B's follow-on, not here.

## 9. Risks / open questions

- **[VERIFY] §7 row line numbers are STALE.** `_build_adr_log` cited as `project.py:1880,1889`; observed def `:1787`, calls `:1865,1919`. `_get_adr_log_updated_at` cited as `project.py:1378-1381`; observed def `:1288`, call `:1373`. This doc cites the observed coordinates; the §7 numbers were written against an earlier revision of `project.py`.
- **[VERIFY] exact superseded-ADR count.** Plan §7 says "retype the 12"; ADR-0206 context (2026-08-06) said "only 10 of ~181 yadgar ADRs carry adr-status:superseded". Re-census at build time via the seeded rows. The retype mutator is the same regardless of count; only the gate predicate's denominator moves.
- **[VERIFY] `adr_index.py` deletion boundary vs Car L.** `adr_page_slug` (`:54`) and `adr_index_slug` (`:49`) may be needed until Car L's D32③ re-slug lands (`body_slug = {project_id}_adr-{id}`). If L has not merged when G lands, retain the slug helpers transiently and delete them in L; otherwise delete `adr_index.py` wholesale. Coordinate the G→L handoff.
- **[VERIFY] retype mutator call site for `adr_add` supersede.** When `adr_add(..., supersedes="ADR-NNNN")` runs post-G, it must (a) write the new ADR row, (b) call `retype_page_type` on each supersede target's page `adr`→`adr_superseded`, (c) flip the target row's status. The atomicity boundary (D23: "atomic with the status flip") needs a single transaction or a compensating-rollback — [VERIFY the MariaStorageEngine transaction shape Car A ships].
- **[VERIFY] core version bump trigger.** `check_version_bump.py` — whether the core-path edits (`project.py`/`adr.py`/`adr_index.py`/`adr_render.py`/`wiki.py`/`stop_checkpoint_prompt.md`) require a `pyproject.toml` bump, or only `backend_version` in `server.json` (the backend edits). Car A doc `:290` flags the same uncertainty.
- **[VERIFY] `wiki_page_types.yaml` `adr_superseded` shape.** Whether it aliases `adr` (`required: [Context, Decision, Consequences]`) or adds a `Superseded-by` required section for lint. Decide at build time; the yaml is advisory (wiki_lint warns, wiki_add never rejects on page_type mismatch — per the file header).
- **[VERIFY] exact module/filename for the NEW backend seed + retype ops.** Follow the `backend/admin_exec/seed.py` (seed_store precedent) / `backend/admin_exec/ledger.py` convention; confirm against Car B's backend-ops layout.
- **Risk — D35c gate is the load-bearing safety.** A partial seed that passes a `>=` check is the exact 2026-06-16 failure mode (3,622 memories destroyed). The gate MUST be exact-equality on a stated predicate with the residue explained; a seed that cannot state its predicate must not ship.
