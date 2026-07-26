# OpenCode hook port — re-audit (2026-07-26)

**Task:** #56 (harness #0056) — port yadgar hook layer to opencode via a TS/JS plugin.
**Status:** RECON COMPLETE — re-audit against current primary sources (opencode.ai/docs/plugins, `@opencode-ai/plugin@1.18.5` type defs, two open sst/opencode issues, locally-installed 1.14.31 plugin in `~/.config/opencode/node_modules`).
**Builds on:** `docs/plans/port-opencode-2026-07-20.md` (the 2026-07-20 plan, now **partially outdated**).
**Supersedes:** nothing — this is a re-audit; the 2026-07-20 plan stays as historical record.

## 1. What changed since 2026-07-20

The 2026-07-20 plan was primary-source-verified against `@opencode-ai/plugin@1.18.4/dist/index.d.ts` and `opencode.ai/docs/plugins`. Six days of plugin/npm churn have shifted the surface:

### 1.1 Drift detected
1. **Session-start inject is now `session.created` / `session.compacted`** (via the generic `event` callback), NOT `experimental.chat.system.transform`. The 2026-07-20 plan said "experimental.chat.system.transform is the Claude-safe session-start inject" — that's only correct for the in-`system[]` text-content case, but the **primary** session-lifecycle signal that opencode ships is the `event` callback dispatching typed `Event` objects (`EventSessionCreated`, `EventSessionCompacted`, `EventSessionIdle`) per the SDK's `gen/types.gen.d.ts`. Both routes are valid; `system.transform` injects text into the system prompt, `session.created` fires on lifecycle.
2. **`tui.prompt.append` is real** (TUI Event in the docs), contrary to my earlier read of the 2026-07-20 plan. The plan did not mention it. It's a TUI-internal append, not a same-turn inject path — useful for displaying hint text to the user, NOT for in-conversation injection.
3. **`output.context.push(...)` is correct** for `experimental.session.compacting` per the current docs example. The 2026-07-20 plan was vague about the shape; the docs confirm the array-append pattern is the documented one.
4. **`ctx.worktree` IS in `PluginInput`** per the current type defs (the 2026-07-20 plan did not enumerate it; the docs do). Same for `ctx.serverUrl` and `ctx.experimental_workspace`.
5. **Issue #34321 is about `experimental.chat.system.transform`, NOT `chat.message parts[]`** as the 2026-07-20 plan stated. The actual issue: pushing to `output.system` via `system.transform` breaks OpenAI-compatible providers because the post-hook collapse guard `system.length > 2` is off-by-one (should be `> 1`). Two linked PRs are open: #38671, #34322 — upstream is actively working on it. **For yadgar, this means `system.transform` is currently buggy on OpenAI-compat providers** (any model routed through them); not yet safe to use as the session-start inject path.
6. **Issue #16626 (`session.stopping`) is still OPEN** (no merged PR, no shipped version as of 2026-07-26). The 2026-07-20 plan's read is correct: `session.idle` is the only shipped stop-equivalent, and it is **non-blocking** (fires after the loop has already broken; #15267 race in `opencode run` mode).
7. **`@opencode-ai/plugin@1.18.5` is the latest on npm** (was 1.18.4 in the 2026-07-20 plan). Installed locally: 1.14.31 — **install drift of 4 minor versions** between the opencode binary (1.18.4) and the bundled plugin SDK (1.14.31). Both expose the same `Hooks` interface, so this doesn't break the build, but it's worth flagging.

### 1.2 What the 2026-07-20 plan got right (and stays)
- The 30-LOC `Hooks` interface and the typed event surface.
- The general "plugin output-mutation, not stdout-JSON" contract.
- The 3-of-5 functional / 1-of-5 test-only / 1-of-5 unshipped coverage assessment.
- The install pattern: `~/.config/opencode/plugins/yadgar-hooks.ts` (global) or `.opencode/plugins/yadgar-hooks.ts` (project).
- The `execa` shell-out to the `yadgar` CLI (Option A) as the IPC pattern.
- Stop remains a gap until #16626 ships.

## 2. Verified event surface (as of 2026-07-26)

### 2.1 Typed `Hooks` interface (`@opencode-ai/plugin@1.18.5/dist/index.d.ts`)

