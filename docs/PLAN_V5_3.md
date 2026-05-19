# PLAN_V5_3 — Feature Release Cycle

Cut from `yadgar-v5-stabilize-strategy-tldr-gap-analysis` wiki (2026-05-18) + v5.3 chat-level approval (2026-05-19). Scope: agent integration, hook adoption, competitor parity, QoL, multi-agent collaboration.

**Cross-cutting constraint**: every change works for **any** user. Yadgar ships portable defaults via:
- `yadgar install-hooks` CLI subcommand (extend to register new events).
- `docs/INSTALL.md` non-nix install path.
- `docs/HOOKS.md` ready-to-paste settings.json snippets.
- Optional CLAUDE.md template snippets users opt-in to.

Workflow: bundled-release per `yadgar-bundled-release-integration-model`. `feat/v5.3` long-lived. Sub-branches per item.

## v5.3.0 — Agent Integration Foundation (P0)

- **A1** — `memorize(provenance_agent: str | None = "default")`. Schema migration #005 adds `provenance_agent` column. Subagents pass own type name.
- **A2** — `docs/CLAUDE_SUBAGENT_CONTRACT.md` template: subagent contract (read-first, no-write, report-back, `## Yadgar findings` shape). Users paste into their global CLAUDE.md (nix-claude can bake).
- **A3** — `SubagentStop` hook script (`yadgar/hooks/subagent-stop.py`) + `/hooks/subagent-stop` endpoint. Reads agent transcript final report → extracts `## Yadgar findings` → queues memorize with `provenance_agent=<subagent_type>`.
- **A4** — Agent-prompt versioning. Wiki category `agent-prompt`. Slugs `agent-prompt-<task-pattern>-vN`. New MCP tools: `agent_prompt_get(pattern)`, `agent_prompt_save(pattern, content)`.

## v5.3.1 — Claude Code 2026 Hook Adoption (P1)

Depends on H1-H4 schema verification (claude-code-guide research dispatch in parallel with v5.3.0).

- **H1** — `InstructionsLoaded` hook. Recall on CLAUDE.md load.
- **H2** — `PostCompact` hook. Re-inject anchored memories after compaction.
- **H3** — `TeammateIdle` hook. Flush yadgar writes for idle teammate.
- **H4** — `TaskCreated` + `TaskCompleted` hooks. Capture agent dispatch lifecycle.

All deployed via extended `yadgar install-hooks` subcommand.

## v5.3.2 — Memory Quality (Competitor Parity, P2)

- **C2** — Recall-frequency-modulated decay (MemoryBank). `heat = min(heat * 0.9995 + recall_boost, 1.0)` where `recall_boost = 0.05` per access. Cost: S.
- **C3** — Citation tracing (Zep). `source_memory_id` field on KG edges. Populate on writes. Best-effort backfill. Cost: S-M.
- **C1** — Bi-temporal fact windows (Zep). `valid_from` / `valid_until` on KG edges. Invalidate on conflict, never delete. Cost: M.
- **C4** — LLM conflict ops on write (Mem0). Ollama-only v5.3. Gate behind `YADGAR_CONFLICT_RESOLVER=on`. Anthropic API deferred to v6. Cost: M.

## v5.3.3 — Quality-of-Life (P3)

- **Q1** — Token-budget + cache-hit metrics on `/metrics`. Per-tool budget consumption.
- **Q2** — `_postmortem` / `_incident` tag retrieval boost when current task contains action verbs (deploy, push, merge, restart, vacuum).
- **Q3** — Wiki coverage analyzer. `wiki_coverage()` MCP tool lists modules without wiki pages.
- **Q4** — `PLAN_*.md` first-class memory targets. Auto-detect via `FileChanged` hook → memorize delta tagged `_plan`.

## v5.3.4 — Multi-Agent Collaboration (P4)

- **M1** — Adopt Anthropic Agent Teams JSONL inbox (`team_inbox/<projectId>/<teamName>/<agentName>.jsonl`). Yadgar shadow-watches via `FileChanged` hook → mirrors peer messages into action_log. Solves goal-d.
- **M2** — Subagent dispatch helper that injects yadgar protocol + retrieves latest `agent-prompt-<pattern>` (from A4) into agent's initial prompt.

## Deferred to v6

- A-MEM backward-propagating evolution.
- Letta-style agent-self-edit-at-inference.
- Sleep-phase consolidation.

## Schema migrations

- v5.3.0: migration #005 — `memory.provenance_agent` (text, default "default").
- v5.3.2 C3: migration #006 — KG edge `source_memory_id` (int, nullable).
- v5.3.2 C1: migration #007 — KG edge `valid_from`, `valid_until` (datetime, nullable).

One migration per release for rollback safety.

## Phasing order

v5.3.0 → v5.3.1 → v5.3.2 → v5.3.3 → v5.3.4.

Could parallel: v5.3.2 (C2+C3) + v5.3.3 (Q2+Q3) while H1-H4 research finishes. v5.3.0 sequential (A3 depends on A1).

## Status

v5.3.0 dispatch in progress. claude-code-guide H1-H4 research dispatched in parallel.
