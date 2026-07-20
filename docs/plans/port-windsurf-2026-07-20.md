# Windsurf hook port — plan

- status: PLANNED (deferred)
- verified: 2026-07-20 primary-source (supersedes ADR-0145 + port-clients-survey-2026-07-18.md §Task #57-D)
- task: #57-D
- emitter: `yadgar/core/install/clients/hooks_render.py` — replace `_emit_stub` for Windsurf

---

## Verified contract

**Config file location:**
`.windsurf/hooks.json` (project) or `~/.windsurf/hooks.json` (global).
Hooks are JSON objects mapping event names to shell script paths + optional env.

**Real event names (12 events, verified at docs.windsurf.com/windsurf/cascade/hooks):**
`pre_read_code`, `post_read_code`, `pre_write_code`, `post_write_code`,
`pre_run_command`, `post_run_command`, `pre_mcp_tool_use`, `post_mcp_tool_use`,
`pre_user_prompt`, `post_cascade_response`, `post_cascade_response_with_transcript`,
`post_setup_worktree`

**Inject mechanism:**
Hook stdout is `show_output` UI-only display — NOT injected into model context.
**Inject is impossible via hooks.** There is no `additionalContext` or context-
mutation return contract for Windsurf hooks. This is a hard architectural limit.

- `pre_user_prompt` ≈ UserPromptSubmit → fires; stdout is UI display only
- `post_mcp_tool_use` ≈ PostToolUse → fires; capture possible (stdin payload)
- `post_cascade_response_with_transcript` → delivers JSONL transcript post-run;
  usable as partial session drain (not real-time PreCompact)
- `post_cascade_response` ≈ Stop → fires; non-blocking POST only
- No `session_start` event (confirmed absent)
- No `pre_compact` event (confirmed absent)

**Critical constraint:** `show_output` is UI-only. Any context injection strategy
for Windsurf must rely on MCP tool calls (model pulls context) rather than hook
push — hooks cannot push context into the model's window.

---

## Per-need mapping

| Need | Status | Mechanism |
|------|--------|-----------|
| Session-start inject | NONE | No `session_start` hook; no inject mechanism; must rely on MCP recall-first rules in AGENTS.md |
| User-prompt inject | NONE | `pre_user_prompt` fires but stdout is UI-only; no context inject |
| Post-tool capture | FUNCTIONAL | `post_mcp_tool_use` → stdin payload; `yadgar hook postToolUse` |
| Pre-compact drain | FUNCTIONAL (partial) | `post_cascade_response_with_transcript` → JSONL drain post-run; not real-time |
| Stop checkpoint | NONE (non-blocking) | `post_cascade_response` fires but non-blocking POST; async checkpoint only |

**Coverage: 1/5 functional, 1/5 partial, 3/5 none.**

---

## Build scope

Wire in `hooks_render.py` replacing Windsurf `_emit_stub` (capture+drain only):

1. `post_mcp_tool_use` → `yadgar hook postToolUse` (stdin = payload)
2. `post_cascade_response_with_transcript` → parse JSONL transcript; forward to
   `yadgar drain <cwd>` (batch capture of session writes)
3. `post_cascade_response` → async `yadgar hook stop` (non-blocking; best-effort)
4. `pre_read_code` / `pre_write_code` / `pre_run_command` hooks → optional;
   out of scope v1

Rules file: `.windsurf/rules/yadgar.md` or AGENTS.md (both supported by Windsurf).
Rely on AGENTS.md rules-file contract to drive recall-first behavior since inject
is impossible via hooks.

**No inject path:** accept as architectural constraint. Windsurf users rely on
explicit MCP recall calls (model-driven) rather than automatic context injection.

---

## Open questions / live-tests

- **JSONL transcript format:** verify `post_cascade_response_with_transcript` payload
  structure; confirm tool_use entries are present for yadgar to parse
- **4 pre-tool events** (`pre_read_code`, `pre_write_code`, `pre_run_command`,
  `pre_mcp_tool_use`) — evaluate whether any can act as PreCompact substitute
- **`post_setup_worktree`** — fires on worktree creation; potential SessionStart
  substitute for inject if Windsurf ever adds context mutation return

---

## Recommendation

**BUILD (capture+drain only)** — inject is architecturally impossible (stdout = UI
display only). Wire PostToolUse capture and transcript drain. Accept no-inject
constraint; compensate with strong AGENTS.md rules driving model-initiated recall.
Priority: MEDIUM (reduced from HIGH due to inject gap).

---

## Sources

- https://docs.windsurf.com/windsurf/cascade/hooks
- Internal: `docs/plans/port-clients-survey-2026-07-18.md` §Task #57-D (superseded here)
