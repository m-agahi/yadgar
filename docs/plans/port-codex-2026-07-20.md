# Codex hook port — plan

- status: PLANNED (deferred)
- verified: 2026-07-20 primary-source (supersedes ADR-0145 + port-clients-survey-2026-07-18.md §Task #57-A)
- task: #57-A
- emitter: `yadgar/core/install/clients/hooks_render.py` — replace `_emit_stub` for Codex

---

## Verified contract

**Config file location:**
`~/.codex/hooks.json` (global) or `[hooks]` in `~/.codex/config.toml`; project-local
`.codex/hooks.json`. Shipped v0.114 (March 2026).

**Real event names (from `codex-rs/app-server-protocol/schema/typescript/v2/HookEventName.ts`):**
`sessionStart`, `userPromptSubmit`, `postToolUse`, `preCompact`, `stop`, `postCompact`,
`subagentStart`, `subagentStop`, `preToolUse`, `permissionRequest`

**Inject mechanism:**
stdout-JSON. Hook scripts print a JSON envelope; Codex reads stdout and merges
`additionalContext` (developer-role, append-only) into the context window.

- `sessionStart` → `additionalContext` field (inject)
- `userPromptSubmit` → `additionalContext` field (inject)
- `postToolUse` → full payload delivered; stdout ignored for inject (capture only)
- `preCompact` → fires; shell-out blocks; return 0 = allow drain to complete
- `stop` → **BLOCKING** (unique: only non-CC client with a blocking Stop);
  return non-zero or emit `{"reason": "..."}` to gate continuation

Sources confirm `stop` blocks — verified at `hook_names.rs` and `hook_additional_context.rs`.

---

## Per-need mapping

| Need | Status | Mechanism |
|------|--------|-----------|
| Session-start inject | FUNCTIONAL | `sessionStart` → stdout `additionalContext` (developer-role) |
| User-prompt inject | FUNCTIONAL | `userPromptSubmit` → stdout `additionalContext` (append-only) |
| Post-tool capture | FUNCTIONAL | `postToolUse` → full payload in stdin; `yadgar hook postToolUse` |
| Pre-compact drain | FUNCTIONAL | `preCompact` → shell-out blocks; `yadgar drain <cwd>` then exit 0 |
| Stop checkpoint | FUNCTIONAL | `stop` → blocking; emit `{"reason": "..."}` continuation gate |

**Coverage: 5/5** — best parity outside Claude Code.

---

## Build scope

Wire in `hooks_render.py` replacing Codex `_emit_stub`:

1. `sessionStart` → shell-out to `yadgar hook sessionStart` → stdout inject
2. `userPromptSubmit` → shell-out to `yadgar hook userPromptSubmit` → stdout inject
3. `postToolUse` → shell-out to `yadgar hook postToolUse` (stdin = payload)
4. `preCompact` → `yadgar drain <cwd>`; exit 0
5. `stop` → `yadgar hook stop` (blocking; protocol template prompt)

Config target: write entries to `~/.codex/hooks.json` (or TOML `[hooks]` section if
user's existing config is TOML). `install_hooks --client codex` determines format from
existing config file extension.

AGENTS.md global path for Codex: `~/.codex/AGENTS.md` (confirmed native reader).

---

## Open questions / live-tests

- `subagentStart` / `subagentStop` payload schemas — verify against OC before porting
  subagent hooks (low-priority; core 5/5 ships without them)
- `permissionRequest` hook — useful for blocking disallowed tool calls; out of scope v1
- `postCompact` — `yadgar restore <cwd>`; payload format needs one live test

---

## Recommendation

**BUILD** — 5/5 needs covered including a blocking Stop hook. Highest hook parity of
all non-CC clients. Priority: HIGH.

---

## Sources

- https://developers.openai.com/codex/hooks (→ https://learn.chatgpt.com/docs/hooks)
- https://github.com/openai/codex — `codex-rs/app-server-protocol/schema/typescript/v2/HookEventName.ts`
- https://github.com/openai/codex — `codex-rs/app-server-protocol/src/hook_additional_context.rs`
- https://github.com/openai/codex — `codex-rs/app-server-protocol/src/hook_names.rs`
- Internal: `docs/plans/port-clients-survey-2026-07-18.md` §Task #57-A (superseded here)
