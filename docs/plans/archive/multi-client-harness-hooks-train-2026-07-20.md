# Multi-Client Harness Hooks — Train Plan (#58 + #61)

- status: **PARTIAL SHIP** — Car 0 (shared hook-emitter seam + HookCapability matrix + CLI) + Cursor Car B + folded-in #30 (subagent capture gauge) + #87 (capture-loop fix: Stop-hook sweep) + #85 (anchor-audit: de_anchor tool + scheduler + injected prompt) SHIPPED as `feat/multi-client-hooks` @ v5.158.0 (2026-07-20). OpenCode (#58) + Codex/Cline/Kiro/Windsurf/Amp (#61 client fan-out) DEFERRED — each client now warrants its own per-client plan/task. Train NOT archived: deferred cars remain outstanding.
- date: 2026-07-20
- base: origin/master @ **v5.157.0** (HEAD 8edc485e)
- tasks: **#58** (OpenCode hook spike — the pattern-prover) + **#61** (fan-out to
  Codex / Cursor / Cline / Windsurf / Kiro / Amp)
- train discipline: cars stack as commits on ONE branch; **ONE PR at the END**
  (user rule 2026-06-24)
- supersedes/reconciles: `docs/plans/port-opencode-2026-07-18.md` (#58 skeleton) and
  `docs/plans/port-clients-survey-2026-07-18.md` (#61 survey). Where those docs
  conflict with **ADR-0145** (the #59 primary-source verification), ADR-0145 wins.
- ADR anchors: **ADR-0143** (per-client hook/MCP API claims MUST be re-verified
  before building — the GATE), **ADR-0144** (the #215 descriptor framework this
  train plugs into), **ADR-0145** (#59 survey verification — corrects the survey
  matrix; task-mirror only Kiro-Specs + Cline-Kanban).

---

## 0. BLUF

Port yadgar's HOOK layer (the glue that makes memory *automatic*) from Claude Code
to the seven HIGH-tier clients. #215 already shipped the FRAMEWORK (MCP registration
+ rules files, descriptor-driven). Every `ClientDescriptor` already **carries a
`hooks_kind` field that is declared-but-unused** (`descriptor.py:105,126` — "the
hook layer is a later train"). This train is that layer: it makes `hooks_kind`
live by adding a hook-emitter dispatch that mirrors the existing
`mcp_register` / `rules_render` generators.

- **Size:** ~9 cars, one branch, one PR. Car 0 (shared abstraction) is the only
  large one; each client car is a thin adapter (one `_serialize_<kind>()` +
  fixtures + a verify gate).
- **Core-only, no backend bump — CONFIRMED CONDITIONALLY.** The daemon `/hooks/*`
  HTTP surface is already client-agnostic (`cwd` + `branch_hint` + payload; see §1).
  As long as every client reuses those endpoints unchanged, this is a `core` bump
  only. Backend bumps **iff** a client forces a *new* daemon endpoint — none is
  foreseen (§5, R6).
- **Verified vs needs-gate:** Codex / Cursor / Cline / Windsurf / Kiro / Amp hook
  surfaces were primary-source verified by #59 (ADR-0145, 2026-07-18). **OpenCode
  is the SOFTEST-verified client** — #59 left its hook event names + payload schema
  "TBC" (survey line 77). Because Car A (OpenCode) is the pattern-prover, **Car A
  opens with an OpenCode payload spike** before any emitter code. Per ADR-0145's
  own revisit_trigger ("fast-moving tools; re-verify before each port build"), the
  matrix is a **2026-07-18 snapshot** and **every car re-verifies its client's hook
  API as its first build step** — the gate is per-car, not retired by #59.
- **Only Stop needs a blocking hook.** The other four (SessionStart,
  UserPromptSubmit, PostToolUse, PreCompact) are inject / fire-and-forget and do not
  need the client hook to block. So blocking-coverage gaps degrade **Stop
  specifically**, not the whole harness (§1, §2).

### Not in scope / not contaminated by adjacent findings
- **Gemini is deliberately OUT** (advisory-only hooks, cannot block →
  inject-only). `hooks_kind=None` in the registry. MCP + rules already ship via
  #215; no hook car here. One-line note, not a silent drop.
- The `yadgar-hook-integration-layer` "**DEFERRED INDEFINITELY / INFEASIBLE**"
  verdict (ADR-0021 resolution) is **orthogonal to this train**. It is narrowly
  about a PreToolUse hook **rewriting the Agent-tool prompt via `updatedInput`**.
  None of our five hooks needs `updatedInput` — they use `additionalContext`
  (inject) or a POST (capture) or a blocking `decision` (Stop). Stated here so a
  reviewer does not conflate the two.

---

## 1. Reference model — the 5 Claude Code hooks + the daemon hook surface

Verified against the live core (`install_hooks_lib.py`, `hook_runner.py`,
`core/hooks/*`, `server/http.py`, `auth_middleware.py`).

### The 5 core hooks (the port target)

| Hook | CC event | What it does | Client-hook needs to… | Daemon endpoint |
|------|----------|--------------|------------------------|-----------------|
| **SessionStart** | `SessionStart` (+`compact` matcher) | inject `project_brief` catalog + checkpoint/task-list resume nudge; on `compact` → `restore()` | **inject** text pre-first-prompt (additionalContext / stdout) | `GET /hooks/session-context`, `GET /hooks/post-compact` |
| **UserPromptSubmit** | `UserPromptSubmit` | auto-recall on the prompt text, inject a recall block | **inject** text into the prompt | `GET /hooks/prompt-recall` |
| **PostToolUse** | `PostToolUse` | batch-capture state-mutating tool actions → action_log | fire-and-forget POST; **no return needed** | `POST /hooks/auto-capture` |
| **PreCompact** | `PreCompact` | drain the write queue + capture in-flight orchestration before compaction | fire-and-forget; **never blocks** | `POST /hooks/pre-compact` |
| **Stop** | `Stop` | every ~25 human msgs, **block** and inject the checkpoint protocol prompt (model self-reports checkpoint + task-list) | **BLOCK** (`{"decision":"block","reason":…}`) *or* deliver a transcript the daemon derives from | *local* script; no daemon call (state file only) |

**Blocking is required by exactly one hook.** SessionStart / UserPromptSubmit
inject via `additionalContext`; PostToolUse / PreCompact are fire-and-forget. Only
Stop's checkpoint relies on blocking the turn so the model self-reports
(`hook-integration-layer` doc: Stop "reliably fires because it BLOCKS"). This is
the single most important framing for the capability matrix: **a client that can't
block still gets 4/5 hooks.**

**Stop's two viable mechanisms** (per client):
1. **Blocking** — inject the checkpoint protocol prompt, model self-reports
   (CC's mechanism).
2. **Transcript-delivery** — a hook that hands the daemon a JSONL transcript path
   (Windsurf `post_cascade_response_with_transcript`, Amp `agent.end`) lets the
   daemon derive the checkpoint server-side without blocking. Requires a small
   daemon endpoint IF a client goes this route (flagged as the one possible
   backend touch — see R6).

### The shared daemon hook surface (already client-agnostic)

All under `server/http.py`, all behind bearer auth. **These are the seams the port
reuses unchanged** — they take `directory` (host cwd) + `branch_hint` + payload,
never the client identity:

| Route | Method | Purpose |
|-------|--------|---------|
| `/hooks/session-context` | GET | SessionStart inject (project_brief) |
| `/hooks/post-compact` | GET | compact rehydrate (restore) |
| `/hooks/prompt-recall` | GET | UserPromptSubmit auto-recall (bounded pool, 2s timeout, ADR-0077/0078) |
| `/hooks/auto-capture` | POST | PostToolUse batched action logging |
| `/hooks/pre-compact` | POST | PreCompact drain (forwards to backend admin op) |
| `/hooks/block-reflect` | GET | PostToolUse memory-block re-inject (v5.35.1) |

Plus `/hooks/subagent-*`, `/hooks/instructions-loaded`, `/hooks/file-changed`,
`/hooks/seed-*` — **out of scope** for this train (the 5 core hooks only; subagent
+ instructions + file-changed are Claude-Code-specific or lower-value and deferred).

**Host-side vs daemon-driven (task Q1b):** none of the 5 hooks can be
*daemon-triggered* — the daemon has no session-lifecycle visibility (it can't know
a session started, a prompt was submitted, or compaction is imminent). The client's
own host-side hook runtime MUST fire each one. The only *partial* daemon-derivation
is transcript-based Stop: a host hook hands the daemon a transcript path and the
daemon derives the checkpoint server-side (Windsurf/Amp; see R6). Everything else is
host-side-triggered, daemon-computed.

### Auth (the load-bearing detail)

- Every `/hooks/*` route requires `Authorization: Bearer <token>` when
  `YADGAR_REQUIRE_AUTH=True` (default), timing-safe compared
  (`auth_middleware.py`).
- **The token is NOT baked into the client config** (BUG B/C fix). CC hooks read
  `YADGAR_MCP_AUTH_TOKEN` from **ambient env** at runtime; the settings.json env
  block is empty by design (no secret-at-rest).
- **Port implication:** every ported hook script/plugin must read
  `YADGAR_MCP_AUTH_TOKEN` from the process env and send the Bearer header. Clients
  that spawn hooks in a clean env (no inherited shell env) need the token surfaced
  another way (per-client env stanza or the client's own secret mechanism) — this
  is a **per-car auth gate** (§5, R2). The #215 MCP registration already uses
  `BEARER_ENVREF` (`${YADGAR_MCP_AUTH_TOKEN}`) so the pattern exists.

### Host-side vs container/daemon split

- **Host-side (the client's own hook runtime):** the hook script/plugin itself;
  git detection (`gitness`, `default_branch`, current branch via
  `git branch --show-current`); transcript + worktree parsing for
  PreCompact in-flight capture; the Bearer HTTP call.
- **Daemon/container:** all `/hooks/*` handlers + backend compute (drain, restore,
  project_brief, recall). The daemon **cannot see the host `.git`** → the host hook
  detects the branch and passes `?branch=<branch>` (branch_hint). **Every ported
  client hook must do this branch detection host-side** and pass branch_hint — else
  writes land on the wrong branch (a known class of yadgar bug).
- **`yadgar drain` / `yadgar restore` CLI** (`cli/drain.py`, `cli/restore.py`) are
  thin forwarders to the backend; PreCompact/PostCompact can shell to them (as
  CC's `pre-compact-drain.sh` does) — the cleanest cross-client shared path
  (see §3).

---

## 2. Per-client capability matrix (7 HIGH-tier clients)

**Facts from ADR-0145 (primary-source verified 2026-07-18); mechanics (config
paths / hook format) from the survey. Where they conflict, ADR-0145 wins.**
`hooks_kind` / `task_mirror` values are the **live registry** (`registry.py`).

Legend: ✅ native event maps · ⚠️ maps but needs verify/adaptation · ❌ absent
(document, don't fake) · **Stop col**: `block` = can block · `transcript` =
delivers a transcript the daemon can derive from · `❌` = neither.

| Client | `hooks_kind` | SessionStart | UserPromptSubmit | PostToolUse | PreCompact | **Stop** | task_mirror | Verify status |
|--------|-------------|:---:|:---:|:---:|:---:|:---:|:---:|---|
| **opencode** | `opencode_plugin` | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ block? | `None` | **UNVERIFIED** — #59 left event names/payloads "TBC". Car A spike required. |
| **codex** | `codex_hooks_json` | ✅ | ✅ *(blocks)* | ✅ | ✅ | ⚠️ **no block** | `None` | VERIFIED (#59): 10 events, only 3 block (PreToolUse Bash-only, UserPromptSubmit, PermissionRequest). **Stop does NOT block** → checkpoint degrades. |
| **cursor** | `cursor_hooks` | ✅ `sessionStart` | ✅ `beforeSubmitPrompt` | ✅ `postToolUse` | ✅ `preCompact` | ✅ `stop` (8 blocking events) | `None` **(survey's "task list" = hallucination — Checkpoints only)** | VERIFIED (#59): 22 events, 8 blocking. Best parity after CC. |
| **cline** | `cline_hooks` | ⚠️ `TaskStart`≈ | ✅ `UserPromptSubmit` | ✅ `PostToolUse` | ❌ **no PreCompact** | ⚠️ `TaskCancel`/blocking-tbd | **`cline_kanban`** (real persistent store — survey MISSED it) | VERIFIED (#59): 6 hooks + Kanban store. Drain must ride PostToolUse/TaskCancel. |
| **windsurf** | `windsurf_hooks` | ❌ **no SessionStart** | ⚠️ `pre_user_prompt`≈ | ✅ `post_mcp_tool_use`≈ | ❌ **no PreCompact** | **transcript** `post_cascade_response_with_transcript` | `None` | VERIFIED (#59): 12 hooks. Session-inject must ride first prompt; Stop via transcript. |
| **kiro** | `kiro_hooks_json` | ✅ **HAS SessionStart** | ✅ `UserPromptSubmit` (blocks) | ✅ `PostToolUse` | ❌ **no PreCompact** | ⚠️ `Stop` (blocking-tbd) | **`kiro_specs`** (persistent Specs store) | VERIFIED (#59): **10 events — survey's "7, no SessionStart" is WRONG.** |
| **amp** | `amp_hooks` | ✅ `session.start` | ❌ **no UserPromptSubmit** | ⚠️ `tool.result`≈ | ❌ **no PreCompact** | **transcript** `agent.end` | `None` | VERIFIED (#59): 5 hooks. `synthesize` is **PRE-run replace** (survey mis-framed as post-run). No prompt-recall hook. |

### Reading the matrix (per advisor framing)

- **PreCompact is the most-commonly-absent hook** (Cline / Windsurf / Kiro / Amp).
  Its absence is NOT a failure — it degrades to *"no explicit pre-compaction
  drain; rely on continuous PostToolUse capture + the periodic Stop checkpoint."*
  Document this per client; don't fake a PreCompact.
- **Stop degradation is per-client and has two escape hatches** (block or
  transcript). Codex is the sharp case: Stop doesn't block AND no transcript hook →
  checkpoint can only fire opportunistically (e.g. on the next SessionStart or via
  a periodic capture), not the CC self-report loop. Document as a known gap, not a
  blocker.
- **Amp** has neither UserPromptSubmit nor PreCompact → it gets SessionStart +
  PostToolUse(≈) + Stop(transcript). Thinnest port; still valuable (inject +
  capture + checkpoint).
- **task-mirror targets = Kiro (Specs) + Cline (Kanban) ONLY.** Every other client
  gets hooks + MCP + rules, no task mirror. (Cursor's "task list" was a #59-flagged
  hallucination.)

---

## 3. Shared abstraction (Car 0) — the hook-emitter, mirroring #215

The #215 framework gives the exact pattern to copy. `hooks_kind` is the analog of
`mcp_entry_schema` / the rules bridge; the emitter is the analog of
`mcp_register.register_mcp()` / `rules_render.write_rules()`.

### What is common across all clients (factor into Car 0)

1. **The daemon endpoints + payloads are identical** across clients — the port
   never changes server code. The per-client difference is *only* (a) the config
   file format the hook is registered in, and (b) how the native hook payload maps
   to `{cwd, branch_hint, session_id, transcript_path}`.
2. **The hook LOGIC is identical** — detect branch host-side, Bearer-auth, call
   the endpoint, inject/POST. This should live **once**, not per client.

### Recommended factoring (the cleanest Car 0)

A per-client emitter should be a **thin adapter that normalizes the native hook
payload → invokes a shared entrypoint → hits the existing endpoint**. Concretely:

- **Introduce a `yadgar hook <event>` CLI** (mirrors how PreCompact already shells
  to `yadgar drain`). It reads `{cwd, branch_hint, session_id, transcript_path}`
  from stdin/args, does the Bearer HTTP call to the right `/hooks/*` endpoint, and
  writes the client-appropriate output to stdout. This is the **shared hook body**
  — written once, reused by every client. **CC switches to it too:** Car 0 extracts
  the shared auth/branch/endpoint logic out of `hook_runner.py` into `yadgar hook`,
  and `hook_runner.py` becomes a thin shim that calls it. This keeps **one** code
  path for the load-bearing logic (the whole justification for Car 0) — but it means
  Car 0 **modifies the live, load-bearing CC hook path**, not just adds alongside
  it. That is Car 0's real risk (regression the working CC hooks) → Car 0 tests must
  pin CC hook behavior (characterization) before the extract.
- **A `hooks_render` module** (sibling of `rules_render.py` /
  `mcp_register.py`) with:
  - `register_hooks(descriptor, url, token, scope, project_dir) -> dict` — the
    public entrypoint (mirror `register_mcp`).
  - one `_emit_<hooks_kind>()` per value
    (`_emit_opencode_plugin`, `_emit_codex_hooks_json`, `_emit_cursor_hooks`,
    `_emit_cline_hooks`, `_emit_windsurf_hooks`, `_emit_kiro_hooks_json`,
    `_emit_amp_hooks`) — each writes the client's native hook-registration artifact
    (a TS plugin for OpenCode, a `hooks.json`/TOML entry for Codex, `.mdc`/JSON for
    Cursor, an executable for Cline, etc.), and each artifact simply invokes
    `yadgar hook <event>`.
  - reuse Car 0's `merge_json` / `merge_toml` / `_atomic_write_text` primitives
    (already exist in `clients/merge.py`) for idempotent, format-preserving,
    crash-safe writes.
- **A per-client `HookCapability` declaration** on the descriptor (or a companion
  table) enumerating which of the 5 events the client supports + the Stop
  mechanism (`block` / `transcript` / `none`). The emitter emits only the
  supported subset — this is how "client genuinely can't support hook X" is
  encoded structurally instead of faked.

Net: **per-client cars stay thin** — one `_emit_<kind>()` + a capability row +
fixtures + a verify gate. All the HTTP/auth/branch logic is in the shared
`yadgar hook` CLI.

### Why not rewrite each client's hooks natively (TS/shell)?
Shell-out to `yadgar hook <event>` (the CC `pre-compact-drain.sh` pattern) ships
faster and keeps ONE code path for the load-bearing auth/branch/endpoint logic.
Native rewrites (e.g. a self-contained OpenCode TS plugin) are a **follow-up**, not
this train. The OpenCode plan's "Option A shell-out vs Option B native TS" — take
**Option A** for the whole train.

---

## 4. Car breakdown (ordered; stack on ONE branch `feat/multi-client-hooks`)

Each client car's **step 1 is ALWAYS re-verify the client's hook API** against
primary docs (ADR-0143 gate; ADR-0145 is a 2026-07-18 snapshot of fast-moving
tools). A car that **fails re-verify drops out of the train (deferred), it does
NOT block the PR** — the single PR ships with whatever passed.

| Car | Title | Scope | Files | Test approach | Gate / risk |
|-----|-------|-------|-------|---------------|-------------|
| **0** | Shared hook-emitter abstraction | `yadgar hook <event>` client-neutral CLI (generalize `hook_runner.py`); `hooks_render.register_hooks()` + `_emit_<kind>` dispatch skeleton; `HookCapability` per-descriptor; reuse `merge.py`. **No client wired yet.** | new `clients/hooks_render.py`, new `cli/hook.py` (or extend), `descriptor.py` (+capability), `registry.py` (+capability rows) | unit: capability table complete for 7 clients; `yadgar hook session-start-context` hits `/hooks/session-context` with Bearer + branch_hint (mock daemon); idempotent emit (2nd run no-op) | Foundation. Risk: over-abstracting before OpenCode proves the shape → keep Car 0 minimal, harden in Car A. |
| **A** | **OpenCode (#58 spike + port — the pattern-prover)** | **Step 1: payload spike** — minimal OC plugin logs SessionStart/UserPromptSubmit/PostToolUse/PreCompact/Stop payloads; run OC; record exact schemas (unblocks the rest). **Step 2:** `_emit_opencode_plugin` writes `~/.config/opencode/plugins/yadgar.ts` shelling to `yadgar hook`. | new `clients/_hooks_opencode.py` (or `_emit_` in hooks_render), plugin template asset | spike doc committed; unit: emitted plugin references all supported events + `yadgar hook`; smoke: run OC session, confirm inject + capture fire | **HARD GATE** — OpenCode is the softest-verified (#59 "TBC"). Prove the whole shape here before fanning out. Risk: OC plugin return-contract for inject/block unknown → resolve in spike. |
| **B** | Cursor | re-verify 22-event set; `_emit_cursor_hooks` writes `.cursor/hooks` + `.mdc` registration for sessionStart/beforeSubmitPrompt/postToolUse/preCompact/stop | `_emit_cursor_hooks`, fixtures | fixture-diff of emitted config; capability = 5/5 | Highest parity → lowest risk. Re-verify `.mdc` vs JSON hook format. |
| **C** | Codex | re-verify hooks.json / `[hooks]` TOML; emit SessionStart/UserPromptSubmit/PostToolUse/PreCompact; **Stop = no-block** → document checkpoint degradation | `_emit_codex_hooks_json`, TOML fixtures | fixture-diff; capability marks Stop `none` | Gate: confirm Stop still doesn't block on installed version. Risk: checkpoint loop unavailable → opportunistic capture only. |
| **D** | Cline | re-verify 6 hooks + Kanban; emit TaskStart≈SessionStart / UserPromptSubmit / PostToolUse; **no PreCompact** → drain rides TaskCancel/PostToolUse; **task-mirror car** (Kanban) | `_emit_cline_hooks`, `task_mirror=cline_kanban` wiring | fixture-diff; task-mirror smoke against Kanban store | Gate: Cline hooks are executables not JSON — confirm exec contract. PreCompact gap documented. |
| **E** | Kiro | re-verify 10 events (**confirm SessionStart present** — ADR-0145 vs survey); emit SessionStart/UserPromptSubmit/PostToolUse; **no PreCompact**; **task-mirror car** (Specs) | `_emit_kiro_hooks_json`, `task_mirror=kiro_specs` wiring | fixture-diff; Specs mirror smoke | Gate: verify SessionStart really fires (survey said absent, ADR-0145 says present — re-confirm empirically). |
| **F** | Windsurf | re-verify 12 hooks; **no SessionStart** → inject rides `pre_user_prompt` first-fire; PostToolUse via `post_mcp_tool_use`; **Stop via `post_cascade_response_with_transcript`** | `_emit_windsurf_hooks` | fixture-diff; transcript-path capture smoke | Gate: transcript Stop path may need a small daemon endpoint (R6). SessionStart absence documented. |
| **G** | Amp | re-verify 5 hooks; emit `session.start` + `tool.result`≈PostToolUse + `agent.end`(transcript)≈Stop; **no UserPromptSubmit, no PreCompact**; `synthesize` is PRE-run (don't use for post-capture) | `_emit_amp_hooks` | fixture-diff; thinnest capability row | Thinnest port. Gate: confirm `agent.end` transcript shape. |
| **H** | Integration + install wiring + nix note | wire `hooks_render` into the unified `install_client()` / `yadgar install --client X`; `--print` dry-run includes hooks; **nix note for #67** (home-manager per-client hook activation — declarative, gated on non-interactive install) | `clients/install.py`, `detect.py` if needed, `docs`/MIGRATION note | end-to-end: `yadgar install --client opencode --print` shows MCP + rules + hooks; idempotent | Final car. Nix provisioning itself is **#67 (separate nix-repo task)** — this car only leaves the note + ensures `--print` is declarative-friendly. |

**Ordering rationale:** Car 0 → Car A (prove the pattern on the softest-verified
client, hardening Car 0) → Cursor first among the fan-out (highest parity, lowest
risk, validates the emitter on a VERIFIED client) → then Codex/Cline/Kiro/Windsurf/
Amp in decreasing parity → integration last. Cars B–G are independent of each other
(all depend only on Car 0 + the pattern proven in Car A) and can be built/reviewed
in any order within the train.

---

## 5. Risks & gates

| # | Risk | Severity | Mitigation |
|---|------|----------|-----------|
| **R1** | Per-client hook APIs drift since #59 (2026-07-18) — matrix is a snapshot (ADR-0143 gate; ADR-0145 revisit_trigger) | **HIGH** | **Every client car step 1 = re-verify against primary docs.** Fail → car drops from train (deferred), PR still ships the rest. Don't trust the survey OR the ADR blindly. |
| **R2** | Daemon Bearer-token auth per client — hook may run in a clean env without `YADGAR_MCP_AUTH_TOKEN` | **HIGH** | Per-car auth gate: confirm the client surfaces env to hooks; if not, use the client's secret/env stanza (mirror #215 `BEARER_ENVREF`). Never bake the token into config (BUG B/C). |
| **R3** | Host-git / container split — daemon can't see `.git`; wrong branch_hint → writes land on wrong branch | **HIGH** | Every ported hook detects branch host-side (`git branch --show-current`) and passes `?branch=`. Covered once in the shared `yadgar hook` CLI (Car 0). |
| **R4** | OpenCode softest-verified (#59 "TBC") yet is the pattern-prover | **MED** | Car A opens with the payload spike; don't build the fan-out until the shape is proven. |
| **R5** | Stop degradation on no-block/no-transcript clients (Codex) — checkpoint self-report loop unavailable | **MED** | Document per client. Fallback: opportunistic checkpoint on SessionStart / periodic PostToolUse capture. Not a blocker (4/5 hooks still work). |
| **R6** | Transcript-based Stop (Windsurf/Amp) has **no existing daemon route** — CC's Stop is local-only and there is no `/hooks/stop`/checkpoint-from-transcript endpoint. Committing to transcript-Stop for these 2/7 clients = a NEW daemon endpoint = **backend bump**. This is likely-to-fire, not an edge case. | **MED** | **Explicit either/or (user picks — see §5 version):** (a) **core-only** — Windsurf/Amp degrade Stop to opportunistic-checkpoint (same fallback as Codex R5) → whole train stays core-only; or (b) **enhancement** — build a `POST /hooks/checkpoint-from-transcript` endpoint → Cars F/G (only) bump backend, richer checkpoints. Recommend (a) for this train, (b) as a follow-up. |
| **R7** | Clients that genuinely can't support a hook (Amp no-UserPromptSubmit, Windsurf/Kiro/Cline no-PreCompact, Windsurf no-SessionStart) | LOW | Encode as `HookCapability` (emit only supported subset). Document the gap; **never fake** a missing hook. |
| **R8** | TS/exec runtime at hook-fire (OpenCode plugin, Cline executable) | LOW | Shell-out to `yadgar hook` (Option A); runtimes ship with the clients. Native rewrite deferred. |

### Version / bump — the decision (task deliverable "confirm core-only")

**DECISION (2026-07-20, user): Path A. Train is core-only, no backend bump.**

Hooks live in `yadgar/core/install/` so the base is a **core bump**. The single
thing that was flagged as possibly forcing a backend bump is transcript-Stop for
Windsurf/Amp (R6).

- **Path A (CHOSEN): core-only.** Windsurf/Amp degrade Stop to
  opportunistic-checkpoint (like Codex). Every client reuses the existing
  `/hooks/*` surface unchanged → **core bump only, no backend bump.**
- **Path B (deferred follow-up, NOT this train): transcript-driven Stop for
  Windsurf/Amp.**

**Correction to the original draft (verified against `origin/master`, 2026-07-20):**
Path B was mis-scoped as a "new backend endpoint / backend bump." Both wrong:
1. **Not backend.** All `/hooks/*` routes live in **core** (`core/server/http.py`);
   a new hook route is a core change.
2. **Probably no new endpoint at all.** Transcript→checkpoint already ships:
   PreCompact's `pre-compact-drain.sh` → `yadgar drain --transcript-path` →
   existing `/hooks/pre-compact` route already parses a transcript and writes an
   **auto-checkpoint**. A Windsurf/Amp Stop hook can shell out to that same local
   CLI (as PreCompact does) — needing at most a small "stop-vs-compact" epoch flag,
   **no new backend code, no backend bump.**

So even the deferred Path B stays core-only. Recorded here so the follow-up isn't
re-scoped as a backend release. Why Stop is special at all: CC's Stop hook is a
**dumb pipe** — it writes nothing, it *blocks the stop and injects a prompt* so the
agent self-checkpoints in-session via MCP tools. Windsurf/Amp Stop can't
block-and-inject (post-response transcript only), which is the sole reason they
can't use the standard mechanism.

---

## 6. Open questions (resolve during the relevant car's verify step)
1. OpenCode plugin inject/block return contract (Car A spike).
2. Codex Stop confirmed non-blocking on installed version (Car C).
3. Kiro SessionStart confirmed present + fires (Car E — survey/ADR conflict).
4. ~~Windsurf/Amp transcript Stop: derive from existing surface or new endpoint? (R6).~~ **RESOLVED: Path A — no Stop-checkpoint for Windsurf/Amp this train; degrade to PostToolUse capture. Deferred Path B (if ever) reuses the existing `yadgar drain` path, core-only.**
5. Cline hook executable contract + Kanban mirror API (Car D).
6. Which clients spawn hooks with `YADGAR_MCP_AUTH_TOKEN` in env vs need a stanza (R2, per car).

---

## Sources
- ADR-0143, ADR-0144, ADR-0145 (yadgar wiki).
- `docs/plans/port-opencode-2026-07-18.md`, `docs/plans/port-clients-survey-2026-07-18.md`,
  `docs/plans/yadgar-hook-integration-layer-2026-07-01.md`.
- Reference impl (live, v5.157.0): `yadgar/core/install/install_hooks_lib.py`,
  `yadgar/core/install/hook_runner.py` path, `yadgar/core/hooks/*`
  (session-start-context.py, stop-memory-checkpoint.py, pre-compact-drain.sh),
  `yadgar/core/server/http.py` (`/hooks/*` routes: session-context 1025,
  post-compact 704, prompt-recall 1197, auto-capture 753, pre-compact 653,
  block-reflect 723), `yadgar/core/auth_middleware/auth_middleware.py`,
  `yadgar/core/cli/drain.py`, `yadgar/core/cli/restore.py`.
- #215 framework: `yadgar/core/install/clients/` — `descriptor.py`
  (`hooks_kind`:105/126, declared-but-unused), `registry.py` (9 descriptors +
  `hooks_kind`/`task_mirror` values), `mcp_register.py` (`register_mcp`),
  `rules_render.py` (`write_rules`/`section_replace`), `merge.py`.
