# ADR-Consultable + Type-Aware Read-First-Write Discipline

**Status:** DRAFT — awaiting audit
**Date:** 2026-07-14
**Scope:** core (`yadgar/core/server/tools/adr.py`, `.../project.py`, `.../wiki.py`, `.../misc.py`/memorize wrapper), backend (`yadgar/backend/retrieval/recall_pipeline.py`, `yadgar/backend/write_exec/memorize_impl.py`), config (`config_registry.py`, `config.py`), migration script (`scripts/`), tests (`yadgar/tests/core/`, `yadgar/tests/backend/`).
**Tracks:** task #76 (make ADRs consultable) + the read-first-write discipline (coupled changes A + B).
**Target version:** core next-minor (design only — no version bump in this doc).

---

## BLUF

Two coupled changes, sequenced A→B.

**A — Recall-native ADR records (task #76).** Kill the monolith. Today all ADRs live as sections of ONE wiki page `yadgar-adr-log` — **275,923 bytes / 120 full-snapshot versions / 115 ADRs** (verified `wiki_history`, 2026-07-14). `wiki_read` errors on it (too big) → the log is **write-only**: `adr_add` appends but nothing ever reads it, so the mandated read-first-dedup is skipped and no ADR has informed a decision. Fix: **one wiki page per ADR** (`yadgar-adr-NNNN`), tagged `["adr","decisions"]`, default-branch-pinned. Because the wiki store is a **first-class recall source** (WikiProvider fuses with memory results — `recall_pipeline.py:398,458`) and wiki pages **never decay** (no `heat`/`is_protected`/TTL on `wiki_page` — verified), per-ADR pages are **in the recall corpus and immortal for free**. Recall IS the read path: `recall("...decision topic...")` surfaces the relevant ADR at decision time; `recall(type="wiki", tags=["adr"])` lists them targeted. A **thin index page** (`yadgar-adr-index`) supplies what recall can't: sequential ID assignment, "list all open ADRs", supersede-chains. `project_brief` gains a **Recent/Relevant ADRs** block so decisions surface at session start.

**B — Type-aware read-first-write dedup discipline.** With recall now fast (Ettin), a `recall`-before-write is affordable for **durable** writes (facts/semantic memories, decisions/ADRs, feedback rules, curated wiki). `wiki_add` already has a drainer similarity gate (0.80 cosine, `dlq.py:261`); **`memorize` has NO gate at all** (confirmed — the only near-thing is the LLM contradiction phase, off by default, plus post-hoc consolidation merge at 0.95). B adds a **soft-gate on `memorize`** that returns near-duplicates so the caller UPDATEs-in-place (`memory_update`) instead of appending, plus an **agent-discipline** protocol line for the contradiction judgment (which is not automatable). **Episodic memories are scoped OUT** — they are meant to accumulate + decay; gating them wastes cost.

**Storage-model recommendation:** reuse the wiki store, one page per ADR + one thin index page. Do NOT add a new `store_type` and do NOT move ADRs into the memory store. Rationale in [§A.1](#a1-storage-model-decision).

---

## Investigation findings (verified against code, file:line)

### The monolith today
- Storage: `adr_add` (`adr.py:180`) writes ALL ADRs into a single wiki page `<project>-adr-log` (`adr_log_slug`, `adr.py:99`). First ADR → `wiki_add(wait=True)` (`adr.py:319`); subsequent → `wiki_append_section(position="new_section_bottom")` (`adr.py:290`).
- Record format (verified via `wiki_diff` v119→v120): `## ADR-NNNN: <title>` header at col 0, followed by flat bullets `status/date/context/decision/rationale/alternatives/consequences/revisit_trigger/supersedes`. `supersedes: ADR-0114` carries the chain.
- ID assignment: `_next_adr_id` (`adr.py:130`) **re-scans the whole page** for `^## ADR-(\d{4})` (`_ADR_HEADER_RE`, `adr.py:82`), takes max+1. Serialized under a per-project `threading.Lock` (`_adr_log_lock`, `adr.py:63`).
- Branch pinning: both read and write pin `default_branch` (`adr.py:265,296,326`). Known bug (recalled): unpinned `wiki_read` from a feature branch returns "not found" for the master-scoped page → per-ADR records inherit this risk.
- Version amplification: 120 full-content snapshots of a growing page (`wiki_page_version`) ≈ storage blowup — the version table holds ~Σ(v1..v120) bytes for 115 records.

### Recall corpus + type filtering
- `recall` MCP tool (`recall.py:129`) → backend `_fanout_recall` (`recall_pipeline.py:458`). Providers: `MemoryProvider` + `WikiProvider`, gathered in parallel and fused (`_build_provider_tasks`, `recall_pipeline.py:398`). `_source` stamped "memory"/"wiki" (`recall_pipeline.py:155`; `providers/wiki.py:93`).
- Wiki pages ARE embedded on write (`WikiStore._compute_embedding`, `store.py:2185`, formula `title\ncontent[:4000]`) and ARE retrieved by recall — so a per-ADR wiki page is recall-visible with **zero new plumbing**.
- `store_type` is a **memory-only free `str`** (`models.py:84`, values "episodic"/"semantic"); recall **never filters on it**. Not the ADR hook.
- **Agent-prompt exclusion mechanism** (the structure to replicate, the visibility to INVERT): tag-based post-rank filter. `wiki_exclude = None if tags else ["agent-prompt","agent-prompt-toc"]` (`recall_pipeline.py:432`) → passed as `exclude_tags` to `WikiProvider` → `WikiStore._tag_filter_pass` drops matching pages (`store.py:896`). Override: passing `tags=["agent-prompt"]` sets exclude to None. **For ADRs we want the OPPOSITE: do NOT add "adr" to `wiki_exclude`**, so ADR pages surface in normal recall. (Replicate the one-record-per-item + tag-reachable structure; invert the recall-visibility decision.)

### Write gates + retention (the decision hinge)
- **Wiki pages never decay** — no `heat`/`is_protected`/`valid_until` on `wiki_page` (`storage/wiki.py:151`); decay loop touches only the `memory` table (`heat_decay.py:98`). **This is the hinge: reuse-wiki gives ADR immortality for free — no anchor tier, no memory store needed.**
- `wiki_add` gate: enforced in the **drainer** (`dlq.py:261` `_sim_gate_for_drainer`), NOT the request thread (I9 fix, `wiki.py:249`). Bypassed on `force=True`/`replace_slug`/`append=True`. Threshold `WIKI_SIM_CONTENT_THRESHOLD=0.80` (`config.py:325`), mode `WIKI_SIM_MODE="hard"`. `wiki_check_duplicate` (`wiki.py:772`) is the read-only preflight.
- **`memorize` has NO similarity/dedup gate** (`memorize_impl.py:32` — validate→resolve_branch→embed→contradiction→store→post_write; contradiction only runs under `YADGAR_CONFLICT_RESOLVER=on`, default off). Post-hoc only: consolidation `_merge_duplicates` at 0.95 cosine (`cls.py:824`).
- `memory_update` (`admin_other.py:498`) patches `{content, tags, is_protected, is_stale}` only. **Content update does NOT re-embed** (`admin_exec/memory.py:79` writes DB directly) — a dedup-by-update flow must re-embed (targeted or `reembed_all`).
- Anchor tiers (`config.py:635`, `validate.py`): `ephemeral` 14d / `conditional` 90d / `semantic_immortal` no-expiry (requires `reason`). All auto-set `is_protected=True`. **Not needed for ADRs** (wiki is already immortal) — relevant only if a future decision moves ADRs to the memory store.

### Surfacing points
- `project_brief` render (`project.py:183` `_render_project_brief`): sections Global/Project Anchors → Checkpoint → Hot Memories → Wiki Index. **Natural ADR insertion point: a new "## Recent ADRs" section after Hot Memories.** Data via a metadata-only index read (like `_get_adr_log_updated_at`, `project.py:1339`).
- A **write-side** ADR nudge already exists: `_apply_adr_signal` (`project.py:1401`) appends a `capture_adr` action when active_work is newer than the ADR log. A/task #76 adds the **read-side** counterpart.
- Config already present: `YADGAR_ADR_DUE_WARN_HOURS=12.0` (`config_registry.py:326`), `YADGAR_AGENT_PROMPT_LIBRARY_ENABLED` (`config_registry.py:428`).

---

## Part A — Recall-native ADR records

### A.1 Storage-model decision

**Decision: reuse the wiki store — one page per ADR + one thin index page. No new `store_type`, no memory-store migration.**

Discriminator resolved by evidence: the task pulls two ways — "agent-prompt library is the model to extend" (agent-prompts live in the wiki store) vs "decisions ≠ curated wiki … different retention." Reconciliation: **a distinct type does NOT require a new store_type or a new table.** The agent-prompt precedent proves a distinct, filterable, tag-reachable class lives atop the wiki store. The retention objection dissolves because **wiki pages already never decay** — the "different retention" ADRs need (never age out, unlike episodic memories) is exactly what the wiki store provides natively. A memory-store ADR would need `tier="semantic_immortal"` machinery to get what wiki gives for free, and would fragment the record across two stores. Reuse-wiki wins on all axes: recall-visible (WikiProvider), immortal (no decay), versioned (wiki_page_version), branch-aware (§25), and already the substrate ADRs live in today.

**ADR stays a DISTINCT TYPE** — achieved by the `["adr"]` tag + slug prefix `yadgar-adr-NNNN`, the same way agent-prompts are a distinct type via their tag + slug. Same recall plumbing, different role (decision record) and different discovery affordance (index + supersede-chain), without dissolving ADRs into generic memories or generic wiki noise.

### A.2 Per-ADR record design

- **Slug:** `<project>-adr-NNNN` (e.g. `yadgar-adr-0042`). Zero-padded 4-digit, matching existing ID format.
- **Title:** `ADR-NNNN: <title>`.
- **Content:** the existing `ADR.to_markdown_body()` rendering (unchanged) — the 9 flat bullets. Reuse `_build_adr_body` (`adr.py:144`) verbatim; only the destination changes (own page, not a section).
- **Tags:** `["adr","decisions", "adr-status:<status>", "adr-<NNNN>"]`. The `adr-status:*` tag lets a targeted recall/query filter open vs superseded without parsing content. The `adr-<NNNN>` tag supports precise cross-ref lookup.
- **Category:** `decision` (existing wiki category; better than `reference` for these).
- **Branch:** always `branch_hint=default_branch` (canonical), both write and every read. This closes the recalled feature-branch "not found" bug at the source — records are written to and read from the default-branch slot, and recall resolution must use `branch_hint=default_branch` for ADR pages regardless of the caller's working branch.
- **Recall visibility:** `["adr"]` is NOT added to `wiki_exclude` (`recall_pipeline.py:432`) → ADR pages appear in normal `recall(query)`. (Inversion of the agent-prompt exclusion — the whole point of task #76.)

### A.3 Thin index design

The index is what recall cannot do: authoritative sequential ID, open-ADR list, supersede-chains.

- **Physical form:** a single small wiki page `<project>-adr-index`, tagged `["adr","adr-index"]`, default-branch-pinned. Kept small (metadata rows only, not full ADR bodies) so `wiki_read` never errors on it. One table:

  | ADR | Status | Date | Title | Supersedes | Superseded-by | Slug |
  |---|---|---|---|---|---|---|

- **ID source of truth:** the index, not a page re-scan. `_next_adr_id` reads the index (max ADR + 1) instead of scanning the monolith. Reuse the existing `_adr_log_lock` read-modify-write pattern (`adr.py:63`) around the index read → assign → write-record → append-index-row triple, so concurrent `adr_add` cannot duplicate IDs.
- **`adr-index` tagging:** the index page IS surfaced in normal recall (it's a compact map, not noise) — but to avoid it competing with the actual ADR body pages, tag it `adr-index` and let `project_brief` read it directly by slug (metadata read, not recall).
- **Supersede-chains:** when a new ADR sets `supersedes: ADR-0114`, `adr_add` (a) writes the new record, (b) appends the index row, (c) patches ADR-0114's index row `Superseded-by` column AND flips its `adr-status:*` tag to `superseded`. **Tag flip uses `wiki_update(page_id, fields={"tags": [...]})`** — NOT `wiki_set_metadata` (that tool only accepts `directory_context`/`branch`, rejects tags). This keeps "list all open ADRs" (filter index Status==open) correct.

- **Index version cost (accepted):** every `adr_add` re-snapshots the index page into `wiki_page_version` — the same amplification pattern flagged as the monolith's sin. But at ~150-char rows the per-version delta is ~3 orders smaller than the monolith's growing full-page snapshots; the index stays a few KB even at hundreds of ADRs. Bounded and acceptable. (The ADR **bodies** — the bulk — no longer re-snapshot on every add, which is the amplification that actually mattered.)

### A.4 New/changed tools

- `adr_add` (rewrite, `adr.py`): write per-ADR page + append index row + patch supersede targets. Signature UNCHANGED (schema stability). Returns `{adr_id, slug, version}`. **MUST create each per-ADR page with `wiki_add(..., force=True)`** to bypass the 0.80 drainer sim gate (`dlq.py:261`, hard mode): two distinct decisions on the same subsystem share the fixed bullet template + topic vocabulary (e.g. ADR-0115 literally reverses ADR-0114) and would plausibly exceed 0.80 → the second would be silently rejected to DLQ as `duplicate_detected`, losing a real ADR. Dedup for ADRs is handled by `adr_add`'s own index/ID check, NOT the wiki similarity gate. (Create path can't use `replace_slug`, so `force=True` is the only bypass.)
- `adr_list` (new, thin): read the index page, return rows; optional `status=` filter. Cheap — one metadata read. (Recall covers fuzzy discovery; `adr_list` covers the deterministic "show me all open ADRs".)
- `adr_get(adr_id)` (new, thin): `wiki_read(<project>-adr-NNNN, branch_hint=default)` — direct fetch by id. Optional; recall + slug convention may suffice, but a named tool removes branch-pin footguns.
- No change to the `ADR` model (`models.py`) or `_build_adr_body`.

### A.5 project_brief surfacing

- Add `_build_recent_adrs(storage, resolved, limit=3)` (metadata-only read of the index page, newest N + any `open`-status). Same defensive pattern as `_get_adr_log_updated_at` (`project.py:1339`).
- Render a **`## Recent ADRs`** section in `_render_project_brief` after Hot Memories (`project.py:258`), one line per ADR: `[ADR-NNNN] <status> — <title>`. Length-capped like the wiki catalog.
- Modes: include in `catalog`/`restore`/`full`; EXCLUDE from `signals` (keep signals ≤100 tokens). The existing write-side `capture_adr` signal (`_apply_adr_signal`) stays as-is — read + write nudges now both exist.

### A.6 Migration plan (monolith → per-ADR + index)

One-shot idempotent migration script `scripts/migrate_adr_monolith.py` (invoked by the user per HARD RULE — no auto-apply; hand the command over in `MIGRATION_NOTES.md`).

1. **Read** the monolith. **Verified 2026-07-14:** the daemon-side tool returns the FULL 275,720-char content — `wiki_get(6771)`'s "error" is the MCP *client* token-limit truncation (content is written to a tool-result file intact), NOT a storage/tool failure. A server-side migration script (importing the wiki store directly, or reading the tool-result file) has full access. No version-reconstruction fallback needed. This closes the largest migration risk.
2. **Parse** each `## ADR-NNNN: <title>` section with `_ADR_HEADER_RE` (`adr.py:82`) — split on header boundaries. For each: extract the 9 bullets + title.
3. **Emit** one `wiki_add(slug=<project>-adr-NNNN, ..., force=True, branch_hint=default)` per ADR (`force=True` to skip the 0.80 sim gate — near-identical ADRs are legitimately distinct records). Preserve original `date`.
4. **Build** the index page from parsed rows; compute `Superseded-by` back-links by inverting each `supersedes:` bullet.
5. **Idempotency:** skip any `yadgar-adr-NNNN` slug that already exists (re-runnable). Emit a dry-run count first (`--dry-run` prints "would create N ADR pages + index, resolve M supersede links").
6. **Retire** the monolith: after verification, `wiki_set_metadata` tag the old `yadgar-adr-log` `deprecated` (do NOT delete — keep version history as an audit trail) OR leave it and stop writing to it. Recommend: keep, tag `adr-archive`, add it to `wiki_exclude` so it stops polluting recall.
7. **Branch:** all writes pinned `default_branch`. Migration MUST run from / target the default branch slot.

**Migration risks:**
- ID gaps / non-contiguous NNNN in the monolith → migration must preserve original IDs verbatim, not renumber.
- Supersede targets referencing not-yet-migrated IDs → two-pass: create all pages first, then resolve links.
- 276k read: RESOLVED (§A.6-1) — daemon returns full content; only the MCP client transport truncates. Server-side migration is unaffected.
- Branch drift: if any ADR was accidentally written to a feature-branch slot (the recalled bug), migration on default branch would miss it — scan for stray `adr-log` rows across branches before declaring done.

---

## Part B — Type-aware read-first-write discipline

### B.1 Scope

| Write type | Store | Gate today | B adds |
|---|---|---|---|
| Episodic memory (session activity, transient obs) | memory | none | **nothing — scoped OUT** (meant to accumulate + decay) |
| Semantic memory (durable fact) | memory | none | **soft-gate on `memorize`** |
| Feedback rule (durable) | memory | none | **soft-gate on `memorize`** |
| ADR / decision | wiki (per A) | wiki 0.80 hard gate **bypassed by design** (`force=True` in `adr_add`, §A.4) — dedup via the index, not similarity | agent-discipline for contradiction |
| Curated wiki page | wiki | 0.80 hard gate (drainer) | already gated |

The gap B closes: durable **memory** writes have no dedup path; `wiki_add` already does. B is narrowly a `memorize` soft-gate + an agent-discipline protocol line — NOT a rewrite of the wiki gate.

### B.2 memorize soft-gate (tooling)

- Add a **soft** (non-blocking) similarity check to `memorize` for durable writes only. Trigger on **caller-settable signals only**: `tags` intersect `{feedback, decision, _anchor}`, OR `is_protected=True`, OR any `tier` set. Do NOT key on `store_type` — `memorize` has no `store_type` param (verified `memorize.py:31` — args are content/context/tags/is_protected/provenance_agent/tier/valid_until/ttl_days/reason/branch_hint), and the field defaults `"episodic"` and is set by the CLS classifier in `_phase_store` AFTER the gate point, so it is always "episodic" at gate-time (the semantic branch would never fire). Plain episodic writes (no durable tag, not protected, no tier) bypass entirely.
- Mechanism: before store, embed the content (the embed phase already runs — `_phase_embed`), query top-K memories by cosine in the same directory, and if any exceed a threshold (`YADGAR_MEMORIZE_SIM_THRESHOLD`, default ~0.85 — higher than wiki's 0.80 because memory content is terser/noisier; calibrate during impl), **return them in the response** as `near_duplicates: [{id, content, score}]` WITHOUT blocking the write. Soft, not hard — the caller (agent) decides update-vs-append.
- New config: `YADGAR_MEMORIZE_SIM_GATE_ENABLED` (default true), `YADGAR_MEMORIZE_SIM_THRESHOLD` (default 0.85), `YADGAR_MEMORIZE_SIM_TOP_K` (default 3). Register in `config_registry.py` + `config.py`.
- The response shape lets the agent then call `memory_update(id, {content: merged})` to update-in-place. **Caveat (verified):** `memory_update` does NOT re-embed — the discipline/impl must trigger a targeted re-embed after an in-place content patch, else the updated memory keeps a stale vector. Options: (a) extend `memory_update` to re-embed when `content` changes, (b) a `memory_replace(id, content)` helper that patches + re-embeds atomically. **Recommend (a)** — smallest surface, fixes a latent correctness gap.

### B.3 Agent-discipline (protocol)

Dedup is similarity-automatable (B.2). **Contradiction detection is a judgment on recall results, not a gate** — encode it as protocol, not code:

- Extend the write-back protocol (stop-hook prompt + `agent-instructions.md` write-back triggers) with: *"Before a DURABLE write (semantic fact, feedback rule, ADR), `recall` the topic. If a near-duplicate exists → UPDATE-in-place (`memory_update` / wiki `replace_slug`) instead of appending. If it CONTRADICTS observed state → mark the old stale (`memory_update is_stale=true`) or supersede (ADR). Episodic writes: skip this — just write."*
- Where enforced: **both** — the `memorize` soft-gate (B.2) surfaces the near-dups mechanically; the protocol line makes the agent act on them. Neither alone suffices: tooling can't judge contradiction; discipline alone is the write-only failure mode task #76 exists to kill.
- Do NOT promise automated contradiction detection — out of scope, and the task explicitly frames it as judgment.

### B.4 Coupling to A

A makes ADRs recall-visible → the read-first step in B.3 can actually find prior ADRs (impossible today, the monolith is unreadable). B without A leaves the "recall the topic" step blind for decisions. Hence A ships first.

---

## Sequencing / cars

| Car | Deliverable | Depends on |
|---|---|---|
| A1 | Per-ADR page + index schema; `adr_add` rewrite; `_adr_log_lock` reused against index | — |
| A2 | `adr_list` / `adr_get` thin tools | A1 |
| A3 | `project_brief` `## Recent ADRs` section + `_build_recent_adrs` | A1 |
| A4 | Migration script + `MIGRATION_NOTES.md` (dry-run, idempotent) | A1 |
| A5 | Recall-visibility wiring: confirm `adr` NOT in `wiki_exclude`; add old `adr-log` to exclude post-migration | A4 |
| B1 | `memorize` soft-gate + config + `near_duplicates` response | A (recall-visible ADRs) |
| B2 | `memory_update` re-embed-on-content-change fix | — (can parallel A) |
| B3 | Write-back protocol update (stop-hook prompt + agent-instructions) | B1 |

---

## Test strategy (describe — not written here; investigate+plan only)

Extend existing suites: `yadgar/tests/core/test_adr.py`, `test_project_brief_adr_log.py`, `yadgar/tests/hooks/test_stop_hook_template.py`; add `yadgar/tests/backend/` cases for the memorize gate + migration.

- **ID-still-sequential:** `adr_add` ×3 → IDs 0001/0002/0003 sourced from the index, not a page re-scan; concurrent calls (lock) never duplicate.
- **recall-surfaces-ADR:** write an ADR, `recall("<its topic>")` returns the per-ADR page with `_source=="wiki"`; confirm it is NOT excluded.
- **branch-pin-resolves:** `adr_get`/recall of an ADR from a simulated feature-branch cwd still resolves the default-branch record (closes the recalled bug).
- **index-integrity:** supersede flips `adr-status` tag + `Superseded-by` back-link; `adr_list(status="open")` excludes superseded.
- **migration-idempotency:** run twice → second run creates 0 pages; dry-run count matches actual; original IDs preserved; supersede links resolved two-pass.
- **project_brief-render:** `## Recent ADRs` present in catalog/restore/full, absent in signals; length-capped.
- **memorize-soft-gate:** semantic write with a near-dup returns `near_duplicates` and STILL stores (soft); episodic write bypasses (no near_duplicates key); threshold boundary.
- **memory_update-reembed:** content patch changes the stored embedding (B2 fix); tags-only patch does not.

Loop-until-clean: run `pytest` on touched suites + lint/types after each car.

---

## Open decisions for the user

1. **Old monolith fate.** Keep-and-deprecate (audit trail, tag `adr-archive` + add to `wiki_exclude`) vs delete after migration. Recommend keep. (§A.6-6)
2. **`adr_get`/`adr_list` as tools vs recall-only.** Do you want the two thin deterministic tools, or is recall + slug convention + the index page enough? Recommend adding both — removes branch-pin footguns and gives a non-fuzzy "open ADRs" list. (§A.4)
3. **memorize soft-gate default-on?** Ship `YADGAR_MEMORIZE_SIM_GATE_ENABLED=true` by default, or default-off behind a flag for a bake-in period? Recommend on (soft = non-blocking, low risk). (§B.2)
4. **memory_update re-embed.** Fix by (a) extending `memory_update` to re-embed on content change, or (b) a new `memory_replace` atomic helper? Recommend (a). (§B.2)
5. **Cross-project generalization.** Task #76 is yadgar-specific but the ADR machinery is project-agnostic (`<project>-adr-*`). Roll out to all projects now, or yadgar-first then generalize? Recommend yadgar-first (dogfood), then lift.
6. **Which durable tags trigger the soft-gate.** Proposed `{semantic store_type} ∪ {feedback, decision, _anchor}`. Confirm the set — too broad slows every anchor write, too narrow misses feedback rules. (§B.2)
7. **memorize gate threshold.** 0.85 proposed vs wiki's 0.80 — memory content is terser/noisier. Needs a small calibration pass (like the wiki 0.80 calibration at `config.py:319`). Confirm we do that calibration before defaulting on.
