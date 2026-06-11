# PLAN v5.71.0 — Agent-Prompt Passive Library (Tier-1 MVP)

Status: PLANNED. Source: [[yadgar-deferred-architecture-ideas-half-baked-exploration]] Idea 6 (Tier-1 spec + Tier-2 Claude-in-loop). Brainstorm + competition research 2026-06-11.

## Goal

Passively build a library of reusable **subagent dispatch prompts**, and passively surface the right one when you're about to dispatch a similar task. **Zero new habits** — all hook-driven, in-flow. Reuse existing primitives (`agent_prompt_save/get/agent_dispatch_prelude`, embeddings, WriteGate surprise-gating, heat). Minimal new code.

## Why now / why this and not the grand version

- Yadgar already has the primitives + the hook scaffolding. Tier-1 is mostly wiring.
- Competition research (2026-06-11): prompt *versioning* is commoditized; **semantic dispatch-time retrieval + heat-curation is unowned**. Generic skill/prompt hosting is commoditizing (K-Dense-AI/claude-skills-mcp died Oct 2025; Anthropic native agents/marketplace; MCP "Skills primitive" on roadmap). The differentiated slice = yadgar's in-the-loop position + memory engine.
- **Auto-improvement is explicitly OUT of this version** (Tier-2, v5.72 — see below). Tier-1 must prove *used* first.

## Non-goals (v5.71)

- Tier-2 Claude-in-loop auto-improve (outcome-fired review hook). → v5.72, gated behind Tier-1 dogfood.
- Skills (Idea 5). Same pattern, later.
- Generic / multi-harness / marketplace anything.
- Rewriting prompts. Tier-1 only *remembers + surfaces*; humans edit.

## Architecture — reuse existing hooks

| Concern | Hook (exists) | Event | What to add |
|---|---|---|---|
| **Capture** dispatch prompt | `yadgar/hooks/subagent-start.py` | SubagentStart | extract the Task dispatch prompt + task description → `agent_prompt_save` via WriteGate (dedup near-identical) |
| **Surface** saved prompt | `yadgar/hooks/prompt-recall.py` | UserPromptSubmit | semantic-match the user request against saved agent prompts → inject top match if score ≥ threshold |
| **Outcome** (Tier-2 only, v5.72) | `yadgar/hooks/subagent-stop.py` | SubagentStop | append outcome `{result, signal}` to the prompt record |

Rationale for surface at UserPromptSubmit (not PreToolUse/SubagentStart): the dispatch prompt is *composed by the orchestrator* in response to the user request. Injecting at UserPromptSubmit puts the suggestion in the orchestrator's context **before** it writes the Agent call — influencing composition. SubagentStart is too late (prompt already written).

## Data model

Extend the existing `agent_prompt` store (used by `agent_prompt_save/get`). Required fields for Tier-1:

```
agent_prompt {
  id, slug,
  task_class: str,        # short task description, EMBEDDED for semantic match
  content: str,           # the dispatch prompt body
  version: int,           # bumped on edit; never overwrite
  heat: float,            # rises on reuse/surface-accept; decays like memory
  tags: [str],
  directory_context: str, # project scoping, like memories
  created_at, updated_at,
  outcomes: [ ... ]       # EMPTY in Tier-1; populated in Tier-2
}
```

- Embedding on `task_class` (+ optionally `content`) → reuse `RemoteEmbeddingEngine`.
- Heat reuses the memory thermodynamics decay path. Surface-and-accepted → heat bump.
- WriteGate (surprise-gating) on capture → near-identical dispatch prompts dedup instead of piling up.

## Components & changes

