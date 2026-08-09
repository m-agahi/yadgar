# Car H — tier + subsystem + rollups

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: shipped (Car H of 0047 spine train — code on car/H-tier-subsystem)
> Depends on: G
> Lifecycle: ADR-0081/0082 — archived as the first commit of car/H-tier-subsystem.
>
> §10 decisions made at build time (now binding):
>   Q1 — rollup regeneration trigger: ON-WRITE (fired from `adr_add` post-commit step).
>     Rationale: keeps §8's "one rollup page" promise honest at read time; the
>     extra wiki write is bounded (~195 ADRs, single-page writes per subsystem).
>     Car K (nightly archive sweep) is unaffected — on-write does NOT consume
>     its sweep-dispatch input. If a stale rollup is observed in future, the
>     nightly sweep can be retro-fitted without code change in Car H.
>   Q2 — subsystem vocabulary home: free-form `VARCHAR(128)` + on-write
>     normalizer (`.lower().strip()`, empty → None). D28 satisfied (explicit,
>     never inferred from title); the seed parses `## Subsystem` headers when
>     present and assigns `unknown` otherwise. No new table, no FK, no
>     runtime_config knob. The normalizer keeps `vacuum`/`Vacuum`/`db-vacuum`
>     from silently drifting apart in the row-level filter; downstream rollup
>     pages are still keyed by the author-supplied lowercase form.

## 1. Scope

Wire up the `tier` and `subsystem` columns on the `adr` table and build the derived per-subsystem rollup pages — the ADR-consolidation axis decided in §2.6 (D27/D28/D29) and deferred to this car by §6.1 build-order step 8 ("Rollups; `tier`/`subsystem`") and §10.

Car A's `002_ledger_tables` creates the `adr` table **with** `tier VARCHAR(32) NULL` and `subsystem VARCHAR(128) NULL` columns per §3.5 (lines 408-409), but leaves them inert (NULL). Car H is what makes them load-bearing:

