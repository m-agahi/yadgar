# Yadgar v6 Plan — LLM Curator Agent

## Context

v5.0 (`docs/PLAN_V5.md`) ships security, auth, and observability. v6 is the first release
where Yadgar stops being a statistical filing system and starts reasoning about its own
contents. The jump from v4→v5 was structure; the jump from v5→v6 is understanding.

**Dependency:** v6 work begins only after v5.0.0 merges. Do not start.

Version bump: `5.x → 6.0.0`. Breaking: new DB tables, new config keys, new Ollama
dependency (optional, degrades gracefully if offline).

---

## 1. Architecture: Two-Tier Consolidation

Current consolidation is single-tier: one fast cycle runs everything. v6 splits it:

```
Tier 1 — Fast cycle (unchanged, every 30 min)
  heat decay, entity extraction, CLS clustering,
  community detection, dream replay, causal discovery,
  action_log processing, auto-narrate
  NEW: marks clusters as synthesis candidates (sets needs_synthesis flag)

Tier 2 — LLM agent pass (nightly, ~19:00 local, skips if Ollama offline)
  Phase 1: Read        — load candidates, build context, no writes
  Phase 2: Propose     — generate proposals, write to proposal table
  Phase 3: Execute     — auto-apply safe proposals immediately
  Phase 4: Surface     — add pending destructive proposals to session-start context
```

Tier 1 feeds Tier 2 via a `synthesis_candidate` table. Tier 2 never blocks Tier 1.
If Tier 2 fails entirely, Yadgar works exactly as before.

---

## 2. What the LLM Agent Does

Six task types, executed in a single nightly pass per cluster batch:

### 2a. Staleness Detection

Input: memory or wiki page content + known world state (recent version shipped, PRs merged,
files deleted).

Output:
```json
{"stale_ids": [123, 456], "reasons": {"123": "v4.8.3 shipped, this was in-progress"}}
```

Risk: low (marking stale is reversible). Auto-apply.

### 2b. Contradiction Detection

Input: cluster of semantically related memories.

Output:
```json
{"contradictions": [{"id_a": 12, "id_b": 34, "reason": "A says X unfixable, B says X fixed"}]}
```

Risk: low (annotation only, not deletion). Auto-apply with `contradicts:ID` tag on both.

### 2c. Semantic Correlation

Input: two clusters from different domains / time periods.

Output:
```json
{
  "correlation": true,
  "type": "causal|pattern|shared_root|recurrence",
  "description": "StorageEngine KeyError pattern repeats across v4.5, v4.7, v4.8 — same missing null-check site",
  "strength": 0.82
}
```

Not the same as co-occurrence (already algorithmic). LLM finds *meaning* in the correlation:
causal chains, recurring failure modes, structural couplings. Auto-apply as knowledge graph
edge with `llm_inferred=True` flag.

### 2d. Wiki Synthesis

Input: cluster of 5–15 related memories with no existing wiki page.

Output: wiki page content (max 300 words). Structured fields:
```json
{
  "title": "...",
  "content": "...",
  "tags": [...],
  "confidence": 0.84,
  "source_memory_ids": [1, 2, 3]
}
```

Confidence ≥ 0.80 + passes sanity checks → auto-approve, tagged `llm_synthesized`.
Confidence < 0.80 → draft queue.

Sanity checks:
- No proper nouns absent from source memories
- 50–300 words
- No self-referential claims ("This memory is about...")
- All structured fields populated

### 2e. Cleanup Proposals (Destructive)

Input: cold memories (heat < 0.2, age > 30 days) that appear to be noise.

Output:
```json
{
  "action": "forget",
  "ids": [474310, 475178],
  "reason": "test fixtures from /tmp context, not real project memories",
  "confidence": 0.91
}
```

**NEVER auto-apply.** Always goes to proposal queue regardless of confidence. Human approves
via session-start surface or explicit `review_proposals()` MCP tool. Soft-delete first
(archive), hard-delete only after 7-day recovery window.

