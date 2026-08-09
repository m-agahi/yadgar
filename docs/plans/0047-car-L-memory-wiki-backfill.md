# Car L — memory + wiki directory_context→project_id backfill + quarantine + ADR re-slug

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: A0
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Car L is the one-shot offline migration that moves memory + wiki onto the `project_id` key (§16.8 + §16.9, migration case 3). Per §7 row (verbatim): "memory + wiki `directory_context`→`project_id` backfill + quarantine + 194-page ADR re-slug (one-shot offline) — §16.11". It depends on Car A0 (`derive_project_id()` + the `project` registry) and on Car A (`body_slug` column + `set_adr_body_slug()` on the ADR ledger row).

It does two things, both offline one-shot operations (NOT live MCP paths):

1. **Memory + wiki backfill** — add a `project_id` field to the SurrealDB `wiki_page` and `memory` tables, backfill it per-row from the existing `directory_context` field (NOT NULL via migration 016, `yadgar/_shared/storage/migrations.py:623` wiki_page, `:663` memory). Three cases per §16.8: path exists + git repo → derive from remote (§16.1/§16.4); path exists, not git → `local/<basename>`; path gone → `project_id='unresolved'` with the original path preserved in a new `legacy_directory` column, surfaced for review (quarantine — suggest-and-confirm, never auto-map per §16.9). Sentinels (`global`) are unaffected.
2. **ADR re-slug (D32 ③)** — re-slug ADR wiki pages to the new `body_slug = {project_id}_adr-NNNN` format (`/`→`_`), update `[[crossrefs]]` (both the `wiki_crossref` table rows and inline `[[old-slug]]` text in page bodies), set the SQL `adr.body_slug` to match, and delete the task-list page (tasks move to SQL in Car D).

This is an OFFLINE one-shot — it runs as a SurrealDB migration (the backfill) plus a one-shot admin op (the re-slug), NOT on the live MCP write path. The live write paths are updated to stamp `project_id` in the same car so post-migration writes do not regress.

