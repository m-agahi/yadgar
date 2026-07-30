# Plan: `_LedgerMixin` spine — dedicated tables for `task`, `adr`, `agent_prompt`

**Date:** 2026-07-29 (rev 2026-07-30 — extended to ADR + agent-prompt, tenancy lookahead)
**Tasks:** #0047 (task list), #0080 (context budget)
**ADR:** ADR-0181 (superseded by ADR-0182 — this plan reflects ADR-0182)
**Status:** design locked, not started

---

## 0. TL;DR

Three structured datasets are stored as markdown pages and re-parsed on every read.
Move their **indexes** to tables; leave their **bodies** as wiki pages.

| | Today | After |
|---|---|---|
| task list | one 68,870-char page, read whole to filter | table query, capped titles |
| ADR index | 29,612-char page, regex-parsed per `adr_list` **and** per `adr_add` | table query |
| agent-prompt TOC | markdown table, scan-replaced; `uses:N` stamped into prose | table columns |
| bodies (268 ADR pages + prompts) | — | **untouched, still wiki pages** |
| MCP surface | — | `adr_*` and `agent_prompt_save` **signatures unchanged** |

One mixin, three tables, one train.

---

## 1. Problem

### 1.1 Three markdown tables

**Task list** — `yadgar-task-list` is **68,870 chars** (~17k tokens) as of 2026-07-30
(an earlier revision of this plan said ~14k, understating it 5×). Read in full at every
session start and stop-hook checkpoint; observed taking a restore from 8% to 18% context.

**ADR index** — `yadgar-adr-index` is **29,612 chars**, and is already a relational row
round-tripped through markdown:

```python
# adr_index.py:21 — seven columns out, seven back in via _render_index_row
_INDEX_ROW_RE = re.compile(r"^\|\s*(ADR-\d{4})\s*\|\s*(?P<status>[^|]*?)\s*\|...")
```

Because the index write can lag the page write, `_next_adr_id` cannot trust it and scans
committed page slugs as a **second id source** (`_committed_page_max_id`), the whole
sequence serialized under `_adr_log_lock`. All of that exists to compensate for the index
not being a table.

**Agent-prompt TOC** — `_TOC_ROW_RE` parses `- \`<pattern>\` → <purpose>` rows;
`_upsert_toc_row` scan-replaces them. Worse, `increment_prompt_usage` maintains the
`uses: N` counter in **two** places — a global `_prompt_usage` memory row *and* the TOC
page — throttled to `count == 1 or count % 10 == 0` explicitly because *"the TOC page
(wiki-versioned) does not churn a version per prelude call."* A throttle to avoid version
churn while incrementing an integer.

### 1.2 Honest framing of why a table

An earlier draft claimed a markdown page "has no query surface." **That is false** — the
daemon could parse server-side and return a projection. The real case:

- maintaining a regex parser over prose as a query engine (the `_INDEX_ROW_RE` situation
  being deleted here)
- no indexes; full read + parse per call regardless of filter
- format drift silently breaks parsing
- whole-page read-modify-write is not atomic

### 1.3 ADR lifecycle is dead

Of ~181 yadgar ADRs, **10** carry `adr-status:superseded`. ~94% sit at `accepted` forever
and every `revisit_trigger` field goes unevaluated. The corpus problem is not accumulation
needing pruning — it is that ADRs never *leave* `accepted`, so staleness is invisible.

### 1.4 Two live defects found while designing this

- **Superseded ADR bodies are recallable today.** `WikiPolicy` resolves
  `recall_disposition` by `page_type` only; status lives in a tag, and `recall(tags=[...])`
  filters *in*, never out. Rides the migration (§7 Car F).
- **The tag-override defeats exclusion.** `providers/wiki.py:102` reads
  `if not self._tags and ...recall_disposition == "exclude"` — any tagged recall skips the
  exclusion. So `recall(type="wiki", tags=["adr"])` — the documented ADR lookup, printed by
  `project_brief` — would still surface superseded bodies after a retype. **This gates Car F.**
- **`downweight` is documented but has zero implementation.** Only `== "exclude"` is ever
  tested. **This gates the task cars.**

---

## 2. Decisions

### 2.1 Structure