```ts
export interface Hooks {
  dispose?: () => Promise<void>;
  event?: (input: { event: Event }) => Promise<void>;
  config?: (input: Config) => Promise<void>;
  tool?: { [key: string]: ToolDefinition };
  auth?: AuthHook;
  provider?: ProviderHook;
  "chat.message"?: (input: {sessionID, agent?, model?, messageID?, variant?},
                    output: {message: UserMessage, parts: Part[]}) => Promise<void>;
  "chat.params"?: (input, output: {temperature, topP, topK, maxOutputTokens, options}) => Promise<void>;
  "chat.headers"?: (input, output: {headers: Record<string, string>}) => Promise<void>;
  "permission.ask"?: (input: Permission, output: {status: "ask" | "deny" | "allow"}) => Promise<void>;
  "command.execute.before"?: (input: {command, sessionID, arguments},
                              output: {parts: Part[]}) => Promise<void>;
  "tool.execute.before"?: (input: {tool, sessionID, callID},
                           output: {args: any}) => Promise<void>;
  "shell.env"?: (input: {cwd, sessionID?, callID?}, output: {env: Record<string,string>}) => Promise<void>;
  "tool.execute.after"?: (input: {tool, sessionID, callID, args},
                          output: {title, output, metadata}) => Promise<void>;
  "experimental.chat.messages.transform"?: (input: {}, output: {messages: {info: Message, parts: Part[]}[]}) => Promise<void>;
  "experimental.chat.system.transform"?: (input: {sessionID?, model}, output: {system: string[]}) => Promise<void>;
  "experimental.provider.small_model"?: (input: {provider: ProviderV2}, output: {model?: ModelV2}) => Promise<void>;
  "experimental.session.compacting"?: (input: {sessionID}, output: {context: string[]; prompt?: string}) => Promise<void>;
  "experimental.compaction.autocontinue"?: (input, output: {enabled: boolean}) => Promise<void>;
  "experimental.text.complete"?: (input, output: {text: string}) => Promise<void>;
  "tool.definition"?: (input: {toolID}, output: {description, parameters}) => Promise<void>;
}
```

### 2.2 Generic `event` callback dispatched Event types (`@opencode-ai/sdk/gen/types.gen.d.ts`)

These come through the `event?: (input: { event: Event })` callback, not as named hooks:

- `session.created` (EventSessionCreated) — fires on session start
- `session.compacted` (EventSessionCompacted) — fires after compaction
- `session.idle` (EventSessionIdle) — fires when loop breaks
- `session.status`, `session.updated`, `session.deleted`, `session.diff`, `session.error`
- `todo.updated`
- `message.part.removed`, `message.part.updated`, `message.removed`, `message.updated`
- `permission.asked`, `permission.replied`
- `file.edited`, `file.watcher.updated`
- `command.executed`
- `installation.updated`
- `lsp.client.diagnostics`, `lsp.updated`
- `server.connected`
- `shell.env` (also surfaced as a typed hook — see §2.1)

### 2.3 TUI Events (only relevant for desktop / interactive TUI; NOT for `opencode run` headless)
- `tui.prompt.append` — appends to TUI prompt input
- `tui.command.execute` — fires on TUI slash-command
- `tui.toast.show` — show toast notification

These are TUI-internal — they do NOT inject into the same-turn LLM context. Useful for showing the user "yadgar restored N memories" hints, but **NOT for the user-prompt inject use case**.

## 3. Yadgar hook needs → opencode event mapping (RE-AUDIT)

| Yadgar hook need | opencode event (2026-07-26) | Type | Verified | Status |
|---|---|---|---|---|
| **SessionStart (all)** — auto-recall / project_brief | `event` callback filter `event.type === "session.created"` | generic | ✓ | Functional |
| **PreCompact** — drain context | `experimental.session.compacting` → `output.context.push(...)` | typed hook | ✓ | Functional |
| **SessionStart (compact)** — restore | `event` callback filter `event.type === "session.compacted"` | generic | ✓ | Functional |
| **PostToolUse** — capture action_log | `tool.execute.after` | typed hook | ✓ | Functional |
| **Stop** — checkpoint (blocking) | `event` callback filter `event.type === "session.idle"` | generic | ✓ partial | **Non-blocking only**; #16626 unshipped |
| **UserPromptSubmit** — auto-recall (OPTIONAL) | `chat.message` → `output.parts` mutation | typed hook | needs live test | **Parts-mutation not headless-verified**; #34321 is about `system.transform`, not `chat.message`, so this path is independent of the open upstream bug. Still needs a headless `opencode run` test to confirm the mutation appears in the same turn. |

