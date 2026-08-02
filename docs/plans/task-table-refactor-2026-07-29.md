# Plan: `_LedgerMixin` spine — dedicated tables for `task`, `adr`, `agent_prompt`

**Date:** 2026-07-29 (rev 2026-08-02 — numbers re-measured, identity re-decided against the
two-engine split, circular dependency and project-key collision resolved, migration/rollback
written, §11.3 answered)
**Tasks:** #0047, #0080 · related #0035 #0048 #0051 #0095 #0096 #0097 #0098 #0119
**ADR:** ADR-0182 (supersedes ADR-0181) · ADR-0183 (portability seam)

**Binding context — `docs/plans/split-store-engine-decision-2026-08-02.md`.** The user has decided
the backend runs **two engines**: SurrealDB keeps graph, memory, **wiki bodies and embeddings**;
**MariaDB** (decided 2026-08-02, that doc §4.5) takes the relational set — which is exactly what
this spine builds. Where this plan touches engine choice, backup, the four operational paths, the
license posture or the FTS motivation, it **defers to that doc by filename and does not restate
it**. Nothing below may contradict it.

**Status:** REFINED — 2026-08-02. Build-ready except the items listed in **§12 Unresolved**.
Not started; **no code exists yet** — there is no `task` table in `migrations.py` and no
`_LedgerMixin`, despite ADR-0183 D30 naming the latter "the engine seam."

---

## 0. TL;DR

Three structured datasets are stored as markdown pages and re-parsed on every read.
Move their **indexes** to tables; leave their **bodies** as wiki pages.

| | Today — **all re-measured 2026-08-02** | After |
|---|---|---|
| task list | **16,060**-char page, read whole every stop-hook checkpoint | table query, capped titles |
| ADR index | **32,163**-char page, regex-parsed per `adr_list`, per `adr_add` **and** per `project_brief` | table query |
| agent-prompt TOC | **9,538**-char markdown table; `uses:N` stamped into prose | table columns |
| bodies (**194** ADR pages + **63** prompt/discipline pages) | — | **untouched — still wiki pages, still in SurrealDB, still embedded** |
| MCP surface | — | `adr_*` and `agent_prompt_save` **signatures unchanged** |
| engine | SurrealDB wiki pages | **MariaDB** rows — see `split-store-engine-decision-2026-08-02.md` §4.5 |

**Every number in the previous revision of this plan was stale.** The task page had shrunk ~77%
(70,341 → 16,060) when completed tasks were culled on 2026-08-02, so this plan's headline
token-saving claim was ~**4× overstated**; §8 is recomputed accordingly. ADR counts were ~181–183
with 11 superseded; they are **194** per-ADR pages / **193** index rows / **195** `page_type='adr'`
rows, **12** superseded. The three counts differ for two separate reasons — see §1.5.

---

## 1. Problem

### 1.1 Three markdown tables

**Task list** — `yadgar-task-list` is **16,060 chars** (measured 2026-08-02, down from 70,341 four
days earlier because the completed tasks were culled by hand — the growth is unbounded but the
*current* size is small). `stop_checkpoint_prompt.md:115-118` mandates a full `wiki_read` of it
**every checkpoint**.

The compaction is itself the argument, not a counter-argument: getting to 16 KB cost a manual
deletion pass, and the page will grow straight back. A table makes the same saving **structural**
(§11.2, D37).

**ADR index** — `yadgar-adr-index` is **32,163 chars**, already a relational row round-tripped
through markdown (`adr_index.py:21` `_INDEX_ROW_RE`, seven columns out via `_render_index_row`).
Because the index write can lag the page write, `_next_adr_id` (`adr_index.py:134`) cannot trust
it and scans committed page slugs as a **second id source** (`_committed_page_max_id`,
`adr_index.py:107`), serialized under `_adr_log_lock` (`adr.py:104,212`). All of that compensates
for the index not being a table — **and it still failed** (§1.5).

**Agent-prompt TOC** — `_TOC_ROW_RE` parses `- \`<pattern>\` → <purpose>`; `_upsert_toc_row`
scan-replaces rows. The `uses: N` counter lives in **two** places (a global `_prompt_usage`-tagged
memory row *and* the TOC page), throttled to `count % 10` explicitly to avoid wiki version churn.

### 1.2 Honest framing

An earlier draft claimed a markdown page "has no query surface." **False** — the daemon could
parse server-side. The real case: maintaining a regex parser over prose as a query engine; no
indexes; full read+parse per call; format drift breaks parsing silently; whole-page
read-modify-write is not atomic.

### 1.3 ADR lifecycle is dead

**12 of 194** yadgar ADRs carry `adr-status:superseded` (measured 2026-08-02; the previous
revision said 11 of 183). ~94% sit at `accepted` forever and every `revisit_trigger` goes
unevaluated. The problem is not accumulation needing pruning — ADRs never *leave* `accepted`, so
staleness is invisible.

### 1.4 Dead-config / live defects (all verified)

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

**Three more, found 2026-08-02 and previously unfiled. All verified in source; each lands on a
surface this plan already touches.**

