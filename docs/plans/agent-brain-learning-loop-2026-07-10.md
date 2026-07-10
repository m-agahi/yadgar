# Agent-Brain Learning Loop — Design Investigation

**Date:** 2026-07-10
**Status:** design / plan-only (no implementation)
**Author:** investigation dispatch (opus + advisor + 2 code-tracing subagents)
**User goal (verbatim):** "i am trying to create a brain that agents can use to get better at the work."

---

## TL;DR — BLUF

**The through-line both questions orbit: CAPTURE works, PROMOTION doesn't.** Agent
experience already flows into memory (two paths, both verified). What is 100%
manual main-thread labour is turning that captured experience into *better
disciplines, patterns, and genesis corpus* — the "brain gets better" part. Q1 is
the write end of that pipe; Q2 is the promote end.

- **Q1 — Should subagents memorize? Keep the rule, but fix why it exists.** Rule 3
  does **not** gate capture — subagent findings *already become memories* via the
  SubagentStop hook, which calls the same `memorize()`. The real defect is not
  "agents can't write"; it's that the write path stores **verbatim worktree paths
  + feature branches**, so worktree-spawned findings are **orphaned** (invisible to
  canonical-repo recall) and never cleaned up. **Recommendation: (a) keep rule 3 as
  the default + (c-normalized) fix worktree→canonical-root+branch normalization on
  the write path.** Opening inline `memorize` adds variance and merge-collision
  surface for ~zero capture gain. The contract text change is small (below).

