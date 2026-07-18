# Plan: Port Yadgar Harness Integration to OpenCode

**Status:** PROPOSED
**Date:** 2026-07-18
**Task:** #56
**Author:** openfantasy-toaster

---

## Background

Yadgar's MCP server already works with OpenCode — the global config at
`~/.config/opencode/opencode.json` has `mcp.yadgar.type = "remote"` pointing at
`http://127.0.0.1:8765/mcp` (streamable-HTTP transport, the yadgar default).
The open question is **harness-specific glue**: lifecycle hooks, the instructions
sync, and the task-list mirror all target Claude Code's proprietary file layout.
This plan maps each touchpoint to its OpenCode equivalent and proposes a
build sequence.

**OpenCode version observed locally:** 1.17.7
**Yadgar version at plan date:** v5.136 / core 5.136.0

---

## 1. Yadgar Claude Code Touchpoint Inventory

All paths rooted at `yadgar/core/`.

| # | Touchpoint | Files | CC mechanism |
|---|-----------|-------|-------------|
| T1 | **MCP transport** | `server/_app.py`, `__main__.py` | streamable-HTTP on :8765 |
| T2 | **Hook install** | `core/install/install_hooks_lib.py`, `install/_settings.py` | Writes scripts to `~/.claude/hooks/`; patches `~/.claude/settings.json` with named events |
| T3 | **SessionStart** | `hooks/session-start-context.py` | CC `SessionStart` event; HTTP GET `/hooks/session-context`; output injected to context |
| T4 | **Stop / checkpoint** | `hooks/stop-memory-checkpoint.py` | CC `Stop` event (every N messages); blocks stop, emits protocol template prompting `checkpoint()` + `wiki_write_task_list()` |
| T5 | **SessionEnd** | `hooks/session-end-capture.py` | CC `SessionEnd` event; sentinel capture |
| T6 | **PreCompact (drain)** | `hooks/pre-compact-drain.sh` → `yadgar drain <cwd>` | CC `PreCompact` event |
| T7 | **PostCompact (restore)** | `hooks/post-compact-rehydrate.sh` → `yadgar restore <cwd>` | CC `PostCompact` event |
| T8 | **PostToolUse (capture)** | `hooks/post-tool-capture.py` | CC `PostToolUse` event; writes action_log rows |
| T9 | **PostToolUse (block-reflect)** | same entry, second hook entry | CC `PostToolUse`; block event reflection |
| T10 | **UserPromptSubmit (recall)** | `hooks/prompt-recall.py` | CC `UserPromptSubmit` event; FTS on prompt text → injects recall block |
| T11 | **PreToolUse (router-guard)** | `hooks/pretooluse-router.py` | CC `PreToolUse` event; decision-gate (block/allow/modify tool inputs) |
| T12 | **SubagentStop** | `hooks/subagent-stop.py` | CC `SubagentStop` event; extracts `## Yadgar findings` from transcript |
| T13 | **SubagentStart** | `hooks/subagent-start.py` | CC `SubagentStart` event; injects context to subagent |
| T14 | **InstructionsLoaded** | `hooks/instructions-loaded.py` | CC `InstructionsLoaded` event; recall on CLAUDE.md load |
| T15 | **FileChanged** | `hooks/file-changed.py` | CC `FileChanged` event; `team_inbox` + `PLAN_*.md` triggers |
| T16 | **sync_instructions** | `server/tools/misc.py:459` | Writes/updates `## Memory System — Yadgar` section in `~/.claude/CLAUDE.md` |
| T17 | **wiki_write_task_list (outbound)** | `server/tools/wiki.py:192` | MCP tool called by Stop hook; saves harness task list to wiki |
| T18 | **Task-list inbound seeding** | `hooks/session-start-context.py` (nudge) | SessionStart output includes imperative nudge to call TaskCreate; mechanical file-writer (Option A) deferred (ADR-0137) |
| T19 | **install_hooks MCP tool** | `server/tools/misc.py`, delegating to `install_hooks_impl` | MCP tool that self-registers hooks into `settings.json` |
| T20 | **HUD status bar** | `llm.nix` — `~/.claude/hud/status.sh` | CC `statusLine` / `subagentStatusLine` keys in `settings.json` |

---

## 2. OpenCode Capability Map (v1.17.7)

Sources: opencode.ai/docs, GitHub sst/opencode, local install, `~/.config/opencode/opencode.json`.