| # | Decision | Rationale |
|---|---|---|
| D1 | **Per-entity tools over a shared `_LedgerMixin`** — not a generic `record_query`/`record_write` | Makes the ADR migration invisible: `adr_add`/`adr_list`/`adr_get` keep their signatures, so no caller, prompt or doc changes. The reuse wanted is *implementation* reuse. A generic filter param reinvents mini-SQL; a generic write tool against arbitrary tables collides with the standing no-direct-DB rule. |
| D2 | **Three tables** — `task`, `adr`, `agent_prompt`. No generic `record` table. | Different status enums, required fields, indexes. A SCHEMALESS `kind` discriminator gives a field union where half the columns are always null. |
| D3 | **One mixin, not two.** `agent_prompt` gets a `number` nothing reads. | Uniform beats exceptional. Consequence: a number carries D6/D7 with it, so pruning the 52 patterns in task #0015 becomes *archive*, not delete. |
| D4 | **Bodies stay wiki pages. No `body` column.** | Bodies inherit `wiki_page_version` — `wiki_history`/`wiki_diff`/`wiki_restore` all work. A row body would have no undo unless we reimplement versioning badly. Given ADR-0090 (surrealkv corruption, still open) and plans lost to corruption historically, that matters. Also: a 64K body in a row is fetched by any careless `SELECT *`; behind a slug it is unfetchable by accident. |
| D5 | **Row owns ALL metadata; page owns ONLY prose.** New page schema version. | Otherwise split authority returns — today `_build_adr_body` renders status/date *into* the page while the index row carries the same fact, and `_flip_superseded_target` writes a status *tag* best-effort with a bare `except: return`. Two writers, one truth. |

### 2.2 Identity

| # | Decision | Rationale |
|---|---|---|
| D6 | **Integer `number`, assigned by `DEFINE SEQUENCE`. No application-level read-then-write.** | `INSERT` and let the DB assign — not seat reservation. SurrealDB sequences are distributed, monotonic, batch-reserved per node, lock-free on the hot path. Kills `_adr_log_lock` *and* `_committed_page_max_id` for the right reason. **Verify `DEFINE SEQUENCE` exists in v3.1.5** (`Dockerfile.backend:20`) before Car A commits. |
| D7 | **Never reused. Archive, never hard-delete.** | External references (`docs/plans/*` filenames, PR titles, `(#93)` in commits) must not silently retarget. Archive-never-delete is the precondition for permanence, not a separate preference. |
| D8 | **Composite id `(origin, number)`.** Display bare when local, prefixed when foreign (`0094` vs `alice/0231`). | Solves upstream/local collision without coordination: your task and mine are `(alice, 0094)` and `(max, 0094)`, distinct by construction. Works offline, sync is idempotent, and **no id ever changes** — which upstream-assigns-on-sync would violate. Sequences stay per-origin. |
| D9 | **Gaps are correct.** Interleaved writers and failed transactions both leave holes. | A number is an identifier, not a count. A hole is strictly better than a reuse. |
| D10 | **Decimal rendering, 4-digit minimum width, natural overflow.** | Encoding is a *rendering* decision — the column is an integer either way, so hex/base32 can be adopted later with no migration. It is the only decision here that is genuinely cheap to defer. Per-origin sequences mean no counter accumulates everyone's writes (~9 years at current rate). **No new code may regex `\d{4}`** — parse the integer, format on render. |
| D11 | **Harness reconcile keys on the `[NNNN]` title prefix**; an unmatched title **warns**, never silently creates. | Claude Code task ids are per-session handles regenerated by the SessionStart restore-nudge — not identity, must not be persisted. Silent-create on an unmatched title is the corruption path. |
| D12 | **Title capped at 200 chars, reject-on-write.** | Longest current subject ~148, so 200 rejects nothing existing. Silent truncation destroys information exactly when someone is being sloppy. |

### 2.3 Project identity

| # | Decision | Rationale |
|---|---|---|
| D13 | **`project_id` = `<owner>/<repo>` parsed from the git remote, host excluded.** Normalize away scheme, SSH alias, and `.git`. | `git@github-personal:m-agahi/yadgar.git` and `https://github.com/m-agahi/yadgar.git` must yield the same key — this repo uses SSH aliases (`github-personal`, `codeberg-agent`). Excluding the host makes a codeberg→github move a **no-op**, which is the incident that motivated this. Fixes the `basename()` collision where two repos named `infrastructure` in different orgs share ADR slugs. |
| D14 | **Non-git directories are `local/<basename>`, never sync, backup-only.** | A non-git dir has no stable shared identity; don't pretend. Permanently `owner_kind=user`. Accepted edge case: an org literally named `local` collides — blast radius is one machine, so it is documented, not engineered around. |
| D15 | **No `.yadgar/project.json`.** The override lives in the runtime config store: `config_set("project.key_override", ..., scope="project")`. | Avoids repo burden, fork inheritance, and accidental edits. ADR-0163's store is already directory-scoped and DB-backed — the machinery exists. |
| D16 | **A derived key is STORED on the row at write time, never recomputed at read time.** | This is what actually broke the task list on the codeberg→github move: the default branch was resolved live from `refs/remotes/origin/HEAD` (`server_helpers.py:326`, falling back to `'master'`), the remote moved, and the resolution key silently changed under existing rows. Applies to `project_id` and `branch` equally. |