Scope limit: agent can only propose deletion of memories with `heat < 0.2` AND
`age > 30 days` AND not `is_protected`. Hot or recent content is hard-blocked.

### 2f. Wiki Deduplication

Input: pairs of wiki pages with embedding similarity > 0.92.

Output:
```json
{
  "action": "merge",
  "keep": "slug-a",
  "discard": "slug-b",
  "merged_content": "...",
  "reason": "both describe the same vacuum architecture from different angles"
}
```

**NEVER auto-apply.** Proposal queue only. Current known problem: 1626 wiki pages,
many are `mod: __main__` variants from repo-wiki generation with near-identical content.

> **Real-time query synthesis (`recall(synthesize=True)`, `wiki_query(synthesize=True)`, `ask()`) — deferred to v7.** Reason: running two 8B models concurrently (deepseek-r1:8b nightly + fast synthesis model) is prohibitive on current hardware. Model landscape expected to improve by v7 window.

---

## 3. New Yadgar Components

### 3a. SynthesisClient (DI, mirrors MLClient pattern)

```python
class SynthesisClient(Protocol):
    def complete(self, messages: list[dict], schema: dict | None = None,
                 thinking: bool = False) -> dict: ...
    def health(self) -> bool: ...

class OllamaSynthesisClient:
    # POST http://localhost:11434/v1/chat/completions
    # format=json for structured output
    # /think prefix in system prompt when thinking=True

class NullSynthesisClient:
    # Always returns health()=False, complete() raises
    # Used when YADGAR_SYNTHESIS_URL unset
```

Config keys:
- `YADGAR_SYNTHESIS_URL` — Ollama base URL, default unset (synthesis disabled)
- `YADGAR_SYNTHESIS_MODEL_FAST` — default `qwen3:8b` (staleness, simple annotations)
- `YADGAR_SYNTHESIS_MODEL_REASONING` — default `deepseek-r1:8b` (contradiction, correlation, cleanup proposals)
- `YADGAR_SYNTHESIS_WINDOW_START` — default `"19:00"`
- `YADGAR_SYNTHESIS_WINDOW_END` — default `"23:00"`
- `YADGAR_SYNTHESIS_MAX_CLUSTERS` — default `50` (cap per night)
- `YADGAR_SYNTHESIS_CONFIDENCE_THRESHOLD` — default `0.80`

### 3b. Proposal Queue (new DB table)

```sql
DEFINE TABLE synthesis_proposal SCHEMAFULL;
  id            record<synthesis_proposal>,
  created_at    datetime,
  action        string,  -- forget|merge|wiki_add|annotate
  payload       object,  -- action-specific data
  confidence    float,
  status        string,  -- pending|approved|discarded|executed
  auto_applied  bool,
  reviewed_at   datetime | none,
```

### 3c. `memory_merge(ids, new_content)` storage operation

Atomic:
1. Insert new memory with synthesized content, `heat = max(source heats)`,
   `access_count = sum(source access_counts)`, tags `llm_synthesized` + `merged`
2. Archive all source memories (soft-delete, 7-day recovery)
3. Repoint any `memory_similarity_link`, `memory_transition`, `caused_by` edges to new ID
4. Log to `consolidation_log`

### 3d. Soft-delete / Recovery Window

Currently `forget()` is hard-delete. Add:
- `memory_soft_delete(id)` — moves to `memory_archive` with `deleted_at`, sets
  `recovery_expires_at = now() + 7 days`
- `memory_recover(id)` — restores from archive if within window
- Vacuum job purges `recovery_expires_at < now()` rows
- `forget()` remains hard-delete for explicit human use; LLM agent always uses soft-delete

### 3e. `review_proposals()` MCP Tool

Returns pending proposals grouped by action type with formatted summary. Accepts:
- `review_proposals(approve_all=True)` — bulk approve (use carefully)
- `review_proposals(discard_action="forget")` — bulk discard all deletion proposals
- `review_proposals(id=42, decision="approve")` — single proposal decision