- **Q2 — Yes, the stop-hook materials loop needs changes, but PROPOSE-not-APPLY.**
  ADR-0091 explicitly forbids auto-editing genesis/disciplines from runtime content.
  Ride the **existing `recommended_actions` signals rail** (checkpoint step 5) rather
  than a new hook step. Highest value / lowest risk: a **genesis-drift backflow
  cadence nag** (the ADR-0091 loop's missing trigger) and a **discipline-staleness
  review nag** gated on *incident/correction signal*, not raw usage. The
  **incidents-ledger** primitive (the pgrep rule was 6 uncounted incidents) is the
  genuinely valuable new mechanism — cheap first cut is a per-discipline correction
  counter, not full semantic attribution.

---

# Q1 — Should subagents memorize?

## Q1.1 Mechanics findings (code-cited)

### Finding A — Subagent findings ALREADY become memories (rule 3 routes, doesn't gate)

The `## Yadgar findings` footer is not a "lesser" capture path that avoids
`memorize`. It **is** a `memorize` call, made by the daemon on the agent's behalf:

- `SubagentStop` hook (`yadgar/core/hooks/subagent_stop.py`) reads the subagent's
  `transcript_path`, extracts `## Yadgar findings` bullets (`_extract_findings`,
  `subagent_stop.py:116-149`), detects branch from cwd (`_detect_branch_from_cwd`,
  `:251-272`), and POSTs to `/hooks/subagent-stop`.
- The endpoint (`yadgar/core/server/http.py:1092-1193`) loops the bullets and calls
  `memorize(content=finding, context=cwd, tags=["from-subagent",
  "agent-type:<type>"], provenance_agent=agent_type, branch_hint=_branch_hint)`
  (`http.py:1163-1178`).

**Consequence:** whatever risk a *direct* `memorize` carries is **already
happening** via the footer. Rule 3 does not protect against pollution or bad
writes — it only changes *who phrases the write* (agent free-form vs. curated
footer bullet). The "long_running may memorize with provenance" exception is
partly redundant: the hook already stamps `provenance_agent`.

### Finding B — The write path stores verbatim paths + feature branches (the real defect)

`memorize` never normalizes `context`:

- `context` is stored **verbatim** as `directory_context` (`memorize.py:145`
  enqueue payload; `_phase_store.py:137` `"directory_context": ctx.context`;
  `insert_memory` `_shared/storage/memory.py:271` does no normalization).
- Branch resolves via `_detect_branch(context)` (`memorize.py:116`,
  `tools/project.py:99-122`), which runs `git -C <dir> rev-parse --abbrev-ref
  HEAD` — **no `--git-common-dir` worktree collapse**.
- `_resolve_project_root` (git-root via `--show-toplevel`,
  `server_helpers.py:185`) exists but is used **only** for the cache-epoch bump
  (`_bump_epoch_for_context`), **NOT** for the stored `directory_context`.

**Empirically verified** on a live stale worktree in this repo:

```
$ git -C .claude/worktrees/agent-a2ccfddd5ff0f332b rev-parse --show-toplevel --git-common-dir --abbrev-ref HEAD
/home/max/git/yadgar/.claude/worktrees/agent-a2ccfddd5ff0f332b   # show-toplevel = worktree path, NOT main repo
/home/max/git/yadgar/.git                                         # common-dir DOES resolve to shared repo
feat/t2-car-b                                                     # branch = feature branch
```

So a finding memorized from a worktree lands as
`directory_context=/home/max/git/yadgar/.claude/worktrees/agent-xyz`,
`branch=feat/foo`.

### Finding C — Recall is exact-match; the failure is ORPHANING, not pollution

- Recall's `directory` is only trailing-slash-stripped (`recall.py:188`) — no
  git-root normalization.
- The active directory filter is a **live Python post-filter**
  `is_directory_eligible` (`_shared/retrieval/providers/memory.py:81-90`,
  `_shared/storage/directory.py:56-79`): a row passes iff
  `directory_context == caller_dir` OR the row is a sentinel (`global`/`''`/`None`).
  (The `ScopeFilter` SQL clause is dead/future code per the provider docstring at
  `providers/memory.py:33`.)
- Branch filter (`_shared/storage/branch.py:33-52`) allows `branch IS NONE OR
  branch = default OR branch = current`.

**Consequence:** when the main repo calls `recall(directory=/home/max/git/yadgar)`,
the worktree row (`directory_context=.../worktrees/agent-xyz`) is neither equal nor
a sentinel → **excluded**. It does **not leak** into canonical recall; it is
**orphaned** — recoverable only by recalling the exact worktree path, which never
recurs once the worktree is deleted.

### Finding D — Orphans are never cleaned up (asymmetry with wiki)

`wiki_cleanup_merged_branches` deletes wiki pages whose branch left `git branch -a`
(`yadgar/backend/admin_exec/project.py:129`). **There is no memory-side
equivalent.** Worktree/feature-branch memories on merged-then-deleted branches
dangle forever, invisible and un-garbage-collected.

### Finding E — The one real cross-scope hazard: the curator merge

New (and subagent) memories pass through the curator, whose
`find_similar_memories` → `storage.search_vectors` is a **global embedding search
with NO directory or branch filter** (`curation/ingestion.py:39-59`). On a
sufficiently-similar hit it **merges** (`merge_memory`, `ingestion.py:109-115`) —
which leaves the surviving row's `directory_context` **unchanged** but overwrites
`branch` with the new caller's branch (`_phase_store.py:98-107`). Net: a merge can
silently absorb canonical knowledge into an orphaned worktree-stamped row (or
vice-versa) with **no scope guard** — a split-brain row (old dir + new branch).

This is the actual reason "open memorize wide" is risky — but note it **already
fires on the footer path today**.

### Finding F — Supporting facts

- **No memory dup-reject gate.** The predictive-coding write gate is shadow-only
  (`WRITE_GATE_THRESHOLD=0.0`, `config.py`); only the secret gate hard-rejects
  (`validate.py`, `gate_or_reject`). There is no wiki-style similarity reject for
  memories — the curator merge is the only dedup, and it is unscoped (Finding E).
- **`provenance_agent` is write-only.** Stored + validated (ASCII, ≤64 chars) but
  **no read-side consumer** — no recall filter, curation decision, or promotion
  logic reads it back. Any design that relies on provenance to gate/quarantine
  agent writes must **build the consumer** — it does not exist yet.
- **Main-thread auto-capture is branch-safe but low-fidelity.** PostToolUse →
  `action_log` (state-modifying tools only, batched 5, ≤500-char summaries) →
  consolidated into `Session activity [Bash(3)…]` memories tagged
  `_action_stream/_auto`, `heat 0.4`, `branch=None` (canonical slot, intentional —
  `cleanup.py:185-203`). **Subagent-internal tool calls are captured nowhere** —
  only the footer survives. Fidelity is tool names + one 80-char field preview; no
  outputs, no reasoning.

## Q1.2 Options spectrum

| # | Option | What it costs | What breaks | What the brain gains |
|---|--------|---------------|-------------|----------------------|
| **(a)** | **Status quo + strengthen harvest** (keep rule 3; do nothing to the write path) | ~0 | Nothing new. But leaves Findings B–E unfixed: worktree findings keep orphaning; curator can split-brain | Nothing beyond today. Capture already works; the leak is silent data loss, not a capability gap |
| **(b)** | **Agents write to a DRAFTS/quarantine tier** that consolidation or main thread promotes | Medium-high: the `wiki_draft` table exists but has **zero production writers** (`insert_wiki_draft` non-test callers = 0); a provenance-tagged memory quarantine needs a **new promotion consumer** (provenance is write-only today, Finding F) | Adds a review queue nobody drains → same write-only fate as usage counters unless a consumer is built and *run*. Doubles the surface: a quarantine that is never promoted is strictly worse than a memory that is at least recall-visible | A safe holding pen — *if* a promoter exists. Without the promoter, it is theater |
| **(c-open)** | **Open `memorize` to all agents** with mandatory provenance | Low code, high risk | Multiplies curator-merge collisions (Finding E) across many uncoordinated writers; provenance gives false comfort (no consumer). More free-form → more near-dup churn | More raw writes, mostly redundant with the footer. Low signal-to-noise |
| **(c-normalized)** | **Keep rule 3 default; normalize worktree→canonical-root + branch on the write path** (fix Findings B–D for BOTH footer and any direct path) | Low-medium: add `--git-common-dir`-based root resolution + default-branch pin in `_detect_branch`/context normalization, gated to worktree contexts; add a memory-side merged-branch GC | Risk: over-normalizing could collapse *intentionally* branch-scoped memories. Mitigate: only collapse `.claude/worktrees/*` + `/tmp/*` throwaway paths to canonical root, leave real project dirs alone | Findings you **already capture** stop being orphaned. The footer path (the one agents actually use) starts landing in canonical recall. Fixes the leak at the source, not per-writer |
| **(d)** | **Do (c-normalized) now + defer (b) until a promoter is proven** | Same as (c-normalized) | — | Best ratio: fixes the real bug; avoids building a second write-only queue |

## Q1.3 Recommendation

**Adopt (c-normalized) + keep rule 3; defer (b).**

Rationale, brutally: the contract's framing ("agents shouldn't memorize") is
answering the wrong question. Capture is not the bottleneck — it already happens.
The bottleneck is that the capture we *do* is **silently orphaned when it comes
from worktrees** (which is exactly where parallel subagent work happens under
Orchestrator Mode). Opening inline `memorize` (c-open) adds writers to a path that
already leaks; it does not fix the leak. Building a drafts/quarantine tier (b)
before a promotion consumer exists reproduces the usage-counter mistake (a store
nothing reads).

**Concrete write-path changes (for a later build, not this plan):**
1. Normalize `directory_context` for throwaway contexts: when `context` resolves
   (via `git rev-parse --git-common-dir`) to a shared `.git` under a canonical
   repo, store the **canonical repo root** as `directory_context`, not the
   worktree path. Scope the rewrite to `.claude/worktrees/*` and `/tmp/*` so real
   project dirs are untouched.
2. Pin the branch for those normalized writes to the **default branch** (the
   `_action_stream` consolidation path already does `branch=None`; the footer path
   should match — cross-branch facts belong in the canonical slot).
3. Add a memory-side merged-branch GC parallel to
   `wiki_cleanup_merged_branches` (Finding D) as a `recommended_action`.
4. Scope-guard the curator merge (Finding E): refuse to merge across
   `directory_context` boundaries (or at minimum, re-stamp the surviving row's
   `directory_context` to the dominant/normalized value instead of leaving a
   split-brain row).

### Contract text delta (rule 3)

Rule 3 today:

> 3. For most agents: do NOT call `memorize()` directly — emit findings in report
>    instead. Exception: long_running agents may call memorize with
>    provenance_agent set.

Proposed replacement:

> 3. Emit durable findings in your report's `## Yadgar findings` footer — the
>    SubagentStop hook memorizes them for you (provenance + branch stamped
>    automatically). Do **not** call `memorize()` directly: the footer path is the
>    single curated write channel, so lessons stay reviewable and de-duplicated.
>    (Direct writes add uncoordinated near-duplicates the curator may merge across
>    scopes.) If a fact is too large or structured for a bullet, name it in the
>    footer and let the main thread decide where it lands.

The substance of the recommendation is that the **behavioural rule stays**
(footer, not direct) but its *justification* is corrected from a false premise
("agents shouldn't contribute to the brain") to the true one ("the footer is the
curated single channel; the write-path normalization — not a per-agent ban — is
what keeps the brain clean"). The engineering fix lives in the write path, not the
prompt.

---

# Q2 — Automating improvement of the agent-prompt materials

## Q2.1 Current-loop audit

The materials are: **contract** (1) + **disciplines** (5:
`recall-first`, `process-hygiene`, `branch-state`, `plan-lifecycle`,
`commit-hygiene`) + **patterns** (26 live / 15 seeded) + **genesis corpus**
(`yadgar/core/seed/materials/agent_prompts.yaml`). Composition is deterministic:
a pattern's `## Composes` section lists `[[agent-discipline-*]]` slugs, and
`agent_dispatch_prelude` assembles contract → disciplines → pattern → recall hint,
deduping `CONTRACT_COVERS` and dropping last-listed disciplines on budget overflow
(`dispatch_helper.py:126-460`). Seed-on-miss re-creates any missing page from
genesis (`_resolve_discipline_text`, `:172-203`).

**What already exists (the rails to ride):**

- **SubagentStop findings → memories** (Q1 Finding A) — the raw material for
  discipline improvement is being captured.
- **Checkpoint step 3 — agent-prompt CAPTURE** (`stop_checkpoint_prompt.md:50-55`)
  — the main thread is *asked* to save reusable dispatch prompts. This is the
  human-in-the-loop capture that works when the main thread remembers.
- **Checkpoint step 5 — MAINTENANCE via `recommended_actions`**
  (`stop_checkpoint_prompt.md:59-75`) — a signal-driven action rail
  (`project.py` `recommended_actions` with `suggested_call`). **This is the
  extension point** — new nags plug in here without a new hook step.
- **ADR-0091 backflow** — the decision that genesis is a *maintained* artifact,
  synced from audited live improvements. Explicitly **manual/agent-audited**, with
  the stated gap: "cadence discipline needed (natural trigger: before each release
  that touches seed materials)". **No trigger fires it today.**
- **Usage counters** — `increment_prompt_usage` (`admin_exec/wiki.py:590`) stamps
  `(uses: N)` on the TOC. **Write-only:** `get_prompt_usage_counts`
  (`_shared/storage/wiki.py:1062`) is read only by its own increment RMW; nothing
  ranks/nags/reports on it.

**What is manual / missing (the observed failures this week):**

1. **Pattern refinement is manual.** The pgrep rule evolved over **6 incidents**;
   each generalization was a main-thread edit. Nothing counted the incidents or
   proposed the generalization.
2. **Findings→discipline flow is lossy.** Agent footers carried reusable lessons
   that only *sometimes* reached discipline pages — no mechanism diffs findings
   against the composed disciplines.
3. **Usage counters drive nothing** (confirmed write-only).
4. **Backflow has no cadence trigger** (ADR-0091's own admitted gap).
5. **No incidents ledger** — the pgrep rule's 6 incidents should have been
   countable; there was no per-discipline incident tally to trip a review.

## Q2.2 Proposed changes — ranked by value / effort

> **Governing principle (from ADR-0091):** automation **surfaces / proposes**,
> humans **dispose**. No stop-hook step may auto-edit disciplines/genesis —
> "unaudited runtime content must not become packaged law without review." Every
> item below emits a `recommended_action` or a proposal; none applies a change.

### Rank 1 — Genesis-drift → backflow-cadence nag  *(high value, low effort)*

The ADR-0091 loop's missing trigger. Add a `recommended_action` (surfaced in
`project_brief(mode="signals")` → checkpoint step 5) that fires when the live
agent-prompt/discipline pages have drifted ahead of the packaged genesis yaml.

- **Signal:** compare `updated_at` of the live pages (contract, disciplines,
  seeded patterns) against the genesis yaml's mtime / a stored `last_backflow`
  marker. If live pages changed since last backflow AND a release-ish boundary is
  near (or N days elapsed) → emit `review_seed_backflow`.
- **Action:** `suggested_call` that lists the drifted slugs for the main thread to
  audit + sync into `agent_prompts.yaml` (the ADR-0091 manual/audited process).
- **Why cheap:** reuses the existing signals rail; no new store; the diff is a
  timestamp compare, not semantic.
- **Effort:** ~1 signal producer + 1 `recommended_actions` entry.

### Rank 2 — Incidents ledger per discipline  *(highest value, medium effort)*

The genuinely valuable new primitive. The pgrep rule was 6 incidents nobody
counted; a per-discipline incident tally would have tripped a review at, say, 3.

- **Cheap first cut (recommended):** when a `from-subagent` finding (or a
  checkpoint-captured lesson) is tagged as a *correction/feedback* (reuse the
  existing `feedback` tag convention), increment a per-discipline counter keyed by
  the discipline the finding most plausibly belongs to. Store as a single
  `_discipline_incidents` memory row (mirror the `_prompt_usage` single-row
  pattern, `wiki.py:1080-1111`) — `{discipline: count}`.
- **Consumer:** a `recommended_action` `review_discipline <name>` when a
  discipline's incident count crosses a threshold since its last `updated_at`.
- **Honest cost:** the hard part is **classification** — mapping a free-form
  finding to a discipline. The cheap cut sidesteps full semantic attribution: only
  count findings the agent/main-thread *explicitly* tags (`discipline:<name>` or
  `feedback` + a discipline mention). Full auto-classification (embedding a finding
  against discipline pages) is a Rank-4 stretch, not the first cut.
- **Effort:** 1 storage row + increment on the subagent-stop / checkpoint path +
  1 signal.

### Rank 3 — Findings-vs-disciplines diff proposer  *(high value, medium-high effort)*

A checkpoint step (or maintenance action) that takes this session's captured
findings, embeds them against the composed discipline pages, and **proposes**
(not applies) a page edit when a finding restates/extends a discipline.

- **Output:** a proposal bullet in the checkpoint reply ("finding X generalizes
  `agent-discipline-process-hygiene`; propose adding: …") for the main thread to
  accept/reject. Optionally write a `wiki_draft` (the dormant `wiki_draft` table,
  Q1 Finding — this would be its **first production writer**, giving it a purpose).
- **Why not Rank 1:** requires embedding + a similarity threshold + a review
  surface. Higher chance of noisy proposals. Gate it behind Rank 2's incident
  signal so it only fires for disciplines already flagged for review — avoids
  proposing edits to disciplines that are working fine.
- **Effort:** embed-compare + proposal rendering + (optional) draft write.

### Rank 4 — Usage-counter-driven review, gated on incidents  *(low value alone)*

Usage counters are write-only, but **usage ≠ staleness**: a heavily-used prompt
that *works* needs no review. Do **not** nag on raw usage. Instead, combine:
`review this pattern` fires only when `uses > N` **AND** the pattern accrued an
incident (Rank 2). This finally gives the counters a consumer, but only as a
*secondary* gate — the incident is the real trigger.

- **Effort:** trivial once Rank 2 exists (add a read of `get_prompt_usage_counts`
  to the Rank-2 threshold check).

## Q2.3 Template / step changes summary

- **No new stop-hook step.** All four items surface as `recommended_actions` on the
  existing checkpoint **step 5** rail, or as a proposal in the checkpoint reply.
  The template's step 5 already handles `suggested_call` verbatim-execution
  (`stop_checkpoint_prompt.md:66-71`) — new actions plug in for free.
- **One optional step-3 tweak:** extend the agent-prompt CAPTURE step to also
  prompt "did a finding this session correct a discipline? tag it
  `discipline:<name>`" — this feeds Rank 2's cheap-cut ledger at point-of-capture,
  where the classification is free (the main thread already knows the context).

---

# What NOT to build (brutal honesty)

The user invited pushback. These are the traps:

1. **Do NOT auto-edit genesis or disciplines from a hook.** ADR-0091 rejected
   auto-sync by name. A stop-hook step that rewrites `agent_prompts.yaml` or a
   discipline page from session content violates a standing accepted decision and
   turns unaudited runtime text into packaged law. Every mechanism above is
   propose-not-apply for this reason. If you build one thing wrong, build this.

2. **Do NOT open inline `memorize` to all agents (Q1 option c-open).** It adds
   uncoordinated writers to a path that already captures via the footer, multiplies
   the unscoped curator-merge collision surface (Q1 Finding E), and leans on
   `provenance_agent` which **has no consumer**. It feels like "letting agents
   contribute" but the contribution channel already exists (the footer). This is
   motion, not progress.

3. **Do NOT build a drafts/quarantine tier before its promoter exists.** The
   `wiki_draft` table already has **zero production writers** and the usage
   counters are already write-only — the codebase has a demonstrated pattern of
   building stores nothing consumes. A quarantine memory tier without a *running*
   promotion consumer is the same mistake with more surface. Build the consumer
   first, or not at all. (Rank 3 may write a draft — but only *after* a human
   review surface for drafts is confirmed to be used.)

4. **Do NOT nag on raw usage counts.** A prompt used 200 times that works needs no
   review; a prompt used twice that caused an incident does. Usage alone is a
   popularity metric, not a quality signal. Gate every review nag on
   incident/correction, with usage as at most a secondary filter.

5. **Do NOT attempt full semantic finding→discipline classification as the first
   cut.** It is the expensive, error-prone part. The cheap cut (count only
   explicitly-tagged corrections) delivers 80% of the incidents-ledger value at
   10% of the risk. Prove the ledger trips reviews usefully before investing in
   auto-classification.

6. **Do NOT capture subagent-internal tool calls into the brain.** They are
   deliberately not captured (only the footer is). Full transcript capture would
   flood consolidation with `Session activity […]` noise (already low-fidelity,
   heat 0.4, fast-decayed). The footer's *curation* is the point — widening capture
   fidelity here degrades signal, it doesn't improve it.

---

# Appendix — key code references

| Concern | Location |
|---|---|
| Subagent findings → memorize | `yadgar/core/hooks/subagent_stop.py:116-149`, `yadgar/core/server/http.py:1092-1193` |
| memorize stores context verbatim | `yadgar/core/server/tools/memorize.py:104-160`, `_phase_store.py:137` |
| branch detect (no worktree collapse) | `yadgar/core/server/tools/project.py:99-122` |
| git-root resolve (cache only, not dir_context) | `yadgar/_shared/server_helpers.py:185-212` |
| recall exact-match dir filter | `yadgar/_shared/storage/directory.py:56-79`, `_shared/retrieval/providers/memory.py:81-90` |
| branch clause (NULL/default/current) | `yadgar/_shared/storage/branch.py:33-52` |
| curator merge (unscoped, split-brain) | `yadgar/backend/curation/ingestion.py:39-59,109-115`, `_phase_store.py:98-107` |
| wiki merged-branch GC (no memory equiv) | `yadgar/backend/admin_exec/project.py:129` |
| action_log auto-capture (branch=None) | `yadgar/backend/consolidation/cleanup.py:98-203` |
| prelude composition (Composes rail) | `yadgar/core/server/tools/dispatch_helper.py:126-460` |
| disciplines + genesis corpus | `yadgar/core/server/tools/agent_prompts.py`, `yadgar/core/seed/materials/agent_prompts.yaml` |
| usage counter (write-only) | `yadgar/backend/admin_exec/wiki.py:590-617`, `_shared/storage/wiki.py:1062-1111` |
| checkpoint prompt (steps 3, 5) | `yadgar/core/hooks/templates/stop_checkpoint_prompt.md:50-75` |
| ADR-0091 backflow decision | wiki `yadgar-adr-log` → ADR-0091 |
