# PLAN — Wiki-repo BUILT-IN: full buildout (inert → live → discoverable → fn-parity)

> **STATUS: IN REVIEW — DO NOT BUILD.** Awaiting user decisions on §4 (open decisions). This plan supersedes the open portion of `wiki-repo-builtin.md`. Last review pass: 2026-06-29.

**Date:** 2026-06-29
**Status:** IN REVIEW — DO NOT BUILD (see banner above). Supersedes the open portion of `docs/plans/wiki-repo-builtin.md`
(which shipped #36: module-level generator + migration 024 hash/source_file columns +
store-bridge schema + the negative `fn:`/`api:` exclusion fix). This doc is the
end-to-end completion plan; it does **not** re-derive #36, it builds on it.
**Theme:** wiki / repo-docs / staleness / discoverability / external-skill removal
**Advisor:** vetted (positions below incorporate a stronger-model review — several of
the original 7 asks collapse; that collapse is the point).

---

## 0. Executive summary + the inert-seam reality

The built-in repo-wiki is ~90% plumbed and **completely inert**. One missing write
seam kills the whole subsystem:

- `repo_wiki/scanner.py` (`scan_repo`/`scan_python_module`, AST) and
  `repo_wiki/generator.py` (`generate_module_page`) **work**. The generator stamps
  `hash = SHA256(file_bytes)` (`generator.py:109`), `source_file`, `category:"code"`,
  `page_type:"code"` into each page dict (`generator.py:175-185`, hash at `:131,:183`).
- `server/tools/repo_wiki.py` `repo_wiki_generate` **returns** those page dicts
  (`:100-110`, `pages: capped` at `:102`) and **does not write the DB**
  (docstring `:10`, TODO `:59`).
- Migration 024 (`storage/migrations.py:939-950`) added `hash` + `source_file` as
  `option<string>` on `wiki_page`. `storage/wiki.py:insert_wiki_page` writes them
  **only if present in the dict** (`:161-169`, `page.get("hash")` guard).
- **THE SEAM:** the only path from generator → DB is `wiki_add` → `WikiStore.add`
  → `insert_wiki_page`. `WikiAddOptions` (`wiki.py:580-596`) carries
  `page_type` + `directory_context` but **NOT `hash`/`source_file`**.
  `WikiStore.add` builds its page dict at `wiki.py:703-716` (insert) and `:678-696`
  (update) **without those fields**. The generator's hash is dropped at submit.
- **Empirical consequence (confirmed in the original plan):** zero `category=code` /
  `page_type=code` pages in the live DB. The DB-read stale checker
  (`project.py:_scan_stale_wiki_slugs_db`, reads `page_type='code'`) is **correct but
  finds nothing**. The only working piece today is the negative fix: the checker
  excludes external `fn:`/`api:` pages from over-reporting (`project.py:2156-2159`).

**The value cliff is P0.** The moment code pages land in the DB with `hash` +
`source_file`, four things light up that already exist and are wired:
`_scan_stale_wiki_slugs_db` finds them; `project_brief`'s `wiki_catalog` groups them
by `page_type`; `recall(type="wiki")` retrieves them; the negative exclusion stops
mattering because the positive path now produces real data. **P0 alone flips
inert→live and delivers ~80% of the user-visible value.** Everything after P0 is
graduated polish with sharply diminishing returns and rising cost. The user can stop
after any phase. This plan is deliberately written so they can.

### What collapses (the brutal core)