- **`agent-prompt-toc` has `page_type = null`** (DB query, 2026-08-02), so `get_policy`
  (`_shared/wiki/policy.py:118-130`) returns `DEFAULT_POLICY`
  (`policy.py:88-94`: `recall_disposition="include"`). The TOC is therefore **recall-visible
  even though every page it indexes is excluded** — `POLICY_BY_TYPE["agent_prompt"]`
  (`policy.py:98-105`) sets `recall_disposition="exclude"`. The index leaks the corpus the
  policy hides. **D24's "excluded unconditionally" is not true today**, and Car I must either
  type the TOC or delete it; deleting it (the plan's stated intent, §5) resolves this for free,
  so **do not re-create an equivalent page**.
- **`wiki_append_section` has no content-size cap** (`core/server/tools/wiki.py:1206`), while the
  two other task-list writers do: `wiki_write_task_list` rejects at `wiki.py:232-233` and
  `wiki_add` at `wiki.py:440-441`, both `65_536` with `reason="content_too_large"`. And
  `wiki_append_section` is exactly the path the stop-hook template **recommends** for surgical
  task-list edits (`stop_checkpoint_prompt.md:142-146`). So the sanctioned incremental writer is
  the one uncapped writer. Not a blocker for the spine — the spine deletes this path for tasks —
  but it must not be inherited by whatever replaces it, and it is worth a standalone fix since
  the spine is not started.
- **The agent-prompt usage counter is write-only.** `get_prompt_usage_counts`
  (`_shared/storage/wiki.py:1067`) has **no caller outside its own incrementer**
  (`increment_prompt_usage`, `wiki.py:1085`, reads it at `wiki.py:1095`) and one test. The count
  is written on every `agent_dispatch_prelude` (`core/server/tools/dispatch_helper.py:313-319`
  forwards the admin op) and read by nothing that acts on it. **See D40** — §3's `uses` column
  reproduces this defect unless it ships with a reader.

### 1.5 ADR index drift is a LIVE DEFECT, not a risk — promoted 2026-08-02

The previous revision of this plan framed *"the index write can lag the page write, so
`_next_adr_id` cannot trust it"* as a **risk** that the dual-source scan mitigates. **It already
happened.** `ADR-0124` has a per-ADR page and **no index row**. Query evidence, 2026-08-02:

| Count | Value | What it is |
|---|---|---|
| index rows in `yadgar-adr-index` | **193** | parsed by `_INDEX_ROW_RE` |
| `yadgar-adr-NNNN` per-ADR pages | **194** | the ID-bearing artifacts |
| `page_type='adr'` rows, `directory_context='/home/max/git/yadgar'` | **195** | 194 pages **+ the index page itself**, which also carries `page_type='adr'` |

So the 194→195 gap is **benign and explained** (the index page is typed `adr`), while the
193→194 gap is **the defect**. Both matter for the cutover acceptance predicate — see D35.

Three structural causes, all verified in source:

1. **`adr_add` does two sequential, non-atomic writes** (`adr.py:143-291`): the per-ADR page
   first with `wait=True`, then the index. Nothing rolls the first back if the second never
   lands. The comment at `adr.py:225-227` says the page-first ordering exists precisely so a
   *lagging* index cannot duplicate an id — it protects the **id**, not the **index**.
2. **The lock is process-local.** `_adr_log_lock(resolved)` (`adr.py:104`) is a
   `threading.Lock` in the calling process. It serialises nothing across the core/backend
   process boundary or across two daemons.
3. **Next-id is a full scan on every call.** `_committed_page_max_id` (`adr_index.py:107`)
   calls `wiki_list(slug_prefix=..., directory=..., limit=10000)` on **every `adr_add`**, then
   regex-matches every returned slug. `_index_max_id` re-parses the whole 32 KB index in the
   same call.

And the 32 KB index is re-parsed on the read side too: on **every** `adr_list`, and again in
`_build_adr_log` (`project.py:1866`, importing `parse_index_rows` at `project.py:1880` and
calling it at `project.py:1889`) on **every `project_brief`** — i.e. every session start.

**This is the single strongest present-cost argument in the plan**, and it is the one the stale
task-list number used to overshadow. The table does not merely make the index faster: **the
index stops existing as a separate artifact that can disagree with the pages.** D31 makes the
number and its **row** one atomic write, which removes *this* drift class rather than mitigating
it.

**Bounding that claim, because the same shape reappears elsewhere and must not be assumed
fixed.** D31's atomicity covers the **number and the ledger row**, both in MariaDB. It does
**not** cover the ledger row and its **body page**, which is in SurrealDB (D4) — those are two
engines and therefore two writes. So `adr_add` goes from *page-then-index* (both in Surreal) to
*page-then-row* (Surreal, then MariaDB): the **write count is unchanged and the drift shape is
relocated, not eliminated.** What genuinely improves is that the surviving second artifact is a
**queryable row rather than a regex-parsed markdown table**, that the lock is a real engine row
lock instead of a process-local `threading.Lock`, and that a missing row is *detectable* by
`check_invariants` where a missing index line was not. The mitigation is ordering — see §4.1's
row-last rule and D35d. Anyone reading §1.5 as "the split makes ADR writes atomic end-to-end"
has read it wrong.

---

## 2. Decisions

### 2.1 Structure

| # | Decision | Rationale |
|---|---|---|
| D1 | **Per-entity tools over a shared `_LedgerMixin`** — not a generic `record_query`/`record_write` | Makes the ADR migration invisible: `adr_*` keep their signatures. The reuse wanted is *implementation* reuse. A generic filter param reinvents mini-SQL; a generic write tool collides with the no-direct-DB rule. **Load-bearing claim is return-shape stability, not just signatures** — see §7 Car F acceptance. |
| D2 | **Three tables** — `task`, `adr`, `agent_prompt`. No generic `record` table. | Different status enums, required fields, indexes. A `kind` discriminator gives a field union where half the columns are always null. |
| D3 | **One mixin.** `agent_prompt` gets a `number` nothing reads. | Uniform beats exceptional. Consequence: a number carries D6/D7, so pruning the 52 patterns in #0015 becomes *archive*, not delete. |
| D4 | **Bodies stay wiki pages. No `body` column. NON-NEGOTIABLE — reaffirmed and sharpened 2026-08-02.** Under the two-engine split this reads: **prose bodies stay in SurrealDB, with their embeddings and their `[[slug]]` crossrefs. ONLY metadata/index rows move to MariaDB.** | Bodies inherit `wiki_page_version` — history/diff/restore all work; a row body has no undo without reimplementing versioning. Given ADR-0090 (surrealkv corruption, open) and plans previously lost to corruption, decisive. Also a 64K body in a row is fetched by any careless `SELECT *`; behind a slug it is unfetchable by accident. **Added 2026-08-02, per `split-store-engine-decision-2026-08-02.md` §3 — this is now the load-bearing conditional of the whole split, not a stylistic preference.** Measured there: 52 crossref rows touch `-adr-`, including task-list→ADR edges, and ADR/task pages are embedded and recall-visible under the default policy. **Crossref and embedding-reachability cost is exactly ZERO if bodies stay, and SEVERE if they move** — moving them would mean either replicating the embedding/ANN path into engine #2 or breaking `[[slug]]` reachability. ADR-0182 had already decided this on versioning grounds; the split makes it structural. **Corollary, measured 2026-08-02:** `source_memory_ids` is effectively dead — **2 populated rows out of 2,213** DB-wide, and **none on ADRs** (hardcoded `None`) — so preserving the field on the surviving page rows costs nothing and requires no design. Do not spend effort porting it, and do not delete it either. |
| D5 | **Row owns ALL metadata; page owns ONLY prose.** New page schema version. | Otherwise split authority returns — `_build_adr_body` renders status/date *into* the page while the row carries the same fact, and `_flip_superseded_target` writes a status *tag* best-effort with a bare `except: return`. **Achievable: nothing parses the flat bullets back** (`adr_get` forwards raw content; `adr_list` reads only the index). **But** `wiki_page_types.yaml:28-31` requires `[Context, Decision, Consequences]` headings on `adr` pages and `wiki_meta.py:64-95` enforces it — that `required:` list must change in the same car, and `adr_get` must merge the row into its response or `date`/`rationale`/`alternatives`/`revisit_trigger`/`supersedes` vanish from its output. |

### 2.2 Identity

> **REWRITTEN 2026-08-02 against a binding user rule.** Verbatim: *"we do not touch next id.
> that is engin drivers job we only use insert / select / update / delete. we do not get
> involved in index management."* Application code is confined to those four verbs; it manages
> no sequences and no indexes. D6 is replaced; D8 and D9 keep their conclusions but lose their
> old rationale, which the rule invalidated. D31 is new.

| # | Decision | Rationale |
|---|---|---|
| D6 | **REPLACED 2026-08-02. Two distinct identity concepts, not one.** (a) The **row's primary key is the engine's native identity column** — `AUTO_INCREMENT` on MariaDB. Yadgar never reads it, never sets it, never reasons about it. (b) The **semantic `number`** (`ADR-0194`, `task:0119`) is an ordinary application column, allocated per D31. | **What it was:** a monotonic integer taken from the engine's sequence facility — concretely `DEFINE SEQUENCE … BATCH <knob> IF NOT EXISTS` created once at migration time, with `<knob>` read from the runtime config store. **Why it changed, three reasons:** (1) the user's rule above puts sequence management outside application code entirely, and issuing DDL to create a sequence and calling `nextval()` *is* index/identity management; (2) the relational set now lands in **MariaDB**, not SurrealDB, so `DEFINE SEQUENCE` is not the available primitive and `AUTO_INCREMENT` is; (3) **`AUTO_INCREMENT` cannot be the ADR number anyway** — it is per-table, not per-project, and InnoDB burns values on rollback, so two projects would interleave into one ADR series. Separating (a) surrogate from (b) semantic is what makes both rules satisfiable at once. **Consequence:** the cross-engine round-trip concern is **dissolved**. `_next_id` (`_shared/storage/client.py:437`, `UPSERT counter:{table} SET val = (val ?? 0) + 1`) stays a **Surreal-only** mechanism for Surreal-only tables; no engine-#2 table ever calls it, and no id ever crosses the engine boundary. `_adr_log_lock` and `_committed_page_max_id` still die — they are replaced by a real row lock (D31), not by a sequence. |
| D6-note | **The SurrealDB sequence measurements are retained as evidence, not as mechanism.** | Kept deliberately so nobody re-runs the experiment. **Measured 2026-07-31 against a throwaway `surrealdb/surrealdb:v3.1.5` container:** `DEFINE SEQUENCE` executes; `sequence::nextval()` increments; the engine default is `BATCH 1000 START 0` (from `INFO FOR DB` — the docs do not state it); sequence state persists across a process restart, but **a restart discards the unconsumed remainder of the reserved batch** (a `BATCH 1000` sequence at value 2 returned **1000** after restart; a `BATCH 1` sequence at 4 returned 5), so ids would have advanced by up to 1000 per daemon restart regardless of writes. `OVERWRITE` does **not** reset the counter. These facts killed an earlier proposal to preserve position via `START`, and they are the reason the sequence path was never going to give readable per-project ADR numbers. None of it is load-bearing any more. |
| D7 | **Never reused. Archive, never hard-delete.** *(unchanged)* | External references (plan filenames, PR titles, `(#93)` in commits) must not silently retarget. Archive-never-delete is the precondition for permanence. **See §11.2 and D37** — a table makes this cheap in a way the markdown page never could, because closed rows cost nothing when the reader `SELECT`s open ones. |
| D8 | **Composite id `(origin, number)`** — **clarified 2026-08-02: the uniqueness and allocation scope is `(project_id, origin)`, not `origin` alone.** | Conclusion unchanged: your task and mine are `(alice, 231)` and `(max, 231)`, distinct by construction; works offline, sync is idempotent, no id ever changes. **What changed:** the old rationale ended *"sequences stay per-origin,"* which is now both mechanically wrong (there are no sequences) and semantically insufficient — `{project}-adr-NNNN` is already a **per-project** series today, so numbering scoped only by origin would merge every project's ADRs into one run. The full uniqueness key is therefore `(project_id, origin, number)`; `(origin, number)` remains the *display* form because `project_id` is implied by context. Reconciled with D31, which is where the allocation predicate lives. |
| D9 | **Gaps are correct.** *(conclusion unchanged; rationale replaced)* | **What it was:** grounded in SurrealDB sequence semantics — `nextval` is not rolled back on failure, and the measured `BATCH` discard burns up to 1000 per restart. **That rationale is now false**: under D31's `MAX(number)+1 … FOR UPDATE` inside a transaction, a rolled-back transaction does **not** burn a number. **Scope, because D6 says the opposite about a different column and the two must not be confused:** the engine-owned surrogate PK (D6a, `AUTO_INCREMENT`) *does* burn on rollback and nothing cares, because nothing reads it. The semantic `number` (D6b) does *not* burn. That asymmetry is precisely why D6 splits them. **Why the decision survives anyway:** a number is an identifier, not a count, and gaps still arise from D7 archival, from hard-deleted rows in other projects' history, and from any future allocation change. Nothing may assume density, contiguity, or `count(*) == max(number)`. Stated explicitly because leaving the old reasoning in place would have taught the next reader a mechanism that no longer exists. |
| D31 | **NEW 2026-08-02 — the semantic number is allocated by `SELECT MAX(number) + 1 … FOR UPDATE` inside the same transaction as the `INSERT`, scoped to `(project_id, origin)`.** Reach-global entities (`agent_prompt`, D3) scope to `origin` alone with the literal string `'global'` as `project_id` — a real value, never `NULL`, per D30's scalar-columns-only rule. | Stays inside the four allowed verbs; no DDL, no sequence, no index management. InnoDB gives a real row/gap lock for the duration of the transaction, so concurrent `adr_add` calls serialise **in the engine**, across processes — which the current `threading.Lock` (§1.5 cause 2) cannot do. **This is the fix for the id half of §1.5**, not a mitigation of it: the number and its row become **one atomic write**, so there is no window in which a number exists without its row, and no second *metadata* artifact to drift from. **It does not make the row and its SurrealDB body page atomic** — see §1.5's bounding paragraph and §4.1. It also deletes both id sources (`_index_max_id`, `_committed_page_max_id`) rather than reconciling them. Cost, accepted: writes to one `(project_id, origin)` series are serialised. At ~195 ADRs total and single-digit writes per day that is free; if it ever is not, the answer is a per-series allocation row, not a sequence. **Portability (D30, ADR-0183):** stated as the capability *"allocate the next value in a series, transactionally, under the same lock as the insert"* — `SELECT … FOR UPDATE` on MariaDB, `SELECT … FOR UPDATE` on PostgreSQL, `BEGIN IMMEDIATE` on SQLite. Not an engine literal. |
| D10 | **No zero-padding anywhere. Display in Crockford base32, default on.** | Ids are integers that grow past any fixed width — a 4-digit form must not exist in code or prose. Base32 gives 4 chars ≈ 1M (vs 10k decimal, 65k hex) and drops ambiguous `I/L/O/U`. **Storage stays an integer, so the encoding is reversible at any time with zero migration.** Cost, accepted: `ADR-0158` renders `ADR-4Y`, `ADR-0182` renders `ADR-5P` — **194** existing references (2026-08-02) in commits, PRs and ADR bodies stay in decimal in immutable history. **No code may regex `\d{4}` or assume any width.** |
| D11 | **Harness reconcile keys on the id prefix** — `[231]` local, `[alice/231]` foreign. Unmatched → **warn**, never silently create. | Claude Code task ids are per-session handles regenerated by the restore-nudge, not identity. **`http.py:923` `_TASK_RE = ^## task:(\d+)` must change** to accept an optional origin segment — forced by D10 regardless. And the nudge (`http.py:946`) already emits `[{tid}]` but never instructs the model to *preserve* it in the `TaskCreate` subject: the template needs that instruction. |
| D12 | **Title capped at 200 chars, reject-on-write.** | Longest current subject ~148. Silent truncation destroys information exactly when someone is being sloppy. |

### 2.3 Project identity

| # | Decision | Rationale |
|---|---|---|
| D13 | **`project_id` = `<owner>/<repo>` from the git remote, host excluded**, normalizing away scheme, SSH alias and `.git`. | `git@github-personal:m-agahi/yadgar.git` and the HTTPS form must yield one key — this repo uses SSH aliases. Excluding the host makes a host migration a no-op. **The note that used to sit here — "slugs remain basename-derived, so the collision persists, out of scope" — was a shrug and is replaced by D32**, which states precisely which surface uses which scheme, why the mapping is total, and which collision surfaces genuinely remain. |
| D14 | **Non-git dirs are `local/<basename>`, never sync, backup-only.** | No stable shared identity exists; don't pretend. Permanently `owner_kind=user`. Accepted edge case: an org named `local` collides — blast radius one machine. **This is also what makes D32's mapping total.** |
| D15 | **No repo file.** Override via `config_set("project.key_override", …, scope="project")`. | Avoids repo burden, fork inheritance, accidental edits. ADR-0163's store is already directory-scoped. **This creates a dependency on the config store — see D33 for the cycle it would otherwise close.** |
| D32 | **NEW 2026-08-02 — three key schemes coexist and each keeps a distinct job. They are not competitors.** | **The three, and the rule:<br>① `project_id` = `<owner>/<repo>` (D13/D14) — the **identity** key. Stamped on every ledger row. The only key that is meaningful on another machine. Cross-instance identity is its whole purpose.<br>② the config store's **absolute filesystem path** (`runtime_config.directory`) — the **lookup** key, and it must stay a path. At call time the caller's only handle *is* a directory; every MCP tool signature takes `directory=`. A store keyed on `project_id` could not be read before `project_id` had been derived, and deriving it requires reading the store (D15's override) — that is the cycle, and keying the store by path breaks it by construction.<br>③ `body_slug`, basename-derived (`adr_index.py:46,51,61`) — the **addressing** key for wiki pages.<br>**Totality of ①:** every directory resolves — git remote present → `<owner>/<repo>`; absent → `local/<basename>` (D14). No third case, no failure mode, so `path → project_id` is a total function.<br>**Non-injective direction, stated because it is the ambiguous case:** two clones of one repo on one machine share a `project_id` but have distinct paths. That is correct for a lookup key (per-checkout settings) and correct for an identity key (both are the same project). Rows from both clones merge, which is the intent.<br>**What genuinely does NOT resolve — ③.** Two repos named `infrastructure` produce the same `body_slug`. Ordinary reads are safe: `wiki_slug_idx` (`migrations.py:1390`) is **non-unique** and resolution is (slug, `directory_context`, branch). **Two real collision surfaces remain:** `get_wiki_page_by_slug` (`_shared/storage/wiki.py:380`) is a **slug-only, DB-wide** lookup, and `wiki_bookmark_slug_idx` (`migrations.py:190,1404`) is **UNIQUE on slug**, so bookmarking the same-named page from two repos collides.<br>**Decision: ③ is OUT OF SCOPE for this train and is a named blocker on task 0095.** Re-slugging 194 ADR pages plus their `[[slug]]` crossrefs is a wiki-corpus migration, not a ledger one, and it must not ride along. Stated as a known live limitation, not as "fixed." |
| D16 | **A derived key is STORED on the row at write time, never recomputed at read time.** | **Rationale corrected post-audit.** An earlier draft blamed live default-branch resolution (`server_helpers.py:326`) for the codeberg→github task-list break. **That is refuted**: `_default_branch_for_root` has exactly one non-test caller (`server_helpers.py:386`, the memory-write worktree path), and the live `yadgar-task-list` row has `branch: null`, so branch resolution never participated. The documented cause is commit `eefa176e` (2026-07-15) — the v5.42.3 `missing_branch` hard-reject made canonical writes impossible, so the mirror never persisted; fixed by `wiki_write_task_list`. The decision stands on its own merit (a recomputed key silently re-points existing rows); the incident is *not* evidence for it. |
| D33 | **NEW 2026-08-02 — the spine's dependency on the config store has two limbs. One is eliminated; the other is an explicit ordering constraint. Neither is left implicit.** | **The cycle, stated plainly:** D6 used to read `ledger.sequence_batch` from the config store **at migration time**, and D15 stores `project.key_override` **in** the config store. If the knob store and the spine fold together (task 0119) into engine #2, the spine would consume the very thing it is merging into.<br>**Limb 1 — migration-time read: ELIMINATED.** D6's rewrite deletes `ledger.sequence_batch` outright. There is no sequence, so there is no batch knob, so **the spine's migration reads nothing from the config store**. This is not a mitigation; the dependency no longer exists. It is also why D6's rewrite had to land before this one could be resolved.<br>**Limb 2 — write-time read: ORDERING CONSTRAINT.** D15's `project.key_override` is read when a row is **written** (to stamp `project_id`, D16), never at migration time. Two conditions make that safe and **both are binding**:<br>  (a) **Schema ordering.** `runtime_config` must be created **before** the ledger tables in whichever engine's ordered migration list holds them. Under D34 that is a single Alembic revision chain, so this is an ordinary `down_revision` edge, not a cross-system handshake.<br>  (b) **The read is lazy and NON-FATAL.** A config-store miss, an empty store, or an unreachable store **falls back to the derived key (D13/D14) and never fails a spine write.** The override is a convenience, not a precondition. A spine write that can be blocked by the settings store is a worse defect than the one D15 solves.<br>**Ordering rule, one line:** *migration-time: spine reads nothing from the config store. Write-time: spine may read it, must tolerate its absence.* |

**Task 0095 is the blocking decision, and it is time-boxed.** The project-key scheme above is
cheap to change **only while `runtime_config` is empty** — verified **0 rows, 2026-08-02**
(`config_list()` → `[]`). Task **0035** seeds ~200 config rows; the moment it does, changing the
key becomes a data migration over every row. **Decide 0095 before 0035 seeds, or pay for it
later.** This is the same window `split-store-engine-decision-2026-08-02.md` §1.4 flags, and it is
open right now.

### 2.4 Tenancy (design now, build later)

| # | Decision | Rationale |
|---|---|---|
| D17 | **Two orthogonal axes** — `owner_kind` (user\|team\|org) · `owner_id` · `reach` (project\|global) · `project_id`. **The COLUMNS are deferred to the tenancy task; Car A ships a scope-filter HOOK that is a no-op today.** | One enum cannot express six cells (user/team/org × project/global), so the shape is decided now even though nothing is built. **Revised 2026-07-31 after an independent audit challenged "free and inert" and the fact-check refuted BOTH positions.** Facts: the tables are SCHEMALESS (`migrations.py:71`) and the migration mechanism is idempotent `DEFINE FIELD IF NOT EXISTS` (`migrations.py:81`), so adding a field later is ONE line of DDL — there is no `ALTER TABLE`, no table rewrite, and rows lacking the field stay valid. Seed size is ~400 rows total, so even a backfill is milliseconds. So the earlier claim that retrofitting is "a migration on every row" was wrong, and the counter-claim that the columns cost anything at insert/select was also wrong. **The only real cost is code** — threading the columns through the mixin's signatures and query builders — and that cost is identical whenever it is paid. The genuine risk is narrower: a mixin written with no tenancy concept bakes single-tenant query SHAPES in, and rewriting query builders later is the expensive part, not the DDL. A no-op filter hook captures exactly that risk at lower cost than four unread columns. **Amended 2026-08-02 — one supporting fact is now false, the conclusion is not.** The 2026-07-31 rationale leaned on the tables being SurrealDB SCHEMALESS with idempotent `DEFINE FIELD IF NOT EXISTS` (`migrations.py:71,81`), i.e. *"there is no `ALTER TABLE`."* Under MariaDB (D34) there **is** an `ALTER TABLE`, and rows do not stay valid without the field. But at ~400 rows total an `ALTER TABLE … ADD COLUMN` with a default is milliseconds and one Alembic revision, so the cost conclusion is unchanged and **the real risk is still the same one**: a mixin written with no tenancy concept bakes single-tenant query shapes in. Keep the no-op filter hook; do not add the columns. |
| D18 | **Sync selectivity IS the owner axis.** `user` → nowhere · `team` → team · `org` → org | "Should this reach my team" and "who owns this" are the same question. Personal backup is a separate independent flag. |
| D19 | **Explicit `PERMISSIONS` on every `DEFINE TABLE`.** **Amended 2026-08-02: the literal does not port** — ledger tables are MariaDB (D34), where the equivalent is a dedicated least-privilege user with `GRANT`s scoped to the ledger schema. The *intent* survives verbatim: the connection is not omnipotent by default. See §3. | SurrealDB advisory GHSA-x5fr-7hhj-34j3: defaults were FULL. **Defense in depth only** — the daemon opens one connection with system credentials (`storage/__init__.py:263-273`), so table permissions do not constrain it and there is no per-user `$auth` today. Not "AAA need not be hand-rolled." |
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
| D34 | **NEW 2026-08-02 — engine-#2 schema changes are Alembic revisions, not entries in `migrations.py`'s list.** The two migration systems are **separate and must not be merged**: `_MigrationsMixin._run_migrations` (`_shared/storage/migrations.py:1175`) keeps owning SurrealDB; Alembic owns MariaDB, with its own version table and its own ordered chain. **Rationale:** MariaDB is MySQL-wire, so `mysql+asyncmy://` is a first-class SQLAlchemy 2.0 async dialect and **Alembic works out of the box** — which is why the engine decision **moots task 0051 (surrealmigrate fork) and collapses most of 0048** (`split-store-engine-decision-2026-08-02.md` §4.4/§4.5). Not merging them is deliberate: one ordered list spanning two engines has no meaningful "version N" and would deadlock the first time a revision needed both. **Two constraints the existing mechanism imposes on the new one, both verified in source:** (1) `_run_migrations` returns immediately when `self._db_url` is falsy — **server mode only**, embedded mode gets nothing (`migrations.py:1181-1189`); the Alembic chain must be invoked from the same gate or the two silently diverge in test/dev. (2) `_run_migrations` is called from `StorageEngine.__init__` under an `fcntl.flock` on `STATE_DIR/.migration.lock` (`migrations.py:1191-1198`) held for the whole run. **A process-wide lock inside a constructor is not a place to backfill 195 pages** — which is why D35 makes the seed a separate operation that the migration merely enables. |
| D30 | **New ledger tables are born portable.** Scalar columns only — no record links, no `RELATE`, no nested objects. Identity, authorization and batching expressed as **capabilities**, not engine literals. **Amended 2026-08-02:** the identity capability is no longer *"a sequence"* (`DEFINE SEQUENCE` ↔ `CREATE SEQUENCE`) but *"allocate the next value in a series, transactionally, under the same lock as the insert"* (D31) — a capability every SQL engine has and SurrealDB does not, which is a further reason the relational set is the part that moves. Authorization: Surreal `PERMISSIONS`+`$auth` ↔ MariaDB least-privilege `GRANT` ↔ Postgres RLS. All row access through `_LedgerMixin`, which **is** the engine seam. **Sync is an outbox table, never an engine changefeed** — a changefeed would have to be rebuilt on a swap. **The legacy corpus is NOT portable and this train does not make it so**: memories, embeddings, entities, relationships and engrams use Surreal-specific vector indexes and graph edges. Retrofit is task #0098; vector search is its hard problem. |

---

## 3. Schema

Tables live in **MariaDB** (D34). The previous revision specified SurrealDB SCHEMALESS tables with
idempotent `DEFINE FIELD IF NOT EXISTS`; under the split that becomes ordinary DDL in an Alembic
revision. **D19's explicit `PERMISSIONS` is a SurrealDB construct and does not port** — the
equivalent is a dedicated MariaDB user with least-privilege grants on the ledger schema. The
*intent* of D19 (defense in depth; the connection is not omnipotent by default) survives; the
literal does not. See `split-store-engine-decision-2026-08-02.md` §5.3 for where the engine runs.

```
# spine (all three)
<pk>                          engine-native AUTO_INCREMENT, never touched   (D6a)
origin · number               semantic identity, MAX+1 FOR UPDATE           (D6b/D8/D31)
owner_kind · owner_id         user|team|org                                 (D17)
reach · project_id            project|global · 'global' sentinel for reach=global (D31)
title                         <= 200, reject                                (D12)
status                        per-entity enum
body_slug                     wiki page IN SURREALDB, ALWAYS present        (D4)
created_at · modified_at

  UNIQUE (project_id, origin, number)                                       (D8)

task          { active_form · state · plan_path · blocked_by[] · blocks[] }
              pending | in_progress | completed | archived
              state: open | planned | spike | needs_decision | built_unverified  (D36)
              blocked_by / blocks: real columns, scalar arrays                   (D39)

adr           { date · subsystem · tier · supersedes[] · superseded_by[] }
              open | accepted | superseded | rejected | deprecated | archived
              body_slug MANDATORY non-null

agent_prompt  { kind: pattern|discipline|contract · purpose · composes[] · uses }
              active | archived · reach always global · project_id = 'global'
              number assigned and ignored                                        (D3)
```

`composes[]` replaces the `## Composes` section parsed by `_COMPOSES_SECTION_RE`.
`uses` replaces the two-location counter and its `%10` throttle — **subject to D40**.

**Scalar arrays and D30.** `blocked_by[]`, `blocks[]`, `supersedes[]`, `superseded_by[]` and
`composes[]` are arrays of scalars, which ADR-0183 permits (it forbids record links, `RELATE`
and nested objects). On MariaDB they are a `JSON` column of a flat string array or a child join
table; either satisfies D30 because neither is an engine-specific reference type. Pick one in
Car A and use it uniformly — do not mix.

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

**Amended 2026-08-02.** Ledger rows live in **MariaDB** (D34), so `StorageEngine.batch_writes`
(`client.py:875-900`) is **not** the transaction primitive for them — an InnoDB transaction is,
and it is a stronger one. D31's `MAX+1 FOR UPDATE` depends on that: a real row lock held to
commit, which the Surreal path could not give.

The three Surreal clauses below are retained because **Car K's sweep and the supersede retype
still touch wiki pages**, which stay in SurrealDB (D4):

- atomic only **within a chunk** (500 statements / 1 MB); a failure in one chunk does not roll
  back earlier chunks. Supersede batches are tiny — fine, but state it.
- **embedded mode has no transaction at all** — statements run per-statement via `_q`. The nightly
  consolidation cycle runs embedded, so Car K's sweep is not atomic there.
- ~~`sequence::nextval` is not rolled back on failure, so a rolled-back batch burns a number.~~
  **Void as of D6** — there is no sequence. Under D31 a rolled-back transaction burns nothing.
  D9 still holds, for the reasons D9 now gives.

**A supersede that touches both engines is two writes to two systems and is NOT atomic** — the
row's status flip (MariaDB) and the page retype (SurrealDB) can diverge. This is exactly the
orphaned-row class ADR-0183 predicted and `split-store-engine-decision-2026-08-02.md` §5.2
transposes to task/ADR rows. **Car G must order it row-last** (retype the page first, flip the
row second) so a crash leaves a retyped page with a stale row — recoverable and detectable —
rather than a flipped row pointing at a page still visible to recall. `check_invariants` gains
the cross-engine check.

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

| # | Decision | Rationale |
|---|---|---|
| D40 | **NEW 2026-08-02 — the `uses` column ships WITH a reader in the same car, or it does not ship.** Minimum acceptable reader: `agent_prompt_list` returns `uses` and sorts by it descending by default, so pattern selection is informed by actual use. | §1.4 established that today's counter is **write-only** — `get_prompt_usage_counts` (`_shared/storage/wiki.py:1067`) is called by nothing but its own incrementer. Replacing two write-only stores with one write-only column is a smaller version of the same defect and would look like progress. It is also the specific thing that makes the column earn its place: the counter's only stated purpose is knowing which prompts are dead, which is task #0015's input (archive under D3/D7). **If no reader is built, delete the counter entirely instead of migrating it** — that is a strictly better outcome than a tidier dead field. |

---

## 6. Migration and rollback

**Rewritten 2026-08-02.** The previous revision listed a build order and one sentence of
rollback (*"stop reading the tables"*). That is not a cutover design — it did not say how ~195
ADR pages and 6 task-list pages become rows, whether writes are dual, how the result is
verified, what rollback means *after* a partial run, or what happens to the old pages. Those are
D35 and §6.3.

### 6.1 Build order (unchanged in substance)

1. `_LedgerMixin` + Alembic chain + tables + chokepoint guard (D34; no sequences — D6).
2. Backend ops + cache.
3. **Car C lands before any seed** — it gates correctness (§1.4).
4. Task tools; seed; rewire SessionStart + stop-hook (incl. the D11 prefix instruction).
5. ADR tools re-pointed; seed from the **pages**, not the index (D35b).
6. **Retype the 12 existing superseded pages.** Acceptance: zero pages with
   `page_type='adr' AND directory_context='/home/max/git/yadgar'` carry an
   `adr-status:superseded` tag. **Scope the assertion to yadgar** — `page_type='adr'` spans
   8 projects (yadgar's own share is the 195 rows of §1.5; other projects are untouched by this
   train and their pages must not be retyped).
7. agent_prompt table + tools; delete TOC machinery (and with it the §1.4 TOC-leak).
8. Rollups; `tier`/`subsystem`.
9. Nightly archive sweep, policy-dispatched.
10. **Verification gate** (D35c). Old pages stay in place, unread, until it passes.

### 6.2 D35 — the cutover

| # | Decision | Rationale |
|---|---|---|
| D35a | **The migration creates schema. The SEED is a separate one-shot operation the migration merely enables — it is NOT a migration step.** Ship it as an explicit admin op / CLI command, run once by the operator, idempotent (re-running converges, never duplicates). | Forced by the mechanism, not preference. `_run_migrations` is called from `StorageEngine.__init__` while holding an `fcntl.flock` for its entire duration (`migrations.py:1191-1198`), and returns immediately in embedded mode (`migrations.py:1181-1189`). Reading 195 wiki pages inside a constructor under a process-wide lock would stall every daemon start, and would silently never run in embedded mode — where the nightly consolidation cycle lives. Alembic (D34) has the same shape and the same reason. |
| D35b | **ONE-SHOT, not dual-write. Source of truth for the ADR seed is the per-ADR PAGES, not the index. Cutover is a single atomic flip of the read path.** | Dual-write means two writers, two failure modes and a reconciliation job for ~250 rows that no external system reads — the drift class of §1.5, reintroduced deliberately at the exact moment the plan exists to remove it. One-shot is defensible here **only because** the corpus is tiny, single-writer, and has no live external consumer. **Pages over index** because the page is the ID-bearing artifact — `_committed_page_max_id`'s own docstring (`adr_index.py:107-118`) states the index "may lag" and the committed page carries the ID — and §1.5 proves the index is already missing a row (`ADR-0124`). Seeding from `parse_index_rows` (the previous plan's instruction) would have **silently dropped ADR-0124**. Metadata absent from the page body is recovered from the index row where one exists; where none exists it is filled from the page and flagged. |
| D35c | **Verification gate: EXACT equality on a stated predicate, with the three known counts reconciled BEFORE cutover, never absorbed silently.** | `split-store-engine-decision-2026-08-02.md` §2 records why: on 2026-06-16 a vacuum bug destroyed **3,622 memories** because a partial restore (1,484 of 3,622) passed a `>=` check. **`>=` is not a gate.** Naive equality also fails here, because the three counts legitimately differ (§1.5): 193 index rows / 194 pages / 195 `page_type='adr'` rows. The predicate must therefore be stated against the right denominator, and the residue explained rather than tolerated. |

**The acceptance predicate, written out:**

```
let PAGES = { wiki_page : page_type='adr'
                          AND directory_context=<yadgar>
                          AND slug MATCHES '^yadgar-adr-[0-9]+$' }
             # per-ADR pages only. The INDEX page is excluded by this slug predicate,
             # and that exclusion is the entire 194 -> 195 gap.

COUNT(adr rows WHERE project_id=yadgar) == COUNT(PAGES)              exact equality, never >=
AND  { number } == { number parsed from PAGES slugs }                SET equality — the real check
AND  every seeded row's body_slug resolves to an existing wiki page  (D4)
AND  the 193 -> 194 residue is EXPLAINED and reconciled: ADR-0124 has a page and NO index row;
     its row is seeded from the page. Any OTHER index/page mismatch at seed time is a
     STOP condition, not a warning.
```

**Do not hardcode 194.** The counts in §1.5 are a 2026-08-02 snapshot, and the series is
contiguous `0001…0194` *today* only by accident — D9 forbids assuming density or contiguity, and
one `adr_add` between now and cutover invalidates the literal. The gate must compare the table
against the **live** page set; a gate that fails because a new ADR was written is a gate that
gets disabled. Note also `[0-9]+` not `[0-9]{4}` in the slug predicate, per D10's prohibition on
assuming a four-digit width.

Task-list seed: 6 `page_type='task_list'` pages DB-wide; per-page `## task:<id>` section count
must equal seeded rows per project, same exact-equality rule. Agent-prompt seed: 57
`agent-prompt-*` + 6 `agent-discipline-*` = **63** pages.

### 6.3 Rollback

**Before cutover** (tables written, read path still on pages): rollback is `DROP` the ledger
tables — nothing read them and nothing wrote to the pages differently. Free.

**After a partial seed** (the real case): the seed is **idempotent and re-runnable**, keyed on
`(project_id, origin, number)` (D8). A partial run is resumed, not undone. The tables are
**append-only during seed** — the seed never deletes or mutates a wiki page, so a half-seeded
table cannot damage the source corpus. If resume is not wanted, `DELETE FROM <table> WHERE
project_id = ?` and re-run; the source is untouched either way.

**After cutover** (read path flipped): rollback is flipping the read path back, which works
**only while the old pages still exist** — hence D35d below. Any rows written after cutover are
lost on rollback; at single-digit writes per day the operator re-enters them. Do not build a
reverse-sync.

| # | Decision | Rationale |
|---|---|---|
| D35d | **The old pages are KEPT-AND-IGNORED, not deleted, for one full release cycle after cutover. They are marked, not removed.** Mark by adding a `superseded-by-ledger` tag; the `{project}-adr-index`, `{project}-task-list` and `agent-prompt-toc` pages additionally get their content replaced by a one-line pointer, preserving the slug. Delete only after the gate has held for a cycle **and** the ADR bodies are confirmed unaffected. | Deleting at cutover destroys the only rollback path at exactly the moment rollback is most likely. Keeping them live is worse — two authorities, which is §1.5's whole failure. Kept-and-ignored is the only option that preserves rollback without preserving drift, and the marker makes "ignored" checkable rather than aspirational. **The per-ADR BODY pages are never deleted at all** (D4) — this clause concerns only the three *index* pages. Preserving the slugs matters: `[[slug]]` crossrefs point at them (52 crossref rows touch `-adr-`, per `split-store-engine-decision-2026-08-02.md` §3), so deleting the slug outright breaks reachability that the pointer preserves. |

---

## 7. Cars

| Car | Scope | Depends on |
|---|---|---|
| A | `_LedgerMixin` + **Alembic revision chain (D34; NOT `migrations.py`'s list)** + tables + **`MAX+1 FOR UPDATE` allocation scoped `(project_id, origin)` (D31) — no sequence, no DDL at runtime (D6)** + least-privilege grants in place of `PERMISSIONS` (§3, D19) + **a no-op scope-filter hook** (not tenancy columns — D17) + **new AST guard `scripts/check_ledger_chokepoint.py` with an allowlist for pre-existing violations**. **Includes a RED test that two concurrent allocations on one `(project_id, origin)` never collide** — that is D31's entire claim and the current `threading.Lock` fails it across processes. | — |
| B | backend ops + cache | A |
| C1 | **3a** — tag-override matches the page type's own opt-in tag | — |
| C2 | **3b** — implement `downweight` | — |
| C3 | **3c** — redesign + reimplement the identity gate (D21) | — |
| D | task tools | B, C |
| E | task seed + SessionStart/stop-hook rewire + `http.py:923` matcher + D11 prefix instruction | D |
| F | ADR tools re-pointed — **acceptance: a characterization test pins `adr_list`/`adr_get` return shapes pre-migration and asserts them green post-migration** (live consumers: `project.py:1889` `r["adr_id"]`, `adr_render.py:181`, 7 test refs) | B, C |
| G | ADR seed **(one-shot admin op, seeded from PAGES not the index — D35a/D35b)** + **add `adr_superseded` to `CANONICAL_PAGE_TYPES` + build the retype mutator** + retype the **12** + delete parser/serializer/lock + **re-point `project_brief`: `_build_adr_log` (`project.py:1880,1889`) and `_get_adr_log_updated_at` (`project.py:1378-1381`, a second hardcoded slug site)** + **fix the dead `{project}-adr-log` read at `stop_checkpoint_prompt.md:26-33`** (that page no longer exists — every checkpoint runs a dead read) | F |
| H | `tier` + `subsystem` + rollups | G |
| I | `agent_prompt` table + `list`/`get` + delete TOC machinery + **re-point `_build_agent_prompt_toc` (`project.py:1898-1921`)** | B |
| J | `mutability` policy field + per-page override + `wiki_set_mutability`, **enforced at `storage/wiki.py:215`** and covering `admin_exec/wiki.py:139 wiki_update` | A |
| K | nightly archive sweep, policy-dispatched | E, G, I |

D/E ∥ F/G ∥ I after B. **C gates D and F.** J depends only on A and can land early.

**Ordering constraint from D33(a):** `runtime_config` must exist in engine #2 before any ledger
table — an ordinary `down_revision` edge in Car A's Alembic chain if the knob store moves in the
same train (task 0119), and a no-op if it does not.

**Every seed step (E, G, I) is a separate one-shot operation, not a migration step (D35a).** Each
ships with its verification gate (D35c) in the same car; a seed whose gate is deferred to a later
car ships an unguarded write.

TDD throughout, RED-verified per car. Gates: ruff, import-linter, I32, I33, `check_versions`.

---

## 8. Expected impact

**Recomputed 2026-08-02 from measured inputs. The token claims below are ~4× smaller than the
previous revision's, and that is a correction, not a downgrade of the plan** — see the two
paragraphs after the table for why the case does not rest on this number.

| Metric | Today (measured 2026-08-02) | After |
|---|---|---|
| **Full task-list read, mandated every stop-hook checkpoint** | 16,060 chars ≈ **~4k tok** | ~1k tok |
| `adr_list` | **32,163**-char read + regex parse | indexed query |
| `adr_add` | index read + **full `wiki_list(limit=10000)` slug scan** + process-local lock + **two non-atomic writes** | one `INSERT`, number allocated in the same transaction (D31) |
| `project_brief` | re-parses the same 32,163 chars again (`project.py:1866`) | indexed query |
| prompt usage counter | 2 stores + `%10` throttle + page version churn, **and nothing reads it** (§1.4) | one `UPDATE`, exact — **only if D40's reader ships** |
| "what governs `vacuum`?" | scan **194** entries | one rollup page |
| body pages | **194** ADR + **63** prompt/discipline | unchanged, in SurrealDB, versioning and embeddings intact (D4) |
| Superseded ADR in recall | **yes (live defect)** | no |
| `agent-prompt-toc` recall-visible while its corpus is excluded | **yes (live defect, §1.4)** | no — page deleted |
| ADR index drift | **PRESENT — `ADR-0124` has a page and no index row** (§1.5) | none — the index *is* the query, and the number is written with the row (D31) |
| Cross-instance id collision | n/a | none — `(project_id, origin, number)` (D8/D31) |

**Restated honestly: the token argument is now the WEAKEST argument in this plan, and the
correctness argument is the strongest.** The previous revision led with *"~24k tok, paid TWICE
per cycle"*. At 16,060 chars the checkpoint read is ~4k tokens, so the twice-per-cycle figure is
~8k, not ~48k. A plan justified primarily on that saving would not clear its own build cost. It
does not need to: §1.5 is a **live data-integrity defect** in the id path, and §1.4 lists three
more. Lead with those.

**Two things about the task-list number that must not be lost.**

*First — the 16 KB is a manually maintained figure, not a steady state.* It reached 114 KB on
2026-08-02 and came down only because the user deleted 15 closed rows and 9 shipped cars by
hand (§11.2). The page has no mechanism that keeps it small; D37 is that mechanism. Quoting
16 KB as the post-fix baseline would be measuring the workaround, not the system.

*Second — the session-start cost is real and is NOT a page read.* Retained from the 2026-07-31
correction, because it is the part of the argument that survives re-measurement intact: the
SessionStart hook POSTs without `mode` (so `http.py:1080` defaults to `catalog`, and no
`project_brief` mode inlines the task-list page), and the only task-list touch is
`_task_list_restore_nudge` (`http.py:864-981`), capped at 12 lines (`_CAP = 12`). Those facts are
true, and an earlier revision wrongly concluded from them that session start was free. **The nudge
is an INSTRUCTION, and its execution is the cost:** *"ACTION REQUIRED — restore your task list
BEFORE any other work. N open task(s)… Call TaskCreate for EACH one now… Full descriptions:
`wiki_read("{project}-task-list")`"*. Complying means a full-page read **plus N `TaskCreate` calls
plus the harness re-injecting every task as a system-reminder** — which is task #0080's
itemisation and matches the observed 8%→18% context jump on a single restore. **That cost scales
with N (open tasks), not with page size**, so shrinking the page from 70 KB to 16 KB barely
touched it. D37 (open-only reads) and D11 (prefix reconciliation) attack the N term; page size
was never the dominant one.

## 9. Out of scope — filed separately

**#0095** project identity (near-term — a live bug) · **#0096** encryption posture (blocks sync) ·
**#0097** AAA/sync design (outbox decided per D30) · **#0098** DB driver seam / engine portability ·
**#0043** anchor metadata sprawl · **#0015** agent-prompt prune (becomes *archive* under D3/D7)

**Changed by the engine decision, 2026-08-02:** **#0051** (surrealmigrate fork) is **mooted** and
most of **#0048** collapses into "adopt Alembic", because MariaDB is MySQL-wire (D34;
`split-store-engine-decision-2026-08-02.md` §4.5). **#0098** is rescoped from "make SurrealDB
swappable" to "let the relational set address engine #2" — see that doc §7, not restated here.
**#0119** (knob/spine fold) interacts with this plan through D33 and is listed in §12.

## 10. Open build questions

- **Rollup regeneration trigger** — on write (fresh, one small page write) vs nightly (cheaper, stale between runs).
- **`subsystem` vocabulary** — free-form drifts (`vacuum`/`Vacuum`/`db-vacuum`); a controlled list needs a home.
*(A third item — sequence-knob semantics — was resolved 2026-07-31 by container measurement and
then made MOOT on 2026-08-02 when D6 removed sequences entirely. The measurements are retained
in **D6-note** as evidence, not as mechanism; nothing in the build depends on them now.)*

---

## 11. Field note (2026-08-02) — `state` is orthogonal to `status`, and D7 collides with practice

Added from live use of the markdown task list, for refinement before Car A. Two
things surfaced that the current schema does not express.

### 11.1 `status` cannot distinguish "planned" from "shipped"

The user asked why tasks 0115, 0116, 0117 and 0122 were still open when they
"looked tackled". All four were legitimately open — but for four *different*
reasons, and `status: pending` flattened them into one:

| task | why it was open | what `pending` implied |
|---|---|---|
| 0115 | 693-line plan doc written, **no code** | indistinguishable from untouched |
| 0116 | 924-line plan doc written, **no code**, deliberately not a train car | same |
| 0117 | spike, **never started** | same |
| 0122 | needs a **decision**, nothing to build until one is made | same |

The confusion was reasonable: substantial artifacts existed for two of them. A
`pending → in_progress → completed` axis measures *motion*; it says nothing about
*what kind of thing is missing*. The interim fix was a subject prefix —
`[PLANNED]` design doc but no code · `[SPIKE]` measurement not started ·
`[DECIDE]` blocked on a decision · `[VERIFY]` built but unproven live — because
the harness renders only `[status] [id] subject` in its injected list, so
metadata alone is invisible at restore.

**For the spine:** a second dimension, e.g. `state ∈ {open, planned, spike,
needs_decision, built_unverified}`, orthogonal to `status`. Two properties matter
more than the exact enum:

- **`built_unverified` is not `completed`.** Car 0111's acceptance case (backend
  stopped, core stays `active` with MainPID unchanged) cannot be proven by any
  artifact in this repo — it needs a fresh VM. Marking it `completed` on merge
  would assert something unverified; leaving it `in_progress` implies work
  remains. Neither is true. Same shape as tasks 0022/0023 (hooks built, never
  live-tested) and 0063 (verifiable only in a browser).