### 3f. `llm_synthesized` Tag Contract

All LLM-generated content carries `llm_synthesized=True`. Hard rule enforced at write time:
**LLM synthesis input queries always exclude `llm_synthesized=True` content.** Prevents
hallucination compounding across cycles.

---

## 4. Safety Model

| Operation | Gate | Recovery |
|-----------|------|----------|
| Staleness flag | Auto-apply | Remove tag manually |
| Contradiction annotation | Auto-apply | Remove tag manually |
| Correlation edge | Auto-apply | `forget()` the edge |
| Wiki synthesis (high conf) | Auto-approve | `wiki_discard()` |
| Wiki synthesis (low conf) | Draft queue | Discard draft |
| Deletion proposal | Proposal queue → human | 7-day archive window |
| Merge proposal | Proposal queue → human | 7-day archive window |

**Scope limits on LLM agent (hard-coded, not configurable):**
- Cannot propose deletion of `is_protected=True` memories
- Cannot propose deletion of memories with `heat > 0.2`
- Cannot propose deletion of memories newer than 30 days
- Cannot touch any `llm_synthesized=True` content
- Cannot propose more than 20 deletions per night (circuit breaker)

**Audit log:** every agent action (including auto-applied) written to `consolidation_log`
with `source=llm_agent`, `model=qwen3:8b`, `confidence=X`. Queryable via `memory_stats`.

---

## 5. Ollama Integration Details

Structured output via Ollama `format` param (JSON schema enforcement). Two model tiers:

- **Fast model** (`YADGAR_SYNTHESIS_MODEL_FAST`, default `qwen3:8b`): staleness detection,
  simple annotations, straightforward wiki synthesis. No thinking mode. Speed priority.
- **Reasoning model** (`YADGAR_SYNTHESIS_MODEL_REASONING`, default `deepseek-r1:8b`):
  contradiction detection, correlation discovery, cleanup proposals, wiki dedup.
  deepseek-r1 has native CoT — reasoning is not a mode to enable, it's the default behavior.
  If substituting qwen3:8b here, add `/think` as first token of every user message (not
  system prompt) to guarantee thinking mode activation.

Task → model routing (hard-coded, not configurable per-task):
```python
FAST_TASKS = {"staleness", "annotation"}
REASONING_TASKS = {"contradiction", "correlation", "cleanup_proposal", "wiki_dedup", "wiki_synthesis_complex"}
```

Pre-flight check before each nightly pass:
```
GET http://localhost:11434/api/tags
```
If offline or model not found → skip Tier 2 entirely, log INFO. Never fails Tier 1.

Prompt structure (same pattern for all tasks):
```
SYSTEM: [Yadgar schema brief — what memories/wiki/tags mean, 500 tok]
USER:   [Task description + input cluster, structured output schema]
```

The schema brief is generated once per nightly pass and passed to both models. Includes:
- Memory store types (episodic/semantic)
- Tag taxonomy (`_anchor`, `auto-abstracted`, `llm_synthesized`, etc.)
- Current version + recent shipped changes (for staleness context)
- Available actions and their consequences

---

## 6. Missing Pieces (must build before v6 agent runs)

1. `SynthesisClient` protocol + `OllamaSynthesisClient` + `NullSynthesisClient`
2. `synthesis_proposal` DB table + migrations
3. `memory_merge()` storage operation
4. `memory_soft_delete()` + `memory_recover()` + vacuum for expired archives
5. `review_proposals()` MCP tool
6. `needs_synthesis` flag on cluster/community records
7. `llm_synthesized` tag enforcement at write time
8. Proposal circuit breakers (max 20 deletions/night)
9. Audit log entries in `consolidation_log` for agent actions
10. Session-start hook: surface pending proposal count

---

## 7. Test Plan

