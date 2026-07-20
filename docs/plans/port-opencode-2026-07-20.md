# OpenCode hook port — plan

- status: PLANNED (deferred)
- verified: 2026-07-20 primary-source (supersedes ADR-0145 + port-opencode-2026-07-18.md)
- task: #56
- emitter: `yadgar/core/install/clients/hooks_render.py` — replace `_emit_stub` for OpenCode

---

## Verified contract

**Config file location:**
Plugin file at `~/.config/opencode/plugins/yadgar-hooks.ts` (global) or
`.opencode/plugins/yadgar-hooks.ts` (project). Config: `~/.config/opencode/opencode.json`.

**Real event names (unpkg @opencode-ai/plugin@1.18.4/dist/index.d.ts):**
`tool.execute.after` (PostToolUse), `experimental.session.compacting` (PreCompact),
`experimental.chat.system.transform` (SessionStart inject), `chat.message` (UserPromptSubmit),
`session.idle` (Stop observer), `session.stopping` (open issue #16626 — unshipped)

**Inject mechanism:**
Output-mutation callbacks (NOT stdout-JSON). Plugin exports typed callbacks; yadgar
adapter routes CLI stdout into `output.system[]` / `parts[]` arrays. Print-based
inject does NOT work — must use plugin return contract.

- `experimental.chat.system.transform` → returns mutated `system[]` array (Claude-safe)
- `chat.message` `parts[]` mutation for user-prompt inject → **UNRESOLVED**
  (headless `opencode run` test needed; open issue #34321)
- `tool.execute.after` → full context object; capture to yadgar action log
- `experimental.session.compacting` → `context[]` drain; return mutated context
- `session.idle` → non-blocking observer; stop checkpoint fires post-session
- `session.stopping` → not yet shipped (issue #16626); stop remains non-blocking

**Key upgrade from port-opencode-2026-07-18.md:** hook mechanism is plugin
output-mutation (TS/JS callbacks), NOT stdout-JSON. Shell-out from plugin is Option A
(thin TS shim calling existing Python via `execa`); native TS rewrite is Option B.

---

## Per-need mapping

| Need | Status | Mechanism |
|------|--------|-----------|
| Session-start inject | FUNCTIONAL | `experimental.chat.system.transform` → `system[]` mutation (Claude-safe) |
| User-prompt inject | NEEDS-LIVE-TEST | `chat.message` `parts[]` mutation; headless `opencode run` test required |
| Post-tool capture | FUNCTIONAL | `tool.execute.after` → full context object; adapter routes to yadgar |
| Pre-compact drain | FUNCTIONAL | `experimental.session.compacting` → `context[]` drain |
| Stop checkpoint | NONE | `session.stopping` unshipped (issue #16626); `session.idle` is non-blocking |

**Coverage: 3/5 functional, 1/5 needs live-test, 1/5 none (pending issue #16626).**

---

## Build scope

Wire in `hooks_render.py` replacing OpenCode `_emit_stub`:

1. `experimental.chat.system.transform` → call `yadgar hook sessionStart`; inject
   into `system[]` return (Option A: shell-out via `execa`)
2. `tool.execute.after` → call `yadgar hook postToolUse` with context payload
3. `experimental.session.compacting` → `yadgar drain <cwd>`; return drained `context[]`
4. `chat.message` → wire after headless test confirms `parts[]` mutation works

Stop: wire `session.idle` observer for best-effort async checkpoint; promote to
blocking if issue #16626 ships `session.stopping`.

Plugin scaffold writes TS file to `~/.config/opencode/plugins/yadgar-hooks.ts`.
`install_hooks --client opencode` gains `client` parameter (from plan #56 Car A).

Rules file: `~/.config/opencode/AGENTS.md` (global) via `sync_instructions --target opencode`.

---

## Open questions / live-tests

- **`chat.message` `parts[]` inject:** run headless `opencode run` test; verify
  mutation appears in same-turn context (resolves issue #34321 path)
- **`execa` shell-out latency:** benchmark Option A (shell-out) vs Option B (native TS);
  accept Option A for v1 if <100ms
- **`experimental.*` stability:** both compacting and system.transform are marked
  experimental in plugin@1.18.4; pin version; recheck on upgrade
- **`session.stopping` ship date:** watch sst/opencode#16626; upgrade Stop to
  blocking when merged

---

## Recommendation

**BUILD (+1 headless test)** — 3/5 functional; user-prompt inject needs one headless
test before claiming full inject coverage. Stop remains non-blocking until #16626
ships. Priority: HIGH.

---

## Sources

- https://unpkg.com/@opencode-ai/plugin@1.18.4/dist/index.d.ts
- https://opencode.ai/docs/plugins
- https://github.com/sst/opencode/issues/16626 (session.stopping — open)
- https://github.com/sst/opencode/issues/34321 (chat.message parts[] mutation)
- Internal: `docs/plans/port-opencode-2026-07-18.md` (superseded by this file)
- Internal: `docs/plans/port-clients-survey-2026-07-18.md` §8 (scope boundary with #56)