### 2.4 Tenancy (design now, build later)

| # | Decision | Rationale |
|---|---|---|
| D17 | **Two orthogonal axes, four columns** — `owner_kind` (user\|team\|org) · `owner_id` · `reach` (project\|global) · `project_id` | One enum cannot express the six real cells (user/team/org × project/global). Today is the top row with `owner_id=null`; nothing changes behaviourally. Retrofitting these columns onto a populated table is a migration on every row — adding them now is free and inert. |
| D18 | **Sync selectivity IS the owner axis.** `user` → nowhere (opt-in backup only) · `team` → team · `org` → org | "Should this reach my team?" and "who owns this?" are the same question. No per-record sync flags, no rules engine. Personal backup is a separate independent `backup` flag. |
| D19 | **Explicit `PERMISSIONS` on every `DEFINE TABLE`.** Never inherited defaults. | SurrealDB advisory GHSA-x5fr-7hhj-34j3: default table permissions were FULL. Cheap at define time, expensive to retrofit. Row-level security via `PERMISSIONS` + `$auth` is native, so AAA need not be hand-rolled in Python. |
| D20 | **One choke point: every row access goes through `_LedgerMixin`. Enforced by lint.** | The single highest-value thing here. If it holds, adding a tenant filter later is one change in one place; if callers hand-roll SurrealQL it is unfixable without auditing every site. import-linter + AST guards already exist. |

### 2.5 Retrieval and mutability

| # | Decision | Rationale |
|---|---|---|
| D21 | **`gate_mode="identity"` for all three types.** | Deterministic slugs, legitimately self-similar content (two ADRs on one subsystem *should* look alike). Deletes `adr_add`'s `force=True` sim-gate workaround. |
| D22 | **`recall_disposition` becomes status-driven**: `accepted`/`open` → include · `superseded`/`rejected`/`deprecated` → **exclude** · `task` → downweight · `agent_prompt` → exclude, unconditional. | A rejected ADR's body describing an approach you deliberately turned down is the most dangerous thing in the corpus to hand an agent as guidance. |
| D23 | **Supersede = retype `adr` → `adr_superseded`, atomic with the status flip.** Never delete the page, never NULL `body_slug`. | Semantic recall must never surface it; explicit `adr_get("ADR-0010")` must still work — "what did we used to think, and why did we change our mind?" is legitimate. Deletion cannot tell those apart and destroys the reasoning that explains the *current* decision. Doing it at supersede time rather than nightly leaves no window; consolidation is the backstop for drift. |
| D24 | **Agent-prompt discovery is `agent_prompt_list` + `agent_prompt_get`, not recall.** No `agent_prompt_search` for now. | A TOC row carries name + purpose + a directly-retrievable slug: retrieval is a lookup, not a ranking problem. 61 patterns × name+purpose ≈ 5 KB, and task #0015 wants to *prune* to fewer. This is what lets `agent_prompt` be excluded from recall **unconditionally**, which is what kills the tag-override. If listing proves insufficient, add search then — with evidence, and with the ADR superseding ADR-0007 that it deserves. |
| D25 | **`mutability` as `WikiPolicy` field #6** (`free\|append_only\|derived\|locked`) + a nullable per-page `mutability_override` + one power-gated `wiki_set_mutability(slug, value, reason)`, logged. | A lock with no unlock is a trap, and a per-`page_type` policy alone can only be unlocked by editing `policy.py` and redeploying — which unlocks the whole type, not the one page. The escape hatch forces a per-page dimension. Unlock must be a deliberate, named, logged act, never a flag on the ordinary write path. |
| D26 | **Per type:** `adr`/`adr_superseded` → **locked** · `task` → free · `agent_prompt` → free · rollups → **derived** | Tasks and prompts are edited constantly — that is the point of a versioned prompt library. Decisions are not. **`locked` blocks agent/tool edits, not sanctioned server-side lifecycle transitions** — otherwise the supersede retype deadlocks against its own guard. |

**What mutability is really for:** not dangling-pointer prevention (that is a side benefit) but
**stopping the well-intentioned repair**. The runaway instance that deletes an ADR page to
"resolve" a dangling reference, or rewrites a derived rollup because it looks stale, is the
actor this guards against. That argues for `locked` being the default for anything generated
or historical, not the exception.

