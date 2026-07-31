# Plan: `_LedgerMixin` spine — dedicated tables for `task`, `adr`, `agent_prompt`

**Date:** 2026-07-29 (rev 2026-07-30 — ADR + agent-prompt, tenancy lookahead, post-audit)
**Tasks:** #0047, #0080 · related #0095 #0096 #0097 #0098
**ADR:** ADR-0182 (supersedes ADR-0181)
**Status:** AUDITED (build-with-changes) — audit findings folded in below. Not started.

---

## 0. TL;DR

Three structured datasets are stored as markdown pages and re-parsed on every read.
Move their **indexes** to tables; leave their **bodies** as wiki pages.

| | Today | After |
|---|---|---|
| task list | 70,341-char page, read whole every stop-hook checkpoint | table query, capped titles |
| ADR index | 30,002-char page, regex-parsed per `adr_list` **and** per `adr_add` | table query |
| agent-prompt TOC | 9,466-char markdown table; `uses:N` stamped into prose | table columns |
| bodies (270 ADR pages + prompts) | — | **untouched, still wiki pages** |
| MCP surface | — | `adr_*` and `agent_prompt_save` **signatures unchanged** |

---

## 1. Problem

### 1.1 Three markdown tables

**Task list** — `yadgar-task-list` is **70,341 chars / 63 `## task:` sections** (was 47
sections four days earlier: unbounded growth, confirmed). `stop_checkpoint_prompt.md:115-118`
mandates a full `wiki_read` of it **every checkpoint**.

**ADR index** — `yadgar-adr-index` is **30,002 chars**, already a relational row round-tripped
through markdown (`adr_index.py:21` `_INDEX_ROW_RE`, seven columns out via `_render_index_row`).
Because the index write can lag the page write, `_next_adr_id` cannot trust it and scans
committed page slugs as a **second id source** (`_committed_page_max_id`), serialized under
`_adr_log_lock`. All of that compensates for the index not being a table.

**Agent-prompt TOC** — `_TOC_ROW_RE` parses `- \`<pattern>\` → <purpose>`; `_upsert_toc_row`
scan-replaces rows. The `uses: N` counter lives in **two** places (a global `_prompt_usage`-tagged
memory row *and* the TOC page), throttled to `count % 10` explicitly to avoid wiki version churn.

### 1.2 Honest framing

An earlier draft claimed a markdown page "has no query surface." **False** — the daemon could
parse server-side. The real case: maintaining a regex parser over prose as a query engine; no
indexes; full read+parse per call; format drift breaks parsing silently; whole-page
read-modify-write is not atomic.

### 1.3 ADR lifecycle is dead

**11 of 183** yadgar ADRs carry `adr-status:superseded`. ~94% sit at `accepted` forever and every
`revisit_trigger` goes unevaluated. The problem is not accumulation needing pruning — ADRs never
*leave* `accepted`, so staleness is invisible.

### 1.4 Three dead-config / live defects (all verified)

- **Superseded ADR bodies are recallable today.** `recall_disposition` resolves by `page_type`
  only; status lives in a tag; `recall(tags=[...])` filters *in*, never out. `project.py:288`
  prints `recall(type='wiki', tags=['adr'])` to the model as the suggested call.
- **The tag-override defeats exclusion.** `providers/wiki.py:102` —
  `if not self._tags and get_policy(...).recall_disposition == "exclude"`. Any tagged recall skips
  it. **Gates Car G.**
- **`downweight` is documented, zero implementation.** Two docstring mentions
  (`policy.py:34,67`), no code path. **Gates the task cars.**
- **`gate_mode="identity"` is dead code.** Removed with repo_wiki's decommission
  (`dlq.py:292-296`: *"no page_type sets gate_mode='identity' any more"*). D21 requires
  **reimplementing** it, not flipping a config.

---

## 2. Decisions

### 2.1 Structure