| OpenCode concept | Detail |
|-----------------|--------|
| **MCP transport** | `type: "remote"` in `opencode.json` → streamable-HTTP. Already working (yadgar is wired). |
| **Instructions file** | `AGENTS.md` (project root) or `~/.config/opencode/AGENTS.md` (global). Also supports `"instructions"` glob array in `opencode.json`. Fallback: `CLAUDE.md`. |
| **Hook system** | Plugin-based (TypeScript/JS). Plugins live in `.opencode/plugins/` (project) or `~/.config/opencode/plugins/` (global). No declarative shell-script hooks in JSON config — hooks are plugin exports. |
| **Hook events available** | `SessionStart`, `SessionEnd`, `PreCompact`, `PostCompact`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `UserPromptSubmit`, `FileChanged`, `Notification`. (Source: opencode.ai/docs/plugins) |
| **Hook payload** | Plugins receive/return JSON context objects. `SessionStart` receives trigger type (`"startup"`, `"resume"`, `"compact"`) + `sessionID`. |
| **Config location** | `~/.config/opencode/opencode.json` (global); `opencode.json` (project). JSON/JSONC format. Configs merge. |
| **Task/todo** | `TodoWrite` tool — in-session memory only. No persistent store across sessions. No `~/.config/opencode/tasks/` equivalent. Feature-requested but unshipped. |
| **Status bar / HUD** | No equivalent to CC's `statusLine` config key. TUI settings in `~/.config/opencode/tui.json` (cosmetic only). |
| **SubagentStop / SubagentStart** | Events present per docs. Payload includes `transcript_path`? To be verified. |

**Key discovery — streamable-HTTP confirmed working:** The agent report claimed streamable-HTTP was unsupported; observed state refutes this. `~/.config/opencode/opencode.json` uses `type: "remote"` with URL `/mcp`, which is streamable-HTTP. Yadgar's default transport is `streamable-http` (`__main__.py:52`). MCP already connects.

---

## 3. Touchpoint → OpenCode Mapping

| # | Touchpoint | Verdict | Notes |
|---|-----------|---------|-------|
| T1 | MCP transport | **PORTS-CLEANLY** | Already wired in `~/.config/opencode/opencode.json`. No code change needed. |
| T2 | Hook install mechanism | **NEEDS-ADAPTATION** | CC writes scripts + JSON entries to `~/.claude/`. OC needs a plugin (TS/JS) in `~/.config/opencode/plugins/` instead of shell scripts in `~/.claude/hooks/`. `install_hooks_impl` must grow an `--client opencode` path. |
| T3 | SessionStart context | **NEEDS-ADAPTATION** | Event name matches (`SessionStart`). Plugin calls the same HTTP endpoint (`/hooks/session-context`) and injects output. Port: wrap the Python logic as a TS plugin export or shell-out from plugin. |
| T4 | Stop / checkpoint | **NEEDS-ADAPTATION** | `Stop` event present. Same protocol template approach works. Plugin replaces the shell/Python script. Output mechanism (inject prompt) needs OC plugin return contract verified. |
| T5 | SessionEnd capture | **NEEDS-ADAPTATION** | `SessionEnd` event present. Straightforward port. |
| T6 | PreCompact drain | **NEEDS-ADAPTATION** | `PreCompact` event present. Plugin replaces shell script; calls `yadgar drain <cwd>`. |
| T7 | PostCompact restore | **NEEDS-ADAPTATION** | `PostCompact` event present. Plugin replaces shell script; calls `yadgar restore <cwd>`. |
| T8 | PostToolUse capture | **NEEDS-ADAPTATION** | `PostToolUse` event present. Action-log write via HTTP. Plugin replaces Python script. |
| T9 | PostToolUse block-reflect | **NEEDS-ADAPTATION** | Same event slot. Bundle into same plugin handler. |
| T10 | UserPromptSubmit recall | **NEEDS-ADAPTATION** | `UserPromptSubmit` event present. Plugin calls `/hooks/recall-forward` and injects recall block. |
| T11 | PreToolUse router-guard | **NEEDS-ADAPTATION** | `PreToolUse` event present. Decision-gate (block/allow/modify) — plugin must return the OC equivalent of a block response; contract needs checking against OC docs. |
| T12 | SubagentStop | **NEEDS-ADAPTATION (unverified)** | Event listed in OC docs. Payload (esp. `transcript_path`) needs verification — CC passes the JSONL path; OC may differ. |
| T13 | SubagentStart | **NEEDS-ADAPTATION (unverified)** | Event listed. Payload unknown. |
| T14 | InstructionsLoaded | **NO-EQUIVALENT / SKIP** | CC fires this when CLAUDE.md loads. OC has no exact analog — AGENTS.md is loaded at session start, not as a separate event. The recall-on-instructions-load behavior can be folded into SessionStart plugin. |
| T15 | FileChanged | **NEEDS-ADAPTATION (unverified)** | OC has `FileChanged` event per docs. `team_inbox` + `PLAN_*.md` glob matching should port. |
| T16 | sync_instructions | **NEEDS-ADAPTATION** | Target changes from `~/.claude/CLAUDE.md` to `~/.config/opencode/AGENTS.md`. Logic is identical (find/replace a named section); just parameterize the path. |
| T17 | wiki_write_task_list (outbound) | **PORTS-CLEANLY** | Pure MCP tool call in Stop hook — already client-agnostic once Stop fires. No change to server code. |
| T18 | Task-list inbound seeding | **NO-EQUIVALENT** | OC has no persistent task store. Mechanical file-write (Option A) has nothing to write to. The nudge (Option B) still works if the model calls a tool to create tasks — but OC has no TaskCreate harness tool equivalent. This leg is **blocked** until OC ships persistent todos or a plugin provides them. |
| T19 | install_hooks MCP tool | **NEEDS-ADAPTATION** | Must grow `client` parameter (`"claude-code"` | `"opencode"`). OC path writes a TS plugin file instead of JSON entries. |
| T20 | HUD status bar | **NO-EQUIVALENT / OUT-OF-SCOPE** | OC has no `statusLine` equivalent. Skip for v1. |

