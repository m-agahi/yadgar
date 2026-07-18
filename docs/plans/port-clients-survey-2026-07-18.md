# Port-Clients Survey — Yadgar Harness Compatibility Matrix

- status: PROPOSED
- date: 2026-07-18
- task: #57
- related: plan #56 (OpenCode port — SEPARATE plan, not duplicated here)

---

## 0. Purpose

Yadgar's harness integration runs entirely inside Claude Code today. This plan surveys
the major agentic CLI/IDE clients for compatibility with yadgar's four integration
surfaces, identifies HIGH-value port targets vs MCP-only partial ports, and proposes
one per-client task for viable targets.

OpenCode (plan #56) is already tracked separately — referenced here for context only.

---

## 1. The four integration surfaces

| Surface | What it is | Claude Code mechanism | Port challenge |
|---------|-----------|----------------------|----------------|
| **S1 — MCP** | Memory/knowledge tools (`recall`, `wiki_add`, `memorize`, etc.) | Streamable-HTTP daemon at `127.0.0.1:8765/mcp` | Low — near-universal by 2026 |
| **S2 — Rules file** | Auto-loaded global + project instructions (CLAUDE.md equivalent) | `~/.claude/CLAUDE.md` + `.claude/CLAUDE.md` | Medium — different filename per tool; AGENTS.md may unify (see §2) |
| **S3 — Lifecycle hooks** | Context injection, checkpointing, capture at session boundaries and tool use | `settings.json` hook scripts per named event | HIGH — the discriminator; many clients now have this |
| **S4 — Task system** | Harness task list (TaskCreate/Complete); yadgar mirrors via `wiki_write_task_list` stop-hook | Claude Code built-in task list + stop-hook mirror | Medium — needs S3 (Stop hook) to mirror |

**S3 (hooks) is the discriminator.** Without hooks, yadgar cannot inject context at
session start, capture after stop, drain before compaction, or mirror task state. The
survey result is that this surface is now substantially present across major clients.

The yadgar hooks in use today (from `yadgar-hook-integration-layer-2026-07-01.md`):

| Event | Yadgar script | What it does |
|---|---|---|
| SessionStart | `yadgar-session-start-context.py` | project_brief + recall inject |
| UserPromptSubmit | `yadgar-prompt-recall.py` | per-prompt recall inject |
| PostToolUse | `yadgar-post-tool-capture.py` | write-back capture batch |
| PreCompact | `yadgar-pre-compact-drain.sh` | drain queue before compaction |
| SessionStart(compact) | `yadgar-post-compact-rehydrate.sh` | restore() after compaction |
| Stop | `yadgar-stop-memory-checkpoint.py` | checkpoint + adr_add; blocking push |
| SubagentStart | `yadgar-subagent-start.py` | agent-prompt + recall hint inject |
| SubagentStop | `yadgar-subagent-stop.py` | extract `## Yadgar findings` |
| InstructionsLoaded | `yadgar-instructions-loaded.py` | rules reinforce |
| SessionEnd | `yadgar-session-end-capture.py` | sentinel write-back |
| FileChanged | `yadgar-file-changed.py` | plan/*.md + team_inbox/*.jsonl capture |

---

## 2. AGENTS.md — the cross-tool S2 standard

**AGENTS.md** is a plain-Markdown rules file standard ("a README for agents") that
emerged mid-2025, stewarded under the **Agentic AI Foundation (Linux Foundation)**
alongside MCP. ~30+ tools honor it natively; 60,000+ open-source repos include one.

If a client honors AGENTS.md, S2 portability is a filename alias only — one
`AGENTS.md` template can serve all such clients simultaneously.

**Claude Code does NOT read AGENTS.md natively** (uses CLAUDE.md). Bridge options:
symlink `ln -s AGENTS.md CLAUDE.md`, or `@AGENTS.md` import inside CLAUDE.md.

Official site: https://agents.md / https://github.com/agentsmd/agents.md

---

## 3. Compatibility matrix

**Scoring legend:**
- Tier: HIGH (S1+S2+S3 viable), MEDIUM (S1+S2, no S3), MCP-ONLY (S1 only)
- Hook count = named lifecycle events with script/JSON mechanism

| Client | MCP transports | Rules file | Hook events (count) | Task system | Tier |
|--------|---------------|-----------|---------------------|-------------|------|
| **Claude Code** (baseline) | streamable-http, stdio | CLAUDE.md (global + project) | 11+ events, blocking (`Stop`, `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`, `InstructionsLoaded`, `SessionEnd`, `FileChanged`) | Yes (TaskCreate/Complete) | native |
| **OpenCode** | streamable-http, stdio | AGENTS.md + `opencode.md` | Architecture mirrors Claude Code; event names TBC (plan #56 spike) | Partial? | HIGH? |
| **Kiro** (Amazon) | stdio, streamable-http, SSE | `.kiro/steering/*.md` + AGENTS.md compatible | **7 events, some blocking:** `Stop`, `PreToolUse` (blocking), `PostToolUse`, `PreTaskExec` (blocking), `PostTaskExec`, `UserPromptSubmit` (blocking), `PostFile*` (no `SessionStart` — docs confirmed absent) | Yes — Specs system (`.kiro/specs/`): requirements.md + design.md + tasks.md per feature | **HIGH** |
| **Cursor** | stdio, SSE, streamable-http | `.cursor/rules/*.mdc` (primary), `.cursorrules` (legacy), AGENTS.md | **~18 events:** `sessionStart`, `sessionEnd`, `beforeSubmitPrompt`, `preToolUse`, `postToolUse`, `postToolUseFailure`, `beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit`, `subagentStart`, `subagentStop`, `afterAgentResponse`, `afterAgentThought`, `stop`, `preCompact`, `workspaceOpen` | Yes — built-in task lists (agent panel, v1.2+) | **HIGH** |
| **Cline** | stdio, SSE, streamable-http | `.clinerules/` (primary), AGENTS.md, `.cursorrules`, `.windsurfrules` | **6 events:** `TaskStart`, `TaskResume`, `TaskCancel`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | Yes — Cline Kanban (standalone app, research preview) | **HIGH** |
| **Windsurf** | stdio, SSE, streamable-http | `.windsurf/rules/*.md`, AGENTS.md, `.windsurfrules` (legacy) | **12 events:** `pre_read_code`, `post_read_code`, `pre_write_code`, `post_write_code`, `pre_run_command`, `post_run_command`, `pre_mcp_tool_use`, `post_mcp_tool_use`, `pre_user_prompt`, `post_cascade_response`, `post_cascade_response_with_transcript`, `post_setup_worktree` | No (Memories feature, not task list) | **HIGH** |
| **Codex CLI** | stdio, streamable-http | AGENTS.md (native; walk from git-root to cwd; global `~/.codex/AGENTS.md`) | **10 events, blocking:** `SessionStart`, `Stop`, `PreCompact`, `PostCompact`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop`, `PermissionRequest` — shipped v0.114 (March 2026); config: `hooks.json` or `[hooks]` in `config.toml`; verified at learn.chatgpt.com/docs/hooks | None native (`todo_write` tool only) | **HIGH** |
| **Gemini CLI** | stdio, SSE, streamable-http | `GEMINI.md` (configurable to AGENTS.md via `settings.json → context.fileName`) | **4 events, advisory-only (cannot block):** `SessionStart`, `SessionEnd`, `Notification`, `PreCompress` | Yes — `write_todos` (session-local) + `tracker_*` (persistent DAG, experimental) | MEDIUM |
| **Amp** | stdio, streamable-http (SSE via URL) | AGENTS.md (primary, hierarchical; falls back to CLAUDE.md) | **5 events:** `session.start`, `agent.start`, `tool.call` (blocking: allow/reject/modify/synthesize), `tool.result`, `agent.end` | None | **HIGH** |
| **Aider** | **No MCP** | `.aider.conf.yml` (`read:` key; opt-in only, no auto-discovery) | None in core | None | **NO-MCP** |
| **Continue.dev** | stdio, SSE, streamable-http | `.continue/rules/*.md` (project + global); no AGENTS.md support | None (config.ts `modifyConfig()` only — not event hooks) | None | MCP-ONLY |
| **Zed** | stdio (via extensions) | System prompt in settings | None | None | MCP-ONLY |

---

## 4. Tiering — HIGH vs MEDIUM vs MCP-ONLY

### HIGH-value port targets (S1 + S2 + S3 all viable)

These clients have a named lifecycle hook surface that can carry yadgar's injection
and capture scripts. Most are JSON-over-stdio hook configs, similar to Claude Code.

| Client | Hook coverage vs yadgar | Gap vs Claude Code | Notes |
|--------|------------------------|-------------------|-------|
| **Kiro** | Stop ✓, PreToolUse ✓, PostToolUse ✓, UserPromptSubmit ✓, PreTaskExec ✓ | **No SessionStart** (confirmed absent in docs), no SubagentStart/Stop, no PreCompact | Specs system is a bonus S4; context inject must move to UserPromptSubmit |
| **Cursor** | ~18 events including `preCompact`, `sessionStart`, `stop`, `subagentStart`, `subagentStop`, `preToolUse`, `postToolUse` | Nearly complete parity | Closest to Claude Code; also has built-in task list |
| **Cline** | TaskStart ≈ SessionStart, UserPromptSubmit ✓, PreToolUse ✓, PostToolUse ✓ | No PreCompact/SubagentStart equivalent; TaskCancel/TaskResume extra | Well-aligned for core hooks |
| **Windsurf** | pre_user_prompt ≈ UserPromptSubmit, pre_mcp_tool_use ≈ PreToolUse, post_mcp_tool_use ≈ PostToolUse, post_cascade_response ≈ Stop | No SessionStart; no SubagentStart/Stop; no PreCompact | post_cascade_response_with_transcript delivers JSONL transcript — powerful for capture |
| **Codex CLI** | Near-parity: SessionStart ✓, Stop ✓, PreCompact ✓, PostCompact ✓, UserPromptSubmit ✓, PreToolUse ✓, PostToolUse ✓, SubagentStart ✓, SubagentStop ✓ | 9/11 yadgar events covered | Best hook parity after Claude Code |
| **Amp** | session.start ✓, agent.end ≈ Stop (can continue), tool.call ≈ PreToolUse (blocking), tool.result ≈ PostToolUse | No SubagentStart/Stop, no PreCompact, no UserPromptSubmit | `tool.call` can synthesize results — more powerful than simple allow/deny |
| **OpenCode** | TBC (plan #56 spike) | Unknown | Architecture mirrors Claude Code; likely near-parity |

### MEDIUM targets (S1 + S2, no S3)

**Gemini CLI** — MCP (all 3 transports), GEMINI.md (configurable to AGENTS.md), 4
advisory-only hooks. Hooks cannot block flow, limiting yadgar to context injection
only (no capture-on-stop, no pre-compaction drain). Practically: yadgar MCP tools
accessible; rules file carries the contract; user must call tools manually or rely on
the session-start injection. Resident on the user's nix setup — low-friction port.

Upgrade path: if Gemini CLI adds blocking hooks in a future release, this moves to HIGH.

### MCP-ONLY (S1 alone, no meaningful S2/S3)

| Client | Reason |
|--------|--------|
| Continue.dev | No AGENTS.md support; rules format is own `.continue/rules/` markdown with frontmatter; no lifecycle hook events |
| Zed | MCP via extensions; no session-level hooks; no auto-loaded rules file equivalent |

### NO-MCP (no S1)

| Client | Reason |
|--------|--------|
| Aider | No MCP support as of 2026-07; no auto-loaded rules file |

---

## 5. AGENTS.md as a unifying S2 surface

One canonical `AGENTS.md` template carries the yadgar contract to all AGENTS.md-native
clients simultaneously. Template should include:

1. **Yadgar MCP endpoint:** `http://127.0.0.1:8765/mcp` (streamable-http)
2. **Recall-first contract:** check yadgar before codebase searches
3. **Write-back triggers:** memorize non-obvious cross-session discoveries
4. **Dispatch prelude rule:** call `agent_dispatch_prelude(pattern, task_topic)` before any agent dispatch
5. **`## Yadgar findings` footer:** required on every agent report

Clients that read AGENTS.md natively: **Codex CLI, Cursor, Cline, Windsurf, Amp, Kiro**
(compatible). Clients requiring a bridge: Claude Code (symlink or `@AGENTS.md` import).

A yadgar task (`install_hooks` variant) could emit `AGENTS.md` alongside Claude Code
hooks, making S2 cross-client by default.

---

## 6. Proposed tasks

### Task #57-A — Codex CLI: full harness port (HIGH priority)
- **Scope:** Register yadgar MCP; write AGENTS.md carrying the yadgar contract; port
  9-event hook set (nearly 1:1 with Claude Code). Verify blocking Stop hook carries
  checkpoint + adr_add.
- **Effort:** Medium (9 hooks to adapt; format is JSON-over-stdio similar to Claude Code)
- **Hook gaps:** PostCompact may need daemon endpoint; SubagentStart/Stop payloads need verification
- **Dependency:** None (Codex CLI available; AGENTS.md native)
- **Docs:** https://github.com/openai/codex, https://github.com/openai/codex#agentsmd

### Task #57-B — Cursor: full harness port (HIGH priority)
- **Scope:** Register yadgar MCP (streamable-http); write `.cursor/rules/yadgar.mdc`
  carrying the contract; port ~10 relevant hooks from Cursor's 18-event set
  (`sessionStart`, `stop`, `preCompact`, `userPromptSubmit`, `preToolUse`,
  `postToolUse`, `subagentStart`, `subagentStop`). Verify `preCompact` blocks.
- **Effort:** Medium-High (18 events to map; `.mdc` format vs JSON)
- **Bonus:** built-in task list makes S4 available
- **Docs:** https://docs.cursor.com/advanced/mcp, https://cursor.com/docs/hooks

### Task #57-C — Cline: full harness port (HIGH priority)
- **Scope:** Register yadgar MCP; write `.clinerules/` yadgar rules; port 6 hooks
  (`TaskStart`→SessionStart, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`; `TaskCancel`
  for drain). No PreCompact equivalent — drain via `PostToolUse` or `TaskCancel` only.
- **Effort:** Medium (6 hooks; macOS/Linux only; hook scripts are executables not JSON config)
- **Gap:** No PreCompact → pre-compaction drain not fully portable
- **Docs:** https://cline.bot/blog/cline-v3-36-hooks, https://docs.cline.bot/customization/cline-rules

### Task #57-D — Windsurf: harness port with transcript capture
- **Scope:** Register yadgar MCP; write AGENTS.md or `.windsurf/rules/yadgar.md`;
  port hooks (`pre_user_prompt`, `pre_mcp_tool_use`, `post_mcp_tool_use`,
  `post_cascade_response`). Leverage `post_cascade_response_with_transcript` for
  JSONL-based session capture (may be richer than Claude Code's SubagentStop).
- **Effort:** Medium (12 events; JSONL transcript capture is a bonus feature)
- **Gap:** No SessionStart hook; no PreCompact; no SubagentStart/Stop
- **Docs:** https://docs.devin.ai/desktop/cascade/hooks

### Task #57-E — Kiro: harness port (HIGH priority)
- **Scope:** Register yadgar MCP; use `.kiro/steering/` + AGENTS.md; port 7 hooks
  (`Stop`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `PreTaskExec`, `PostTaskExec`, `PostFile*`).
  Note: no `SessionStart` in Kiro — context injection must run on first `UserPromptSubmit` instead.
  Integrate with Kiro Specs for S4 task mirroring.
- **Effort:** Medium (GA as of March 2026; docs available; format is JSON in `.kiro/hooks/`)
- **Bonus:** Specs system (`.kiro/specs/`) is closest thing to a native S4
- **Docs:** https://kiro.dev/docs/hooks/, https://kiro.dev/docs/mcp/, https://kiro.dev/docs/steering/

### Task #57-F — Amp: harness port (HIGH priority)
- **Scope:** Register yadgar MCP; write AGENTS.md (Amp's native format with hierarchical
  lookup including global `~/.config/amp/AGENTS.md`); port 5 hooks (`session.start`,
  `agent.end`, `tool.call`, `tool.result`). Use `tool.call` `synthesize` action for
  context injection.
- **Effort:** Medium (5 hooks; `synthesize` action is novel — needs spike)
- **Docs:** https://ampcode.com/manual, https://ampcode.com/news/hooks

### Task #57-G — Gemini CLI: S1+S2 port + advisory hook injection (MEDIUM)
- **Scope:** Register yadgar MCP in `~/.gemini/settings.json` (already installed per
  nix); set `context.fileName: "GEMINI.md"` + write GEMINI.md with yadgar contract.
  Wire `SessionStart` advisory hook for context injection (cannot block). Document that
  capture (Stop/PostToolUse write-back) requires manual calls.
- **Effort:** Small (already installed; advisory hooks only — no blocking needed for inject)
- **Dependency:** None (gemini-cli in nix already)
- **Docs:** https://github.com/google-gemini/gemini-cli

### Task #57-H — AGENTS.md cross-client template + install_hooks emit
- **Scope:** Write canonical `AGENTS.md` template with full yadgar contract; add
  `yadgar install_hooks --agents-md` subcommand to emit it per-project. Test across
  Codex CLI + Cursor + Cline + Windsurf + Amp + Kiro.
- **Effort:** Small-Medium (template authoring + install_hooks extension)
- **Dependency:** Precedes all per-client S2 tasks above (shared foundation)

### NOT proposed (deferred)
- **Aider:** no MCP → no port until Aider adds MCP support
- **Continue.dev:** MCP works but no AGENTS.md + no lifecycle hooks → port value too low
- **Zed:** MCP only via extensions; no lifecycle hooks; low priority

---

## 7. Open questions

1. **OpenCode hook schema** — exact event names and payload format not public. Plan #56 spikes this.
2. **Codex CLI SubagentStart/Stop payloads** — verify payload schema before porting subagent hooks.
3. **Windsurf SessionStart absence** — no `pre_session_start` event; confirm whether `post_setup_worktree` is a viable substitute for session-context injection.
4. **Cline PreCompact gap** — no equivalent event; draft a drain strategy (TaskCancel or scheduled PostToolUse?) before building.
5. **Gemini CLI blocking hooks** — track if future releases add exit-code-based blocking; would upgrade from MEDIUM to HIGH.
6. **AGENTS.md global path per tool** — each tool has a different global location (`~/.codex/AGENTS.md`, `~/.config/amp/AGENTS.md`, `~/.agents/AGENTS.md` for Cline); the install_hooks emit needs to handle per-tool global paths.
7. **Amp `synthesize` action** — most powerful tool.call action; spike needed to verify it can inject yadgar context into the tool result stream without breaking the model's tool call flow.

---

## 8. Scope boundary with plan #56

Plan #56 (OpenCode port) covers:
- OpenCode hook event names + payload schema (spike)
- OpenCode rules-file registration
- Full harness port: SessionStart, Stop, PreCompact equivalents
- Nix module wiring for opencode hook scripts

This plan (#57) covers all other clients. References to OpenCode in §3 matrix are
for comparison only.

---

## Sources

- AGENTS.md standard: https://agents.md, https://github.com/agentsmd/agents.md
- AGENTS.md AAIF (Linux Foundation): https://learn.chatgpt.com/docs/agent-configuration/agents-md
- **OpenAI Codex CLI:** https://github.com/openai/codex
- Codex CLI AGENTS.md: https://github.com/openai/codex#agentsmd
- Codex CLI hooks: hooks.json / [hooks] in config.toml (from source)
- **Gemini CLI:** https://github.com/google-gemini/gemini-cli
- Gemini CLI MCP: https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md
- Gemini CLI GEMINI.md: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md
- Gemini CLI hooks: https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
- Gemini CLI todos: https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/todos/
- **Cursor MCP:** https://docs.cursor.com/context/model-context-protocol
- Cursor rules: https://docs.cursor.com/context/rules-for-ai
- Cursor hooks: https://cursor.com/docs/hooks, https://blog.gitbutler.com/cursor-hooks-deep-dive
- Cursor tasks: https://cursor.com/changelog/1-2
- **Cline MCP transport:** https://docs.cline.bot/mcp/mcp-transport-mechanisms
- Cline rules: https://docs.cline.bot/customization/cline-rules
- Cline hooks: https://cline.bot/blog/cline-v3-36-hooks
- Cline Kanban: https://docs.cline.bot/kanban/overview
- **Windsurf MCP:** https://docs.devin.ai/desktop/cascade/mcp
- Windsurf AGENTS.md: https://docs.devin.ai/desktop/cascade/agents-md
- Windsurf hooks: https://docs.devin.ai/desktop/cascade/hooks
- **Amp MCP + AGENTS.md:** https://ampcode.com/manual, https://ampcode.com/news/AGENT.md
- Amp hooks: https://ampcode.com/news/hooks
- **Kiro MCP:** https://kiro.dev/docs/mcp/, https://kiro.dev/docs/mcp/configuration/
- Kiro steering: https://kiro.dev/docs/steering/
- Kiro hooks: https://kiro.dev/docs/hooks/
- Kiro specs: https://kiro.dev/docs/specs/
- **Aider:** https://aider.chat/docs/config/aider_conf.html, https://aider.chat/docs/usage/conventions.html
- Aider MCP status: https://www.wearewarp.com/agents/mcp/aider
- **Continue.dev MCP:** https://docs.continue.dev/customize/deep-dives/mcp
- Continue.dev rules: https://docs.continue.dev/customize/deep-dives/rules
- Yadgar internal: docs/plans/yadgar-hook-integration-layer-2026-07-01.md
