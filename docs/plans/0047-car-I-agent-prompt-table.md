# Car I — agent_prompt ledger tables + list/get + delete TOC machinery

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: B
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Materialize the agent-prompt library as queryable MariaDB ledger tables and replace the `agent-prompt-toc` wiki index page + its write/read machinery with `agent_prompt_list` / `agent_prompt_get` MCP tools. Per the §14 schema rewrite, the single `agent_prompt` table (D3 shape half) is RETIRED in favor of three tables — `agent_pattern`, `agent_discipline`, and the contract as an `always_applied=TRUE` discipline — plus the `agent_pattern_composes` ordered join (§3.2). `agent_pattern_model` + `client` (§3.3, closes task 0094) are part of the same schema family and ship here. Car I then:

- adds an Alembic revision (after Car A0's `003_project_registry`) creating those tables;
- adds `agent_prompt_list` / `agent_prompt_get` as NEW core MCP tools (§4 line 434; confirmed absent today — see §3);
- re-points the read path — `_build_agent_prompt_toc` and `_get_agent_prompt_toc_updated_at` — from the wiki TOC page to `agent_pattern` table queries;
- deletes the TOC write/read CODE (`_upsert_toc_row`, `_set_toc_row_count`, `increment_prompt_usage`, `_ensure_library_anchor`, the `%10` throttle, the dual `_TOC_SLUG`/`_TOC_ROW_RE` constants, `_COMPOSES_SECTION_RE`/`_parse_composes`/`_strip_composes_section`) and the `StorageEngine.increment_prompt_usage` / `get_prompt_usage_counts` methods + the `_prompt_usage` memory row;
- ships the one-shot seed (D35a: seeded from the body PAGES, not the TOC index) with its verification gate (D35c) in this same car;
- marks the `agent-prompt-toc` wiki page superseded per D35d (content → one-line pointer, `superseded-by-ledger` tag, slug preserved for one release cycle — NOT hard-deleted).

`agent_prompt_save`, `agent_dispatch_prelude`, `discipline_save`, `seed_agent_prompts` signatures are unchanged; their return shapes GAIN `baseline_hash`/`content_hash` (ADR-0209, §14.3).

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/storage/sql/migrations/versions/0001_config_table.py` | existing revision `0001_config` (down_revision=None) — Car I chains after it via Car A's `002_ledger_tables` and Car A0's `003_project_registry` | `:49` `revision = "0001_config"` |
| `yadgar/_shared/storage/sql/migrations/versions/00NN_agent_tables.py` (NEW) | new Alembic revision creating `agent_pattern`, `agent_discipline`, `agent_pattern_composes`, `agent_pattern_model`, `client`; `down_revision = "003_project_registry"` [VERIFY: exact revision id of Car A0's `003_project_registry` — not yet shipped] | [VERIFY: Car A0 revision id] |
| `yadgar/_shared/storage/sql/migrations/env.py` | no change (hand-written revisions, `target_metadata=None`, `OFFLINE_DIALECT="mysql"`) — cite only | `:37` `target_metadata = None`, `:41` `OFFLINE_DIALECT = "mysql"` |
| `yadgar/_shared/storage/wiki.py` | DELETE `get_prompt_usage_counts` and `increment_prompt_usage` methods + the `_prompt_usage`-tagged memory row write path | `get_prompt_usage_counts` at `:931`; `increment_prompt_usage` at `:949`; `_prompt_usage` tag literal at `:939,965,980` (plan §5 cited `:1067/:1085/:1075/:1101/:1116` — STALE) |
| `yadgar/backend/admin_exec/wiki.py` | DELETE `_TOC_SLUG`/`_TOC_ROW_RE` (`:56-57`), `_upsert_toc_row` (`:343-383`), `_set_toc_row_count` (`:509-549`), `increment_prompt_usage` admin op (`:553-580`), `_ensure_library_anchor` (`:386-412`), `_LIBRARY_ANCHOR_*` constants (`:58-60`); strip TOC upsert + library-anchor calls from `agent_prompt_save` (`:415-503`, calls at `:495`) | verified via grep + read |
| `yadgar/core/server/tools/agent_prompts.py` | DELETE core `_TOC_SLUG` (`:42`)/`_TOC_ROW_RE` (`:45`)/`_LIBRARY_ANCHOR_*` (`:47-48`) constants; ADD `agent_prompt_list` + `agent_prompt_get` MCP tools; re-point `agent_prompt_save` (`:111`) to also write the `agent_pattern`/`agent_discipline` ledger row (via backend admin op, ADR-0078); `discipline_save` (`:366`) gains ledger-row write; `seed_agent_prompts` (`:479`) becomes the idempotent seed-of-tables (keyed on PK `name`); `_read_agent_prompt` (`:543`) stays as the body-page reader used by `agent_prompt_get` | verified; plan §4/§14.3 cited `discipline_save` at `:373` — STALE, def is `:366` |
| `yadgar/core/server/tools/dispatch_helper.py` | DELETE `_COMPOSES_SECTION_RE` (`:136`), `_parse_composes` (`:143-159`), `_strip_composes_section` (`:162-168`); re-point `agent_dispatch_prelude` (`:343`) to resolve composes from the `agent_pattern_composes` table (via backend) instead of parsing the `## Composes` section from the wiki body; remove the `increment_prompt_usage` forward at `:318` (replaced by `UPDATE agent_pattern SET uses = uses + 1` on the backend) | verified |
| `yadgar/core/server/tools/project.py` | RE-POINT `_build_agent_prompt_toc` (`:1821-1842`) from `get_wiki_page_by_slug(_TOC_SLUG)` to a `SELECT name, purpose FROM agent_pattern ORDER BY uses DESC LIMIT 20` (via backend); RE-POINT `_get_agent_prompt_toc_updated_at` (`:1421-1442`) from the TOC page `updated_at` to `MAX(agent_pattern.updated_at)`; `_apply_agent_prompt_signal` (`:1547-1620`, toc read at `:1581`) unchanged in logic — its `toc_ts` now comes from the table. Plan §7 cited `_build_agent_prompt_toc` at `project.py:1898-1921` — STALE, actual is `core/server/tools/project.py:1821-1842` | verified via read |
| `yadgar/core/server/tools/__init__.py` | EXPORT `agent_prompt_list`, `agent_prompt_get` (today exports only `agent_prompt_save`, `discipline_save`, `seed_agent_prompts`, `agent_dispatch_prelude` at `:103-107,187-190`) | verified absent today |
| `yadgar/backend/admin_exec/__init__.py` | REGISTER backend admin ops for `agent_prompt_list` / `agent_prompt_get` / `agent_pattern_composes` reads + `uses` increment (forwarded from core per ADR-0078) | [VERIFY: exact registration site — grep confirms `agent_prompt_save`/`increment_prompt_usage` registered here today] |
| `yadgar/backend/retrieval/recall_pipeline.py` | the `wiki_exclude = ["agent-prompt", "agent-prompt-toc"]` default at `:418` — the `agent-prompt-toc` entry becomes dead once the page is superseded; KEEP `agent-prompt` (body pages stay excluded, D22). Drop the `agent-prompt-toc` token from the exclude list as part of deleting the TOC machinery | `:418` verified |
| `yadgar/backend/admin_exec/invariants_cross_engine.py` | `SPINE_LEDGER_TABLES = ("adr", "agent_discipline", "agent_pattern")` at `:117` already names these tables; `check_page_row_desync` (`:392`) flips from `REASON_SPINE_NOT_SHIPPED` (`:74`) tri-state-unavailable to a live `content_hash` row↔page comparison once the tables APPEAR — Car I MUST ship `content_hash` on both row and page (§14.3 hard requirement) | `:117,392,74` verified |
| `yadgar/tests/core/test_agent_prompt_discovery_s6.py` | FLIP tests from "TOC page created/leaks" to "`agent_prompt_list` returns rows / TOC page is a pointer". Today asserts page creation (`:67`), anchor (`:110`), restore slug (`:135`), no-leak (`:147-160`) | verified |
| `yadgar/tests/backend/test_prompt_usage_counter.py` | DELETE / replace — pins the `_prompt_usage` memory row + `increment_prompt_usage` op (`:57`) + `get_prompt_usage_counts` (`:47`); replaced by `agent_pattern.uses` SQL integer tests (D40) | verified |
| `yadgar/tests/core/test_agent_prompt_migration.py`, `test_agent_prompt_model.py`, `test_agent_prompts.py`, `test_seed_agent_prompts_cli.py` | UPDATE for ledger-row return shape (`+baseline_hash/content_hash`) and table-backed list/get | [VERIFY: exact assertion sites during build] |
| `server.json` (`:10` version, `:11` backend_version), `pyproject.toml` (`:7` version) | bump core version AND backend version (Car I touches both `yadgar/core/` and `yadgar/backend/`) | `:10` `5.181.0`, `:11` `backend_version: 5.71.0`, `pyproject.toml:7` `5.181.0` verified |
| `scripts/check_ledger_chokepoint.py` | [VERIFY: does not exist yet — Car A scope per §7. Car I's new backend reads of `agent_pattern` must be added to the allowlist if Car A's guard is live by then] | `find` confirms absent today |

## 3. Functions / symbols

NEW (core MCP tools, registered in `core/server/tools/__init__.py`):

```python
@_tool()
def agent_prompt_list(
    status: str | None = None,   # default: active only (D26 free mutability; D40 sorts by uses DESC)
    directory: str | None = None,  # accepted for contract symmetry; tables are reach-global (no project_id)
) -> dict:
    """List agent patterns. Returns {"patterns": [{"name","purpose","uses","status","body_slug"}], ...}.
    Default sort: uses DESC (D40). No search — D24 explicitly defers search to a future ADR superseding ADR-0007."""

@_tool()
def agent_prompt_get(pattern: str, directory: str | None = None) -> dict:
    """Get one pattern: row metadata + body content (via _read_agent_prompt at agent_prompts.py:543).
    Replaces the removed bespoke agent_prompt_get (agent_prompts.py:19 notes its removal)."""
```

EXISTING — signatures unchanged, return shape GAINS `baseline_hash`/`content_hash`:

```python
def agent_prompt_save(pattern, content, directory=None, purpose=None, storage=None) -> dict  # agent_prompts.py:111
def discipline_save(...) -> dict                                               # agent_prompts.py:366 (plan §4 cite :373 STALE)
def agent_dispatch_prelude(pattern, task_topic, ...) -> str                    # dispatch_helper.py:343
def seed_agent_prompts(storage=None) -> dict                                   # agent_prompts.py:479
```

NEW backend admin ops (registered in `backend/admin_exec/__init__.py`):

```python
def agent_prompt_list_rows(payload: dict) -> dict      # SELECT from agent_pattern (+ join agent_pattern_model for model tier)
def agent_prompt_get_row(payload: dict) -> dict        # one row + body_slug resolution
def increment_agent_pattern_uses(payload: dict) -> dict # UPDATE agent_pattern SET uses = uses + 1 WHERE name = ?  (D40; replaces increment_prompt_usage)
def list_pattern_composes(payload: dict) -> dict       # SELECT discipline_name, position FROM agent_pattern_composes WHERE pattern_name=? ORDER BY position
```

DELETE (backend `admin_exec/wiki.py`): `_TOC_SLUG`, `_TOC_ROW_RE`, `_LIBRARY_ANCHOR_REASON`, `_LIBRARY_ANCHOR_CONTENT`, `_upsert_toc_row`, `_set_toc_row_count`, `_ensure_library_anchor`, `increment_prompt_usage`.

DELETE (core `agent_prompts.py`): the core copies of `_TOC_SLUG`, `_TOC_ROW_RE`, `_LIBRARY_ANCHOR_*` (the plan §5 flags "TWO independent copies" — both go).

DELETE (`_shared/storage/wiki.py`): `StorageEngine.get_prompt_usage_counts`, `StorageEngine.increment_prompt_usage`, the `_prompt_usage`-tagged memory delete-then-insert path.

DELETE (`core/server/tools/dispatch_helper.py`): `_COMPOSES_SECTION_RE`, `_parse_composes`, `_strip_composes_section` (composes now read from `agent_pattern_composes` table).

RE-POINT (`core/server/tools/project.py`): `_build_agent_prompt_toc` (`:1821-1842`) → table query; `_get_agent_prompt_toc_updated_at` (`:1421-1442`) → `MAX(agent_pattern.updated_at)`.

## 4. Build steps (TDD)

1. RED — `test_agent_prompt_list_returns_rows_sorted_by_uses`: seed `agent_pattern` rows directly, call `agent_prompt_list()`, assert rows returned with `uses` and default DESC sort. Fails (table + tool absent).
2. RED — `test_agent_prompt_get_returns_row_and_body`: insert one row + its body wiki page, call `agent_prompt_get("dispatch-fix-bug")`, assert `{name, purpose, uses, body_slug, content, baseline_hash, content_hash}`. Fails.
3. RED — `test_build_agent_prompt_toc_reads_table_not_page`: assert `_build_agent_prompt_toc(storage)` returns `{"slug": <pointer-slug>, "patterns": [...]}` from `agent_pattern` rows and does NOT read `get_wiki_page_by_slug("agent-prompt-toc")`. Fails (still reads page at `project.py:1837`).
4. RED — `test_get_agent_prompt_toc_updated_at_reads_max_updated_at`: assert timestamp comes from `MAX(agent_pattern.updated_at)`, not the wiki page. Fails.
5. RED — `test_increment_agent_pattern_uses_is_one_update`: assert `UPDATE agent_pattern SET uses = uses + 1` runs and no `_prompt_usage` memory row is written; `StorageEngine.increment_prompt_usage` is gone. Fails.
6. RED — `test_toc_machinery_deleted`: import-test that `_upsert_toc_row`, `_set_toc_row_count`, `_ensure_library_anchor`, `increment_prompt_usage` (admin op), `_TOC_SLUG`, `_TOC_ROW_RE` are absent from `backend/admin_exec/wiki.py` and `core/server/tools/agent_prompts.py`; `_COMPOSES_SECTION_RE`/`_parse_composes`/`_strip_composes_section` absent from `dispatch_helper.py`. Fails (still present).
7. RED — `test_agent_pattern_composes_resolved_from_table`: `agent_dispatch_prelude` composes order comes from `agent_pattern_composes` rows, not `## Composes` section parsing. Fails.
8. RED — `test_seed_agent_prompts_idempotent_on_name`: seed twice, assert `created + skipped` stable, rows keyed on `name` PK. Fails (seed writes pages only today).
9. RED — `test_check_page_row_desync_live_for_agent_pattern`: once table exists, `check_page_row_desync` (`invariants_cross_engine.py:392`) returns `status=ok` (or `violation` on injected mismatch), NOT `status=unavailable, reason=spine_not_shipped`. Fails (table absent → unavailable today).
10. GREEN — add Alembic revision `00NN_agent_tables` (chain after `003_project_registry`); add backend admin ops; add core `agent_prompt_list`/`agent_prompt_get`; re-point `project.py` readers; delete TOC machinery + usage-counter methods + composes-section regex; update `seed_agent_prompts` to write rows; flip the S6 tests.
11. REFACTOR — collapse the two `_TOC_*` constant sites into zero (both deleted); ensure `recall_pipeline.py:418` exclude list drops `agent-prompt-toc` but keeps `agent-prompt`; mark the `agent-prompt-toc` wiki page superseded (D35d pointer + tag) in the seed.

## 5. Acceptance gates

- [ ] `agent_prompt_list` / `agent_prompt_get` registered as MCP tools (exported in `core/server/tools/__init__.py`, registered in `backend/admin_exec/__init__.py`)
- [ ] `_build_agent_prompt_toc` (`project.py:1821-1842`) and `_get_agent_prompt_toc_updated_at` (`project.py:1421-1442`) read from `agent_pattern` table, not the wiki page
- [ ] TOC write/read code deleted from both `backend/admin_exec/wiki.py` and `core/server/tools/agent_prompts.py`; `_COMPOSES_SECTION_RE`/`_parse_composes`/`_strip_composes_section` deleted from `dispatch_helper.py`
- [ ] `StorageEngine.increment_prompt_usage` + `get_prompt_usage_counts` + `_prompt_usage` memory-row path deleted; `uses` is a SQL integer (D40)
- [ ] `agent-prompt-toc` wiki page marked superseded (pointer content + `superseded-by-ledger` tag), slug preserved (D35d)
- [ ] seed (D35a) seeded from body PAGES, idempotent on `name` PK, re-runnable; verification gate (D35c) green
- [ ] `check_page_row_desync` (`invariants_cross_engine.py:392`) is a LIVE `content_hash` comparison for `agent_pattern`/`agent_discipline`, not `unavailable/spine_not_shipped` (§14.3 hard requirement)
- [ ] core version bumped (Car I touches `core/`) AND backend version bumped (Car I touches `backend/`) per WORKFLOW RULE; `scripts/check_versions.py` green; `scripts/check_backend_bump.py` green
- [ ] pre-commit green (ruff, import-linter, I32, I33 per §7 gate line)
- [ ] tests pass; S6 discovery tests + usage-counter tests flipped and green
- [ ] `agent_pattern`/`agent_discipline`/`agent_pattern_composes` carry NO `project_id` (reach-global, D3; §16.11 confirms)

## 6. Sequencing

- MUST merge after: **B** (backend ops + cache — Car I's backend admin ops + PTC reads depend on B's backend/cache layer) and transitively **A** (`_LedgerMixin` + Alembic chain + `002_ledger_tables`) and **A0** (`003_project_registry` — Car I's revision chains after it).
- Parallel with: D/E/F/G (independent table families). §7 line 603: "D/E ∥ F/G ∥ I after B."
- Waits on this one: **K** (nightly archive sweep — policy-dispatched; needs `agent_pattern.status` to drive archive disposition) and the cross-engine invariant arm once the table appears.

## 7. ADRs / decisions

- **D2** — three tables (task/adr/agent_prompt), no generic `record` table; §14.1 further splits agent_prompt into `agent_pattern`/`agent_discipline`/`agent_pattern_composes`.
- **D3** (shape half, RETIRED by §14.1) — single `agent_prompt` table with `kind` enum → THREE tables; contract is `always_applied=TRUE` discipline. Reach-global = absence of `project_id` on these tables.
- **D22** — `agent_prompt` → `recall_disposition` exclude, unconditional. Body pages stay excluded from recall after the table ships.
- **D24** — discovery is `agent_prompt_list` + `agent_prompt_get`, NOT recall; no search for now (deferred to a future ADR superseding ADR-0007).
- **D26** — `agent_prompt` mutability = free (edits constant; `adr` is locked).
- **D40** — `uses` is a plain SQL integer; `agent_prompt_list` returns `uses` and sorts DESC by default. No dedicated reader function.
- **D35a** — seed is a one-shot admin op, seeded from PAGES not the index.
- **D35c** — seed ships with its verification gate in the same car.
- **D35d** — old `agent-prompt-toc` page is KEPT-IGNORED one cycle: content → one-line pointer, `superseded-by-ledger` tag, slug preserved; hard-delete only after the gate holds a cycle. "Delete TOC machinery" = delete the CODE, mark the PAGE — do NOT hard-delete the page at cutover.

## 8. Out of scope

- Search over agent prompts (D24 defers; needs a new ADR superseding ADR-0007).
- Hard-deleting the `agent-prompt-toc` wiki page (D35d keeps it one cycle as the rollback path).
- Per-project `project_id` on `agent_pattern`/`agent_discipline` (reach-global, D3; §16.11 line 1381).
- Cross-project `project=` param on `agent_prompt_list`/`agent_prompt_get` — that is Car M's scope (§16.11); the tools accept `directory` for contract symmetry only.
- The `agent_pattern_model`/`client` tables (§3.3, task 0094) are included here as the natural home, but if the build splits them into a follow-up car the `agent_pattern` FK dependency means they cannot land before Car I's `agent_pattern` table. [VERIFY: whether the user wants a separate car for §3.3 — no §7 row names it.]
- Retiring the `agent-prompt` wiki body pages (D4: bodies are the versioned artifact; they stay in SurrealDB with versioning + embeddings intact).

## 9. Risks / open questions

- [VERIFY: exact revision id of Car A0's `003_project_registry` — not yet shipped; Car I's `down_revision` depends on it.]
- [VERIFY: `agent_pattern_model` + `client` (§3.3, task 0094) ownership — no §7 car names them; included here by schema-family proximity. Confirm with user before build.]
- [VERIFY: the global anchor memory (id 531516, `"see wiki [[agent-prompt-toc]]"`) references the soon-superseded TOC slug. Car I should re-anchor its content to reference `agent_prompt_list` instead. Confirm whether to update-in-place or delete+re-anchor — the anchor is `is_protected` (anchor:agent-prompt-library).]
- [VERIFY: `scripts/check_ledger_chokepoint.py` does not exist yet (Car A scope). If Car A ships before Car I, Car I's new backend reads of `agent_pattern` must be added to its allowlist.]
- Plan §7's `_build_agent_prompt_toc (project.py:1898-1921)` line range is STALE — actual is `yadgar/core/server/tools/project.py:1821-1842`. The function exists and is correct in substance; only the coordinate drifted.
- Plan §5's deletion coordinates are STALE across the board (`wiki.py:1067/1085/1075/1101/1116`, `agent_prompts.py:36/39`, `wiki.py:52-53`) — actuals verified and cited in §2.
- `discipline_save` coordinate: plan §4/§14.3 cite `agent_prompts.py:373`; actual def is `:366` (373 is inside the body).
- Two-engine atomicity (§4.1): a `agent_prompt_save` that writes the ledger row (MariaDB) AND upserts the body wiki page (SurrealDB) is NOT atomic. Order row-last (page first, row second) so a crash leaves an orphan page, not an orphan row — mirror of Car G's ordering rule. `check_page_row_desync` is the detection arm.
