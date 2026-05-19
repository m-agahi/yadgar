# PLAN_V5_2 — Stabilize-Only Release

Cut from `yadgar-v5-stabilize-strategy-tldr-gap-analysis` wiki (2026-05-18). Scope: ship v5.2 as the "stabilize before v6 LLM curator" cycle — security cleanup, hook adoption, competitor parity, QoL — without any LLM integration work.

Workflow: bundled-release per `yadgar-bundled-release-integration-model`. Long-lived branch `feat/v5.2`, sub-branches per S/A/H/C/Q item off `feat/v5.2`. One final merge to master at end.

## v5.2.0 — Security + correctness baseline (P0)

Reason first: 4 outstanding H-level findings since gap audit. No feature work until cleared.

- **S1** — parameterize SurrealQL: `yadgar/storage/ops.py:110` (`extra_where` raw interpolation, H-5), `yadgar/storage/ops.py:138` + `yadgar/storage/client.py:375` (raw `json.dumps` in INSERT, H-4). Add regression tests for injection vectors.
- **S2** — `yadgar/config_yaml.py:840` write path: `os.chmod(path, 0o600)` (H-9). Single-line + permission-assert test.
- **S3** — `yadgar/rules_engine.py:445` regex sandboxing (H-6, ReDoS). Options: pre-flight catastrophic-backtracking detector, OR `regex` library with `timeout=` (replaces stdlib `re.sub`). Pick simplest viable.
- **S4** — `yadgar/tests/integration/test_vacuum_e2e.py` bootstrap race: replace conditional-skip with `_wait_for_yadgar_rw_auth()` poll-loop. Removes false-green path that masked the v5.1.4 silent no-op.
- **S5** — `docs/architecture.md` rev: module table (post-v5.1.0 split — `server/`, `storage/`, `vacuum/`, `hooks/`), branch-boost formula (convex combination `score + (1-score)*0.2`, not the documented 1.5× scalar), vacuum/backend service section, 2-container model.
- ~~**S6** — anchor scope split.~~ Shipped v5.1.8.

Dispatch order: S1+S2+S3 parallel (security cluster), S4 + S5 follow.

## v5.2.1 — Agent integration (P1)

Unblocks proper subagent participation in the memory system.

- **A1** — `mcp__yadgar__memorize` accepts optional `provenance_agent: str | None`. Defaults to `"default"`. Subagents pass their type name (e.g. `"general-purpose"`, `"Explore"`). Unlocks per-source curation for v6 LLM pass.
- **A2** — global `~/.claude/CLAUDE.md` addition: Subagent Yadgar Contract (read-first, no-write, report-back). User's nix-claude bakes in.
- **A3** — `SubagentStop` hook handler that reads agent transcript final report, extracts `## Yadgar findings` section, queues memorize entries with `provenance_agent = <subagent-type>`. Depends on A1.
- **A4** — agent-prompt versioning. New wiki category `agent-prompt`. Slugs `agent-prompt-<task-pattern>-vN`. Per-dispatch helper reads latest version + injects.

## v5.2.2 — Anthropic 2026 hook adoption (P2)

- **H1** — `InstructionsLoaded` hook fires recall on CLAUDE.md load.
- **H2** — `PostCompact` hook re-injects anchored memories after compaction.
- **H3** — `TeammateIdle` hook flushes yadgar writes per-teammate before idle.
- **H4** — `TaskCreated` / `TaskCompleted` capture agent dispatch lifecycle into `action_log`.

## v5.2.3 — Competitor-parity (P3)

- **C1** — bi-temporal fact windows (Zep parity). Schema migration: `valid_from` / `valid_until` on KG edges. Invalidate on conflict, never delete. Cost M.
- **C2** — recall-frequency-modulated decay (MemoryBank parity). Heat formula change: `heat = min(heat * 0.9995 + recall_boost, 1.0)`. Cost S.
- **C3** — citation tracing (Zep parity). Add `source_memory_id` field on KG edge schema. Populate on KG writes. Cost S-M.
- **C4** — LLM-resolved conflict ops on write (Mem0 parity). Optional small Ollama call at memorize time. Retrieve top-K similar, decide ADD/UPDATE/DELETE/NOOP. Gate behind `YADGAR_CONFLICT_RESOLVER=on` env flag. Cost M.

## v5.2.4 — Quality-of-life (P4)

- **Q1** — token-budget + cache-hit metrics on `/metrics` (goal-h).
- **Q2** — `_postmortem` / `_incident` tag retrieval boost when current task contains action verbs (deploy, push, merge, restart) (goal-j).
- **Q3** — wiki coverage analyzer: `wiki_query` for known modules, flag uncovered (goal-f).
- **Q4** — `PLAN_*.md` as first-class memory targets (goal-g).

## Deferred to v6

- A-MEM backward-propagating evolution (gates on LLM curator).
- Letta-style agent-self-edit-at-inference (architectural rethink needed).
- Sleep-phase consolidation soft-gating (overlaps with v6 nightly curator).

## Open design forks

1. Write-time conflict resolution (Mem0) vs nightly batch (v6 plan).
2. Bi-temporal in v5.2.3 or v6.1?
3. v6 LLM curator chunking strategy (SleepGate saturation at depth ~15).
4. Anthropic Agent Teams JSONL inbox vs custom inter-agent channel.
5. Auto Memory CLI (`~/.claude/projects/<project>/memory/`) — monitor or compete?

## Status

v5.2.0 dispatch in progress.