| Original ask | Verdict |
|---|---|
| 1 (write-seam) + 4 (MCP-vs-queue) | **Merge into ONE tool:** a dedicated batch `wiki_upsert_code_pages`. Kills both. |
| 5 (page_type retrieval) + 6's "easy to recall as cluster" | **Same thing.** "recall code docs as a cluster" = filter by `page_type` + group by package slug-prefix. No `cluster_id` needed. |
| 6's "show as clusters in viz" | Deterministic package-path grouping of `wiki:code` nodes in `graph_api.py`. NOT community detection, NOT memory-ification. |
| 2 (auto-trigger) | **Nudge-first** (P1, like ADR/agent-prompt nudges). Auto-dispatch behind an I25 knob, **default OFF** — auto-default is a runaway risk. |
| 7 (#47 fn parity) | **Split 80/20.** P3a fn-hash manifest (M-L, drops external skill). P3b per-fn pages (XL, questionable value). |

---

## 0b. Design rationale & pushbacks

This section is the **why** layer for a cold reviewer: it expands every row of the
"What collapses" table above into the *reasoning* behind the verdict, names each
rejected alternative and *why* it was rejected, and consolidates the advisor catches
in one place. Implementation deltas for each point live in §2 (cross-referenced); this
section does not restate them — read it for the reasoning, §2 for the how.

### Pushback (a) — the "clusters" ask dissolves; it isn't the hard part

The user framed clustering as the hard, uncertain part. It is only hard under the
(wrong) assumption that wiki pages must acquire a `cluster_id` the way memories do.
Drop that assumption and the ask splits into two pieces that are each trivial:

- **"Recall code docs as a cluster"** reduces to the `page_type='code'` filter
  (P2, §2 Point 5+6) plus slug-prefix grouping. Slugs are package-pathed
  (`code:yadgar.retrieval.core`), so filtering by `page_type` and ordering by slug
  *is* the cluster — deterministically, with no new machinery. This is **redundant
  with the code-index anchor in P2** and needs no `cluster_id`.
- **"Visible as a cluster in the viz"** reduces to deterministic **package-path
  grouping** of `wiki:code` nodes in `/api/graph` (§2 Point 6-viz): string-split the
  module path on its top-level package, emit one group per package. Bounded,
  deterministic, stable across runs, zero new tables.

**REJECTED alternatives and why:**

- **(i) Memory-ifying code pages (option 6c).** Category error. Code pages are
  deterministic, regenerated docs that should never decay and should never compete for
  heat. Putting them in the heat-ranked, decay-gated *memory* store would pollute it
  with hundreds of code-doc rows that crowd out real episodic memories in `recall`,
  corrupt heat statistics, and churn consolidation. Code docs are `wiki_page` rows by
  nature; they must stay there. Hard no.
- **(ii) Community detection over the wiki-link graph (option 6b).** The
  `wiki_crossref` link graph and Louvain machinery both exist, so it is *tempting*.
  But it is **non-deterministic** machinery run to produce a result a one-line string
  split on the module path gives for free — and it would make the viz grouping jitter
  between runs. Machinery for no gain.

### Pushback (b) — "schema/MCP for hash" + "MCP vs queue" collapse into ONE batch tool

Original asks 1 (write-seam: schema/MCP for the generator hash) and 4 (MCP vs
file-queue upload) are the *same* problem viewed twice. Both are answered by a single
dedicated batch tool, **`wiki_upsert_code_pages`** (idempotent, slug-keyed,
deletion-reconciling, similarity-gate-exempt, one transaction for the whole list —
full design in §2 Point 1+4).

**REJECTED — extend `wiki_add` instead:**

- Its UPDATE path **tag-MERGES and confidence-MAXes** (`wiki.py:664-675`). That is
  *correct* for curated prose (accumulate knowledge over edits) but **WRONG for
  deterministic regenerated code pages**, where each regen must *fully replace* the
  prior content — merge semantics would accumulate cruft across regens.
- `wiki_add` already carries ~14 params; `hash`/`source_file` are meaningless for the
  ~99% of pages that are `reference`/`adr`/etc. and would invite misuse via bloat.

**REJECTED — the file-queue / `~/.yadgar/queue/` bulk path:**

- It **violates the project's own MCP-is-the-only-write-path hard rule** (the
  DLQ / similarity-gate write contract). Bypassing the gate lands ungoverned rows.
- It **loses the transactional `wiki_page` + `wiki_page_version` write integrity**
  that `insert_wiki_page` guarantees. The throughput concern the queue was meant to
  address is already solved by the tool's single batch transaction.

A dedicated batch tool gives idempotent slug-keyed **upsert** + **replace-semantics**
(not merge) + transactional writes, all through the sanctioned MCP surface.

### Pushback (c) — auto-trigger on-by-default is a self-inflicted runaway

The user asked for an auto-trigger on the stop-hook. The stop-hook fires every ~25
human messages. An **on-by-default** auto-regen would, during normal work, spawn
repeated background agents and DB-write storms — and risk thrash if a file is
mid-edit. That is a self-inflicted runaway. Don't ship it that way.