**Summary counts:** PORTS-CLEANLY: 2 | NEEDS-ADAPTATION: 15 | NO-EQUIVALENT/SKIP: 3

---

## 4. What Is Buildable Now vs. Blocked

### Buildable now

All NEEDS-ADAPTATION rows are buildable. The plugin system is available and the
events map cleanly:

- All core lifecycle events (SessionStart, Stop, SessionEnd, PreCompact, PostCompact,
  PostToolUse, UserPromptSubmit, PreToolUse) have OC equivalents.
- The yadgar HTTP endpoints the hooks call are already client-agnostic.
- `sync_instructions` only needs a path parameter change.
- `install_hooks` needs an OC branch that writes a plugin file.

### Blocked

- **T18 — task-list inbound** (the "inbound leg"): OC has no persistent task store
  and no `TaskCreate` equivalent tool. Until OC ships this (or a plugin provides a
  shim), the wiki → harness round-trip cannot be closed. The outbound leg (T17)
  still works — tasks are saved to the wiki at Stop.
- **T11 / T12 / T13 / T15 — payload verification**: PreToolUse block semantics,
  SubagentStop `transcript_path`, SubagentStart payload, and FileChanged glob
  behavior all need hands-on testing against OC 1.17.7. Build can proceed
  speculatively; each needs a smoke test before declaring done.

### Key uncertainty

The OC plugin system requires TypeScript/JavaScript. Yadgar's hooks are Python.
Two options:

- **Option A (shell-out):** TS plugin is a thin shim that `execa`s the existing
  Python scripts. Low rewrite cost; adds Node→Python subprocess overhead (~50ms).
- **Option B (native TS):** Rewrite hook logic in TypeScript. Higher effort but
  eliminates the subprocess and makes the plugin self-contained. Viable because
  all hook logic is thin (HTTP call → format → return).

Option A ships faster. Option B is cleaner long-term. Recommend Option A for v1,
with Option B as a follow-up.

---

## 5. Proposed Build-Car Breakdown

### Car A — Foundation (prerequisite)
Extend `install_hooks_impl` to support `client: "opencode"` scope. Output:
a TS plugin file at `~/.config/opencode/plugins/yadgar.ts` (or `.js`) that
registers all event handlers as shell-outs to existing Python scripts.
The `install_hooks` MCP tool gets a `client` parameter defaulting to `"claude-code"`.

**Deliverable:** `yadgar install-hooks --client opencode` writes the plugin.
**Files:** `yadgar/core/install/_settings.py`, `install_hooks_lib.py`, new `_opencode_plugin.py`.

### Car B — Core lifecycle hooks (SessionStart, Stop, SessionEnd, PreCompact, PostCompact)
Wire the five session-lifecycle events in the plugin. Each handler:
1. Reads hook payload from OC plugin context (extract `cwd`, `sessionID`, `branch`).
2. Calls the same yadgar HTTP endpoint the Python scripts call.
3. Returns the result in OC plugin contract format.

**Blocked-on:** Car A.

### Car C — Tool hooks (PostToolUse, UserPromptSubmit, PreToolUse)
Wire action capture, recall-forward, and router-guard. PreToolUse block/allow
semantics need a test harness against OC before declaring done.