### 2.6 ADR consolidation

| # | Decision |
|---|---|
| D27 | **`tier: binding \| historical`**, `adr_list` defaults to `binding`. Cheapest lever against §1.3 — one field, reversible. |
| D28 | **`subsystem`** — explicit, never inferred from the title. |
| D29 | **Derived per-subsystem rollup pages** — "decisions in force for `vacuum`". Generated on write, never authored. Replaces one big index write with one small rollup write. |

---

## 3. Schema

SCHEMALESS, fields via idempotent `DEFINE FIELD IF NOT EXISTS`, **explicit `PERMISSIONS`** (D19).

### 3.1 Spine (all three)

```
id            record id
origin        instance id — composite key part            (D8)
number        integer, DEFINE SEQUENCE per origin         (D6)
owner_kind    user | team | org                           (D17)
owner_id      null = local single-tenant today
reach         project | global
project_id    "<owner>/<repo>" | "local/<basename>"       (D13/D14)
title         <= 200, reject on overflow                  (D12)
status        per-entity enum
body_slug     wiki page — ALWAYS present in responses     (D4)
created_at / modified_at
```

### 3.2 Per-entity

```
task           { active_form · blocked_by[] · blocks[] }
               status: pending | in_progress | completed | archived

adr            { date · subsystem · tier · supersedes[] · superseded_by[] }
               status: open | accepted | superseded | rejected | deprecated | archived
               body_slug MANDATORY non-null

agent_prompt   { kind: pattern|discipline|contract · purpose · composes[] · uses }
               status: active | archived
               reach always global; number assigned and ignored (D3)
```

`agent_prompt.composes[]` replaces the `## Composes` section currently parsed out of prose
by `_COMPOSES_SECTION_RE` + `_WIKI_LINK_RE` and stripped back out before injection.
`agent_prompt.uses` replaces the two-location counter and its `%10` throttle.

---

## 4. Tools (D1)

```
task_list(directory, status=None, blocked=None)     NEW — capped metadata only
task_get(directory, number)                         NEW
task_write(directory, ...)                          NEW

adr_add / adr_list / adr_get                        EXISTING — signatures UNCHANGED
                                                    (adr_list gains optional tier/subsystem filters — additive)

agent_prompt_list(kind=None, composes=None)         NEW — TOC replacement
agent_prompt_get(name, kind="pattern")              NEW
agent_prompt_save(...)                              EXISTING — unchanged
agent_dispatch_prelude(...)                         EXISTING — unchanged, reads the table
seed_agent_prompts(...)                             EXISTING — unchanged

wiki_set_mutability(slug, value, reason)            NEW — power=True, logged (D25)
```

### 4.1 Batch-write primitive — spine level

`write_batch(table, new_row, updates[])`, atomic. Required because every entity mutates
sibling rows in one logical write:

- **ADR supersede** — flips N targets' `status` and appends to `superseded_by`; a half-applied
  supersede leaves a row claiming `accepted` while its superseder claims to have superseded it
- **task** — `blocked_by`/`blocks` are bidirectional
- **agent_prompt** — `composes[]` back-references

---

## 5. Architecture fit

All cross-layer communication via `_forward_admin` (HTTP) — no import-linter violations.

| Layer | What | Files |
|---|---|---|
| `_shared/storage/` | migration + `_LedgerMixin` (CRUD, sequence, batch write) — mixin #19 on `StorageEngine` | `migrations.py`, `ledger_store.py` (new) |
| `_shared/wiki/` | `mutability` policy field + enforcement | `policy.py`, `store.py` |
| `backend/admin_exec/` | `*_list` / `*_write` ops | `ledger.py` (new) |
| `backend/retrieval/` | 3a tag-override fix · 3b `downweight` implementation | `providers/wiki.py`, fusion |
| `core/server/tools/` | task + agent_prompt tools; ADR tools re-pointed | `tasks.py` (new), `adr*.py`, `agent_prompts.py` |
| `core/hooks/` | SessionStart nudge + stop-hook step 5 read the table | `session-start-context.py`, `stop_checkpoint_prompt.md` |
| consolidation | policy-dispatched archive sweep | consolidation cycle |
| lint | choke-point guard (D20) | `scripts/` + import-linter |
| `CAPABILITY_REGISTRY.md` | new entries (I32) | edit |

### 5.1 Deletions — named deliverables

The migration only pays if the compensating machinery goes:

`_index_max_id` · `_committed_page_max_id` · `_next_adr_id` dual scan · `_next_adr_id_from_index` ·
`_adr_log_lock` · `_INDEX_ROW_RE` · `parse_index_rows` · `_render_index_row` ·
`_build_index_content` · `_assemble_index_rows` · the `{project}-adr-index` page ·
`_flip_superseded_target`'s page-tag write · `adr_add`'s `force=True` ·
`_TOC_ROW_RE` · `_upsert_toc_row` · `_set_toc_row_count` · the `agent-prompt-toc` page ·
the `_prompt_usage` memory row · the `%10` throttle · `_COMPOSES_SECTION_RE` ·
`_parse_composes` · `_strip_composes_section` · the `providers/wiki.py` tag-override

Keeping any of them "for safety" preserves exactly the dual-authority complexity this removes.

---

## 6. Migration

1. Tables + migration + sequences + PERMISSIONS.
2. Backend ops + cache.
3. **3a + 3b land before any seed** (they gate correctness, §1.4).
4. Task tools; seed from `{project}-task-list`; rewire SessionStart + stop-hook.
5. ADR tools re-pointed; seed from `{project}-adr-index` (use `parse_index_rows`, then delete it).
6. **Retype the 10 existing superseded pages** — acceptance: after seed, zero `page_type='adr'`
   pages carry an `adr-status:superseded` tag. Not automatic; the seed reads index rows and does
   not touch page metadata. If skipped, the live defect survives the migration and closes for nothing.
7. agent_prompt table + tools; delete TOC machinery.
8. Rollups; `tier`/`subsystem`.
9. Nightly archive sweep, policy-dispatched.
10. **Verification gate** — old pages stay in place, unread, until row counts and spot-checks
    match. Only then delete `{project}-task-list`, `{project}-adr-index`, `agent-prompt-toc`.

Rollback before step 10 is: stop reading the tables. The old pages are still there.

---

## 7. Cars

| Car | Scope | Depends on |
|---|---|---|
| A | `_LedgerMixin` + migration + `DEFINE SEQUENCE` + explicit PERMISSIONS + tenancy columns + choke-point lint | — |
| B | backend ops + cache integration | A |
| C | **3a** tag-override matches the type's own opt-in tag · **3b** implement `downweight` | — |
| D | task tools (`task_list`/`task_get`/`task_write`) | B, C |
| E | task seed + SessionStart/stop-hook rewire | D |
| F | ADR tools re-pointed — signatures unchanged | B, C |
| G | ADR seed + retype the 10 superseded + delete parser/serializer/lock | F |
| H | `tier` + `subsystem` + derived rollups | G |
| I | `agent_prompt` table + `list`/`get` + delete TOC machinery | B |
| J | `mutability` policy field + per-page override + `wiki_set_mutability` | A |
| K | nightly archive sweep, policy-dispatched | E, G, I |

D/E ∥ F/G ∥ I after B. **C gates D and F** — without it the retype is cosmetic and
`downweight` is a no-op config value.

TDD throughout, RED-verified per car. Gates: ruff, import-linter, I32 capability coverage,
I33 observe coverage, `check_versions`.

---

## 8. Expected impact

| Metric | Today | After |
|---|---|---|
| Session-start task rehydration | ~24k tok | ~3k tok |
| Stop-hook checkpoint read | ~4k tok | ~200-400 tok |
| `adr_list` | 29,612-char read + regex parse | indexed query |
| `adr_add` | index read + page-slug scan + lock | one `INSERT`, sequence-assigned |
| prompt usage counter | 2 stores + throttle + page version churn | one `UPDATE`, exact |
| "what governs `vacuum`?" | scan ~181 entries | one rollup page |
| body pages | 268 ADR + 61 prompt | unchanged, versioning intact |
| Superseded ADR in recall | **yes (live defect)** | no |
| Index drift risk | present | none — the index *is* the query |
| Number collision across instances | n/a | none (composite id) |

## 9. Out of scope — filed separately

- **#0095 project identity** — near-term, not SaaS-later: a live bug that already cost a recovery session.
- **#0096 encryption posture** — decides the trust boundary; blocks sync design.
- **#0097 AAA / sync design** — two-key hierarchy, transport (outbox vs `CHANGEFEED` vs CRDT).
- **#0043** anchor/memorize metadata sprawl — adjacent, stays there.
- **#0015** agent-prompt prune — becomes *archive* under D3/D7.

## 10. Open build questions

- **Does `DEFINE SEQUENCE` exist in SurrealDB v3.1.5?** Blocks D6's implementation shape.
- **Rollup regeneration trigger** — on write (fresh, one small page write) vs nightly (cheaper, stale between runs).
- **`subsystem` vocabulary** — free-form drifts (`vacuum` / `Vacuum` / `db-vacuum`); a controlled list needs a home.