ADR count: §7/§8/§16.11 say "194" (measured 2026-08-02). **Live `wiki_list(slug_prefix="yadgar-adr")` on 2026-08-09 returns 222 ADR body pages** (range `yadgar-adr-0001`..`yadgar-adr-0222`, excluding the `yadgar-adr-index` and `yadgar-adr-log` non-body pages). The re-slug must cover the **live count at execution time**, not a hardcoded "194" — the figure is stale by 28 pages. [VERIFY: re-run `wiki_list(slug_prefix="yadgar-adr")` immediately before the re-slug and use that count; do not bake any number into the script.]

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/storage/migrations.py` | ADD `_migration_030_project_id_backfill` fn; APPEND entry to `_MIGRATIONS` list (`migrations.py:1390`+) after `029_drop_branch_column` (`migrations.py:1492`). Adds `project_id` field + `legacy_directory` field to `wiki_page` + `memory`; backfills per-row from `directory_context`; leaves `directory_context` in place (read path still uses it until Car M flips readers). | `_MIGRATIONS` list head at `migrations.py:1390`; last entry `029_drop_branch_column` at `migrations.py:1492`; migration 016 fn at `migrations.py:553`; wiki_page NOT NULL at `:623`, memory NOT NULL at `:663` — all confirmed |
| `yadgar/_shared/storage/wiki.py` | ADD `project_id` to write path (`insert_wiki_page` / `update_wiki_page` — the `links`/`slug`/`directory_context` write at `wiki.py:151-164`); ADD `get_wiki_page_by_slug_project(slug, project_id)` reader alongside the existing `get_wiki_page_by_slug_directory` (`wiki.py:409`); update `replace_wiki_crossrefs` (`wiki.py:729`) callers if crossref table gains `project_id` (it does NOT — crossrefs are slug-keyed, see §3) | `insert_wiki_page` write at `wiki.py:151`; `get_wiki_page_by_slug` at `:379`; `get_wiki_page_by_slug_directory` at `:409`; `replace_wiki_crossrefs` at `:729`; `get_all_wiki_crossrefs` at `:768` — all confirmed |
| `yadgar/_shared/storage/memory.py` | ADD `project_id` to `insert_memory` (`memory.py:269`) and the update path (`memory.py:103`, `:123` where `directory_context` is stamped); ADD `project_id` to `_MEMORY_UPDATABLE_FIELDS` (`memory.py:946` references it from `client.py`) | `insert_memory` at `:269`; `directory_context` write at `:103`/`:123`; `_MEMORY_UPDATABLE_FIELDS` referenced at `:946` — confirmed |
| `yadgar/backend/admin_exec/reslug.py` | NEW — one-shot ADR re-slug admin op (backend execution body, same pattern as `admin_exec/seed.py`). Dispatched from core via the existing `admin_exec` route (`admin_exec/__init__.py` registry). Rewrites `wiki_page.slug` for every ADR page, updates `wiki_crossref.from_slug`/`to_slug`, replaces inline `[[old-slug]]` text in page bodies, and calls `set_adr_body_slug()` (Car A) to sync the SQL row. | `admin_exec/seed.py` confirmed as the one-shot backend op pattern; `admin_exec/__init__.py` registry confirmed (`reembed_all` at `:77`); `wiki_crossref` columns `from_slug`/`to_slug` confirmed at `wiki.py:740,743,762,776` |
| `yadgar/backend/admin_exec/__init__.py` | REGISTER the new `reslug` op in the admin op dispatch map (alongside `seed`, `reembed_all`, etc.) | `__init__.py:77` shows the `"reembed_all": memory.reembed_all` registration pattern — confirmed |
| `yadgar/core/scripts/reslug_adr.py` | NEW — core-side CLI entry point that dispatches the `reslug` admin op to the backend over HTTP (mirrors how `seed_project` / `nightly_cycle` dispatch). Invoked as a one-shot: `python -m yadgar.core.scripts.reslug_adr` or a console_script. | `pyproject.toml:79` shows `yadgar-nightly-cycle = "yadgar.core.scripts.nightly_cycle:main"` console_script pattern — confirmed; `yadgar/core/scripts/` dir exists |
| `yadgar/core/server/tools/dispatch_helper.py` | NO CHANGE to `_WIKI_LINK_RE` (`dispatch_helper.py:140` — `re.compile(r"\[\[([a-zA-Z0-9_-]+)\]\]")`) — the regex already matches the new slugs (`m-agahi_yadgar_adr-0194` is all `[a-zA-Z0-9_-]`). The re-slug script reuses this exact regex to find + replace inline `[[old-slug]]` text. | `_WIKI_LINK_RE` at `dispatch_helper.py:140` confirmed; character class `[a-zA-Z0-9_-]+` already covers the new slug charset |
| `yadgar/backend/consolidation/cleanup.py` | UPDATE `_try_store_action_summary` (`cleanup.py:173`) to stamp `project_id` alongside `directory_context` at the `insert_memory` call (`cleanup.py:192`) — derive via `derive_project_id(directory)` so the nightly consolidation path does not regress post-backfill. | `cleanup.py:192` confirmed as the `"directory_context": directory,` write site inside `_try_store_action_summary` — confirmed |
| `yadgar/backend/queue_drainer/apply.py` | UPDATE the `wiki_add` replay path (`apply.py:126`) to stamp `project_id` from the per-item `directory_context` (derive via `derive_project_id(p["directory"])`). Per §16.11 Car M: "drainer derives per-item from `directory_context` (`apply.py:126`, already per-item)" — this is folded into Car L per the §16.11 backend-bump note. | `apply.py:126` confirmed: `p["directory_context"] = p.get("directory_context") or p.get("directory")` — the per-item fallback site |
| `yadgar/_shared/storage/sql/ledger.py` | NOT touched by L directly — Car A ships `set_adr_body_slug()` here; Car L CALLS it from the re-slug script. | confirmed absent at HEAD (Car A deliverable) |
| `yadgar/core/identity.py` | NOT touched by L — Car A0 ships `derive_project_id()` here; Car L IMPORTS it. | confirmed absent at HEAD (Car A0 deliverable) |
| `~/.yadgar/` files | NEVER touched by L (or any car) — §16.3 + the user's global infra rule. The backfill reads `directory_context` from SurrealDB, NOT `~/.yadgar/`. | n/a |

## 3. Functions / symbols

### `yadgar/_shared/storage/migrations.py` — `_migration_030_project_id_backfill(storage)`

```python
def _migration_030_project_id_backfill(storage) -> None:
    """Add project_id + legacy_directory to wiki_page + memory; backfill from directory_context.

    One-shot offline (§16.8 + §16.9, Car L). Three cases per row:
      - directory_context is a git repo with a remote → derive per §16.1/§16.4
      - directory_context exists, no remote → 'local/<basename>'
      - directory_context path no longer exists → project_id='unresolved',
        original path preserved in legacy_directory, surfaced for review.
    Sentinels ('global', '') → project_id='global' (unchanged semantics).

    Idempotent: a second run finds every row already has project_id and no-ops.
    Does NOT drop directory_context — the read path still uses it until Car M
    flips readers. The field is removed in a later migration after Car M lands.
    """