1. **`NullSynthesisClient`** — synthesis disabled when URL unset; Tier 1 unaffected.
2. **Ollama offline** — pre-flight fails; Tier 2 skips; no error propagation to Tier 1.
3. **Staleness detection** — fixture memory with "v4.8.1 in progress"; agent marks stale
   given world state showing v4.8.3 shipped.
4. **Contradiction annotation** — two memories with explicit contradiction; agent tags both.
5. **Wiki synthesis auto-approve** — high-confidence output passing all sanity checks lands
   as approved wiki page tagged `llm_synthesized`.
6. **Wiki synthesis draft** — low-confidence output goes to draft, not approved.
7. **Cleanup proposal never auto-applies** — even confidence=0.99 deletion goes to queue.
8. **Scope limit enforcement** — agent cannot propose deletion of `heat > 0.2` memory;
   raises hard error if attempted.
9. **Feedback loop guard** — `llm_synthesized` memories excluded from synthesis input query.
10. **`memory_merge` atomicity** — partial failure (crash mid-merge) leaves source memories
    intact; no data loss.
11. **Soft-delete recovery** — memory archived, recovered within window, intact.
12. **Circuit breaker** — attempt to propose 21 deletions in one pass; 21st rejected.
13. **`review_proposals`** — approve/discard individual + bulk; status transitions correct.

---

## 8. Order of Work

1. `SynthesisClient` protocol + Ollama client + health check + config keys
2. `synthesis_proposal` table + migrations
3. `memory_soft_delete` + `memory_recover` + vacuum
4. `memory_merge` operation
5. `review_proposals()` MCP tool
6. `llm_synthesized` tag enforcement + feedback loop guard
7. Staleness detection task (simplest, validate Ollama integration end-to-end)
8. Contradiction annotation task
9. Wiki synthesis task (auto-approve + draft path)
10. Correlation discovery task
11. Cleanup proposals task (most dangerous, build last)
12. Wiki dedup proposals task
13. Session-start hook: pending proposal count
14. Audit log wiring
15. Full nightly pass integration test
16. Version bump 5.x → 6.0.0, open PR

---

## Open Questions (resolve before implementation starts)

1. **Schema brief size.** 500 tokens for Yadgar context in every LLM prompt — is this
   accurate? Too small misses context, too large wastes tokens on 50 cluster calls/night.
   Benchmark once Ollama integration exists.

2. **Cluster selection.** Which 50 clusters/night? Highest entropy? Oldest unvisited?
   Most recently grown? Strategy affects what the agent actually learns about.

3. **Confidence calibration.** Self-reported confidence from both models is uncalibrated.
   Need empirical testing per model: run 50 tasks with known ground truth, measure actual
   accuracy vs. reported confidence for each model tier separately. May need separate
   `SYNTHESIS_CONFIDENCE_THRESHOLD_FAST` and `SYNTHESIS_CONFIDENCE_THRESHOLD_REASONING`.
   Start with deepseek-r1:14b as upper-bound baseline to understand quality ceiling before
   tuning 8b thresholds.

4. **Merge UI.** `review_proposals()` MCP tool shows the merge diff — but how? Side-by-side
   content comparison in a terminal is ugly. May need a viz endpoint or at minimum a
   structured diff format Claude can render as markdown.

5. **`memory_merge` and heat inheritance.** `max(source heats)` — right policy? Could argue
   `mean` is more honest. Decide before implementation.

6. **World state injection.** Staleness detection needs to know "v4.8.3 shipped on 2026-05-14".
   Where does this come from? Options: parse recent `consolidation_log` entries, read
   `counter` table for version markers, inject from session-start hook. Decide.

---

## Deferred to v7+

- Claude API as escalation path for low-confidence qwen3:8b proposals
- Per-tool granular confidence thresholds (deletion vs. synthesis vs. correlation)
- Active re-embedding of LLM-synthesized pages with better model
- Multi-model ensemble (run two models, take intersection of proposals)
- Web UI for proposal review (beyond MCP tool)
- Cross-project memory correlation (memories from yadgar repo linked to memories from qwfm)
