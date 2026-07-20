# Cline hook port — plan

- status: PLANNED (deferred)
- verified: 2026-07-20 primary-source (supersedes ADR-0145 + port-clients-survey-2026-07-18.md §Task #57-C)
- task: #57-C
- emitter: `yadgar/core/install/clients/hooks_render.py` — replace `_emit_stub` for Cline

---

## Verified contract

**Config file location:**
Hooks registered in Cline settings (VSCode extension JSON) or `.clinerules/` for
rules. Hook scripts are executables pointed to by path. macOS/Linux only.

**Real event names (8 hooks, verified at docs.cline.bot/features/hooks; deepwiki commit 8a6441):**
`TaskStart`, `TaskResume`, `TaskCancel`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `ApiRequest`, `ApiResponse`

**Inject mechanism:**
`TaskStart` → `contextModification` return field → content inserted as
`<hook_context>` block in `userMessageContent` (the user-visible message prefix).
`UserPromptSubmit` → same `contextModification` mechanism; inject timing (next-turn
vs same-turn) is the live-test open question.

- `TaskStart` ≈ SessionStart: fires on new task; inject via `contextModification`
- `UserPromptSubmit` → inject but same-turn vs next-turn needs live verification
- `PostToolUse` → full payload in stdin; capture without gating
- `TaskCancel` → non-blocking POST; usable as drain trigger (return `cancel: false`)
- No `PreCompact` event (confirmed absent in Cline 3.36 hooks)
- `TaskComplete` (Stop equivalent) → non-blocking POST; cannot block continuation

**PreCompact drain note:** No blocking PreCompact. Strategy: drain on `TaskCancel`
with `return {cancel: false}` to let task complete while drain runs; or defer to
periodic PostToolUse drain cadence.

**Kanban note:** Cline Kanban is a standalone app (research preview), NOT a hook
extension point — do not attempt to wire yadgar through Kanban.

---

## Per-need mapping

| Need | Status | Mechanism |
|------|--------|-----------|
| Session-start inject | FUNCTIONAL | `TaskStart` → `contextModification` → `<hook_context>` in userMessageContent |
| User-prompt inject | FUNCTIONAL (+1 live-test) | `UserPromptSubmit` → `contextModification`; same-turn timing to be verified |
| Post-tool capture | FUNCTIONAL | `PostToolUse` → full payload stdin; `yadgar hook postToolUse` |
| Pre-compact drain | NONE | No PreCompact event; partial via `TaskCancel` drain workaround |
| Stop checkpoint | NONE (non-blocking) | `TaskComplete` fires but cannot block; checkpoint fires async post-session |

**Coverage: 3/5 functional, 1/5 partial, 1/5 none.**

---

## Build scope

Wire in `hooks_render.py` replacing Cline `_emit_stub`:

1. `TaskStart` → `yadgar hook sessionStart`; return `contextModification` JSON
2. `UserPromptSubmit` → `yadgar hook userPromptSubmit`; return `contextModification`
3. `PostToolUse` → `yadgar hook postToolUse` (stdin = payload); return void
4. `TaskCancel` → `yadgar drain <cwd>`; return `{cancel: false}` (pass-through)

Stop hook writes asynchronously via `TaskComplete`; checkpoint may arrive after
session ends — acceptable for non-blocking path.

Rules file: `.clinerules/yadgar.md` carrying the yadgar contract (AGENTS.md-compatible).
AGENTS.md global path for Cline: `~/.agents/AGENTS.md` (per survey §6 Q6).

---

## Open questions / live-tests

- **UserPromptSubmit timing:** confirm inject appears in the same turn (not deferred
  to next turn) — run one live session in Cline 3.36+
- **PreCompact workaround:** evaluate whether `TaskCancel` drain strategy is
  sufficient or whether a periodic PostToolUse fallback is needed
- **`ApiRequest` / `ApiResponse` hooks:** not mapped above; potential for token-count
  capture; out of scope v1

---

## Recommendation

**BUILD (+1 live-test)** — 3/5 core needs functional; missing PreCompact drain and
blocking Stop are acceptable gaps. UserPromptSubmit timing needs one live-test before
declaring inject reliable. Priority: HIGH.

---

## Sources

- https://docs.cline.bot/features/hooks
- https://deepwiki.com/cline/cline/7.3-hooks-system (commit 8a6441)
- https://cline.ghost.io/cline-v3-36-hooks
- Internal: `docs/plans/port-clients-survey-2026-07-18.md` §Task #57-C (superseded here)