1. Decide the two §10 open questions that were explicitly deferred here: the **rollup regeneration trigger** (on-write vs nightly) and the **`subsystem` vocabulary home** (free-form drifts today — `vacuum`/`Vacuum`/`db-vacuum`).
2. Populate `tier`/`subsystem` on existing ~195 ADR rows in a one-shot seed (same D35a one-shot-admin-op shape as Car G's ADR seed).
3. Extend `adr_add`/`adr_list` to accept and filter on `tier` (D27: `binding|historical`, `adr_list` defaults to `binding`).
4. Build the per-subsystem rollup pages (D29): derived, generated on the trigger decided in (1), replacing the one big index read with one small rollup read. Mutability = derived/locked per D26.
5. Re-point the ADR read surface (`_build_adr_log`, `_assemble_index_rows`) at rollup-backed queries where a rollup is the cheaper source — Car G already re-pointed them at `list_adr_rows`; Car H layers the rollup shortcut on top.

This car does NOT introduce a new alembic revision for columns — the columns ship in Car A's `002`. [VERIFY: when Car A lands, confirm `002_ledger_tables.py` creates `adr` with `tier`/`subsystem` columns per §3.5; if Car A ships without them, Car H adds an `op.add_column` revision `00X_adr_tier_subsystem` before the seed.]

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/storage/sql/migrations/versions/0001_config_table.py` | reference only — confirms alembic chain shape; Car H adds NO revision here unless 002 lacks the columns | verified: only revision present today |
| `yadgar/_shared/storage/sql/migrations/versions/002_ledger_tables.py` | [VERIFY: does not exist yet — Car A ships it. Car H depends on `adr` carrying `tier`/`subsystem` columns per §3.5 lines 408-409] | not yet present |
| `yadgar/core/server/tools/adr.py:142-155` | extend `adr_add` signature with `tier: str \| None = None`, `subsystem: str \| None = None`; stamp onto the row via `_LedgerMixin` (built Car A) | verified: current signature has neither param |
| `yadgar/core/server/tools/adr.py:318-319` | extend `adr_list` with `tier: str \| None = "binding"` filter (D27 default); pass through to `list_adr_rows` | verified: current sig is `(directory, status, limit, offset)` |
| `yadgar/core/server/tools/project.py:1786-1818` | `_build_adr_log` — Car G re-points to `list_adr_rows`; Car H may switch the "recent ADRs" read to a rollup-backed shortcut if a rollup is the cheaper source for that subsystem | verified: currently reads canonical index via `parse_index_rows` |
| `yadgar/core/server/tools/project.py:1287-1288` | `_get_adr_log_updated_at` — second hardcoded-slug site Car G re-points; Car H bumps the timestamp on rollup regeneration | verified: exists |
| `yadgar/core/server/tools/adr_render.py:167-181` | `_assemble_index_rows` — Car G re-points from `parse_index_rows` to `list_adr_rows`; Car H may source per-subsystem renders from the rollup page | verified: imports `parse_index_rows` at :179 |
| `yadgar/core/server/tools/wiki.py:29` | add rollup `page_type` to `CANONICAL_PAGE_TYPES` so rollup pages are writable server-side and excluded from recall (D22-style) | verified: `frozenset({"task_list", "adr"})` today |
| `yadgar/backend/admin_exec/invariants_cross_engine.py:117` | `SPINE_LEDGER_TABLES = ("adr", "agent_discipline", "agent_pattern")` — Car H asserts the `adr` rows' `tier`/`subsystem` agree with the rollup pages (cross-engine desync check, sibling to `check_page_row_desync`) | verified: exists, today reports `REASON_SPINE_NOT_SHIPPED` (:74) |
| `yadgar/backend/admin_exec/seed.py` or new `seed_adr_tier_subsystem.py` | one-shot seed op (D35a shape) — backfills `tier`/`subsystem` onto existing rows from the body pages | [VERIFY: seed module path — `seed.py` exists but the tier/subsystem backfill entry point is new] |
| rollup generator (NEW, backend) | `_should_regenerate_rollup` + per-subsystem page writer — D29 regeneration; lives backend (server-side, derived per D26) | [VERIFY: exact module — `_should_regenerate_rollup` was moved into `adr.py` per §13 Fix 4 line 842, but that was PR #32 work; current repo has no such symbol — grep clean] |

## 3. Functions / symbols

New (backend, rollup generation):
- `_should_regenerate_rollup(subsystem: str, project_id: str) -> bool` — decides whether the rollup for `(project_id, subsystem)` is stale. [VERIFY: trigger policy decided per §10 Q1 — on-write or nightly; see §9]
- `_regenerate_subsystem_rollup(storage, project_id: str, subsystem: str) -> dict` — SELECT ADR rows for `(project_id, subsystem)`, render the rollup page body, write via the canonical wiki path with `page_type` = the new rollup type. Derived/locked (D26) — agent edits rejected.
- `list_adr_rows(...)` extension — Car A's `_LedgerMixin` reader gains optional `tier`/`subsystem` filter params (indexed scan on `(project_id, status)` per §3.5 note ¹; `tier`/`subsystem` are non-indexed NULLable columns — filter is a table scan unless an index is added). [VERIFY: whether Car H adds an index on `(project_id, subsystem)` — ~195 rows, probably not worth it]

New (core tool surface):
- `adr_add(..., tier: str | None = None, subsystem: str | None = None) -> dict` — current sig is 11 params (`adr.py:143-155`); adds two optional, both validated against the controlled vocabulary once §10 Q2 is decided.
- `adr_list(directory, status=None, tier="binding", limit=50, offset=0) -> dict` — current sig (`adr.py:319`) gains `tier` with default `"binding"` per D27. `"binding"` excludes `historical` (superseded/rejected/deprecated); `tier=None` returns all.

[VERIFY: subsystem vocabulary home — §10 Q2. Candidates: (a) a `runtime_config` knob (Car A's table) holding a JSON list, (b) a new `subsystem` lookup table, (c) free-form `VARCHAR(128)` with a normaliser. D28 says "explicit, never inferred from the title" — the value must come from the author, but the *controlled list* needs a home. Decision belongs in §9 / build time.]

## 4. Build steps (TDD)

1. **RED** — `test_adr_list_tier_filter_defaults_to_binding`: call `adr_list` with a mix of `tier='binding'` and `tier='historical'` rows; assert only `binding` returned by default; assert `tier=None` returns both. Requires Car A/G landed (ledger rows exist). Mock the `_LedgerMixin` reader returning rows with `tier` set.
2. **GREEN** — add `tier` param to `adr_list`; thread to `list_adr_rows`; default `"binding"`.
3. **RED** — `test_adr_add_stamps_tier_subsystem`: `adr_add(..., tier="binding", subsystem="storage")` creates a row with both columns set; asserts the row returned by `adr_get` carries them.
4. **GREEN** — extend `adr_add` signature; pass `tier`/`subsystem` to `create_adr_row`.
5. **RED** — `test_subsystem_rollup_regenerated_on_trigger`: after the decided trigger fires (on-write per §10 Q1 option A, or nightly per option B), assert a rollup page exists for that subsystem with the right ADR IDs, `page_type` = rollup type, and is excluded from `recall` (D22-style disposition). Assert agent edit of the rollup page is rejected (D26 derived/locked).
6. **GREEN** — implement `_regenerate_subsystem_rollup` + the trigger hook; add rollup `page_type` to `CANONICAL_PAGE_TYPES`; add recall exclusion for the new type.
7. **RED** — `test_rollup_row_page_desync_check`: flip a row's `subsystem` without regenerating the rollup → `check_invariants` (cross-engine, `invariants_cross_engine.py:117` region) reports desync. Green when rollup regenerated.
8. **GREEN** — wire the rollup↔row agreement into the cross-engine invariant (sibling to `check_page_row_desync`).
9. **REFACTOR** — pull the rollup render into the `adr_render.py` path so `_assemble_index_rows` can serve a single subsystem from its rollup when one is fresher than re-scanning rows.

## 5. Acceptance gates

- [ ] `adr_list` defaults to `tier="binding"` and the default is covered by a test (D27)
- [ ] `adr_add` accepts and stamps `tier`/`subsystem`; `adr_get` returns them
- [ ] per-subsystem rollup page generated on the decided trigger (D29); agent edits rejected (D26 derived)
- [ ] rollup pages excluded from `recall` (new `page_type` in `CANONICAL_PAGE_TYPES` + D22-style disposition)
- [ ] "what governs `vacuum`?" answerable by reading ONE rollup page, not scanning 194 entries (§8 line 628)
- [ ] cross-engine desync check catches a row/subsystem flip not propagated to the rollup
- [ ] the two §10 open questions (rollup trigger, subsystem vocab home) have a recorded decision in the ADR log before this car merges
- [ ] core/backend version bumped per WORKFLOW RULE (new core/backend version) — backend-bump = YES (rollup generator + seed are backend; `BACKEND_BUILD_DIRS=("backend",)` per `scripts/check_backend_bump.py`). [VERIFY exact bump mechanism if unsure]
- [ ] pre-commit green
- [ ] tests pass

## 6. Sequencing

**Must merge before H:** Car G (ADR seed + retype + re-point `_build_adr_log`/`_get_adr_log_updated_at`/`_assemble_index_rows` to `list_adr_rows`). Car A (creates `002_ledger_tables` with the `adr` table incl. `tier`/`subsystem` columns) and Car A0 (`project_id` derivation, required for any per-project rollup) must also be landed via the G→H chain. Car F (ADR tools re-pointed) is a G dependency, hence transitively required.

**Waits on H:** Car K (nightly archive sweep, policy-dispatched) — depends on E, G, I per §7, but the rollup regeneration policy decided in H (§10 Q1) is the input K's sweep dispatch consumes if nightly is chosen. If on-write is chosen, K is unaffected.

## 7. ADRs / decisions

- **D26** (line 257) — `adr`/`adr_superseded` locked; `task`/`agent_prompt` free; **rollups derived**. Locked blocks agent edits, not sanctioned server-side regeneration.
- **D27** (line 267) — `tier: binding | historical`, `adr_list` defaults to `binding`. One field, reversible.
- **D28** (line 268) — `subsystem` explicit, never inferred from the title.
- **D29** (line 269) — derived per-subsystem rollup pages, generated on write. Replaces one big index write with one small rollup write.
- **D35a** (§6) — every seed step (incl. the tier/subsystem backfill) is a separate one-shot operation, not a migration step; ships with its verification gate in the same car.
- **§10** (lines 680-681) — two open questions deferred to Car H: rollup regeneration trigger (on-write vs nightly); `subsystem` vocabulary (free-form drifts; controlled list needs a home). Decide when Car H starts; record in the ADR log.

## 8. Out of scope

- Adding `tier`/`subsystem` to any entity other than `adr` (tasks and agent-prompts are free-form and global respectively; no axis there).
- Per-owner or per-project rollups beyond the subsystem dimension (D29 is per-subsystem).
- A full-text search over ADR bodies — D24 keeps agent-prompt discovery lookup-only; ADR search is a separate bet.
- The legacy-corpus portability retrofit (task 0098) — rollup pages are new pages in SurrealDB; legacy vector/graph portability is out of scope.
- Renumbering ADRs — `id` IS the number (ADR-0197); the seed preserves existing IDs.

## 9. Risks / open questions

- **[VERIFY: §10 Q1 — rollup regeneration trigger.** On-write (fresh, one small page write per ADR add/supersede) vs nightly (cheaper, stale between runs). On-write keeps the §8 "one rollup page" promise honest at read time; nightly costs nothing extra but a stale rollup is a lie with a page slug. Recommend on-write, decided at build time.]
- **[VERIFY: §10 Q2 — `subsystem` vocabulary home.** Free-form drifts (`vacuum`/`Vacuum`/`db-vacuum`). A controlled list needs a home. Candidates: (a) `runtime_config` knob with a JSON list (cheapest, re-syncable), (b) a `subsystem` lookup table (enforced FK, but a new table for ~10 values), (c) free-form `VARCHAR(128)` + a normaliser on write. D28 ("explicit, never inferred from the title") is satisfied by any of the three; the choice is the open question.]
- **[VERIFY: whether Car A's `002_ledger_tables` includes `tier`/`subsystem` columns.** §3.5 lists them on `adr`; §6.1 step 8 lists "tier/subsystem" as a discrete build step. Consistent reading: 002 creates the columns (inert NULL), Car H populates + wires them. If Car A ships 002 WITHOUT them, Car H must add an `op.add_column` revision before the seed — confirm against the landed 002.]
- **[VERIFY: rollup page_type name and recall disposition.** A new `page_type` (e.g. `adr_rollup`) must be added to `CANONICAL_PAGE_TYPES` (`wiki.py:29`) and excluded from recall like `agent_prompt` (D22). The exact type name is a build-time decision.]
- **[VERIFY: how `subsystem` is sourced for the one-shot seed.** D28 forbids inferring it from the title. Existing ~195 ADR body pages may carry a subsystem marker in prose/front-matter, or the seed may require manual assignment per row (suggest-and-confirm, like §16.9 case 3). A heuristic here produces silent wrong answers — same asymmetry as the project-key quarantine.]
- **[VERIFY: index on `(project_id, subsystem)`.** ~195 rows; a table scan is likely fine. If `adr_list(subsystem=...)` becomes hot, add the index in a later revision.]
- **Risk: on-write rollup regeneration adds a second wiki write per `adr_add`.** The body page + index row already cost two writes (Car G deletes the index-row write by going ledger-backed). Rollup regeneration re-introduces a second write. Net write count vs today is still lower; state it in the ADR.