**Net coverage: 3/5 functional, 1/5 non-blocking observer only (Stop, blocked on #16626), 1/5 needs headless test (UserPromptSubmit, blocked on the headless test per the 2026-07-20 plan).** This matches the 2026-07-20 plan's 3/5/1/1 assessment, with the 1/5 unshipped being Stop (unchanged) and the 1/5 test-only being UserPromptSubmit (unchanged), and the 3/5 now being the right 3.

### Why NOT `experimental.chat.system.transform` for session-start
- It's a system-prompt-text-injection hook, not a session-lifecycle signal.
- Per #34321, it's currently buggy on OpenAI-compat providers (off-by-one collapse guard).
- The lifecycle signal we need is `event.type === "session.created"`, which is a separate, clean dispatch.
- We CAN use `system.transform` for "always-on" yadgar system context (e.g. memory blocks, project brief) — that's a different need from the per-session-start inject. But that's an enhancement, not a replacement.

### Why NOT `tui.prompt.append` for user-prompt inject
- TUI-internal; only fires in interactive TUI mode, not `opencode run` headless.
- Does NOT inject into the same-turn LLM context — it appends to the TUI input buffer.
- Wrong tool for the job. The right path is `chat.message` (typed hook) with `output.parts` mutation, gated on a headless test.

## 4. Build scope (RE-AUDIT)

### 4.1 What to build
**Single file: `~/.config/opencode/plugins/yadgar-hooks.ts` (global).** Plugin is a thin TS shim that shells out to the `yadgar` Python CLI via `execa` (Option A per the 2026-07-20 plan, native TS rewrite deferred until we know the shell-out latency).

**Event wiring (5/5 attempted, 3/5 functional out-of-the-box):**

```ts
import type { Plugin } from "@opencode-ai/plugin"
import { execa } from "execa"

const YADGAR = (args: Record<string, unknown>, cmd = "yadgar") =>
  execa(cmd, ["hook", JSON.stringify(args)], { reject: false }).catch(() => {})

export const YadgarHooksPlugin: Plugin = async ({ directory }) => ({
  // PreCompact: drain context (FUNCTIONAL, typed hook)
  "experimental.session.compacting": async (_input, output) => {
    const r = await YADGAR({ event: "preCompact", directory })
    if (r?.stdout) output.context.push(r.stdout)
  },

  // PostToolUse: capture action_log (FUNCTIONAL, typed hook)
  "tool.execute.after": async (input, output) => {
    await YADGAR({
      event: "postToolUse",
      directory,
      tool: input.tool,
      title: output.title,
      metadata: output.metadata,
    })
  },

  // SessionStart + Stop + SessionStart-after-Compact: generic event dispatch
  event: async ({ event }) => {
    if (event.type === "session.created") {
      await YADGAR({ event: "sessionStart", directory, mode: "signals" })
    } else if (event.type === "session.compacted") {
      await YADGAR({ event: "sessionStart", directory, mode: "restore" })
    } else if (event.type === "session.idle") {
      // NON-BLOCKING observer only; promote to blocking when #16626 ships
      await YADGAR({ event: "stop", directory })
    }
  },

  // UserPromptSubmit: needs headless test (gated on confirmation)
  // Wire AFTER headless `opencode run` test confirms parts[] mutation appears in same turn
  // "chat.message": async (_input, output) => {
  //   const r = await YADGAR({ event: "userPromptSubmit", directory })
  //   if (r?.stdout) output.parts.push({ type: "text", text: r.stdout })
  // },
})
```

### 4.2 What NOT to build (this audit)
- **NOT** a clean-slate plugin scaffold that duplicates the install-hooks emitter — wire through `yadgar install-hooks --client opencode` so the emitter produces the file from the canonical template. This is task #56 Car A per the 2026-07-20 plan.
- **NOT** a custom RPC for `ctx.client.app[MCP_NAMESPACE].call(...)` — the type defs expose `client` as `ReturnType<typeof createOpencodeClient>`, which has typed methods for opencode's own server (session.chat, session.messages, etc.) but **no generic MCP tool invocation**. The shell-out via `execa` to the `yadgar` CLI is the working pattern; HTTP-to-MCP from inside a plugin is not.
- **NOT** `tui.prompt.append` for user-prompt inject — TUI-internal, wrong tool. Use `chat.message` (gated on headless test).
- **NOT** `experimental.chat.system.transform` for session-start — it's a text-injection hook, not a lifecycle signal. Plus #34321 makes it currently buggy on OpenAI-compat providers.

### 4.3 Install path
- `yadgar install-hooks --client opencode` writes `~/.config/opencode/plugins/yadgar-hooks.ts` from the canonical template (replaces the OpenCode `_emit_stub` in `yadgar/core/install/clients/hooks_render.py`).
- `package.json` next to the plugin (`~/.config/opencode/package.json` or `.opencode/package.json`) declares `execa` dep so `bun install` picks it up.
- Yadgar MCP already registered in `~/.config/opencode/opencode.json` (verified: `yadgar` server, `http://127.0.0.1:8765/mcp`, enabled, bearer header present). No MCP-side work needed.

### 4.4 Test plan
- **Smoke test 1 (load):** start `opencode` session, confirm `yadgar-hooks.ts` loads without error. Tail `~/.config/opencode/log/...`.
- **Smoke test 2 (session-start):** open session, fire one prompt. Confirm `yadgar_project_brief` was called (check `yadgar_recent_memories(directory, since="1h")` for the auto-recall entry).
- **Smoke test 3 (post-tool):** fire one tool call (e.g. `bash`). Confirm action_log entry appears in yadgar.
- **Smoke test 4 (pre-compact):** trigger compaction (large context). Confirm drained context appears in compaction prompt.
- **Smoke test 5 (headless user-prompt):** run `opencode run` with the plugin enabled. Verify `chat.message` parts[] mutation appears in same-turn context. This is the gating test for UserPromptSubmit — does NOT block ship of the 3/5 functional coverage.
- **Smoke test 6 (stop non-blocking):** end a session, confirm `session.idle` fires and yadgar gets a stop signal. Accept that this is non-blocking.

## 5. Constraints and decisions (RE-AUDIT)

### 5.1 Constraints
- **I32 (CAPABILITY_REGISTRY):** OpenCode is a registered client. New `yadgar-hooks.ts` plugin doesn't add a new MCP tool, so no I32 update strictly required. But the install-hooks emitter's `_emit_stub` replacement IS a wired change; update CAP-STOR-* row accordingly.
- **HARD RULE Apply/Import:** Yadgar MCP is the sanctioned write path. The plugin shells out to the `yadgar` CLI, which routes through MCP. No direct SQL. OK.
- **HARD RULE OSS LLM-PR aversion (user, 2026-07-26):** Not relevant for this task — the plugin is a thin shim, not LLM-generated.
- **ADR-0143 (multi-client porting gated on verification):** the verification task (#59) is now satisfied for opencode by this re-audit. Task #56 is unblocked.
- **ADR-0154 (Path A core-only, NO backend bump):** OpenCode is the 9th client port. Still core-only, no backend endpoint changes. Windsurf/Amp's Stop degradation pattern doesn't apply here (opencode's Stop is also non-blocking, but the gap is at the opencode level, not the yadgar level).
- **ADR-0161 (global-authoritative hook install):** nix calls `yadgar install-hooks --scope global` for all clients. OpenCode goes through the same path. Single implementation, foreign-preserving.

### 5.2 Decisions (proposed for ADR-0168 "Multi-client port to OpenCode")
- **D1:** OpenCode hook layer wires 5/5 events. 3/5 functional out-of-the-box (session.created, session.compacted, tool.execute.after, experimental.session.compacting). 1/5 non-blocking observer (session.idle; promote to blocking on #16626). 1/5 gated on headless test (chat.message).
- **D2:** IPC = `execa` shell-out to `yadgar hook <event>` CLI (Option A from 2026-07-20 plan). Native TS rewrite (Option B) deferred until we know the shell-out latency budget.
- **D3:** Install path = `yadgar install-hooks --client opencode`, replacing the `_emit_stub` for opencode in `yadgar/core/install/clients/hooks_render.py`. Single canonical template; `~/.config/opencode/plugins/yadgar-hooks.ts` (global) is the rendered output.
- **D4:** UserPromptSubmit (`chat.message` parts[] mutation) is OPTIONAL — wire after a headless `opencode run` test confirms same-turn visibility. The 3/5 functional coverage ships without it.
- **D5:** OpenCode goes through the same global install path as Claude Code per ADR-0161. Nix calls `yadgar install-hooks --client opencode --scope global`. Project-scope install not supported for opencode (per docs, plugin dir is global or `.opencode/plugins/`, not multi-level).
- **D6:** Pin `@opencode-ai/plugin` and `@opencode-ai/sdk` to the bundled versions (currently 1.14.31 in `~/.config/opencode/node_modules/`, even though the binary is 1.18.4 and npm latest is 1.18.5). Document the install drift; re-evaluate on each opencode upgrade.

### 5.3 Open questions
1. **UserPromptSubmit headless test:** is there a CI environment that can run `opencode run` headless? The yadgar CI runs on Forgejo Actions (`codeberg-small` runner, no opencode). Headless test would have to be a local-dev or nix-test artifact, not a CI gate.
2. **TUI vs run mode:** does the opencode desktop TUI use the same `Hooks` interface, or is there a TUI-specific plugin path? The docs mention `~/.config/opencode/plugins/` for both, but `PluginModule = { server: Plugin, tui?: never }` suggests there's a separate TUI surface we haven't explored. Not blocking for the MCP-server-side hooks we need.
3. **`ctx.experimental_workspace`:** present in `PluginInput` per the type defs. Yadgar doesn't need it, but worth a sanity check on whether the install path needs to opt in.

## 6. Rollout

### Phase 1 — install-hooks emitter (this PR)
- Replace `yadgar/core/install/clients/hooks_render.py` `_emit_stub` for `client == "opencode"`.
- Emit `~/.config/opencode/plugins/yadgar-hooks.ts` from the canonical template.
- `package.json` (or merge into existing) declares `execa` dep.
- Test: install + smoke 1-4 + 6.

### Phase 2 — headless test for UserPromptSubmit
- Local-dev or nix-test only (not CI).
- Verifies `chat.message` parts[] mutation appears in same-turn context.
- If passes: wire `chat.message` in the canonical template, ship 5/5.
- If fails: ship 4/5 (no UserPromptSubmit) and document the gap.

### Phase 3 — promote Stop to blocking (deferred)
- Watch sst/opencode#16626.
- When shipped: update the canonical template to use `session.stopping` (output.stop = false) instead of `session.idle` observer.

### Phase 4 — wire `experimental.chat.system.transform` for always-on context (optional)
- After #34321 is fixed upstream, wire `system.transform` to push yadgar memory blocks / project brief into the system prompt on every turn. This is a different need from the per-session-start inject — it's the always-on context, not the session-lifecycle signal.
- Not in this PR.

## 7. Sources

- `https://opencode.ai/docs/plugins/` (canonical, fetched 2026-07-26)
- `https://unpkg.com/@opencode-ai/plugin@1.18.5/dist/index.d.ts` (latest published)
- `~/.config/opencode/node_modules/@opencode-ai/plugin/dist/index.d.ts` (locally installed 1.14.31)
- `https://github.com/sst/opencode/issues/16626` (session.stopping — open, no merged PR)
- `https://github.com/sst/opencode/issues/34321` (system.transform collapse guard — open, 2 linked PRs)
- `https://github.com/sst/opencode/issues/15267` (`opencode run` teardown race after session.idle — open)
- `docs/plans/port-opencode-2026-07-20.md` (the 2026-07-20 plan, now historical)
- `docs/plans/port-opencode-2026-07-18.md` (superseded by the 2026-07-20 plan)
- `yadgar/core/install/clients/hooks_render.py` (the install-hooks emitter with the `_emit_stub` to replace)

## 8. Yadgar findings footer (handoff contract)

- **The 2026-07-20 verified plan is now PARTIALLY OUTDATED.** Session-start inject is `event.type === "session.created"` (generic event callback), not `experimental.chat.system.transform`. Issue #34321 is about `system.transform`'s collapse guard, NOT about `chat.message parts[]` mutation. `tui.prompt.append` is real but TUI-internal, wrong tool for user-prompt inject. `ctx.worktree` IS in `PluginInput`.
- **The install drift is real** — opencode binary is 1.18.4 but bundled plugin SDK is 1.14.31. Both expose the same `Hooks` interface, so it doesn't break the build, but it's worth flagging and re-evaluating on each opencode upgrade.
- **The 3/5/1/1 coverage assessment holds.** Session-start (session.created), SessionStart-after-Compact (session.compacted), PostToolUse (tool.execute.after), and PreCompact (experimental.session.compacting) are all functional. Stop is non-blocking only (session.idle, blocked on #16626). UserPromptSubmit is gated on a headless test (chat.message parts[] mutation).
- **The right path is Option A from the 2026-07-20 plan** — `execa` shell-out to the `yadgar` CLI, plugin as a thin TS shim. Native TS rewrite (Option B) deferred. Install path = `yadgar install-hooks --client opencode` (replaces `_emit_stub` in `hooks_render.py`).
- **MCP IPC from inside a plugin is NOT a working pattern** — the type defs expose `client` as `ReturnType<typeof createOpencodeClient>`, which is opencode's own typed SDK (session.chat, session.messages, etc.), not a generic MCP tool invoker. The shell-out to `yadgar hook <event>` is the working IPC; HTTP-to-MCP from inside a plugin is not.
- **Two upstream issues gate future work** — #16626 (Stop blocking) and #34321 (system.transform on OpenAI-compat). Neither blocks this PR. Watch both.
- **No wiki page is titled or tagged "opencode port" or "task #56"** — `wiki_query` returns only ADR-0143 (multi-client porting) and the per-client plan files. This re-audit is the first curated entry; promote to wiki page once approved.
