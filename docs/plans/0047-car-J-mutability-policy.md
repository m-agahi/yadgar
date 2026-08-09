# Car J — mutability policy field + per-page override + wiki_set_mutability

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: A
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Add a `mutability` axis to wiki page governance: a per-`page_type` policy default (D25 — `WikiPolicy` field #6), a nullable per-page `mutability_override` column on `wiki_page`, and a power-gated, logged `wiki_set_mutability` MCP tool as the sole escape hatch. Enforcement lives at the storage write chokepoint `update_wiki_page` (D25) so it covers every write path — the eight anchor-text/positional edit ops via `WikiStore._apply_text_edit`, `append_section`, `restore_version`, the `WikiStore.add` append path, and the backend `admin_exec/wiki.py` `wiki_update` op that bypasses `WikiStore` entirely. Per-type defaults (D26): `adr`/`adr_superseded` → locked; `task`, `agent_prompt` → free; rollups → derived. `locked` blocks agent/tool edits but NOT sanctioned server-side lifecycle transitions (e.g. the Car G supersede retype), otherwise the guard deadlocks its own lifecycle. Mutability is about stopping the well-intentioned repair (rewriting a derived rollup that looks stale, deleting an ADR page to "resolve" a dangling ref) — a side benefit is dangling-pointer prevention.

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/wiki/policy.py:64` | Add `mutability: str = "free"` as `WikiPolicy` field #6 (appended with default so existing 4–5-arg positional callers keep working). Per-type values set in `POLICY_BY_TYPE` (line 123) and a new `MUTABILITY_BY_TYPE` map for D26 defaults. | `yadgar/_shared/wiki/policy.py:64` (class), `:98` DEFAULT_POLICY, `:123` POLICY_BY_TYPE — confirmed via Read |
| `yadgar/_shared/storage/wiki.py:215` | Enforce mutability in `update_wiki_page`: resolve effective policy (per-type default OR per-page `mutability_override`), reject `locked`/`derived` writes that are not sanctioned lifecycle transitions. Also gate `insert_wiki_page` (`:136`) and `delete_wiki_page` (`:450`) for symmetry. | `yadgar/_shared/storage/wiki.py:215` `def update_wiki_page(self, page_id: int, updates: dict) -> bool:` — confirmed exact line |
| `yadgar/_shared/storage/migrations.py` | New SurrealDB migration (next number after 029) adding `DEFINE FIELD mutability_override ON TABLE wiki_page TYPE option<string>;` (nullable, schemaless — idempotent per existing pattern at `:111`). | `yadgar/_shared/storage/migrations.py:111` (DEFINE FIELD pattern), `:1348` `_migration_029_drop_branch_column` is last numbered migration; `_MIGRATIONS` list at `:1390` — confirmed via grep |
| `yadgar/_shared/wiki/store.py:1900` | `_apply_text_edit` and the direct `update_wiki_page` call sites (`:764`, `:791`, `:1189`, `:1554`, `:1692`, `:1916`) — enforcement moves to storage layer so these need NO per-call guard, but a sanctioned-transition bypass hook may be needed for the supersede retype (Car G). | `yadgar/_shared/wiki/store.py:1900` `def _apply_text_edit`, `:1916` `self._storage.update_wiki_page(page_id, {"content": new_content})` — confirmed |
| `yadgar/backend/admin_exec/wiki.py:126` | `wiki_update` op (spans `:126–148`, storage call at `:143`) — enforcement at `storage.update_wiki_page` covers it automatically since it calls storage directly, bypassing `WikiStore`. | `yadgar/backend/admin_exec/wiki.py:126` `def wiki_update(payload: dict)`, `:143` `storage.update_wiki_page(page_id, fields)` — confirmed via Read; line `:139` (the §7 row citation) is `if result is None:` inside the same function |
| `yadgar/core/server/tools/wiki.py:1055` | NEW `wiki_set_mutability(slug, value, reason)` MCP tool, `@_tool(power=True)`, forwarding to backend via `_forward_admin("wiki_set_mutability", ...)`. Pattern mirrors `wiki_set_metadata` (`:1056`). | `yadgar/core/server/tools/wiki.py:1055` `@_tool(power=True)` + `:1056` `def wiki_set_metadata`, `:1101` `_forward_admin("wiki_set_metadata", ...)` — confirmed |
| `yadgar/backend/admin_exec/wiki.py:184` | NEW `wiki_set_mutability(payload)` backend handler, mirrors `wiki_set_metadata` (`:184`). Calls a new `WikiStore.set_mutability_by_slug(slug, value, reason)` which writes `mutability_override` + logs. | `yadgar/backend/admin_exec/wiki.py:184` `def wiki_set_metadata(payload: dict)` — confirmed pattern |
| `yadgar/core/server/tools/__init__.py:80,172` | Export `wiki_set_mutability` in the tool registry (import + `__all__`), mirroring `wiki_set_metadata`. | `yadgar/core/server/tools/__init__.py:80` import, `:172` `"wiki_set_metadata"` in `__all__` — confirmed via grep |
| `yadgar/core/server/tools/admin_other.py:42` | `_WIKI_UPDATE_ALLOWED` frozenset (`{"content", "tags", "category", "confidence"}`) — D25 flags `tags` as an un-gated strip vector (can un-supersede an ADR by stripping `adr-status:superseded`). Decide whether mutability lock also blocks `tags` mutation on locked pages. | `yadgar/core/server/tools/admin_other.py:42` `_WIKI_UPDATE_ALLOWED: frozenset[str] = frozenset({"content", "tags", "category", "confidence"})` — confirmed via grep |
| `yadgar/__init__.py:21` | `BACKEND_VERSION` bump per WORKFLOW RULE. | `yadgar/__init__.py:21` `BACKEND_VERSION = "5.71.0"` — confirmed; core `__version__` derived from pyproject via `importlib.metadata` (`:4`) |

## 3. Functions / symbols

**New / modified:**

- `WikiPolicy` (`yadgar/_shared/wiki/policy.py:64`) — add field `mutability: str = "free"` (field #6, appended with default; docstring already states "New fields MUST be appended with defaults so existing 4-arg positional callers keep working" at `:69`). Values: `"free"` | `"locked"` | `"derived"`.
- `MUTABILITY_BY_TYPE: dict[str, str]` (new, in `policy.py`) — D26 per-type defaults: `adr` → `"locked"`, `adr_superseded` → `"locked"`, `task` → `"free"`, `agent_prompt`/`agent_pattern`/`agent_discipline`/`agent_index` → `"free"`, rollup page types → `"derived"`. Resolver `get_policy(page_type)` (`:147`) surfaces the new field.
- `update_wiki_page(self, page_id: int, updates: dict) -> bool` (`yadgar/_shared/storage/wiki.py:215`) — gain a mutability check at entry: read the page's `page_type` + `mutability_override`, resolve effective mutability (`override` wins over per-type default), reject non-sanctioned writes when effective mutability is `locked`/`derived`. Sanctioned transitions pass a flag (e.g. `_sanctioned=True` kwarg) so the Car G supersede retype is not deadlocked (D26).
- `insert_wiki_page(self, page: dict, branch: str | None = None) -> int` (`:136`) and `delete_wiki_page(self, page_id: int) -> bool` (`:450`) — symmetric mutability gate for insert/delete (a locked page must not be deleted by an agent tool; `derived` pages are regenerated, not hand-edited).
- `wiki_set_mutability(slug: str, value: str, reason: str, directory: str | None = None) -> dict` (NEW, `yadgar/core/server/tools/wiki.py`) — `@_tool(power=True)`, forwards to `_forward_admin("wiki_set_mutability", {"slug": slug, "value": value, "reason": reason})`. Signature mirrors `wiki_set_metadata` (`:1056`). `value ∈ {"free", "locked", "derived", None}` (None clears the override back to per-type default). `reason` is required and logged.
- `wiki_set_mutability(payload: dict) -> dict` (NEW backend handler, `yadgar/backend/admin_exec/wiki.py`) — mirrors `wiki_set_metadata` (`:184`); calls `WikiStore.set_mutability_by_slug(...)`.
- `WikiStore.set_mutability_by_slug(self, slug: str, value: str | None, reason: str) -> dict` (NEW, `yadgar/_shared/wiki/store.py`) — writes `mutability_override` on every row sharing `slug` (all-rows pattern of `set_metadata_by_slug` at `:1782`), creates a version row on real change, logs old+new+reason for audit. Add `"mutability_override"` to `WikiStore._METADATA_FIELDS` (`:1713`) only if the privileged path is kept — otherwise the sole write path is `set_mutability_by_slug`.

**Existing signatures to preserve (verified):**

- `WikiStore._apply_text_edit(self, page_id, new_content, old_content, replaced_count) -> dict` (`yadgar/_shared/wiki/store.py:1900`) — unchanged; enforcement moves DOWN to `storage.update_wiki_page`, so the eight edit ops need no per-call guard.
- `admin_exec` `wiki_update(payload: dict) -> dict` (`yadgar/backend/admin_exec/wiki.py:126`) — unchanged; its `storage.update_wiki_page(page_id, fields)` call at `:143` inherits the guard.

## 4. Build steps (TDD)

1. **RED** — `tests/test_mutability_policy.py`: a locked `adr` page rejects `update_wiki_page` with a `MutabilityLocked` error (or `{ok: False, error}` shape matching store conventions); a `task` page accepts the same update. Assert `wiki_set_mutability("adr-xxx", "free", "test")` unblocks the write. Assert `admin_exec.wiki_update` with `fields={"tags": [...]}` stripping `adr-status:superseded` is rejected on a locked page.
2. **GREEN** — add `WikiPolicy.mutability` field + `MUTABILITY_BY_TYPE` map in `policy.py`; add the guard in `storage/wiki.py:215 update_wiki_page` (+ insert/delete symmetry); add the SurrealDB `DEFINE FIELD mutability_override` migration.
3. **RED** — `wiki_set_mutability` tool test: `@_tool(power=True)` gate fires for non-power caller; power caller gets `{ok: True, rows_updated, page_ids}`; `reason` is required; `value=None` clears the override; the override wins over per-type default on the next `update_wiki_page`.
4. **GREEN** — implement `wiki_set_mutability` core shell + backend handler + `WikiStore.set_mutability_by_slug`; wire export in `tools/__init__.py`; add `"mutability_override"` write path.
5. **RED** — sanctioned-transition test: a `_sanctioned=True` update on a `locked` `adr` page (the supersede retype shape) is NOT rejected — proves D26's "locked blocks agent/tool edits, not sanctioned server-side lifecycle transitions".
6. **GREEN** — wire the sanctioned bypass flag through `update_wiki_page`; confirm Car G's retype call site will pass it (Car G builds the retype mutator; this car exposes the seam).
7. **REFACTOR** — centralise the effective-mutability resolver (`override or MUTABILITY_BY_TYPE.get(page_type, "free")`) in `policy.py` so storage and the tool layer share one function; remove any duplicated logic.

## 5. Acceptance gates

- [ ] `WikiPolicy.mutability` is field #6 with a default; existing 5-arg positional `WikiPolicy(...)` callers (tests) still construct without error.
- [ ] `update_wiki_page` (`storage/wiki.py:215`) rejects non-sanctioned writes on `locked`/`derived` pages; `_sanctioned=True` bypasses the guard.
- [ ] `wiki_set_mutability` is `@_tool(power=True)`, requires `reason`, logs old+new+reason, and its override wins over the per-type default on the next write.
- [ ] `admin_exec` `wiki_update` (`yadgar/backend/admin_exec/wiki.py:126`) inherits the guard via its `storage.update_wiki_page` call (`:143`) — no separate guard needed.
- [ ] D25 `tags` vector closed: stripping `adr-status:superseded` via `wiki_update` is rejected on a locked ADR page.
- [ ] core/backend version bumped per WORKFLOW RULE (`yadgar/__init__.py:21` BACKEND_VERSION; core `__version__` via pyproject) [VERIFY exact bump mechanism via `scripts/bump_version.py` if unsure]
- [ ] pre-commit green (ruff, import-linter, I32, I33, `check_versions`)
- [ ] tests pass

## 6. Sequencing

- **Depends on A** — needs `runtime_config` and the storage/migration seam Car A establishes; the `mutability_override` column is a SurrealDB `DEFINE FIELD` migration appended after Car A's migration chain is in place. J "depends only on A and can land early" (§7).
- **Unblocks / coordinates with G** — Car G's supersede retype (`adr` → `adr_superseded`, atomic with status flip) MUST pass `_sanctioned=True` through `update_wiki_page` or it deadlocks against this car's guard (D26). Ship the sanctioned-bypass seam in J; G consumes it.
- **Unblocks K** — Car K (nightly archive sweep, policy-dispatched) reads mutability to decide which pages are sweep-eligible (`derived` rollups regenerate; `locked` pages are never swept).

## 7. ADRs / decisions

- **D25** — `mutability` as `WikiPolicy` field #6 + nullable per-page `mutability_override` + power-gated logged `wiki_set_mutability`; enforced at `storage/wiki.py:215 update_wiki_page` (and insert/delete), NOT `WikiStore.add`. Enforcement-point correction is load-bearing: `WikiStore._apply_text_edit` backs 8 edit tools and calls `update_wiki_page` directly; `admin_exec/wiki.py` `wiki_update` bypasses `WikiStore` entirely; `_WIKI_UPDATE_ALLOWED` includes `tags` (an un-gated un-supersede vector).
- **D26** — per-type: `adr`/`adr_superseded` → locked; `task`/`agent_prompt` → free; rollups → derived. `locked` blocks agent/tool edits, NOT sanctioned server-side lifecycle transitions (else supersede retype deadlocks its own guard).
- **D22 (adjacent)** — `recall_disposition` is status-driven; mutability is orthogonal (write-side), not a recall concern. No interaction beyond both living on `WikiPolicy`.

## 8. Out of scope

- The supersede retype mutator itself (Car G builds it; J only exposes the `_sanctioned` seam).
- Nightly sweep / `derived` rollup regeneration (Car K).
- Per-row mutability on `memory` (mutability is a `wiki_page` field only; memories have their own `is_protected`/anchor mechanism).
- Removing `tags` from `_WIKI_UPDATE_ALLOWED` (separate decision; this car blocks the harmful strip on locked pages via the mutability guard, it does not shrink the allowlist).
- Alembic/MariaDB side of mutability — `mutability_override` lives on `wiki_page`, which stays in SurrealDB (D4); the ledger tables in MariaDB do not carry a mutability column.

## 9. Risks / open questions

- [VERIFY: `_sanctioned=True` kwarg shape] — `update_wiki_page(self, page_id, updates)` is `@trace_span()` and called from many sites; adding a kwarg defaults to `False` so existing callers are unaffected, but Car G's retype call must be the ONE site that passes `True`. Confirm the retype call point when Car G starts.
- [VERIFY: exact bump mechanism] — `scripts/bump_version.py` and `scripts/check_versions.py` exist; the bump target is `BACKEND_VERSION` (`yadgar/__init__.py:21`) for backend and pyproject `version` for core. Confirm which a storage-layer change bumps.
- [VERIFY: `derived` enforcement on `delete_wiki_page`] — `derived` rollups are regenerated, so a hand delete should be rejected, but the nightly sweep (Car K) needs to delete+recreate them. Either the sweep passes `_sanctioned=True` or `derived` guards delete only on the agent/tool path, not the sweep path. Resolve with Car K.
- [VERIFY: mutability of `adr` pages written by `adr_add` before the column exists] — migration adds the nullable field; pre-migration rows have `mutability_override = NULL` → fall back to per-type default (`adr` → locked). No backfill needed. Confirm no existing test asserts a free-write on an `adr` page that would now break.
- D25 cites `store.py:1905` for `_apply_text_edit`; actual def is `yadgar/_shared/wiki/store.py:1900` (call to `update_wiki_page` at `:1916`). The `:1905` line is the `replaced_count` parameter. Minor drift; the function and the `update_wiki_page` call are confirmed.
- D25 cites `append_section (1729)` and `restore_version (1598)`; actual lines are `yadgar/_shared/wiki/store.py:1621` (`append_section`) and `:1518` (`restore_version`). Both confirmed to call `storage.update_wiki_page` (`:1692` and `:1554` respectively). Drift is pre-existing in the audit text; current coordinates cited above.

## Yadgar findings

- `yadgar/_shared/wiki/policy.py:64` `WikiPolicy` currently has 5 fields (`gate_mode`, `recall_disposition`, `dir_scope`, `merge`, `storage_scope`); its own docstring at `:69` mandates "New fields MUST be appended with defaults so existing 4-arg positional callers keep working" — Car J's `mutability` field #6 conforms.
- `yadgar/_shared/storage/wiki.py:215` is verbatim `def update_wiki_page(self, page_id: int, updates: dict) -> bool:` — the D25 enforcement point. `insert_wiki_page` at `:136`, `delete_wiki_page` at `:450`.
- `yadgar/backend/admin_exec/wiki.py:126` `def wiki_update(payload: dict)` spans `:126–148`; its storage call `storage.update_wiki_page(page_id, fields)` is at `:143`. It bypasses `WikiStore` (calls the storage mixin directly), confirming D25's "never touches WikiStore" — so enforcement MUST be at the storage layer, not `WikiStore.add`. The §7 row's `:139` citation lands inside this function (`if result is None:`).
- `yadgar/core/server/tools/admin_other.py:42` `_WIKI_UPDATE_ALLOWED = frozenset({"content", "tags", "category", "confidence"})` — confirms D25's `tags` un-supersede vector; the `:925` check rejects unknown fields but does NOT gate `tags` content on locked pages.
- `wiki_set_mutability` does NOT exist anywhere in `yadgar/` (grep empty) — it is a NEW tool. The build pattern is `wiki_set_metadata` (`yadgar/core/server/tools/wiki.py:1056`, `@_tool(power=True)` at `:1055`, forwards via `_forward_admin` at `:1101`) + backend handler (`yadgar/backend/admin_exec/wiki.py:184`) + `WikiStore.set_metadata_by_slug` (`yadgar/_shared/wiki/store.py:1782`).
- `yadgar/_shared/wiki/store.py:1900` `_apply_text_edit` is the single chokepoint for 8 anchor-text/positional edit ops and calls `self._storage.update_wiki_page(page_id, {"content": new_content})` at `:1916` — storage-layer enforcement covers all 8 with zero per-call guards.
- Latest SurrealDB migration is `_migration_029_drop_branch_column` (`yadgar/_shared/storage/migrations.py:1348`); the `_MIGRATIONS` append-only list is at `:1390`. The new `mutability_override` field migration is #030, following the `DEFINE FIELD IF NOT EXISTS ... TYPE option<string>` pattern at `:111`.
- `yadgar/__init__.py:21` `BACKEND_VERSION = "5.71.0"`; core `__version__` is derived from pyproject via `importlib.metadata` (`:4`). Bump scripts: `scripts/bump_version.py`, `scripts/check_versions.py`, `scripts/check_backend_bump.py`.