**Design (full deltas in §2 Point 2):** **NUDGE-first** — mirror the existing
ADR / agent-prompt capture nudges in `project.py` by surfacing "N code pages stale,
run repo-wiki update" in `recommended_actions`. The agent then decides. Auto-dispatch
is available but gated behind **all** of: a default-**OFF** I25 knob
(`YADGAR_REPO_WIKI_AUTO_REFRESH_ENABLED=false`) **AND** a cadence floor
(`YADGAR_REPO_WIKI_REFRESH_MIN_INTERVAL_S` ≥ 3600s — at most hourly even when on)
**AND** default-branch-only. The user gets the auto-trigger they asked for, opt-in,
with guardrails that make a runaway impossible.

**Also (the #47 split):** #47 is **not one XL block**. Splitting it is part of this
pushback because presenting it monolithically forces an all-or-nothing decision on
work with wildly different value/cost:

- **P3a — fn-hash manifest** (M-L, the cheap 80%): AST body capture +
  `SHA256(sig+body)` per function, stored in the module-page frontmatter manifest.
  This alone reaches per-fn staleness parity, which **lets the external skill be
  dropped** (and the two-store `.local-review/wiki/` duplication killed at its source).
- **P3b — per-fn pages + index-IR synthesis** (XL): hundreds-to-thousands of rows plus
  **non-deterministic LLM index synthesis**. Gold-plating with **no proven consumer** —
  module pages already render every function as a section. Recommend **defer
  indefinitely** until a concrete fn-granular recall use-case appears.

### Advisor catches

Two failure modes a naive build would miss. Spelled out here so they are not lost in
the per-point deltas.

- **Deletion-reconcile.** Regen must **DEPRECATE orphan pages** for deleted/renamed
  modules — not only upsert the live ones. Slug is deterministic from the module path,
  so a re-run cleanly overwrites *surviving* modules; but a deleted/renamed module
  leaves a `page_type='code'` page whose `source_file` no longer exists on disk. If the
  tool only upserts, those orphans accumulate and the stale checker flags ghosts it can
  **never refresh** — the staleness signal then lies. Spec the reconcile step: the tool
  accepts the full current slug-set, and for any existing code page in
  `directory_context` whose slug is not in the incoming set (or whose `source_file` is
  gone) it marks the page deprecated (deprecated tag + frontmatter flag) / tombstones it
  — never silently leaves it live. (Implementation: §2 Point 1+4 "Idempotency +
  DELETION".)
- **Supersession.** This plan **supersedes the still-open portion of
  `docs/plans/wiki-repo-builtin.md`**. If that supersession is not made explicit, the
  build will run from two live plans and recreate the exact **two-store duplication**
  (disk `.local-review/wiki/*.md` vs the DB) the subsystem already suffers from. After
  this plan is accepted, mark `wiki-repo-builtin.md` SUPERSEDED. (Cross-link: the
  "Reconciliation with `docs/plans/wiki-repo-builtin.md`" section at the end of this
  document.)

---

## 1. Phasing, effort, risk

| Phase | Scope | Effort | Risk | Value |
|---|---|---|---|---|
| **P0** | Write-seam: `wiki_upsert_code_pages` batch tool (idempotent, slug-keyed, deletion-reconciling). Generator → DB. | **M** | **L** | ~80% — inert→live |
| **P1** | Agent-gen + stop-hook **nudge** on default branch (stale code pages → `recommended_actions`). Auto-dispatch behind knob, default OFF. | **M** | **M** (auto-dispatch is the risk; gate hard) | recall/brief stay fresh hands-off |
| **P2** | Discoverability: `page_type` filter on recall/wiki_list/wiki_query; ONE code-wiki index anchor; `project_brief` code-wiki TOC section; package-derived viz grouping of `wiki:code` nodes. | **M** | **L** | first-class access + viz |
| **P3a** | fn-hash manifest: AST body capture, per-fn `SHA256(sig+body)` stored in module-page frontmatter. Staleness parity → **drop the external skill**. | **M-L** | **M** | kills two-store dup |
| **P3b** | Per-fn pages (`fn:file::name` rows) + index-IR synthesis. | **XL** | **H** (hundreds-thousands of rows, non-deterministic LLM index) | marginal — **maybe skip** |

Sequencing is strict: P0 gates everything (no live data → nothing to nudge, surface,
or cluster). P1 and P2 are independent of each other and can go in either order after
P0. P3a is independent of P1/P2. P3b depends on P3a.

---

## 2. Point-by-point design + BRUTAL assessment

### Point 1 + 4 (merged) — Write-seam: ONE batch upsert tool [P0]

**Design.** Add a dedicated MCP tool `wiki_upsert_code_pages(directory, pages: list)`
where each `page` is exactly the dict `repo_wiki_generate` already emits
(`{slug,title,content,tags,category,page_type,directory_context,hash,source_file}`).
Plumb a thin store method `WikiStore.upsert_code_pages` that, per page:

1. Looks up by slug (`get_wiki_page_by_slug` — already used at `wiki.py:660`).
2. **Clean overwrite** (not the merge semantics of `WikiStore.add`): for code pages
   the generator is the sole source of truth — tags/confidence accumulation
   (`wiki.py:664-675`) is wrong; we want replace-in-place.
3. Writes `hash` + `source_file` into the dict so `insert_wiki_page`'s existing guards
   (`storage/wiki.py:161-169`) persist them. Update path needs the same two lines
   added to its `updates` dict (`wiki.py:678-696` currently omits them).
4. Runs as **one transaction** for the whole list (solves the 100s-of-writes problem).
5. Is **similarity-gate exempt**: code pages are deterministic and slug-keyed; the
   dedup gate that protects episodic memory has no meaning here and would cause false
   refusals on near-identical module pages.

**Idempotency + DELETION (advisor catch — self-check misses this).** Slug is
deterministic from module path, so re-run overwrites cleanly. But a deleted/renamed
module leaves an **orphan** code page whose `source_file` no longer exists on disk.
The tool MUST reconcile deletions: accept the full current slug-set from the agent,
and for any existing `page_type='code'` page in `directory_context` whose slug is NOT
in the incoming set (or whose `source_file` no longer exists), mark it deprecated
(append a `deprecated` tag + frontmatter flag) or delete it. The external skill already
does this ("deprecates removed entities"); the in-process path must too, or the stale
checker will forever flag ghosts it can never refresh.

**BRUTAL — dedicated tool vs extend `wiki_add`.** Verified the discriminator: the
`wiki_add`/`WikiStore.add` UPDATE path (`wiki.py:663-699`) does a clean content
overwrite with **no** similarity-gate refusal, so extending is *mechanically* possible.
Reject it anyway. Three reasons: (a) `wiki_add` already takes 14 params; `hash` +
`source_file` are meaningless for the other 99% of pages (`reference`/`adr`/etc.) and
invite misuse; (b) `add`'s tag-merge + confidence-max semantics (`:664-675`) are wrong
for deterministic code pages — you'd accumulate cruft across regens; (c) batch +
deletion-reconcile + gate-exempt is a different contract than `add`'s single-page
similarity-gated write. One purpose-built tool is cleaner than overloading the general
one. **Reject the file-queue (`~/.yadgar/queue/`) outright** — it's an MCP-bypass
(forbidden by the project's own hard rule), loses transactional integrity, and the
batch-transaction already solves the throughput concern the queue was meant to address.

**Deltas.** New MCP tool `wiki_upsert_code_pages` (`server/tools/wiki.py`); new
`WikiStore.upsert_code_pages` (`wiki.py`); 2-line addition to the `add` UPDATE dict OR
(cleaner) keep all code-page logic in the new method and leave `add` untouched
(**preferred** — zero blast radius on the general write path). Capability registry
entry (I32). No new migration (024 already added the columns).

---

### Point 2 — Stop-hook auto-trigger on default branch [P1]

**Design.** Mirror the existing nudge pattern exactly. `project.py` already has
`_apply_adr_signal` (`:1428-1494`) and `_apply_agent_prompt_signal` (`:1526-1598`),
both of which append a `{action, reason, suggested_call}` entry to
`recommended_actions` in `_project_brief_signals` (`:1607-1724`). The stale code-page
count is **already computed**: `_compute_stale_wiki_count` (`:2218-2237`) →
`_scan_stale_wiki_slugs` → `_scan_stale_wiki_slugs_db` (reads `page_type='code'`), and
it's already in the signals dict as `stale_wiki_count` (`:1688`).

Add `_apply_repo_wiki_signal(...)`:
- **Default-branch-gated.** The stop-hook already resolves the default branch via
  `git symbolic-ref refs/remotes/origin/HEAD`
  (`~/.claude/hooks/yadgar-stop-memory-checkpoint.py:155-180`) and `project_brief`
  takes a `branch_hint` (`:1927-1930`). Fire only when current branch == default.
- Fire when `stale_wiki_count > 0`, emitting:
  `{action: "refresh_repo_wiki", reason: "<N> code pages stale", suggested_call:
  "<scan→generate→upsert dispatch>"}`.
- Reuse the `STALE_COUNT_CACHE_TTL_S` (`:2227`, default 300s) cache so we don't rescan
  every stop.

**BRUTAL — auto-dispatch is a runaway risk; nudge-only is the default.** The stop-hook
fires every 25 human messages (`hook:24`). Auto-dispatching a scan→generate→upload
**agent** on every default-branch stop that has stale pages = repeated background
agent spawns + repeated DB write storms + possible thrash if a file is mid-edit. The
two existing nudges are **nudge-only by deliberate design** — they suggest a call, they
don't execute it. Match them. Give the user the auto-trigger they asked for, but as an
**I25 knob defaulting OFF** (`YADGAR_REPO_WIKI_AUTO_REFRESH_ENABLED=false`) plus a
cadence floor (`YADGAR_REPO_WIKI_REFRESH_MIN_INTERVAL_S`, e.g. 3600 — at most once an
hour even when enabled). When the knob is OFF (default), the agent sees a nudge and
decides. When ON, the stop-hook template dispatches the agent, gated by:
default-branch AND `stale_wiki_count>0` AND last-refresh older than the interval. Say
it plainly: **auto-on-by-default would be a self-inflicted runaway. Don't ship it
that way.**

**Deltas.** `_apply_repo_wiki_signal` in `project.py`; append to `recommended_actions`
in `_project_brief_signals`; two I25 knobs (config.py + config_yaml.py +
config_registry.py — `config_registry.py:336` is the template); stop-hook template
addition to read the new action and (if knob ON) dispatch. Capability entry (I32).

---

### Point 3 — Generation runs in a dispatched agent [P1]

**Design.** The daemon must never block live ops on a repo scan. The refresh runs as a
dispatched **subagent** (the orchestrator's `Agent` mechanism), shape:

1. `repo_wiki_generate(directory)` → page dicts (already returns them, no DB write).
2. Optionally diff against `_scan_stale_wiki_slugs_db` to regen **only stale** slugs
   (cheaper; skip clean pages).
3. `wiki_upsert_code_pages(directory, pages)` — the P0 tool, one transaction.
4. Report count upserted + deprecated.

The stop-hook (when the auto knob is ON) dispatches this agent in the background; when
OFF, the nudge tells the *session* agent to run the same three steps. Either way the
write goes through the gated MCP path (P0 tool), never the daemon's main loop, never
the file-queue.

**BRUTAL.** "Run in an agent" is correct and cheap — but the only thing that makes it
safe is P0's batch-transaction + deletion-reconcile. A naive agent looping `wiki_add`
per page (the pre-P0 shape) would be many DB writes through the merge-semantics path —
exactly the wrong thing. The agent shape is trivial; the value is entirely in the P0
tool it calls. Don't over-engineer the agent (no IR, no synthesis at this phase).

---

### Point 5 + Point 6-recall (merged) — page_type retrieval [P2]

**Design.** Verified gap: `page_type` is **stored** (`storage/wiki.py:154-161`,
`wiki_meta.py` PAGE_TYPES registry) but **queryable by no MCP tool**. `wiki_query`
(`server/tools/wiki.py:561-682`), `wiki_list` (`:769-813`), and `recall`
(`server/tools/recall.py:394-406`, `type` filters only all/memory/wiki) all lack a
`page_type` param. Add an optional `page_type: str | None` filter to `wiki_list` and
`wiki_query` (and plumb it through `recall(type="wiki", page_type=...)`), pushed down to
the storage SELECT as a `WHERE page_type = $pt` clause.

**This single change satisfies "code docs form clusters so they're easy to recall."**
Filtering `page_type='code'` + ordering by slug (slugs are package-pathed, e.g.
`code:yadgar.retrieval.core`) groups all code docs together in recall output. That IS
the cluster, deterministically, with no `cluster_id` machinery. The user's point-6
"recall as cluster" ask is **redundant with point 5** — do not build separate
clustering for recall.

**BRUTAL.** Don't over-scope the filter — `page_type` on `wiki_list`/`wiki_query`/
`recall(type=wiki)` is enough. Don't add it to memory recall (memories don't have
page_type). Don't invent a "code namespace" abstraction; the slug prefix already is one.

---

### Point 6-viz — Clusters visible in the graph [P2]

**Design.** Two goals were conflated; split them and both get cheap.
- "Easy to recall as a cluster" → solved by Point 5 above (no graph involved).
- "Show as clusters in the viz" → derive a **deterministic node grouping** for
  `wiki:code` nodes from their module/package path, in `graph_api.py`.

Today `graph_api.py` puts wiki pages in the graph as `wiki:*` nodes
(`_assemble_wiki_nodes`, `:288-314`) with **no `cluster_id`** (cluster_id is
memory-only, `:214`). The `clusters[]` payload (`:162,:168`,
`_build_clusters_payload(mem_ids)`) is built from memory ids only. Add a parallel,
package-derived grouping for `wiki:code` nodes: parse the slug/`source_file` package
prefix (e.g. `yadgar.retrieval.*` → cluster "retrieval"), emit a wiki-cluster grouping
in the graph payload. Bounded (one group per top-level package), deterministic, zero
new tables.

**BRUTAL — this is the biggest brutal win; the "hard part" dissolves.** The user framed
clustering as hard/uncertain. It is only hard if you accept the framing that wiki pages
must get a `cluster_id` like memories do. They must not.
- **Reject option (c) "represent code pages as memories" outright.** It's a category
  error: it pollutes the heat-ranked, decay-gated memory store with hundreds of
  code-doc rows that would compete with real episodic memories in `recall`, corrupt
  heat statistics, and churn through consolidation. Hard no.
- **Flag option (b) "community detection over `wiki_crossref`" as overkill.** The
  `wiki_crossref` link graph exists (`storage/wiki.py:794-797`,
  `graph_api.py:316-329`) and Louvain machinery exists
  (`sleep_compute/community.py:104-110`), so it's *tempting*. But it's
  non-deterministic, runs heavy clustering for a result a string-split already gives,
  and would make the viz grouping jitter between runs. Machinery for no gain.
- **Option (a) package-path is the answer for BOTH goals.** Deterministic, cheap,
  bounded, stable across runs. Ship (a). Nothing else.

**Deltas.** `graph_api.py`: package-derived wiki-cluster grouping in the payload;
optional small frontend legend tweak. No schema, no migration, no community detection.

---

### Point 5-anchor / project_brief seeding [P2]

**Design.** Create **exactly one** code-wiki index anchor, mirroring the agent-prompt
library anchor (`server/tools/agent_prompts.py:40,116-131`,
`_LIBRARY_ANCHOR_REASON="agent-prompt-library"`). Reason e.g.
`"repo-wiki-code-index"`, content = a short pointer ("code docs live as
`page_type=code` wiki pages; filter via `wiki_list(page_type='code')`"). Created lazily
on first successful `wiki_upsert_code_pages` run. Surfaced via `restore`/`project_brief`
which already include anchored memories (`misc.py:251-265`).

For `project_brief`, add a code-wiki TOC section. `_build_wiki_catalog`
(`project.py:470-517`) already fetches and groups wiki pages by `page_type`
(`:503`); `_render_wiki_catalog` (`:520-569`) renders category summaries. Extend the
catalog render to surface a compact `page_type=code` group (count + top package
prefixes, NOT a full slug dump) so the brief shows code-wiki structure without noise.

**BRUTAL — don't spam anchors.** ONE index anchor, period. Do NOT anchor individual
code pages (that's what the `page_type` filter + slug prefixes are for). Anchors are
max-heat protected memory rows; hundreds of them would defeat the protection mechanism
and crowd `restore`. The TOC in `project_brief` must be a **summary** (count +
prefixes), never a full listing — a 300-page repo would blow the brief's token budget
and bury the actually-hot context.

---

### Point 7 — #47 fn/index parity [P3, split 80/20]

**Context.** The over-report is **already handled** by the negative exclusion
(`project.py:2156-2159`: `fn:`/`api:` entity pages return `False` from staleness). So
#47's real marginal value is (a) staleness parity that lets the external skill be
**dropped**, and (b) fn-granular recall. These have very different costs — do not
present #47 as one XL block.

**P3a — fn-hash manifest (M-L, the cheap 80%).** Scanner already extracts per-fn
`signature` (`scanner.py:35`, `_build_signature` `:115-120`) + docstring, but **never
captures the body**. Add body capture (`ast` body node → `ast.unparse` or source slice)
and compute per-fn `SHA256(sig.strip() + body.strip())` — matching the external skill's
scheme (SKILL.md:104). Store the per-fn hashes in the **module page's frontmatter /
manifest** (a dict `{fn_name: hash}`), not as separate pages. This gives: (1) staleness
parity at fn granularity, so the in-repo checker can finally reproduce the external
skill's per-fn hashes; (2) the ability to **drop the external repo-wiki skill** and
kill the two-store `.local-review/wiki/*.md` duplication at its source; (3) the
`fn:`/`api:` negative exclusion can eventually be removed because the positive path now
covers fn-level. Module pages already render per-fn sections via `_render_function`
(`generator.py:57-67`), so no new pages are needed for this.

**P3b — per-fn pages + index-IR (XL, questionable).** Separate `wiki_page` rows per
`fn:file::name`, plus index-page synthesis with `SHA256(serialised IR section)`. This
adds **hundreds-to-thousands** of rows and **non-deterministic LLM index synthesis**
(the external skill's index pages drift across model/prompt — SKILL.md notes this). It
only serves fn-granular *recall* (jump straight to one function's page). Module pages
already surface every function as a section. **Question whether P3b is worth it at all.**
Recommendation: **defer P3b indefinitely** unless a concrete recall use-case demands
per-fn pages. P3a captures the value (parity + drop external skill); P3b is cost
without a proven consumer.

**BRUTAL.** Full fn/index parity as one XL train is the wrong unit. The external skill
isn't dropped by per-fn *pages* — it's dropped by per-fn *hash parity* (P3a). Ship P3a,
delete the external skill + its `.local-review/` store + the `fn:`/`api:` exclusion,
and stop. P3b is a speculative XL with no proven consumer; building it first would be
gold-plating.

---

## 3. Schema appendix

| Table | Field | Type | Status | Migration |
|---|---|---|---|---|
| `wiki_page` | `hash` | `option<string>` | **shipped** (#36) | 024 |
| `wiki_page` | `source_file` | `option<string>` | **shipped** (#36) | 024 |
| `wiki_page` | `page_type` | `string` (PAGE_TYPES) | **shipped** | pre-024 |
| `wiki_page` | `deprecated` (tag/flag) | tag on `tags[]` | **P0** (deletion-reconcile) | none (tag, no schema) |
| `wiki_page` | `fn_hashes` (manifest) | `option<object>` `{name:hash}` | **P3a** | NEW migration 0NN |
| `wiki_crossref` | (from_slug,to_slug) | existing | unchanged | — |
| `memory` | `cluster_id` | `int\|null` | unchanged — **NOT used for wiki** | — |

**New MCP tools:** `wiki_upsert_code_pages` (P0). **Extended MCP params:** `page_type`
on `wiki_list`/`wiki_query`/`recall(type=wiki)` (P2).
**New config knobs (I25 three-way: config.py + config_yaml.py + config_registry.py):**
`YADGAR_REPO_WIKI_AUTO_REFRESH_ENABLED` (default `false`),
`YADGAR_REPO_WIKI_REFRESH_MIN_INTERVAL_S` (default `3600`) — both P1.
**Capabilities (I32):** register the new tool + the auto-refresh capability in
`docs/CAPABILITY_REGISTRY.md`.
**Anchor:** one `reason="repo-wiki-code-index"` index anchor (P2).
**graph_api:** package-derived `wiki:code` node grouping (P2, no schema).

---

## 4. Open decisions for the user (numbered)

1. **Dedicated tool vs extend `wiki_add`.** Plan recommends dedicated
   `wiki_upsert_code_pages` (batch, gate-exempt, deletion-reconciling), leaving
   `wiki_add` untouched. Confirm, or do you want `wiki_add` extended despite the
   merge-semantics + param-bloat downsides?
2. **Deletion handling: deprecate vs delete.** Orphan code pages (module deleted/
   renamed) — mark `deprecated` (keep history, visible) or hard-delete?
   **Options:** deprecate-then-sweep | hard-delete on reconcile.
   **Rec:** deprecate. **Why:** reversible and preserves page history /
   `wiki_page_version` audit trail; a hard-delete on a false-positive reconcile (e.g. a
   transient scan miss) is unrecoverable.
3. **Auto-refresh default.** Plan ships `YADGAR_REPO_WIKI_AUTO_REFRESH_ENABLED=false`
   (nudge-only by default). You asked for auto-trigger.
   **Options:** opt-in (default OFF, nudge-first) | auto-on by default.
   **Rec:** opt-in / default OFF. **Why:** auto-on fires on a ~25-message cadence →
   repeated agent spawns + DB-write storms during normal work = self-inflicted runaway
   (see pushback (c)); nudge-first gives the user the trigger without the blast radius.
4. **Refresh cadence floor.** Min interval between auto-refreshes when auto is ON.
   **Options:** 3600s (default) | lower | higher.
   **Rec:** 3600s. **Why:** lower → write-storm / mid-edit thrash risk on busy repos;
   higher → staleness window widens. 3600s caps refresh at once/hour while keeping
   pages reasonably fresh.
5. **project_brief TOC verbosity.** How much code-wiki structure to surface in the brief.
   **Options:** summary (count + top package prefixes) | full slug listing.
   **Rec:** count + anchor / summary-only. **Why:** a full listing on a 300-page repo
   blows the brief's token budget and buries the actually-hot context (brief bloat); a
   count + package-prefix summary + the single index anchor gives structure without noise.
6. **P3b per-fn pages — build or drop?** Plan recommends **drop indefinitely** (P3a
   captures the value).
   **Options:** build P3b (per-fn pages + index-IR) | drop indefinitely.
   **Rec:** drop. **Why:** no proven consumer — module pages already render every
   function as a section; P3b adds hundreds-to-thousands of rows + non-deterministic LLM
   index synthesis for fn-granular recall nothing currently needs.
7. **External skill removal timing.** When to drop the `.local-review/wiki/` external
   skill.
   **Options:** remove after P3a green/verified | keep both in parallel for a grace
   period.
   **Rec:** remove after P3a green. **Why:** P3a achieves per-fn hash parity, so the
   external skill becomes pure duplication once it is verified working; keeping both
   perpetuates the two-store split this plan exists to kill. Remove once P3a is verified,
   not before.

---

## 5. Anti-goals — explicit "do NOT do X"

- **Do NOT** memory-ify code pages (option 6c). No `Memory` rows for code docs. They
  are `wiki_page` rows; they must not compete with episodic memory in recall/heat.
- **Do NOT** run community detection over `wiki_crossref` for viz clustering (6b).
  Package-path grouping is deterministic and sufficient.
- **Do NOT** use the file-queue (`~/.yadgar/queue/`) or any daemon-bypass for upload.
  The batch MCP tool is the only sanctioned write path.
- **Do NOT** extend `wiki_add` with `hash`/`source_file` (param bloat + wrong
  merge semantics for the 99% non-code pages).
- **Do NOT** make auto-refresh on-by-default. Nudge-first; auto behind a default-OFF
  knob with a cadence floor.
- **Do NOT** anchor individual code pages. ONE index anchor. The `page_type` filter +
  slug prefixes do the per-page discovery.
- **Do NOT** dump a full code-page listing into `project_brief`. Summary only.
- **Do NOT** build P3b (per-fn pages + index-IR) before P3a proves a consumer. It's
  gold-plating until something needs fn-granular recall.
- **Do NOT** leave the over-report negative-fix (`fn:`/`api:` exclusion) as the
  permanent answer — it's a patch over the absence of real data. P0 makes the positive
  path real; P3a lets the exclusion be retired.
- **Do NOT** spawn a regen agent without deletion-reconcile — orphan pages become
  permanent un-refreshable stale flags.

---

## Reconciliation with `docs/plans/wiki-repo-builtin.md`

That doc shipped #36 (module generator, migration 024, store-bridge schema, negative
exclusion fix) and deferred fn/index to #47 (XL). **This doc supersedes its open
portion** and re-scopes the remainder: P0 finishes the write-seam it left as a TODO;
P1/P2 add the agent-trigger + discoverability it never covered; P3 splits its
monolithic "#47 XL" into P3a (cheap parity, drop external skill) + P3b (deferred).
The two docs must not both be treated as live open plans — that two-doc state would be
the exact duplication this subsystem already suffers from. After this plan is
accepted, mark `wiki-repo-builtin.md` as SUPERSEDED.