```

Phases (mirroring the 016/018/023 pattern at `migrations.py:553,678,927`):
- Phase A: `DEFINE FIELD IF NOT EXISTS project_id ON TABLE wiki_page TYPE option<string>` (nullable during backfill; NOT-NULL constraint deferred to a later migration after quarantine rows are resolved).
- Phase B: `DEFINE FIELD IF NOT EXISTS legacy_directory ON TABLE wiki_page TYPE option<string>` (only set on quarantine rows).
- Phase C: `SELECT id, directory_context FROM wiki_page` → Python-side classify each distinct `directory_context` (the distinct-value count is the real job size per §16.8, not the row count) → `UPDATE ... SET project_id = $pid, legacy_directory = $legacy` per-row.
- Phase D: same three phases for `memory`.
- Phase E: `DEFINE INDEX IF NOT EXISTS wiki_page_project_id_idx ON TABLE wiki_page FIELDS project_id` + `memory_project_id_idx` on `memory`.
- Phase F: emit a summary log line naming the distinct `directory_context` values that went to `unresolved` — these are the quarantine set the operator must review.

### `yadgar/backend/admin_exec/reslug.py` — `reslug_adr_pages(payload: dict) -> dict` (backend execution body)

```python
def reslug_adr_pages(payload: dict) -> dict:
    """One-shot ADR re-slug (D32 ③). For every ADR wiki page:
      1. compute new_slug = f"{project_id}_adr-{NNNN}" (project_id with '/' → '_')
      2. UPDATE wiki_page SET slug = $new_slug WHERE slug = $old_slug
      3. UPDATE wiki_crossref SET from_slug = $new_slug WHERE from_slug = $old_slug
         UPDATE wiki_crossref SET to_slug = $new_slug WHERE to_slug = $old_slug
      4. replace inline [[old-slug]] → [[new-slug]] in wiki_page.content
         (regex: _WIKI_LINK_RE from dispatch_helper.py:140)
      5. call storage.set_adr_body_slug(adr_id, new_slug) to sync the SQL row
      6. record (old_slug, new_slug) in the returned manifest for audit

    Dry-run mode (payload["dry_run"]=True): return the manifest WITHOUT writing.
    Idempotent: a second run finds slugs already in the new format and skips them
    (detect by regex ^{project_id_re}_adr-\\d+$).
    """
```

- `def _classify_one_directory(path: str) -> tuple[str, str | None]` — shared with the migration's classifier (factor into `yadgar/core/identity.py` as `classify_directory_context(path)` so both the migration and the nightly path call one function). Returns `(project_id, legacy_directory_or_None)`. [VERIFY: the migration runs at daemon boot BEFORE `yadgar/core/identity.py` is importable in some lifecycles — confirm whether migrations run with the full core import path available; if not, the classifier must be duplicated in the migration body or imported lazily. The 016/018 migrations call only `storage._q`, not core helpers, so this is a real concern.]

### `yadgar/core/scripts/reslug_adr.py` — `main()` (core-side CLI entry)

```python
def main() -> int:
    """Dispatch the reslug admin op to the backend over HTTP.
    Usage: yadgar-reslug-adr [--dry-run]
    Prints the (old_slug, new_slug) manifest; exits 0 on success, 1 on any
    page that could not be classified (those are left untouched + reported).
    """
