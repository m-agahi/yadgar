# PLAN_V5_3 — Feature Release Cycle

> **STATUS: SHIPPED (historical — v5.3 era, 2026-05-19)**

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

## v5.3.1 — Claude Code 2026 Hook Adoption (P1) — REVISED 2026-05-19

Hook-schema research (claude-code-guide) corrected initial assumptions. Source: https://code.claude.com/docs/en/hooks.md.

- **H1** — `InstructionsLoaded` hook ✓ confirmed. Fires session-start + lazy CLAUDE.md/rules load. Use `load_reason` matcher. Inject recall on CLAUDE.md load.
- **H2** — `PostCompact` hook: **REVISED**. Fires AFTER compaction completes, READ-ONLY (cannot inject). Originally planned "re-inject anchored memories after compaction" was based on wrong assumption. Repurpose: snapshot compaction event into action_log. **Or drop** as low value.
- **H3** — `TeammateIdle` hook: **DEFERRED**. Exists per docs but payload schema unknown + TypeScript Agent SDK only per docs (Python SDK support unclear). Empirical test required before wiring.
- **H4** — **REPLACED** with `SubagentStart` hook. `TaskCreated`/`TaskCompleted` do NOT exist as Claude Code CLI hooks. `SubagentStart` captures dispatch metadata; can recall context for the subagent's task description and pass to downstream.

All deployed via extended `yadgar install-hooks` subcommand. See global anchor on CLAUDE CODE 2026 HOOK EVENT SCHEMAS for verified facts.

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

## v5.3.7 — Viz UX (P5) — added 2026-05-19

User-reported frontend issues at `http://localhost:42069/`.

- **V1** — Semantic search box. Sticky input at top of UI. On submit, calls server endpoint (`/api/viz/search?q=...`) which dispatches `recall(q)` + `wiki_query(q)` (capped, ~5 each). Returns node IDs. UI pins matched nodes (color/size/center) and de-emphasizes the rest. Clear button restores full graph.
- **V2** — Wiki node click → content panel. Currently click handler on wiki-typed nodes is broken (does nothing). Fix click delegation in `yadgar/static/index.html` so wiki nodes open the same side-panel that memory nodes use. Reuse `wiki_read(slug)` for fetch.
- **V3** — Click handler coverage audit. Not every node type fires the panel. Iterate node types (`memory`, `wiki`, `entity`, `episode`, `cluster`, etc.) — confirm each has a click → panel binding. Add missing ones. Test plan: visual smoke + console error check.
- **V4** — 2D / 3D mode toggle. Button in viz header switches rendering between current 3D (Three.js) and a 2D layout (force-directed 2D, e.g. d3-force or cytoscape.js). Persist mode in localStorage. Default 3D (current).
- **V5** — Fix `db_size` status bar metric — currently shows "DB SIZE 0.0 MB on disk" (broken). Status bar metric source likely `/api/system` proxied through viz_server. Real endpoint is `embed_service /admin/dbsize` (per `storage/dbsize.py:58`). Trace: frontend fetch path → viz proxy → bearer-injected → embed `/admin/dbsize`. Probably proxy mapping mismatch or response field name change. Confirm via curl post-fix.
- **V6** — `/api/graph` (full graph) returns HTTP 502 through viz proxy. `/api/graph/stats` works. Likely yadgar core timeout / response size limit on full graph (2k+ nodes + 1.7k wiki pages). Diagnose: increase proxy timeout, paginate response, or stream-serialize. Bug surface: empty/incomplete main view → filters appear broken even when data exists. Also clarify "mem-wiki" filter mapping (frontend label vs edge-table source) — surface count in `/api/graph/stats` for visibility.

Files: `yadgar/viz_server.py` (new search endpoint + dbsize proxy fix), `yadgar/static/index.html` (handler fixes + 2D toggle + dbsize reader), maybe `yadgar/server/tools/*.py` (search backend wiring). No DB migration.

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