**Blocked-on:** Car A.

### Car D — sync_instructions for OpenCode
Add `target: "opencode"` parameter to `sync_instructions` (MCP tool + CLI).
When `opencode`, write to `~/.config/opencode/AGENTS.md` instead of
`~/.claude/CLAUDE.md`. Section header changes from `## Memory System — Yadgar`
to the same (or a configurable name). The Nix `llm.nix` module gains a
`home.file."${config.xdg.configHome}/opencode/AGENTS.md"` stanza.

**Blocked-on:** nothing (independent of Cars A-C).

### Car E — SubagentStop / SubagentStart / FileChanged (verify + wire)
These three events exist in OC docs but payload contracts are unconfirmed.
Step 1: write a minimal OC plugin that logs all three payloads to file.
Step 2: run OC, inspect payloads. Step 3: implement handlers if payloads match CC.

**Blocked-on:** Car A (need the plugin scaffold).

### Car F — Task-list inbound (future / blocked)
Watch for OC to ship persistent todo storage or a `TaskCreate`-equivalent tool.
Until then, the Stop hook still writes the wiki (T17 works) and the SessionStart
nudge still fires — it just can't seed a harness task list. Track as a
deferred item; revisit when OC ships the feature.

---

## 6. Follow-up Tasks to Create

1. **Spike: OC plugin payload logging** — Write a minimal TS OC plugin that logs
   all available event payloads (SessionStart, Stop, PreToolUse, PostToolUse,
   UserPromptSubmit, SubagentStop, SubagentStart, FileChanged) to a temp file.
   Run OC through a session. Capture and document the exact payload schemas.
   Unblocks Cars B, C, E.

2. **Car A: `install_hooks --client opencode`** — Extend `install_hooks_impl` with
   `client` parameter; write OC plugin scaffold; update MCP tool signature; tests.

3. **Car B: Session-lifecycle plugin handlers** — Implement SessionStart, Stop,
   SessionEnd, PreCompact, PostCompact in TS plugin (shell-out to existing Python).

4. **Car C: Tool-hook plugin handlers** — Implement PostToolUse, UserPromptSubmit,
   PreToolUse in TS plugin. Smoke-test PreToolUse block semantics.

5. **Car D: `sync_instructions` for OpenCode** — Add `target` param; write to
   `~/.config/opencode/AGENTS.md`; update Nix `llm.nix` to manage AGENTS.md.

6. **Car E: SubagentStop / SubagentStart / FileChanged** — Wire after spike confirms
   payloads. If payloads mismatch CC, adapt extraction logic.

7. **Deferred: OC task-list inbound** — Watch OC issue tracker; implement inbound
   seeding when a persistent task store ships. (Reference OC GitHub issues #5934,
   #18071.)

---

## 7. Risks and Unknowns

| Risk | Severity | Mitigation |
|------|----------|-----------|
| OC plugin payload contracts differ from CC (SubagentStop esp.) | Medium | Spike (task #1) before building Cars B/E |
| PreToolUse block/allow return contract unknown | Medium | Include in spike; may require OC docs PR or source read |
| TS plugin requires Node runtime at hook-fire time | Low | OC ships Node; `execa` available; not a constraint |
| Python shell-out adds 50ms latency per hook | Low | Acceptable for v1; Option B (native TS) fixes later |
| OC releases (1.17.7 → future) may change plugin API | Medium | Pin OC version in Nix; test on upgrade |
| Task-list inbound permanently blocked if OC never ships persistent todos | Low-Medium | Outbound (wiki) still works; only harness visibility breaks |
| `sync_instructions` must not clobber user-authored AGENTS.md content | Low | Same find/replace-section logic CC uses; section-delimited, safe |

---

## Sources

- https://opencode.ai/docs/mcp-servers/
- https://opencode.ai/docs/rules/
- https://opencode.ai/docs/plugins/
- https://opencode.ai/docs/config/
- https://opencode.ai/docs/agents/
- https://github.com/sst/opencode/issues/5934
- https://github.com/sst/opencode/issues/18071
- `~/.config/opencode/opencode.json` (observed, OC 1.17.7)
- `yadgar/core/install/install_hooks_lib.py` (T2)
- `yadgar/core/install/_settings.py` (T3–T15, event names)
- `yadgar/core/server/tools/misc.py:459` (T16)
- `yadgar/core/server/tools/wiki.py:192` (T17)
- `yadgar/__main__.py:52` (streamable-HTTP default)