| # | Decision | Rationale |
|---|---|---|
| D1 | **Per-entity tools over a shared `_LedgerMixin`** — not a generic `record_query`/`record_write` | Makes the ADR migration invisible: `adr_*` keep their signatures. The reuse wanted is *implementation* reuse. A generic filter param reinvents mini-SQL; a generic write tool collides with the no-direct-DB rule. **Load-bearing claim is return-shape stability, not just signatures** — see §7 Car F acceptance. |
| D2 | **Three tables** — `task`, `adr`, `agent_prompt`. No generic `record` table. | Different status enums, required fields, indexes. A `kind` discriminator gives a field union where half the columns are always null. |
| D3 | **One mixin.** `agent_prompt` gets a `number` nothing reads. | Uniform beats exceptional. Consequence: a number carries D6/D7, so pruning the 52 patterns in #0015 becomes *archive*, not delete. |
| D4 | **Bodies stay wiki pages. No `body` column.** | Bodies inherit `wiki_page_version` — history/diff/restore all work; a row body has no undo without reimplementing versioning. Given ADR-0090 (surrealkv corruption, open) and plans previously lost to corruption, decisive. Also a 64K body in a row is fetched by any careless `SELECT *`; behind a slug it is unfetchable by accident. |
| D5 | **Row owns ALL metadata; page owns ONLY prose.** New page schema version. | Otherwise split authority returns — `_build_adr_body` renders status/date *into* the page while the row carries the same fact, and `_flip_superseded_target` writes a status *tag* best-effort with a bare `except: return`. **Achievable: nothing parses the flat bullets back** (`adr_get` forwards raw content; `adr_list` reads only the index). **But** `wiki_page_types.yaml:28-31` requires `[Context, Decision, Consequences]` headings on `adr` pages and `wiki_meta.py:64-95` enforces it — that `required:` list must change in the same car, and `adr_get` must merge the row into its response or `date`/`rationale`/`alternatives`/`revisit_trigger`/`supersedes` vanish from its output. |

### 2.2 Identity