1. **`agent_prompt_save`** — ensure it embeds `task_class`, routes through WriteGate, sets base heat, scopes by `directory_context`. (May already partially do this — audit first.)
2. **`agent_prompt_get`** — add semantic retrieval mode: `agent_prompt_get(query, directory, max_results)` → vector search over `task_class` embeddings, heat-boosted ranking, branch/dir scoped (mirror `recall`/`wiki_query` resolution). Return top match(es) with score + heat + version + use-count.
3. **`subagent-start.py`** — detect SubagentStart, pull dispatch prompt + a task descriptor, call `agent_prompt_save`. Fail-open (never block dispatch). Skip if prompt is trivial/empty.
4. **`prompt-recall.py`** — after the existing memory recall, also call `agent_prompt_get(query=user_prompt, directory)`. If best score ≥ `YADGAR_AGENT_PROMPT_SURFACE_THRESHOLD` (default e.g. 0.55), inject a compact block: `Saved dispatch prompt for a similar task (heat H, used N×, v{ver}): <content>. Reuse/adapt for this dispatch.` Cap to 1 suggestion. Fail-open.
5. **Heat on accept** — when a surfaced prompt is reused (next SubagentStart capture matches an existing slug within a short window), bump that slug's heat = the low-noise reuse signal. (Best-effort correlation; acceptable if imperfect in Tier-1.)
6. **Config** (three-way I25): `YADGAR_AGENT_PROMPT_CAPTURE` (bool, default true), `YADGAR_AGENT_PROMPT_SURFACE_THRESHOLD` (float, default 0.55), `YADGAR_AGENT_PROMPT_SURFACE` (bool, default true).

## TDD plan (write failing first)

- `agent_prompt_save` embeds `task_class` + dedups near-identical via WriteGate (two ~identical saves → one record, version unchanged).
- `agent_prompt_get(query)` semantic: a saved "review a PR for security" prompt is retrieved by query "audit this pull request for vulns" above threshold; unrelated query returns nothing above threshold.
- dir/branch scoping: prompt saved in project A not surfaced in project B.
- heat: reuse bumps heat; ranking prefers higher-heat among similar matches.
- capture hook: given a SubagentStart payload with a dispatch prompt, calls save; trivial/empty prompt → skipped; save failure → hook still exits 0 (fail-open).
- surface hook: best score ≥ threshold → injects exactly one block; below threshold → injects nothing; `YADGAR_AGENT_PROMPT_SURFACE=false` → nothing.
- config three-way-sync invariant (I25) passes for the new knobs.

## Phases

1. Schema + `agent_prompt_save`/`agent_prompt_get` semantic + heat + scoping (+ tests). Pure library — no hooks yet.
2. Capture via `subagent-start.py` (+ tests, fail-open).
3. Surface via `prompt-recall.py` (+ tests, fail-open, threshold-gated).
4. Config knobs + I25 registration. Docs.
5. Ship as v5.71.0 (core release; no backend change). nix bump core; PyPI; container.

## Dogfood kill-gate (LOAD-BEARING)

After ship: use it **2–3 weeks**. Metric = do you actually reach for the surfaced prompt? If you instinctively ignore it, **KILL the feature** (rip the surface injection; keep capture dormant or remove). Do NOT proceed to Tier-2 until Tier-1 is demonstrably used. This gate is the whole defense against building shelfware.

## Token-usage note (secondary justification, not primary)

Costs: surface hook injects ~1 prompt block per qualifying user turn (small, constant). Capture runs out-of-band (≈0 context cost).
Savings: a good surfaced prompt that makes a subagent succeed **first try** avoids a dispatch→bad-result→re-dispatch cycle. Subagent re-runs are token-HEAVY (10k–100k+ each), so avoiding one redo dwarfs many injection costs. Also: semantic load-on-demand of ONE relevant prompt beats stuffing all dispatch prompts into CLAUDE.md (always-in-context). **Net: plausibly token-negative IF the library is good + used** — conditional, not guaranteed. Don't justify the build on tokens; justify on reuse/usability. Tokens are a bonus that only materializes if the dogfood gate passes.

## Tier-2 preview (v5.72 — DO NOT build in v5.71)

Outcome capture in `subagent-stop.py` → threshold (≥5 uses, ≥2 negatives) marks a prompt `review-due` → next checkpoint/stop hook injects "judge + propose v2" into the LIVE Claude session (the user's hook-fires-Claude insight; defeats the no-daemon-LLM + sparse-data objections via reasoning over a few examples). Guardrails: never overwrite (new version), no auto-promote without out-earning incumbent on heat or human approval, cap edit rate, guard feedback drift, human sees every proposal.

## Risks

- Surface injection on noise → trained to ignore → dead. Mitigate: conservative threshold, cap 1 suggestion, fail-open, the kill-gate.
- SubagentStart payload may not expose the dispatch prompt cleanly → verify the hook input contract first (phase 2 spike); fall back to PostToolUse-on-Task capture if needed.
- Reuse-correlation for heat is best-effort; imperfect is acceptable in Tier-1.
- Only worth it if used. The kill-gate is honored, not skipped.
