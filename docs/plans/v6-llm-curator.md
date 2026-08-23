# PLAN_V6 — LLM Curator Cycle (SKELETON)

**Status: skeleton only. Scope pending v5.4 soak data + local-review findings. Refine before any v6 implementation.**

Drafted 2026-05-19. Sourced from `yadgar-v5-stabilize-strategy-tldr-gap-analysis` wiki (open forks + strategy) and `yadgar-roadmap-future-improvements` wiki (v6 / v7 sections).

## Goal

Yadgar stops being a statistical filing system, starts reasoning about its own contents. LLM-in-the-loop curator agent runs nightly via local Ollama. v5 ships the substrate (provenance_agent, bi-temporal edges, citation tracing, recall-modulated decay, agent prompts, hooks). v6 adds the LLM that uses it.

## Two-tier consolidation

- **Tier 1** (existing, ~30 min cycle): heat decay, CLS, community detection, surprise-gate writes. Now marks synthesis candidates.
- **Tier 2 NEW** (nightly 19:00–23:00, skips if Ollama offline): LLM curator in 4 phases — **read → propose → execute-safe → surface-destructive**.

## Six LLM task types

| # | Task | Auto-apply | LLM model | Notes |
|---|---|---|---|---|
| 1 | Staleness detection | YES (reversible) | qwen3:8b FAST | World-state-aware (PR merge state, etc.) |
| 2 | Contradiction annotation | YES (reversible via valid_until) | deepseek-r1:8b REASONING | Uses v5.3.4 C1 `invalidate_edge()` |
| 3 | Semantic correlation | YES (LLM finds causal chains) | deepseek-r1:8b | Adds derived edges with source_memory_id (C3) |
| 4 | Wiki synthesis | AUTO if confidence ≥ 0.80 + sanity; else draft queue | deepseek-r1:8b | 4-source clusters → derived wiki page |
| 5 | Cleanup proposals | NEVER auto — always proposal queue → human | deepseek-r1:8b | Soft-delete only |
| 6 | Wiki dedup | NEVER auto | deepseek-r1:8b | Duplicate-prone slugs (1586 wiki rows risk) |

## Model routing (verified Ollama benchmarks 2026-05-14)

- `YADGAR_SYNTHESIS_MODEL_FAST = qwen3:8b` — staleness, simple annotations (no CoT needed)
- `YADGAR_SYNTHESIS_MODEL_REASONING = deepseek-r1:8b` — contradiction, correlation, cleanup, wiki synthesis
- Single-task latency: 69s median per call (CoT-heavy). Nightly batch only — NOT real-time.
- 50 clusters/night ≈ 57 min total — fits 19:00–23:00 window.

## Safety model

- Scope limit: heat > 0.2, age < 30d, `is_protected = False`. Excludes anchors entirely.
- Circuit breaker: max 20 deletion proposals/night. Halts if exceeded.
- Soft-delete with 7-day recovery (don't hard-delete on auto-apply).
- `llm_synthesized=True` tag — excluded from synthesis input (no LLM-on-LLM loops).
- `provenance_agent` (v5.3.0 A1) — grade memories per source. Low-confidence agent-written rows = first cleanup candidates.

## Open design forks (DECIDE BEFORE IMPLEMENTATION)

1. **Write-time conflict resolution (Mem0) vs nightly batch.** v5.3.4 C4 already shipped Ollama-gated write-time path (default off). Decide: keep both layers, or write-time obsoletes nightly contradiction pass?
2. **Bi-temporal v5.2 vs v6.1.** Shipped in v5.3.4 — fork closed.
3. **Depth saturation in v6 curator (SleepGate paper: collapse at ~15 interfering memories).** Chunking strategy needed BEFORE first nightly run. Recommend: cluster by topic via community detection, curate cluster-by-cluster, never whole-store batch.
4. **Anthropic Agent Teams JSONL inbox** — adopted in v5.3.6 M1. Fork closed.
5. **Auto Memory CLI overlap** (`~/.claude/projects/<project>/memory/`). Decide: shadow-watch via FileChanged (already wired in v5.3.6 — extend), or ignore Anthropic's path.

## Risk

Per SleepGate research: 16.5% accuracy at depth-15 contradictions. Chunking + per-cluster scope limit (above) MUST land before first nightly run. Defer entire v6 if chunking strategy unconvincing post-design.

## New components

- `SynthesisClient` DI — Ollama HTTP client w/ JSON-mode + timeout
- `synthesis_proposal` table — schema: cluster_id, task_type, proposal_json, confidence, status (pending/applied/rejected), reviewed_by
- `memory_merge()` atomic op — wiki synthesis output
- `memory_soft_delete()` + recovery — 7-day window before hard delete
- `review_proposals()` MCP tool — operator queue inspector
- `llm_synthesized` tag enforcement in WriteGate

## v7 hint (real-time synthesis)

Deferred. Target: <10s end-to-end. At v7 planning: re-benchmark faster quantized model, 3-4B alternatives, or hardware upgrade. v7 features: `recall(synthesize=True)`, `wiki_query(synthesize=True)`, `ask()` tool.

## Action

DO NOT implement v6 before v5.4 soak data lands. Use this skeleton to refine `docs/PLAN_V6.md` post-soak.

## See also

- [[yadgar-v5-stabilize-strategy-tldr-gap-analysis]]
- [[yadgar-roadmap-future-improvements]]
- [[deepseek-r1-8b-capability-assessment-yadgar-v6-tasks]] (Ollama benchmark anchor)
- [[plan-v5-3-yadgar-feature-release-cycle]]