- **`planned` should carry its artifact.** A `plan_path` column beats prose: the
  question "is there a design for this?" is then answerable by query rather than
  by reading the description. Note the metadata already written on these rows
  uses exactly `{"state": ..., "plan": ...}`.

### 11.2 D7 (archive, never hard-delete) collides with the token cost of the page

D7's rationale is sound for identifiers: external references — plan filenames, PR
titles, `(#93)` in commit messages — must not silently retarget.

But on 2026-08-02 the markdown page hit **114 KB**, read *in full* at every
session start, and the user's instruction was explicit: delete completed tasks
outright to cut restore cost. Compaction to ~15 KB came from deleting 15 closed
rows plus 9 shipped cars, and reducing survivors to pointers. That is a **~87%
cut on a per-session tax** — task 0080's own subject.

These are not actually in conflict once separated:

- **Identifier permanence** (D6/D7's real concern) requires that a number is
  never *reused*. A sequence guarantees that whether or not the row survives.
- **Row retention** is a storage/read cost question, and a table answers it far
  better than markdown: closed rows can stay in the table at zero restore cost,
  because the reader `SELECT`s open rows instead of parsing the whole document.

**So the spine mostly dissolves this problem** — which is an argument *for* Car A,
worth stating in §8 (Expected impact) rather than leaving implicit. The residual
question for refinement: does an *archived* row keep its body wiki page (D4), or
does the page get deleted while the row persists? Today's compaction deleted both.
If bodies persist for every closed task forever, the wiki grows unbounded in a
corpus that `recall` already has to downweight (D22).

**Answered 2026-08-02 — D38: the body is retained and excluded from recall.** The
unbounded-growth worry is real but is answered by **D37** (open-only reads), not by
deletion: once the reader `SELECT`s open rows and recall excludes closed bodies, a
retained body costs nothing on either path. The 16 KB figure this section reports is
also not a steady state — see §8, it was reached by hand and has no mechanism holding
it there. D37 is that mechanism.

### 11.3 Four questions for Car A — ANSWERED 2026-08-02

Appended in commit `a5b27eca` and left unresolved. Each now has a decision. **Q2 and Q3 are one
question asked twice** and are answered together; Q1 and Q4 are independent.

| # | Question | Decision | Rationale (one line) |
|---|---|---|---|
| D36 | **Q1 — is `state` stored or derived?** | **STORED**, as a real column, enum `open \| planned \| spike \| needs_decision \| built_unverified`, orthogonal to `status`. | Derivation cannot express `built_unverified` — the distinction is *"the artifact exists but nobody has run it on a fresh VM"*, which no column combination in the row implies; and a derived value cannot be corrected by hand when the inference is wrong. `plan_path` is stored alongside it so *"is there a design for this?"* is a query rather than a read of the description (§11.1). |
| D37 | **Q2 — does `task_list` default to open-only?** | **YES.** `task_list()` defaults to `status IN (pending, in_progress)`; closed and archived rows require an explicit filter. | This is the mechanism that makes D7 (archive-never-delete) survivable — §11.2's whole point. The markdown page had no such option, so every reader paid for every row, which is why 114 KB had to be hand-culled to 16 KB. A default of "everything" would rebuild the exact tax the spine exists to remove, and defaults are what get used. |
| D38 | **Q3 — on archive, does the body page persist?** | **YES — retained, never deleted, and excluded from recall** by the same status-driven `recall_disposition` as D22, plus a `page_type` retype on the D23 model. | Deleting the body contradicts D4 (bodies are the versioned artifact) and destroys the reasoning behind a decision that external references still point at (D7). The unbounded-growth worry §11.2 raised **is answered by D37, not by deletion**: a retained body costs nothing at read time once the reader `SELECT`s open rows, and costs nothing at recall time once it is excluded. Growth in *stored* bytes is not the problem the plan is solving — growth in *per-session context* is. Today's compaction deleted both row and page; that was the right call for markdown and is the wrong call for a table. |
| D39 | **Q4 — should `blocked_by` be a real column?** | **YES** — `blocked_by[]` and `blocks[]` both, as scalar arrays (§3). | Dependency chains are load-bearing and currently live in prose, so *"what is blocking 0047?"* cannot be answered without reading every description. D30 permits scalar arrays. The one real cost is referential integrity — nothing enforces that a `blocked_by` entry exists — so `check_invariants` gains a dangling-reference check; the alternative (a join table) buys enforcement the codebase enforces nowhere else today (`split-store-engine-decision-2026-08-02.md` §3: no foreign keys are enforced anywhere). |

*(Q4's original text drifted into task-0095 timing. That material is not about `blocked_by`; it
now lives where it belongs, under **D33** in §2.3.)*

---

## 12. Unresolved — needs a user decision before Car A

Everything else in this plan is decided. These are not.

1. **Does the knob store (task 0119) move to engine #2 in this train, or stay in SurrealDB?**
   D33 makes the spine safe either way, but the answer determines whether Car A's Alembic chain
   needs a `runtime_config` revision ahead of the ledger tables, or none at all.
2. **Task 0095 — the project-key scheme — is blocking and time-boxed.** `runtime_config` is
   empty **today** (0 rows, verified 2026-08-02); the re-key is free only until task 0035 seeds
   its first row. D32 states which scheme owns which surface, but 0095 is where the *value* is
   ratified.
3. **`body_slug` remains basename-derived (D32 ③), and two DB-wide collision surfaces persist** —
   `get_wiki_page_by_slug` (`_shared/storage/wiki.py:380`) and the UNIQUE
   `wiki_bookmark_slug_idx` (`migrations.py:190,1404`). This train deliberately does **not** fix
   them. Confirm that deferral, or accept a wiki-corpus re-slug as separate scope.
4. **D40 — if no reader for `uses` is going to be built, say so and the counter is deleted
   rather than migrated.** Shipping the column without a reader is the one outcome ruled out.
5. **Two §10 items are still open** (rollup regeneration trigger; `subsystem` vocabulary) and are
   unaffected by this refinement.

Deferred to `split-store-engine-decision-2026-08-02.md` §8, not restated here: the cross-engine
backup quiesce point, MariaDB's unverified idle RSS, `asyncmy`'s unverified license, and whether
the FTS hypothesis is tested inside SurrealDB first.
