# ADR-Consultable + Type-Aware Read-First-Write Discipline

**Status:** AUDITED-ready — implementable by a builder
**Date:** 2026-07-14 (audited 2026-07-14)
**Scope:** core (`yadgar/core/server/tools/adr.py`, `.../project.py`, `.../wiki.py`, `.../memorize.py`, `.../admin_other.py`), backend (`yadgar/backend/retrieval/recall_pipeline.py`, `yadgar/backend/write_exec/memorize_impl.py`, `yadgar/backend/admin_exec/memory.py`, `yadgar/backend/queue_drainer/dlq.py`), config (`yadgar/_shared/config/config.py`, `yadgar/_shared/config/config_registry.py`), migration script (`scripts/`), tests (`yadgar/tests/core/`, `yadgar/tests/backend/`, `yadgar/tests/hooks/`).
**Tracks:** task #76 (make ADRs consultable) + the read-first-write discipline (coupled changes A + B).
**Target version:** core next-minor (design only — no version bump in this doc).
**Rollout:** ALL projects (not yadgar-first). ADR machinery is project-agnostic (`<project>-adr-*`); migration sweeps every project's ADR log.

---

## BLUF

Two coupled changes, sequenced A→B.

**A — Recall-native ADR records (task #76).** Kill the monolith. Today every project's ADRs live as sections of ONE wiki page `<project>-adr-log` (yadgar's is **275,923 bytes / 120 full-snapshot versions / 115 ADRs** — verified `wiki_history`, 2026-07-14). `wiki_read` errors on it (too big) → the log is **write-only**: `adr_add` appends but nothing ever reads it, so the mandated read-first-dedup is skipped and no ADR has informed a decision. Fix: **one wiki page per ADR** (`<project>-adr-NNNN`), tagged `["adr","decisions"]`, default-branch-pinned. Because the wiki store is a **first-class recall source** (WikiProvider fuses with memory results — `recall_pipeline.py:399,459`) and wiki pages **never decay** (no `heat`/`is_protected`/`valid_until` on `wiki_page` — verified `storage/wiki.py:180-211`), per-ADR pages are **in the recall corpus and immortal for free**. Explicit `recall()` IS the read path: `recall("...decision topic...")` surfaces the relevant ADR at decision time; `recall(type="wiki", tags=["adr"])` lists them targeted. A **thin index page** (`<project>-adr-index`) supplies what recall can't: sequential ID assignment, "list all open ADRs", supersede-chains. `project_brief` gains a **Recent ADRs** block so decisions surface at session start (catalog/restore/full modes). **Gap caught in audit (§A.5.1): the per-turn auto-recall hook is memory-only and will NOT surface ADRs — the plan adds an explicit read-side convention to close it.**

**B — Type-aware read-first-write dedup discipline.** With recall fast (Ettin), a `recall`-before-write is affordable for **durable** writes (facts/semantic memories, decisions/ADRs, feedback rules, curated wiki). `wiki_add` already has a drainer similarity gate (0.80 cosine, `dlq.py:262`); **`memorize` has NO gate at all** (confirmed `memorize_impl.py` — the only near-thing is the LLM contradiction phase, off by default, plus post-hoc consolidation merge at 0.95). B adds a **soft-gate on `memorize`** (default ON) that returns near-duplicates so the caller UPDATEs-in-place (`memory_update`) instead of appending, plus an **agent-discipline** protocol line for the contradiction judgment (which is not automatable). **Episodic memories are scoped OUT** — they are meant to accumulate + decay; gating them wastes cost.

**Storage-model recommendation:** reuse the wiki store, one page per ADR + one thin index page. Do NOT add a new `store_type` and do NOT move ADRs into the memory store. Rationale in [§A.1](#a1-storage-model-decision).

---

## Resolved decisions (record — all user-final, folded into the body)

1. **Monolith fate: AUDIT → migrate → DELETE.** Not keep-and-deprecate. For EVERY project's `<project>-adr-log`: first audit each ADR against the deprecated-ADR rule (§A.6.1), migrate surviving content to per-ADR pages, then **delete the monolith**. Applies to ALL projects in the DB, not just yadgar. Migration + tooling are project-agnostic and sweep every project.
2. **Add BOTH thin tools:** `adr_get` + `adr_list` (§A.4).
3. **`memorize` soft-gate default ON** (`YADGAR_MEMORIZE_SIM_GATE_ENABLED=true`, §B.2). Calibration pass required before shipping the default (§B.2).
4. **`memory_update` re-embed = option (a):** extend `memory_update` to re-embed ONLY when `content` actually changes (content-change guard keeps metadata-only patches cheap). NOT a new `memory_replace` tool (§B.2).
5. **Roll out to ALL projects** (not yadgar-first). §A.6 sweeps every project.
6. **Soft-gate trigger tags:** `{semantic} ∪ {feedback, decision, _anchor}` — confirmed. Keyed on caller-settable signals only (§B.2).
7. **memorize gate threshold = 0.85, CONFIGURABLE knob** `YADGAR_MEMORIZE_SIM_THRESHOLD` (default 0.85), NOT hardcoded. Calibration pass before defaulting on (§B.2).

---

## Investigation findings (verified against CURRENT code, file:line — 2026-07-14 audit)

> Citation corrections vs the DRAFT (paths drifted): `dlq.py` → `yadgar/backend/queue_drainer/dlq.py:262`; `store.py` (`_tag_filter_pass`) → `yadgar/_shared/wiki/store.py:874`; wiki `_compute_embedding` → `yadgar/_shared/wiki/store.py:2185`; `WIKI_SIM_CONTENT_THRESHOLD` / anchor-tier config → `yadgar/_shared/config/config.py:325` / `:637`; wiki 0.80 calibration comment → `config.py:319`; `memorize` args → `yadgar/core/server/tools/memorize.py:31`; `memory_update` → tool `admin_other.py:498`, backend `admin_exec/memory.py:61`; `wiki_exclude` line → `recall_pipeline.py:432`; `project.py:1339` `_get_adr_log_updated_at` (unchanged). The DRAFT's `recall_pipeline.py:398/458/432`, `dlq.py:261`, `store.py:896`, `config.py:319/635`, `project.py:1339`, `memorize.py:31` map to these corrected lines.

### The monolith today
- Storage: `adr_add` (`adr.py:180`) writes ALL ADRs into a single wiki page `<project>-adr-log` (`adr_log_slug`, `adr.py:99`). First ADR → `wiki_add(wait=True)`; subsequent → `wiki_append_section(position="new_section_bottom")` (`adr.py:290`).
- Record format (verified via `wiki_diff` v119→v120): `## ADR-NNNN: <title>` header at col 0, followed by flat bullets `status/date/context/decision/rationale/alternatives/consequences/revisit_trigger/supersedes`. `supersedes: ADR-0114` carries the chain.
- ID assignment: `_next_adr_id` (`adr.py:131`) **re-scans the whole page** for `^## ADR-(\d{4})` (`_ADR_HEADER_RE`, `adr.py:82`), takes max+1. Serialized under a per-project `threading.Lock` (`_adr_log_lock`, `adr.py:63`).
- Branch pinning: both read and write pin `default_branch` (`adr.py:251,265,296`; `_get_default_branch` imported from `project.py`). Known bug (recalled): unpinned `wiki_read` from a feature branch returns "not found" for the master-scoped page → per-ADR records inherit this risk unless every read pins `default_branch`.
- Version amplification: 120 full-content snapshots of a growing page (`wiki_page_version`) ≈ storage blowup — the version table holds ~Σ(v1..v120) bytes for 115 records.

### Recall corpus + type filtering
- `recall` MCP tool (`recall.py`) → backend `_fanout_recall` (`recall_pipeline.py:459`). Providers: `MemoryProvider` + `WikiProvider`, constructed in `_build_provider_tasks` (`recall_pipeline.py:399`), gathered in parallel and fused. `_source` stamped "memory"/"wiki" (`recall_pipeline.py:181`; `providers/wiki.py:93`).
- Wiki pages ARE embedded on write (`WikiStore._compute_embedding`, `store.py:2185`, formula `title\ncontent[:4000]`) and ARE retrieved by recall — a per-ADR wiki page is recall-visible with **zero new plumbing** (in the default profile).
- `store_type` is a **memory-only free `str`** (values "episodic"/"semantic"); recall **never filters on it**. Not the ADR hook.
- **Agent-prompt exclusion mechanism** (the structure to replicate, the visibility to INVERT): tag-based post-rank filter. `wiki_exclude = None if tags else ["agent-prompt","agent-prompt-toc"]` (`recall_pipeline.py:432`) → passed as `exclude_tags` to `WikiProvider` → `WikiStore._tag_filter_pass` drops matching pages (`store.py:874`). Override: passing `tags=["agent-prompt"]` sets exclude to None. **For ADRs we want the OPPOSITE: do NOT add "adr" to `wiki_exclude`**, so ADR pages surface in normal recall. (Replicate the one-record-per-item + tag-reachable structure; invert the recall-visibility decision.)
- **Profile gates the wiki arm (AUDIT — load-bearing for §A.5.1):** `_should_skip_wiki` (`recall_pipeline.py:300`) skips the WikiProvider arm entirely when the profile declares `wiki=False`. **`profile="fast"` declares wiki=False** (`recall_pipeline.py:314-316`, ADR-0077). Consequence: any caller using `profile="fast"` gets **memory-only** recall — no ADR pages. This is exactly what the per-turn auto-recall hook uses (see below).

### The automatic read path (AUDIT — the north-star trace)
- **Explicit `recall()` MCP tool:** default profile → fans out to WikiProvider → **surfaces ADR pages.** This is the load-bearing automatic-enough path, but it fires only when the model (or a subagent per B.3) chooses to call `recall`.
- **UserPromptSubmit auto-recall hook** (`yadgar/core/hooks/prompt-recall.py`, wired in `install_hooks_lib.py:448`): fires on EVERY user turn without the model choosing to look — the strongest candidate for "automatic." BUT it forwards to the backend `/recall` with **`profile="fast"`** (`http.py:1053`, comment: *"backend runs memory-only … no wiki fanout … ADR-0077"*). **Therefore it will NOT surface ADR wiki pages even after Part A.** Gap. Fix in §A.5.1.
- **SessionStart context hook** (`session-start-context.py`, wired `install_hooks_lib.py:434`): loads `project_brief`. This IS where the new "Recent ADRs" block lands — but only if the block renders in the mode the hook requests (§A.5.2).

### Write gates + retention (the decision hinge)
- **Wiki pages never decay** — the `wiki_page` CREATE SET clause (`storage/wiki.py:180-211`) has NO `heat`/`is_protected`/`valid_until`; the `heat`/`is_protected` matches elsewhere in that file (lines 923-1036) are `memory` CREATE statements (promotion helpers), not `wiki_page`. Decay loop touches only the `memory` table. **This is the hinge: reuse-wiki gives ADR immortality for free — no anchor tier, no memory store needed.**
- `wiki_add` gate: enforced in the **drainer** (`dlq.py:262` `_sim_gate_for_drainer`), NOT the request thread (I9 fix). **Bypassed on `force=True` / `replace_slug` set / `append=True`** (verified `dlq.py:281-283`). Threshold `WIKI_SIM_CONTENT_THRESHOLD=0.80` (`config.py:325`), mode `WIKI_SIM_MODE="hard"`. `wiki_check_duplicate` is the read-only preflight.
- **`memorize` has NO similarity/dedup gate** (verified `memorize_impl.py` phase order: validate→resolve_branch→embed→contradiction→store→post_write; contradiction only runs under `YADGAR_CONFLICT_RESOLVER=on`, default off). Post-hoc only: consolidation `_merge_duplicates` at 0.95 cosine.
- `memory_update` (tool `admin_other.py:498`; backend `admin_exec/memory.py:61`) patches `{content, tags, is_protected, is_stale}` only. **Content update does NOT re-embed** — backend calls `update_memory_fields` (`storage/memory.py:964`), which writes the DB row and only invalidates the memory-doc cache; it never re-encodes an embedding (verified). A dedup-by-update flow MUST re-embed (§B.2 fix).
- Anchor tiers (`config.py:637`): `ANCHOR_EPHEMERAL_TTL_DAYS=14` / `ANCHOR_CONDITIONAL_TTL_DAYS=90` / `semantic_immortal` no-expiry (requires `reason`). All auto-set `is_protected=True`. **Not needed for ADRs** (wiki is already immortal).

### Surfacing points
- `project_brief` render (`project.py:183` `_render_project_brief`): sections Global/Project Anchors → Checkpoint → Hot Memories (`project.py:257`) → Wiki Index. **Natural ADR insertion point: a new "## Recent ADRs" section after Hot Memories.** Data via a metadata-only index read (like `_get_adr_log_updated_at`, `project.py:1339`).
- A **write-side** ADR nudge already exists: `_apply_adr_signal` (`project.py:1401`) appends a `capture_adr` action when active_work is newer than the ADR log. A/task #76 adds the **read-side** counterpart.
- Config already present: `YADGAR_ADR_DUE_WARN_HOURS=12.0` (`config_registry.py:326`), `YADGAR_AGENT_PROMPT_LIBRARY_ENABLED` (`config_registry.py:428`).

---

## Part A — Recall-native ADR records

### A.1 Storage-model decision

**Decision: reuse the wiki store — one page per ADR + one thin index page. No new `store_type`, no memory-store migration.**

Discriminator resolved by evidence: the task pulls two ways — "agent-prompt library is the model to extend" (agent-prompts live in the wiki store) vs "decisions ≠ curated wiki … different retention." Reconciliation: **a distinct type does NOT require a new store_type or a new table.** The agent-prompt precedent proves a distinct, filterable, tag-reachable class lives atop the wiki store. The retention objection dissolves because **wiki pages already never decay** (verified) — the "different retention" ADRs need (never age out, unlike episodic memories) is exactly what the wiki store provides natively. A memory-store ADR would need `tier="semantic_immortal"` machinery to get what wiki gives for free, and would fragment the record across two stores. Reuse-wiki wins on all axes: recall-visible (WikiProvider, default profile), immortal (no decay), versioned (wiki_page_version), branch-aware (§25), and already the substrate ADRs live in today.

**ADR stays a DISTINCT TYPE** — achieved by the `["adr"]` tag + slug prefix `<project>-adr-NNNN`, the same way agent-prompts are a distinct type via their tag + slug. Same recall plumbing, different role (decision record) and different discovery affordance (index + supersede-chain), without dissolving ADRs into generic memories or generic wiki noise.

### A.2 Per-ADR record design

- **Slug:** `<project>-adr-NNNN` (e.g. `yadgar-adr-0042`). Zero-padded 4-digit, matching existing ID format. `<project>` is the project stem the monolith used — the migration derives it per project from each `<project>-adr-log` slug (§A.6).
- **Title:** `ADR-NNNN: <title>`.
- **Content:** the existing ADR body rendering (unchanged) — the 9 flat bullets. Reuse `_build_adr_body`/`record.to_markdown_body()` (`adr.py:144,176`) verbatim; only the destination changes (own page, not a section).
- **Tags:** `["adr","decisions", "adr-status:<status>", "adr-<NNNN>"]`. The `adr-status:*` tag lets a targeted recall/query filter open vs superseded without parsing content. The `adr-<NNNN>` tag supports precise cross-ref lookup.
- **Category:** `decision` (existing wiki category; better than `reference`).
- **`directory_context`:** each ADR page carries its OWN project's `directory_context` (the project the ADR belongs to), NOT `global` and NOT hardcoded to yadgar — the migration threads it through per project (§A.6). This keeps recall scoping correct: a project's ADRs surface for that project.
- **Branch:** always `branch_hint=default_branch` (canonical), both write and every read. This closes the recalled feature-branch "not found" bug at the source — records are written to and read from the default-branch slot, and recall resolution must use `branch_hint=default_branch` for ADR pages regardless of the caller's working branch.
- **Recall visibility:** `["adr"]` is NOT added to `wiki_exclude` (`recall_pipeline.py:432`) → ADR pages appear in normal `recall(query)` **under the default profile**. (Inversion of the agent-prompt exclusion — the whole point of task #76.) Note the profile caveat: `profile="fast"` callers skip the wiki arm regardless (§A.5.1).

### A.3 Thin index design

The index is what recall cannot do: authoritative sequential ID, open-ADR list, supersede-chains.

- **Physical form:** one small wiki page per project, `<project>-adr-index`, tagged `["adr","adr-index"]`, default-branch-pinned, carrying the project's `directory_context`. Kept small (metadata rows only, not full ADR bodies) so `wiki_read` never errors on it. One table:

  | ADR | Status | Date | Title | Supersedes | Superseded-by | Slug |
  |---|---|---|---|---|---|---|

- **ID source of truth:** the index, not a page re-scan. `_next_adr_id` reads the index (max ADR + 1) instead of scanning the monolith. Reuse the existing `_adr_log_lock` read-modify-write pattern (`adr.py:63`) around the index read → assign → write-record → append-index-row triple, so concurrent `adr_add` cannot duplicate IDs.
- **`adr-index` tagging:** the index page IS surfaced in normal recall (it's a compact map, not noise) — but to avoid it competing with the actual ADR body pages, tag it `adr-index` and let `project_brief` read it directly by slug (metadata read, not recall).
- **Supersede-chains:** when a new ADR sets `supersedes: ADR-0114`, `adr_add` (a) writes the new record, (b) appends the index row, (c) patches ADR-0114's index row `Superseded-by` column AND flips its `adr-status:*` tag to `superseded`. **Tag flip uses `wiki_update(page_id, fields={"tags": [...]})`** — NOT `wiki_set_metadata` (that tool only accepts `directory_context`/`branch`, rejects tags). This keeps "list all open ADRs" (filter index Status==open) correct.
- **Index version cost (accepted):** every `adr_add` re-snapshots the index page into `wiki_page_version` — the same amplification pattern flagged as the monolith's sin. But at ~150-char rows the per-version delta is ~3 orders smaller than the monolith's growing full-page snapshots; the index stays a few KB even at hundreds of ADRs. Bounded and acceptable. (The ADR **bodies** — the bulk — no longer re-snapshot on every add, which is the amplification that actually mattered.)

### A.4 New/changed tools

Both thin deterministic tools are added (decision 2).

- `adr_add` (rewrite, `adr.py`): write per-ADR page + append index row + patch supersede targets. Signature UNCHANGED (schema stability). Returns `{adr_id, slug, version}`. **MUST create each per-ADR page with `wiki_add(..., force=True)`** to bypass the 0.80 drainer sim gate (`dlq.py:262`, hard mode; `force=True` bypass verified `dlq.py:281`): two distinct decisions on the same subsystem share the fixed bullet template + topic vocabulary (e.g. ADR-0115 literally reverses ADR-0114) and would plausibly exceed 0.80 → the second would be silently rejected to DLQ as `duplicate_detected`, losing a real ADR. Dedup for ADRs is handled by `adr_add`'s own index/ID check, NOT the wiki similarity gate. (Create path can't use `replace_slug`, so `force=True` is the only bypass.)
- `adr_list(status=None)` (new, thin): read the index page, return rows; optional `status=` filter. Cheap — one metadata read. Covers the deterministic "show me all open ADRs" that recall's fuzzy discovery can't.
- `adr_get(adr_id)` (new, thin): `wiki_read(<project>-adr-NNNN, branch_hint=default_branch)` — direct fetch by id. Removes the branch-pin footgun (a naive `wiki_read` from a feature branch would "not found").
- No change to the `ADR` model or `_build_adr_body`.

### A.5 Read-path surfacing (the north-star section)

Two complementary surfaces + one convention. **Neither surface alone covers planning/debug** — spell out why:

- **`project_brief` "Recent ADRs" = temporal.** Newest-N ADRs. Good at session start ("what did we decide lately"); does NOT guarantee relevance to the specific feature being planned.
- **Explicit `recall(query)` = semantic.** Surfaces the ADR relevant to the current topic — but only when a `recall` is issued (manual, or per B.3 convention), and only under the default profile.

#### A.5.1 The automatic-recall gap + fix (AUDIT)

**Gap:** the per-turn auto-recall hook (`prompt-recall.py`) uses `profile="fast"`, which skips the wiki arm (`_should_skip_wiki`, `recall_pipeline.py:314-316`). So making ADRs recall-native does NOT make them auto-surface on every turn. The only automatic surface is `project_brief` at session start (temporal, not topic-relevant).

**Fix — a planning-time recall convention (chosen; cheapest, on the read-path, no latency regression):**
Add to the write-back/read-first protocol (B.3) an explicit READ-side line: *"Before planning a feature or debugging a subsystem, `recall(type='wiki', tags=['adr'])` or `recall('<subsystem> decision')` for prior ADRs on that topic; consult `adr_list(status='open')` for open decisions."* This is the discipline half of "get used at planning time," and it uses the DEFAULT-profile explicit `recall` (which DOES fan out to wiki), sidestepping the fast-profile gap entirely.

**Rejected alternatives (documented so a builder doesn't re-litigate):**
- *Flip the auto-recall hook to include wiki.* Rejected: the hook is on the hot per-turn path with a strict latency budget (`HOOK_RECALL_TIMEOUT_S`); ADR-0077 deliberately made it memory-only. Adding a wiki fanout to every turn is a latency regression for marginal benefit (the session-start `project_brief` block + the planning-time convention already cover the intent).
- *A dedicated stop-hook/session-start ADR nudge.* Redundant with the existing `_apply_adr_signal` write-nudge + the new Recent-ADRs block; would add machinery not on the read path. Cut per "unused = cut it."

#### A.5.2 project_brief wiring

- Add `_build_recent_adrs(storage, resolved, limit=3)` (metadata-only read of the per-project index page, newest N + any `open`-status). Same defensive pattern as `_get_adr_log_updated_at` (`project.py:1339`).
- Render a **`## Recent ADRs`** section in `_render_project_brief` after Hot Memories (`project.py:257`), one line per ADR: `[ADR-NNNN] <status> — <title>`. Length-capped like the wiki catalog.
- **Modes (AUDIT — verify against the session-start hook):** include in `catalog`/`restore`/`full`; EXCLUDE from `signals` (keep signals ≤100 tokens). The SessionStart context hook loads `project_brief` — confirm during impl which mode it requests (default is `catalog`, `project.py:2027`). If it requests `signals`, the Recent-ADRs block will NOT render at session start; in that case either (a) fit a single compact `[ADR-NNNN] open — <title>` line into the signals budget, or (b) accept that session-start ADR surfacing is catalog/restore/full only and rely on the A.5.1 convention. Decide during A3 with the observed hook mode; do not assert "surfaces at session start" until confirmed.
- The existing write-side `capture_adr` signal (`_apply_adr_signal`) stays as-is — read + write nudges now both exist.

### A.6 Migration plan (ALL projects: audit → migrate → DELETE monolith)

One-shot idempotent, project-AGNOSTIC migration script `scripts/migrate_adr_monolith.py` (invoked by the user per HARD RULE — no auto-apply; hand the command over in `MIGRATION_NOTES.md`). It **sweeps every project's ADR log**, not just yadgar's.

**Project enumeration.** Query the wiki store for every page whose slug ends in `-adr-log` (`SELECT slug, directory_context, branch FROM wiki_page WHERE slug LIKE '%-adr-log'` or the store's equivalent). For each match derive `<project>` = slug with the `-adr-log` suffix stripped, and capture that page's own `directory_context` + default branch — thread BOTH through to its per-ADR pages and index. IDs are sequential PER PROJECT (each project keeps its own 0001.. sequence).

Per project:

1. **Read** the monolith. **Verified 2026-07-14:** the daemon-side tool returns the FULL content — a `wiki_get`/`wiki_read` "error" is the MCP *client* token-limit truncation (content is written to a tool-result file intact), NOT a storage/tool failure. A server-side migration script (importing the wiki store directly) has full access. No version-reconstruction fallback needed.
2. **Parse** each `## ADR-NNNN: <title>` section with `_ADR_HEADER_RE` (`adr.py:82`) — split on header boundaries. For each: extract the 9 bullets + title + status.
3. **Audit — drop deprecated ADRs (§A.6.1 rule).** Apply the deprecated-ADR rule to each parsed record BEFORE emitting. Records that fail the rule are NOT migrated (logged in the dry-run report). Records that pass proceed.
4. **Emit** one `wiki_add(slug=<project>-adr-NNNN, ..., force=True, branch_hint=<project-default-branch>, directory=<project-directory_context>)` per SURVIVING ADR (`force=True` to skip the 0.80 sim gate — near-identical ADRs are legitimately distinct records). Preserve original `date` and original `NNNN` verbatim (never renumber — gaps from dropped/absent IDs are fine).
5. **Build** the per-project index page from surviving rows; compute `Superseded-by` back-links by inverting each `supersedes:` bullet (two-pass — see risks).
6. **Verify** each project: count surviving per-ADR pages == expected; index rows == pages; supersede links resolve; no stray `<project>-adr-*` on a feature branch (branch-drift scan).
7. **DELETE the monolith** (decision 1): after per-project verification passes, `wiki_delete(<project>-adr-log)`. This removes the write-only page and its version-table bloat. (No `wiki_exclude` entry, no `adr-archive` tag — the page is gone, so both are moot.)
8. **Idempotency:** skip any `<project>-adr-NNNN` slug that already exists (re-runnable). A `--dry-run` prints, per project: "would migrate N ADRs (M dropped as deprecated), create index, resolve K supersede links, then DELETE <project>-adr-log". Deletion is gated behind an explicit `--delete-monolith` flag AND successful verification — never deletes on a partial/failed migration.

#### A.6.1 Deprecated-ADR audit rule (concrete, chain-safe)

Who/what decides an ADR is deprecated: **the ADR's own `status` field**, with a supersede-chain safeguard.

- `status == superseded` → **RETAIN** as its own per-ADR page (tag `adr-status:superseded`). Do NOT drop. Reason: (1) a superseded ADR is the inbound target of a later ADR's `supersedes:` — dropping it dangles the chain; (2) "why was this reversed" is high-value debug context. Superseded ADRs stay consultable.
- `status ∈ {rejected, deprecated}` AND **no inbound supersede reference** (no other surviving ADR names it in `supersedes:`) → **DROP** (do not migrate). These are dead decisions with no chain value.
- `status ∈ {rejected, deprecated}` BUT **has an inbound supersede reference** → **RETAIN** (chain integrity wins). Tag it with its status so `adr_list` can still filter it out of "open".
- `status ∈ {open, accepted}` → **RETAIN** (default: keep).

Rationale stated so a builder doesn't paper over it: status-based dropping is safe ONLY when it never orphans a supersede target. The inbound-reference check is what makes it safe. Compute inbound references in the same two-pass that builds `Superseded-by` back-links (§A.6 step 5): pass 1 parses all records + collects every `supersedes:` target across the project; pass 2 applies the drop rule using that target set.

**Migration risks:**
- ID gaps / non-contiguous NNNN → preserve original IDs verbatim, never renumber. Gaps (from drops or pre-existing absences) are expected and fine.
- Supersede targets referencing not-yet-migrated IDs → two-pass: parse+collect all, then emit + resolve links.
- Large read (yadgar's 276k): RESOLVED (§A.6-1) — daemon returns full content; only the MCP client transport truncates. Server-side migration is unaffected.
- Branch drift: if any ADR was accidentally written to a feature-branch slot (the recalled bug), a default-branch-only migration would miss it — scan for stray `<project>-adr-log` rows across branches per project before declaring that project done.
- Cross-project scale: many projects, each with a monolith → the sweep must be resumable (idempotent skip on existing per-ADR slugs makes a re-run cheap) and must isolate failures (one project's bad parse must not abort the others; log + continue).

---

## Part B — Type-aware read-first-write discipline

### B.1 Scope

| Write type | Store | Gate today | B adds |
|---|---|---|---|
| Episodic memory (session activity, transient obs) | memory | none | **nothing — scoped OUT** (meant to accumulate + decay) |
| Semantic memory (durable fact) | memory | none | **soft-gate on `memorize`** (default ON) |
| Feedback rule (durable) | memory | none | **soft-gate on `memorize`** (default ON) |
| ADR / decision | wiki (per A) | wiki 0.80 hard gate **bypassed by design** (`force=True` in `adr_add`, §A.4) — dedup via the index, not similarity | agent-discipline for contradiction |
| Curated wiki page | wiki | 0.80 hard gate (drainer) | already gated |

The gap B closes: durable **memory** writes have no dedup path; `wiki_add` already does. B is narrowly a `memorize` soft-gate + an agent-discipline protocol line — NOT a rewrite of the wiki gate.

### B.2 memorize soft-gate (tooling)

- Add a **soft** (non-blocking) similarity check to `memorize` for durable writes only. Trigger on **caller-settable signals only** (decision 6): `tags` intersect `{feedback, decision, _anchor}`, OR `is_protected=True`, OR any `tier` set. Do NOT key on `store_type` — `memorize` has no `store_type` param (verified `memorize.py:31` — args are content/context/tags/is_protected/tier/valid_until/ttl_days/reason/provenance_agent/branch_hint), and the field defaults `"episodic"` and is set by the CLS classifier in `_phase_store` AFTER the gate point, so it is always "episodic" at gate-time (a `store_type`-keyed semantic branch would never fire). Plain episodic writes (no durable tag, not protected, no tier) bypass entirely. The `{semantic}` in the resolved trigger set maps to "durable, caller-settable" signals — i.e. the tag/protected/tier signals above, since the literal `store_type=="semantic"` is unavailable at gate-time.
- Mechanism: before store, embed the content (the embed phase already runs — `_phase_embed`), query top-K memories by cosine in the same directory, and if any exceed the threshold, **return them in the response** as `near_duplicates: [{id, content, score}]` WITHOUT blocking the write. Soft, not hard — the caller (agent) decides update-vs-append.
- **New config (all in `config_registry.py` + `config.py`):**
  - `YADGAR_MEMORIZE_SIM_GATE_ENABLED` — default **`true`** (decision 3, default ON).
  - `YADGAR_MEMORIZE_SIM_THRESHOLD` — default **`0.85`**, a CONFIGURABLE KNOB (decision 7), NOT hardcoded. Higher than wiki's 0.80 because memory content is terser/noisier. **Calibration pass REQUIRED before shipping the default** — mirror the wiki 0.80 calibration (`config.py:319` comment block, `test_wiki_sim_calibration.py`): assemble sample near-dup vs distinct memory pairs, confirm 0.85 sits in the separation margin, adjust if the margin says otherwise. Do the calibration in the B1 car; do not default-on until it passes.
  - `YADGAR_MEMORIZE_SIM_TOP_K` — default `3`.
- The response shape lets the agent then call `memory_update(id, {content: merged})` to update-in-place. **Fix required (decision 4, option a):** extend `memory_update` to re-embed **only when `content` actually changes**. Backend `memory_update` (`admin_exec/memory.py:61`) currently calls `update_memory_fields` (`storage/memory.py:964`) which writes the row + invalidates the memory-doc cache but NEVER re-encodes — so a content patch keeps a stale vector. Add a content-change guard: if `fields` contains `content` AND the new value differs from the stored content, encode a fresh embedding and include it in the update (metadata-only patches — tags/is_stale/is_protected — take the cheap no-re-embed path). This fixes a latent correctness gap with the smallest surface; no new `memory_replace` tool.

### B.3 Agent-discipline (protocol)

Dedup is similarity-automatable (B.2). **Contradiction detection is a judgment on recall results, not a gate** — encode it as protocol, not code. TWO protocol lines (read-side + write-side):

- **Read-side (the A.5.1 gap-fix):** *"Before planning a feature or debugging a subsystem, `recall(type='wiki', tags=['adr'])` / `recall('<subsystem> decision')` for prior ADRs, and `adr_list(status='open')` for open decisions. Consult them before committing to an approach."* (Uses the default profile — fans out to wiki, unlike the fast-profile auto-recall hook.)
- **Write-side:** *"Before a DURABLE write (semantic fact, feedback rule, ADR), `recall` the topic. If a near-duplicate exists → UPDATE-in-place (`memory_update` / wiki `replace_slug`) instead of appending. If it CONTRADICTS observed state → mark the old stale (`memory_update is_stale=true`) or supersede (ADR). Episodic writes: skip this — just write."*
- Where added: the stop-hook write-back prompt (`yadgar/tests/hooks/test_stop_hook_template.py` guards the template) + `~/.claude/agent-instructions.md` write-back triggers.
- Where enforced: **both** — the `memorize` soft-gate (B.2) surfaces the near-dups mechanically; the protocol makes the agent act on them and READ before planning. Neither alone suffices: tooling can't judge contradiction; discipline alone is the write-only failure mode task #76 exists to kill.
- Do NOT promise automated contradiction detection — out of scope; the task frames it as judgment.

### B.4 Coupling to A

A makes ADRs recall-visible (default profile) → the read-first step in B.3 can actually find prior ADRs (impossible today, the monolith is unreadable). B without A leaves the "recall the topic" step blind for decisions. Hence A ships first.

---

## Sequencing / cars

| Car | Deliverable | Depends on |
|---|---|---|
| A1 | Per-ADR page + per-project index schema; `adr_add` rewrite; `_adr_log_lock` reused against index; `directory_context` threaded | — |
| A2 | `adr_list` / `adr_get` thin tools | A1 |
| A3 | `project_brief` `## Recent ADRs` section + `_build_recent_adrs`; confirm session-start hook mode (§A.5.2) | A1 |
| A4 | Project-agnostic migration script (enumerate all `*-adr-log`, audit-drop deprecated, migrate, verify, DELETE) + `MIGRATION_NOTES.md` (dry-run, idempotent) | A1 |
| A5 | Recall-visibility wiring: confirm `adr` NOT in `wiki_exclude` (nothing to add to exclude — monolith deleted) | A4 |
| B1 | `memorize` soft-gate + config (3 knobs, threshold calibration) + `near_duplicates` response | A (recall-visible ADRs) |
| B2 | `memory_update` re-embed-on-content-change (content-change guard) | — (can parallel A) |
| B3 | Write-back + read-first protocol update (stop-hook prompt + agent-instructions): read-side ADR-recall line (A.5.1) + write-side dedup line | B1 |

---

## Test strategy (describe — investigate+plan only)

Extend existing suites: `yadgar/tests/core/test_adr.py`, `test_project_brief_adr_log.py`, `yadgar/tests/hooks/test_stop_hook_template.py`; add `yadgar/tests/backend/` cases for the memorize gate + migration.

- **ID-still-sequential:** `adr_add` ×3 → IDs 0001/0002/0003 sourced from the index, not a page re-scan; concurrent calls (lock) never duplicate.
- **recall-surfaces-ADR (default profile):** write an ADR, `recall("<its topic>")` returns the per-ADR page with `_source=="wiki"`; confirm it is NOT excluded. **Also assert the fast-profile gap:** `recall(profile="fast", "<topic>")` does NOT return the wiki ADR (documents the A.5.1 constraint so a future change doesn't silently regress the convention rationale).
- **branch-pin-resolves:** `adr_get`/recall of an ADR from a simulated feature-branch cwd still resolves the default-branch record (closes the recalled bug).
- **index-integrity:** supersede flips `adr-status` tag + `Superseded-by` back-link; `adr_list(status="open")` excludes superseded.
- **migration-all-projects:** two synthetic project monoliths → migrate → each yields its own per-ADR pages + index with correct per-project `directory_context`; IDs sequential per project; run twice → second run creates 0 pages; monolith deleted only after verify.
- **migration-deprecated-audit:** a `rejected` ADR with no inbound supersede → dropped; a `superseded` ADR → retained; a `deprecated` ADR that is a supersede target → retained (chain-safe).
- **project_brief-render:** `## Recent ADRs` present in catalog/restore/full, absent in signals; length-capped.
- **memorize-soft-gate:** durable write (tag `decision`/`feedback`/`_anchor` or `tier`/`is_protected`) with a near-dup returns `near_duplicates` and STILL stores (soft); plain episodic write bypasses (no `near_duplicates` key); threshold boundary; gate honors `YADGAR_MEMORIZE_SIM_GATE_ENABLED=false`.
- **memory_update-reembed:** content patch (changed value) re-embeds; content patch (same value) does NOT; tags-only patch does NOT.

Loop-until-clean: run `pytest` on touched suites + lint/types after each car.