| # | Decision | Rationale |
|---|---|---|
| D6 | **Integer `number` from the engine's sequence facility. No application-level read-then-write.** Sequences are created with **`BATCH 1`**, exposed as a tunable knob — `ledger.sequence_batch`, default `1`, in the runtime config store (ADR-0163), falling back to `config.yaml` if the store is unreachable at migration time. | `INSERT` and let the engine assign — no race window to lock around. Kills `_adr_log_lock` *and* `_committed_page_max_id`. Expressed as a *capability* (see D30), not a literal. Batch size is an allocation preference on a `DEFINE SEQUENCE` statement yadgar issues anyway — the same category as `START` — not cluster coordination, which stays entirely the engine's business. `BATCH 1` is the right default for a single-node deployment (no consensus round-trip exists to amortise); the knob keeps a cluster escape hatch without yadgar pretending to manage one. **VERIFIED 2026-07-31 against a throwaway `surrealdb/surrealdb:v3.1.5` container** (the version the backend image ships; the host binary is 3.0.5 — see task #0092): `DEFINE SEQUENCE` executes; `sequence::nextval()` increments (`BATCH 1 START 1` → 1,2,3,4); the engine default is **`BATCH 1000 START 0`** (from `INFO FOR DB`, which the docs do not state); sequence state **persists across a full process restart**. One measured consequence, recorded because it is invisible otherwise: **a restart DISCARDS the unconsumed remainder of the reserved batch** — a `BATCH 1000` sequence at value 2 returned **1000** after restart, while a `BATCH 1` sequence at 4 returned 5. Under the engine default, ids therefore advance by up to 1000 per daemon restart regardless of writes. This is CORRECT per D9 (gaps are expected; a number is an identifier, not a count) and affects only readability, which D10's encoding absorbs. |
| D7 | **Never reused. Archive, never hard-delete.** | External references (plan filenames, PR titles, `(#93)` in commits) must not silently retarget. Archive-never-delete is the precondition for permanence. |
| D8 | **Composite id `(origin, number)`.** | Your task and mine are `(alice, 231)` and `(max, 231)` — distinct by construction. Works offline, sync is idempotent, **no id ever changes** (which upstream-assigns-on-sync would violate). Sequences stay per-origin. |
| D9 | **Gaps are correct.** | A number is an identifier, not a count. SurrealDB docs explicitly do **not** claim gap-free — their own example shows a gap after a cancelled transaction, and `nextval` is not rolled back on failure. |
| D10 | **No zero-padding anywhere. Display in Crockford base32, default on.** | Ids are integers that grow past any fixed width — a 4-digit form must not exist in code or prose. Base32 gives 4 chars ≈ 1M (vs 10k decimal, 65k hex) and drops ambiguous `I/L/O/U`. **Storage stays an integer, so the encoding is reversible at any time with zero migration.** Cost, accepted: `ADR-0158` renders `ADR-4Y`, `ADR-0182` renders `ADR-5P` — 183 existing references in commits, PRs and ADR bodies stay in decimal in immutable history. **No code may regex `\d{4}` or assume any width.** |
| D11 | **Harness reconcile keys on the id prefix** — `[231]` local, `[alice/231]` foreign. Unmatched → **warn**, never silently create. | Claude Code task ids are per-session handles regenerated by the restore-nudge, not identity. **`http.py:923` `_TASK_RE = ^## task:(\d+)` must change** to accept an optional origin segment — forced by D10 regardless. And the nudge (`http.py:946`) already emits `[{tid}]` but never instructs the model to *preserve* it in the `TaskCreate` subject: the template needs that instruction. |
| D12 | **Title capped at 200 chars, reject-on-write.** | Longest current subject ~148. Silent truncation destroys information exactly when someone is being sloppy. |

### 2.3 Project identity

| # | Decision | Rationale |
|---|---|---|
| D13 | **`project_id` = `<owner>/<repo>` from the git remote, host excluded**, normalizing away scheme, SSH alias and `.git`. | `git@github-personal:m-agahi/yadgar.git` and the HTTPS form must yield one key — this repo uses SSH aliases. Excluding the host makes a host migration a no-op. Fixes the `basename()` collision (`adr_index.py:46,51,61`) where two repos named `infrastructure` share ADR slugs. **Note: slugs remain basename-derived**, so `project_id` and `body_slug` use different schemes and the slug-namespace collision persists until slugs migrate too — out of scope here, stated so it is not assumed fixed. |
| D14 | **Non-git dirs are `local/<basename>`, never sync, backup-only.** | No stable shared identity exists; don't pretend. Permanently `owner_kind=user`. Accepted edge case: an org named `local` collides — blast radius one machine. |
| D15 | **No repo file.** Override via `config_set("project.key_override", …, scope="project")`. | Avoids repo burden, fork inheritance, accidental edits. ADR-0163's store is already directory-scoped. |
| D16 | **A derived key is STORED on the row at write time, never recomputed at read time.** | **Rationale corrected post-audit.** An earlier draft blamed live default-branch resolution (`server_helpers.py:326`) for the codeberg→github task-list break. **That is refuted**: `_default_branch_for_root` has exactly one non-test caller (`server_helpers.py:386`, the memory-write worktree path), and the live `yadgar-task-list` row has `branch: null`, so branch resolution never participated. The documented cause is commit `eefa176e` (2026-07-15) — the v5.42.3 `missing_branch` hard-reject made canonical writes impossible, so the mirror never persisted; fixed by `wiki_write_task_list`. The decision stands on its own merit (a recomputed key silently re-points existing rows); the incident is *not* evidence for it. |

### 2.4 Tenancy (design now, build later)

| # | Decision | Rationale |
|---|---|---|
| D17 | **Two orthogonal axes** — `owner_kind` (user\|team\|org) · `owner_id` · `reach` (project\|global) · `project_id`. **The COLUMNS are deferred to the tenancy task; Car A ships a scope-filter HOOK that is a no-op today.** | One enum cannot express six cells (user/team/org × project/global), so the shape is decided now even though nothing is built. **Revised 2026-07-31 after an independent audit challenged "free and inert" and the fact-check refuted BOTH positions.** Facts: the tables are SCHEMALESS (`migrations.py:71`) and the migration mechanism is idempotent `DEFINE FIELD IF NOT EXISTS` (`migrations.py:81`), so adding a field later is ONE line of DDL — there is no `ALTER TABLE`, no table rewrite, and rows lacking the field stay valid. Seed size is ~400 rows total, so even a backfill is milliseconds. So the earlier claim that retrofitting is "a migration on every row" was wrong, and the counter-claim that the columns cost anything at insert/select was also wrong. **The only real cost is code** — threading the columns through the mixin's signatures and query builders — and that cost is identical whenever it is paid. The genuine risk is narrower: a mixin written with no tenancy concept bakes single-tenant query SHAPES in, and rewriting query builders later is the expensive part, not the DDL. A no-op filter hook captures exactly that risk at lower cost than four unread columns. |
| D18 | **Sync selectivity IS the owner axis.** `user` → nowhere · `team` → team · `org` → org | "Should this reach my team" and "who owns this" are the same question. Personal backup is a separate independent flag. |
| D19 | **Explicit `PERMISSIONS` on every `DEFINE TABLE`.** | SurrealDB advisory GHSA-x5fr-7hhj-34j3: defaults were FULL. **Defense in depth only** — the daemon opens one connection with system credentials (`storage/__init__.py:263-273`), so table permissions do not constrain it and there is no per-user `$auth` today. Not "AAA need not be hand-rolled." |
| D20 | **One choke point: every row access goes through `_LedgerMixin`. Lint-enforced.** | Highest-value item here, and the engine seam (D30). **Needs NEW tooling**: import-linter's 4 contracts are import-graph only and cannot see call sites or query literals; none of the 23 `scripts/check_*.py` target storage boundaries. Budgeted into Car A with an allowlist for pre-existing violations (`cli/stats.py:719` own connection, `hooks/prompt-recall.py:83,98`, `project.py:1381`, `audit.py` 10+ sites). |

### 2.5 Retrieval and mutability

| # | Decision | Rationale |
|---|---|---|
| D21 | **Redesign and reimplement the identity gate**; `gate_mode="identity"` for all three types. | Deterministic slugs, legitimately self-similar content. Currently dead code (§1.4) — this is a build, not a flip. Deletes `adr_add`'s `force=True`. |
| D22 | **`recall_disposition` becomes status-driven**: `accepted`/`open` → include · `superseded`/`rejected`/`deprecated` → exclude · `task` → downweight · `agent_prompt` → exclude, unconditional. | A rejected ADR describing an approach you deliberately turned down is the most dangerous thing to hand an agent as guidance. |
| D23 | **Supersede = retype `adr` → `adr_superseded`, atomic with the status flip.** Never delete, never NULL `body_slug`. | Semantic recall must never surface it; explicit `adr_get` must still work. **Two blockers: no tool can change `page_type`** (`_WIKI_UPDATE_ALLOWED` excludes it; `wiki_set_metadata` takes only `directory_context`/`branch`), **and `CANONICAL_PAGE_TYPES` raises** on any type outside `{task_list, adr}` (`wiki.py:30,170-174`), which every ADR write goes through. Car G must add the type *and* build a server-side retype mutator. |
| D24 | **Agent-prompt discovery is `agent_prompt_list` + `agent_prompt_get`, not recall.** No search for now. | A TOC row carries name + purpose + a directly-retrievable slug: lookup, not ranking. This is what lets `agent_prompt` be excluded **unconditionally**, which is what kills the tag-override. Add search later with evidence and an ADR superseding ADR-0007. |
| D25 | **`mutability` as `WikiPolicy` field #6** + nullable per-page `mutability_override` + power-gated logged `wiki_set_mutability`. **Enforced at `storage/wiki.py:215 update_wiki_page`** (and the insert/delete paths), **not `WikiStore.add`.** | A lock with no unlock is a trap, and per-type policy alone can only be unlocked by redeploying — the escape hatch forces a per-page dimension. **Enforcement point corrected post-audit:** `WikiStore._apply_text_edit` (`store.py:1905`) calls `update_wiki_page` directly and backs 8 edit tools; `append_section` (1729), `restore_version` (1598) and the append path (1231) do the same; `admin_exec/wiki.py:139 wiki_update` never touches `WikiStore` at all. Worse, `_WIKI_UPDATE_ALLOWED` includes **`tags`** — so an ungated tool can strip `adr-status:superseded` and un-supersede an ADR. |
| D26 | **Per type:** `adr`/`adr_superseded` → **locked** · `task` → free · `agent_prompt` → free · rollups → **derived** | Tasks and prompts are edited constantly; decisions are not. **`locked` blocks agent/tool edits, not sanctioned server-side lifecycle transitions** — otherwise the supersede retype deadlocks against its own guard. |

**What mutability is really for:** not dangling-pointer prevention (a side benefit) but **stopping the
well-intentioned repair** — the runaway instance that deletes an ADR page to "resolve" a dangling
reference, or rewrites a derived rollup because it looks stale.

### 2.6 ADR consolidation

| # | Decision |
|---|---|
| D27 | **`tier: binding \| historical`**, `adr_list` defaults to `binding`. One field, reversible. |
| D28 | **`subsystem`** — explicit, never inferred from the title. |
| D29 | **Derived per-subsystem rollup pages**, generated on write. Replaces one big index write with one small rollup write. |

### 2.7 Portability

| # | Decision |
|---|---|
| D30 | **New ledger tables are born portable.** Scalar columns only — no record links, no `RELATE`, no nested objects. Identity, authorization and batching expressed as **capabilities**, not engine literals (`DEFINE SEQUENCE` ↔ `CREATE SEQUENCE`; Surreal `PERMISSIONS`+`$auth` ↔ Postgres RLS). All row access through `_LedgerMixin`, which **is** the engine seam. **Sync is an outbox table, never an engine changefeed** — a changefeed would have to be rebuilt on a swap. **The legacy corpus is NOT portable and this train does not make it so**: memories, embeddings, entities, relationships and engrams use Surreal-specific vector indexes and graph edges. Retrofit is task #0098; vector search is its hard problem. |

---

## 3. Schema

SCHEMALESS, idempotent `DEFINE FIELD IF NOT EXISTS`, **explicit `PERMISSIONS`** (D19).

```
# spine (all three)
id · origin · number          composite identity        (D6/D8)
owner_kind · owner_id         user|team|org             (D17)
reach · project_id            project|global
title                         <= 200, reject            (D12)
status                        per-entity enum
body_slug                     wiki page, ALWAYS present (D4)
created_at · modified_at

task          { active_form · blocked_by[] · blocks[] }
              pending | in_progress | completed | archived

adr           { date · subsystem · tier · supersedes[] · superseded_by[] }
              open | accepted | superseded | rejected | deprecated | archived
              body_slug MANDATORY non-null

agent_prompt  { kind: pattern|discipline|contract · purpose · composes[] · uses }
              active | archived · reach always global · number assigned and ignored
```

`composes[]` replaces the `## Composes` section parsed by `_COMPOSES_SECTION_RE`.
`uses` replaces the two-location counter and its `%10` throttle.

---

## 4. Tools

```
task_list / task_get / task_write                   NEW
adr_add / adr_list / adr_get                        EXISTING — signatures AND return shapes unchanged
agent_prompt_list / agent_prompt_get                NEW
agent_prompt_save / agent_dispatch_prelude          EXISTING — unchanged
seed_agent_prompts                                  EXISTING — unchanged
wiki_set_mutability(slug, value, reason)            NEW — power=True, logged
```

### 4.1 Batch write — spine level

`StorageEngine.batch_writes` (`client.py:875-900`) already gives real `BEGIN…COMMIT`. Three
clauses to carry:

- atomic only **within a chunk** (500 statements / 1 MB); a failure in one chunk does not roll
  back earlier chunks. Supersede batches are tiny — fine, but state it.
- **embedded mode has no transaction at all** — statements run per-statement via `_q`. The nightly
  consolidation cycle runs embedded, so Car K's sweep is not atomic there.
- `sequence::nextval` is not rolled back on failure, so a rolled-back batch burns a number
  (consistent with D9).

Do **not** use `_q_multi` (`client.py:695`) — read-only, no `BEGIN/COMMIT` framing.

---

## 5. Deletions — named deliverables

`_index_max_id` · `_committed_page_max_id` · `_next_adr_id` dual scan · `_next_adr_id_from_index` ·
`_adr_log_lock` · `_INDEX_ROW_RE` · `parse_index_rows` *(8 non-test refs incl. `adr_render.py:179-181`,
`project.py:1880,1889`; 7 test refs)* · `_render_index_row` · `_build_index_content` ·
`_assemble_index_rows` · the `{project}-adr-index` page · `_flip_superseded_target`'s page-tag write ·
`adr_add`'s `force=True` · **`_TOC_ROW_RE`/`_TOC_SLUG` — TWO independent copies**
(`backend/admin_exec/wiki.py:52-53` and `core/server/tools/agent_prompts.py:36,39`) ·
`_upsert_toc_row` · `_set_toc_row_count` · **`StorageEngine.increment_prompt_usage`
(`storage/wiki.py:1085`) and its delete-then-insert `_prompt_usage`-tagged memory row** — note
`_prompt_usage` is a tag literal (`storage/wiki.py:1075,1101,1116`), not a symbol · the
`agent-prompt-toc` page · the `%10` throttle · `_COMPOSES_SECTION_RE` · `_parse_composes` ·
`_strip_composes_section` · the `providers/wiki.py:102` tag-override

Keeping any "for safety" preserves the dual-authority complexity this removes.

---

## 6. Migration

1. Tables + sequences + PERMISSIONS + chokepoint guard.
2. Backend ops + cache.
3. **Car C lands before any seed** — it gates correctness (§1.4).
4. Task tools; seed; rewire SessionStart + stop-hook (incl. the D11 prefix instruction).
5. ADR tools re-pointed; seed from the index (use `parse_index_rows`, then delete it).
6. **Retype the 11 existing superseded pages.** Acceptance: zero pages with
   `page_type='adr' AND directory_context='/home/max/git/yadgar'` carry an
   `adr-status:superseded` tag. **Scope the assertion to yadgar** — there are 270 `page_type='adr'`
   pages across 8 projects (yadgar 183, flux 35, karyab 18, aws-eks 14, aws-work 8, nix 5,
   infrastructure 4, github-runners 3).
7. agent_prompt table + tools; delete TOC machinery.
8. Rollups; `tier`/`subsystem`.
9. Nightly archive sweep, policy-dispatched.
10. **Verification gate** — old pages stay in place, unread, until row counts and spot-checks match.
    Only then delete the three index pages.

Rollback before step 10: stop reading the tables.

---

## 7. Cars

| Car | Scope | Depends on |
|---|---|---|
| A | `_LedgerMixin` + migration + `DEFINE SEQUENCE … BATCH 1` behind the `ledger.sequence_batch` knob (**test `OVERWRITE` reset semantics FIRST — §10**) + explicit PERMISSIONS + **a no-op scope-filter hook** (not tenancy columns — D17) + **new AST guard `scripts/check_ledger_chokepoint.py` with an allowlist for pre-existing violations** | — |
| B | backend ops + cache | A |
| C1 | **3a** — tag-override matches the page type's own opt-in tag | — |
| C2 | **3b** — implement `downweight` | — |
| C3 | **3c** — redesign + reimplement the identity gate (D21) | — |
| D | task tools | B, C |
| E | task seed + SessionStart/stop-hook rewire + `http.py:923` matcher + D11 prefix instruction | D |
| F | ADR tools re-pointed — **acceptance: a characterization test pins `adr_list`/`adr_get` return shapes pre-migration and asserts them green post-migration** (live consumers: `project.py:1889` `r["adr_id"]`, `adr_render.py:181`, 7 test refs) | B, C |
| G | ADR seed + **add `adr_superseded` to `CANONICAL_PAGE_TYPES` + build the retype mutator** + retype the 11 + delete parser/serializer/lock + **re-point `project_brief`: `_build_adr_log` (`project.py:1880,1889`) and `_get_adr_log_updated_at` (`project.py:1378-1381`, a second hardcoded slug site)** + **fix the dead `{project}-adr-log` read at `stop_checkpoint_prompt.md:26-33`** (that page no longer exists — every checkpoint runs a dead read) | F |
| H | `tier` + `subsystem` + rollups | G |
| I | `agent_prompt` table + `list`/`get` + delete TOC machinery + **re-point `_build_agent_prompt_toc` (`project.py:1898-1921`)** | B |
| J | `mutability` policy field + per-page override + `wiki_set_mutability`, **enforced at `storage/wiki.py:215`** and covering `admin_exec/wiki.py:139 wiki_update` | A |
| K | nightly archive sweep, policy-dispatched | E, G, I |

D/E ∥ F/G ∥ I after B. **C gates D and F.** J depends only on A and can land early.

TDD throughout, RED-verified per car. Gates: ruff, import-linter, I32, I33, `check_versions`.

---

## 8. Expected impact

| Metric | Today | After |
|---|---|---|
| **Full task-list read, mandated every stop-hook checkpoint** | ~24k tok | ~2k tok |
| `adr_list` | 30,002-char read + regex parse | indexed query |
| `adr_add` | index read + page-slug scan + lock | one `INSERT`, sequence-assigned |
| prompt usage counter | 2 stores + throttle + page version churn | one `UPDATE`, exact |
| "what governs `vacuum`?" | scan 183 entries | one rollup page |
| body pages | 270 ADR + 61 prompt | unchanged, versioning intact |
| Superseded ADR in recall | **yes (live defect)** | no |
| Index drift risk | present | none — the index *is* the query |
| Cross-instance id collision | n/a | none (composite id) |

**The ~24k is paid TWICE per cycle — at session start AND at every stop-hook checkpoint.**

*Corrected 2026-07-31.* A previous revision of this section claimed there was no session-start
cost, on the grounds that the SessionStart hook POSTs without `mode` (so `http.py:1080` defaults to
`catalog`, and no `project_brief` mode inlines the task-list page) and that the only task-list touch
is `_task_list_restore_nudge` (`http.py:864-981`), which parses server-side and emits at most 12
lines (`_CAP = 12`).

Those facts are true but the conclusion drawn from them was wrong. **The nudge is an INSTRUCTION,
and its execution is the cost.** It reads *"ACTION REQUIRED — restore your task list BEFORE any
other work. N open task(s)… Call TaskCreate for EACH one now… Full descriptions:
`wiki_read("{project}-task-list")`"*. Complying means a full-page read plus N `TaskCreate` calls plus
the harness re-injecting every task as a system-reminder — which is exactly task #0080's
itemisation, and matches the observed 8%→18% context jump on a single restore.

Measuring the hook's output size and concluding the session start is cheap was the error; the
surrounding claim was never checked.

## 9. Out of scope — filed separately

**#0095** project identity (near-term — a live bug) · **#0096** encryption posture (blocks sync) ·
**#0097** AAA/sync design (outbox decided per D30) · **#0098** DB driver seam / engine portability ·
**#0043** anchor metadata sprawl · **#0015** agent-prompt prune (becomes *archive* under D3/D7)

## 10. Open build questions

- **Rollup regeneration trigger** — on write (fresh, one small page write) vs nightly (cheaper, stale between runs).
- **`subsystem` vocabulary** — free-form drifts (`vacuum`/`Vacuum`/`db-vacuum`); a controlled list needs a home.
- **⚠ Does `DEFINE SEQUENCE OVERWRITE` reset the counter?** The `ledger.sequence_batch` knob (D6) is
  only meaningful if it can be re-applied to an existing sequence, and the syntax is
  `DEFINE SEQUENCE [OVERWRITE | IF NOT EXISTS] …`. **If `OVERWRITE` resets to `START`, re-tuning the
  knob would reuse ids and silently destroy D7's permanence invariant** — the worst failure this
  design can have, since every external reference (`docs/plans/*` filenames, PR titles, `(#93)` in
  commits) would retarget. Car A MUST test this before wiring the knob to anything that re-issues
  the DDL. If `OVERWRITE` does reset, the knob applies at creation only and re-tuning requires an
  explicit, guarded migration that preserves the current value via `START`.

*(Resolved 2026-07-31: `DEFINE SEQUENCE` availability, the `BATCH 1000 START 0` engine default, and
restart behaviour are all now measured facts — see D6.)*