```

### `yadgar/backend/consolidation/cleanup.py` — modify `_try_store_action_summary`

The existing signature (`cleanup.py:173`) is unchanged; the edit adds one line deriving `project_id` before the `insert_memory` call at `:187`:

```python
from yadgar.core.identity import derive_project_id  # noqa: PLC0415
_pid, _ = derive_project_id(directory)
# ... inside the insert_memory dict, after "directory_context": directory, (cleanup.py:192):
"project_id": _pid,
```

### `yadgar/backend/queue_drainer/apply.py` — modify the `wiki_add` replay branch

At `apply.py:126`, after setting `directory_context`, also set `project_id`:

```python
p["directory_context"] = p.get("directory_context") or p.get("directory")
from yadgar.core.identity import derive_project_id  # noqa: PLC0415
_pid, _ = derive_project_id(p["directory_context"])
p["project_id"] = _pid
```

[VERIFY: `apply.py` runs in the backend container — confirm `yadgar.core.identity` is importable there at replay time; the backend already imports from `yadgar._shared` and `yadgar.backend`, but `yadgar.core` imports from backend are the forward-direction (core → backend over HTTP). If core is NOT importable in the drainer, the derivation must be duplicated or moved to `_shared`. This is the same import-direction question Car A0's "identity.py is core-side" decision raises — flag at build time.]

## 4. Build steps (TDD)

1. **RED — backfill post-conditions test.** Write `tests/backend/test_car_l_project_id_backfill.py` that: (a) inserts fixture `wiki_page` + `memory` rows with known `directory_context` values covering all three cases (git-repo-with-remote, exists-no-remote, path-gone) plus the `global` sentinel; (b) runs `_migration_030_project_id_backfill`; (c) asserts `project_id` per case: derived `owner/repo` for the git case, `local/<basename>` for the no-remote case, `'unresolved'` with `legacy_directory` set for the gone case, `'global'` for the sentinel. Fails: migration does not exist.
2. **GREEN — implement the migration.** Add `_migration_030_project_id_backfill` to `migrations.py`; append to `_MIGRATIONS` after `029`. Re-run the test.
3. **RED — re-slug dry-run test.** Write `tests/backend/test_car_l_adr_reslug.py` that: (a) inserts 3 fixture ADR wiki pages with old slugs `yadgar-adr-0001`..`yadgar-adr-0003` + a `wiki_crossref` row linking two of them + an inline `[[yadgar-adr-0002]]` in one body; (b) calls `reslug_adr_pages({"dry_run": True, "project_id": "m-agahi/yadgar"})`; (c) asserts the manifest maps each old slug to `m-agahi_yadgar_adr-0001`..`0003` AND that NO write occurred (slugs unchanged). Fails: `reslug.py` does not exist.
4. **GREEN — implement `reslug_adr_pages`** (dry-run path first). Re-run.
5. **RED — re-slug apply test.** Same fixture, `dry_run=False`. Assert: wiki_page.slug rewritten, wiki_crossref.from_slug + to_slug rewritten, inline `[[old]]` replaced with `[[new]]` in content, `set_adr_body_slug` called once per page with the new slug. Fails: apply path not implemented.
6. **GREEN — implement the apply path.** Re-run.
7. **RED — idempotency test.** Run the re-slug twice. Assert the second run skips every page (already in new format) and writes nothing. Fails or no-op assertion.
8. **GREEN — add the skip-detector regex.** Re-run.
9. **RED — regression test for nightly + drainer.** Write `tests/backend/test_car_l_write_paths_stamp_project_id.py` that patches `derive_project_id` and asserts `_try_store_action_summary` (`cleanup.py:173`) and the `wiki_add` drainer branch (`apply.py:120`) stamp `project_id` on the inserted dict. Fails: the two write sites do not yet set `project_id`.
10. **GREEN — add the two `project_id` stamps** to `cleanup.py:192` and `apply.py:126`. Re-run.
11. **REFACTOR — extract `classify_directory_context`** into `yadgar/core/identity.py` if the import-direction concern (§3 [VERIFY]) resolves cleanly; otherwise keep the classifier inline in the migration and document why.

## 5. Acceptance gates

- [ ] `_migration_030_project_id_backfill` is idempotent: a second run is a full no-op (every row already has `project_id`).
- [ ] No row is silently dropped: every `wiki_page` + `memory` row has a `project_id` after the migration (quarantine rows have `project_id='unresolved'` + `legacy_directory` set — fail-loud lineage, not silent deletion).
- [ ] The quarantine set is surfaced: a log line names every distinct `directory_context` that mapped to `unresolved`; the operator review list is non-empty only when such paths exist.
- [ ] ADR re-slug is idempotent: a second run skips every page already in `{project_id}_adr-NNNN` format.
- [ ] `wiki_crossref` has zero dangling rows after the re-slug: every `from_slug`/`to_slug` matches an existing `wiki_page.slug`.
- [ ] Inline `[[old-slug]]` text in page bodies is replaced (not just the structured crossref rows) — the regex is `_WIKI_LINK_RE` (`dispatch_helper.py:140`), reused verbatim.
- [ ] `adr.body_slug` (SQL, Car A) matches the new `wiki_page.slug` for every ADR row.
- [ ] Nightly (`cleanup.py:192`) + drainer (`apply.py:126`) stamp `project_id` on every new write — no post-migration row lacks it.
- [ ] Live ADR count re-verified via `wiki_list(slug_prefix="yadgar-adr")` immediately before the re-slug; the script operates on the live list, not a baked number.
- [ ] core/backend version bumped per WORKFLOW RULE (new core + backend version — both sides touched: `core/identity.py` import + `core/scripts/reslug_adr.py` on core; `backend/admin_exec/reslug.py` + `backend/consolidation/cleanup.py` + `backend/queue_drainer/apply.py` on backend) [VERIFY exact bump mechanism: `scripts/check_versions.py` + `scripts/check_backend_bump.py` with `BACKEND_BUILD_DIRS=("backend",)` — confirm the version-key at build time]
- [ ] pre-commit green (ruff, import-linter, I32, I33, `check_versions`, `check_ledger_chokepoint`)
- [ ] tests pass (the 6 new tests above + the full suite)

## 6. Sequencing

- **Must merge before L:** A0 (ships `yadgar/core/identity.py` + `derive_project_id()` — L imports it; ships the `project` registry — L's `unresolved` quarantine writes a row the registry must accept). A (ships `body_slug` column + `set_adr_body_slug()` — L's re-slug calls it to sync the SQL ADR row; ships `002_ledger_tables` with the `project_id` column on `adr`).
- **Waits on L:** M (cross-project `project=` param — L's backfill must have landed so the registry has rows to validate against; M's override-validation reads the registry L populates). Car G (ADR seed + retype) benefits from L's re-slug having run but does not strictly block on it — coordinate the order so the re-slug does not race the ADR index deletion in G.
- **Parallel-safe:** L's backfill migration runs at daemon boot and is independent of cars B–K's code paths. The re-slug admin op touches `wiki_page` + `wiki_crossref` only — coordinate with Car K (nightly archive sweep) so they do not run simultaneously against the same corpus.

## 7. ADRs / decisions

- **D32 ③** — `body_slug = project_id` with `/` → `_`, then `_` as universal separator (e.g. `m-agahi_yadgar_adr-0194`). Globally unique by construction — eliminates both collision surfaces. Car L applies this to the existing ADR corpus.
- **§16.8 / §16.9 (migration case 3)** — QUARANTINE, suggest-and-confirm, never auto-map. Rows whose `directory_context` no longer exists get `project_id='unresolved'` + `legacy_directory` preserved. The asymmetry decides it: a few dozen rows are trivial to review by hand; a basename heuristic applied across ~5,156 rows is unrecoverable.
- **§16.11 Car L backend-bump** — YES (scripts + backend consolidation path). Both core and backend versions bump (identity.py import + scripts on core; admin_exec + consolidation + drainer on backend).
- **ADR-0202** — enforcement = FAIL LOUD (Car A0). Car L's quarantine writes `project_id='unresolved'` — [VERIFY: is `'unresolved'` a pre-registered `project` registry row, or does the backfill bypass the registry check? The registry check is on the LIVE write path (Car A0's `_ensure_project_exists`); the migration writes SurrealDB directly, NOT through the registry guard. Confirm `'unresolved'` is NOT required to be a registry row — the registry guards SQL `task`/`adr` writes, not SurrealDB `wiki_page`/`memory` rows. If they ARE linked, pre-seed an `unresolved` registry row before the migration runs.]

## 8. Out of scope

- **Dropping `directory_context`** — L adds `project_id` and backfills it but does NOT remove `directory_context`. The read path (`get_wiki_page_by_slug_directory` at `wiki.py:409`, the `directory_context = $dir` filters at `memory.py:518,928,1173`) still reads it. Car M flips readers onto `project_id`; a later migration drops the field after Car M is confirmed green. This avoids a flag-day cutover.
- **Re-keying the SQL `task`/`adr` tables** — those ship with `project_id` from Car A's `002_ledger_tables`; no backfill needed (they are new tables, seeded by Car E/G).
- **The `[[crossrefs]]` in non-ADR pages** — only ADR pages are re-slugged in this car. Prompt/discipline pages (Car I) and task-list pages keep their slugs. [VERIFY: do any non-ADR pages cross-reference ADR slugs inline? `get_all_wiki_crossrefs` (`wiki.py:768`) returns every `from_slug`/`to_slug` pair — the re-slug updates ALL crossref rows referencing an ADR slug regardless of which page owns them, so this is covered. Inline `[[adr-slug]]` text in non-ADR bodies is also replaced (the regex scans all page content).]
- **Federation / cross-instance renumbering** — deferred to task 0095 / §16 (out of scope per §8 of the master plan).
- **The task-list page deletion** — §16.11 says "task-list page deleted (tasks move to SQL, Car D)". The deletion is listed in Car L's scope but MUST be coordinated with Car E (task seed + SessionStart/stop-hook rewire): deleting the page before Car E's stop-hook stops reading `{project}-task-list` (`stop_checkpoint_prompt.md:26-33` per Car G's dead-read fix) breaks the restore nudge. [VERIFY: confirm with Car E owner whether the page deletion lives in L or E — the §16.11 text places it in L, but the safe order is E-rewires-stop-hook → L-deletes-page. If E has not landed, L must NOT delete the page.]

## 9. Risks / open questions

- **[VERIFY: ADR count] — "194" is stale.** Live `wiki_list(slug_prefix="yadgar-adr")` on 2026-08-09 returns **222** ADR body pages (0001–0222). The re-slug script must operate on the live list, not a hardcoded count. Re-run `wiki_list` immediately before execution.
- **[VERIFY: import direction — `yadgar.core.identity` in the backend] —** The drainer (`apply.py`) and nightly (`cleanup.py`) run in the backend container. `yadgar.core.identity` is core-side (Car A0 decision). If core is not importable in the backend process, the `derive_project_id` call at `apply.py:126` and `cleanup.py:192` fails at import time. The backend already imports `yadgar._shared.*` freely; `yadgar.core` imports are the HTTP-forward direction. Resolve at build time: either (a) move `derive_project_id` to `yadgar/_shared/` (contradicts Car A0's "core-side" decision), (b) duplicate a backend-side classifier, or (c) have the backend call core over HTTP for derivation (heavy for a per-row op). This is the load-bearing open question for Car L.
- **[VERIFY: migration import of `classify_directory_context`] —** Migrations 016/018/023 (`migrations.py:553,678,927`) call only `storage._q`, never core helpers. If `_migration_030` imports `yadgar.core.identity` and migrations run before the full core import path is available, the migration fails at boot. Either duplicate the classifier inline in the migration body, or confirm the migration runner has the full import path. Check how `_run_migrations_locked` is invoked in the backend lifecycle.
- **[VERIFY: `'unresolved'` registry row] —** If the `project` registry guards SurrealDB writes (not just SQL `task`/`adr`), the migration's `project_id='unresolved'` writes must be preceded by a registry insert for the `unresolved` key. Confirm the registry's scope (SQL only vs. both stores) at build time — see §7 ADR-0202 note.
- **[VERIFY: task-list page deletion ownership] —** §16.11 places it in L, but the safe order requires Car E's stop-hook rewire to land first. Confirm the cross-car ordering with the Car E author; if E has not landed, defer the deletion to E or gate it on E's merge.
- **[VERIFY: `legacy_directory` field on `memory` table] —** The §16.9 text names `legacy_directory` generically. Both `wiki_page` and `memory` get it (quarantine rows in either store). Confirm the field is `option<string>` (nullable — only quarantine rows set it), not NOT NULL.
- **[VERIFY: distinct `directory_context` count] —** §16.8 says "Survey the distinct `directory_context` values first — that count, not the row count, is the real size of the job." The backfill should classify distinct paths once and apply the mapping to all rows sharing that path. Run `SELECT directory_context, count() FROM memory GROUP BY directory_context` + same for `wiki_page` before the build to size the classifier cache and the quarantine set. Not done in this doc — it is a runtime survey, not a coordinate.
