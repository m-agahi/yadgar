# Yadgar Hook Integration Layer — Feasibility + Design

- date: 2026-07-01
- status: design (investigate-only; nothing implemented)
- branch: master (docs-only)
- scope: expand Claude Code hooks into a smooth integration layer so the MAIN
  thread AND every SUBAGENT naturally leverage all of Yadgar's features to
  improve output. Task #82 (PreToolUse Agent-hook per ADR-0021) is the seed; the
  ask is the general layer.
- seed ADRs: ADR-0021 (pull fails, push works — needs a PreToolUse Agent hook,
  feasibility unconfirmed until now), ADR-0022 (#81 hook-recall freeze — bounded
  2-worker pool; the design constraint: any hook that calls yadgar must be
  cheap/bounded/async or served from a precomputed cache).

---

## 0. Executive summary (the three load-bearing findings)

1. **FEASIBILITY PLAUSIBLE, SPIKE-GATED — push is *likely* buildable.** Official
   docs confirm a `PreToolUse` hook can rewrite tool input *generally* via the
   `updatedInput` field in `hookSpecificOutput` (paired with
   `permissionDecision: "allow"`). The Agent-*prompt*-specific rewrite — that
   `updatedInput.prompt` on a `Agent`/`Task` dispatch reaches the subagent — is
   the INFERRED part (a community gist was the primary citation), and is what
   ADR-0021 called "feasibility unconfirmed." It is the strongest lever if it
   holds: auto-inject, not just block. **Do not build on (a) as settled — OQ-1
   must spike it first** (§6).

2. **THE FREEZE COLLISION IS ALREADY AVOIDED for the seed feature.** #82's
   headline is "inject the *best-matching* pattern into each dispatch." Matching a
   pattern is a lookup. If that lookup were a semantic `recall()`, it would be
   exactly the ~1.5s uncancellable hot-path recall that froze the core (ADR-0022).
   But `agent_dispatch_prelude` → `_read_agent_prompt()` resolves patterns by an
   **exact-key wiki slug read** (`agent-prompt-<pattern>`), NOT `recall()`
   (`dispatch_helper.py:139`). So *deterministic* pattern injection is freeze-safe
   today. The freeze risk only reappears if the hook tries to *choose* the best
   pattern by semantic search on the hot path — which the design forbids (§4
   constraint, §5 recommendation).

3. **THE SUBAGENT IS THE BIGGEST GAP AND IS ALREADY PARTLY WIRED.** SubagentStart
   already injects agent-prompt + a recall hint (`yadgar-subagent-start.py`), but
   there is **no PreToolUse hook on the Agent tool** — the only PreToolUse binding
   today is `db-lockdown-check` on Bash. The push lever for #82 does not exist
   yet; that is the first thing to build.

---

## 1. Feasibility matrix — hook × capability (each CITED)

Sources (all fetched 2026-07-01):
- CC-DOCS: https://code.claude.com/docs/en/hooks
- SDK-DOCS: https://platform.claude.com/docs/en/agent-sdk/hooks
- MEM-ANCHOR: verified in-memory schema anchor (id 491682, 2026-05-19, via
  claude-code-guide) — matcher semantics + which events exist.
- ISSUE-23885: https://github.com/anthropics/claude-code/issues/23885 (SubagentStart
  `updatedPrompt` feature request, open).

Legend for MODIFY/INJECT: **rewrite** = can change tool input/prompt; **context** =
can add text only; **observe** = read-only.

**Provenance discipline (per "cite, do not assume").** Only two ground truths in
this doc are verified: the in-memory schema **anchor** (id 491682, 2026-05-19) and
the **live hook scripts** Explore actually read. Everything else came from the
claude-code-guide agent, which shows confabulation tells and must be spike-checked:
it cited `permissionDecision:"defer"` (standard values are allow/deny/ask — "defer"
appears invented) and a `ConfigChange` hook (NOT in the verified anchor, which
listed InstructionsLoaded/PostCompact/TeammateIdle/SubagentStart/SubagentStop). Treat
guide-only claims — including the load-bearing (a) — as PLAUSIBLE-NOT-VERIFIED.
Provenance per claim: **[A]** anchor-verified · **[L]** live-code-verified (Explore
read it) · **[G]** guide-only → needs spike. `permissionDecision:"defer"` and
`ConfigChange` are **excluded** from the design as suspected confabulation.

| Hook | READ | MODIFY/INJECT | BLOCK | Exit/JSON | Fires for subagent? |
|---|---|---|---|---|---|
| **SessionStart** | session_id, cwd, source(startup/resume/clear/compact) [SDK] | **context** via `hookSpecificOutput.additionalContext` — lands pre-first-prompt [SDK] | no | exit0→parse JSON | main only |
| **UserPromptSubmit** | prompt text, session_id, cwd [SDK] | **context** via `additionalContext` (injected into prompt) [SDK] | **yes** (exit 2) [CC] | exit0→JSON, exit2→block | main only |
| **PreToolUse** | `tool_name`, `tool_input` (full arg dict), tool_use_id [SDK] | **rewrite** via `hookSpecificOutput.updatedInput` (+ `permissionDecision:"allow"`); also `additionalContext` to main thread [SDK, gist] | **yes** — `permissionDecision:"deny"` or exit 2 [CC] | JSON `hookSpecificOutput{hookEventName, permissionDecision, updatedInput?, permissionDecisionReason?}` | **fires when MAIN dispatches Agent/Task** |
| **PostToolUse** | tool_name, tool_input, tool_output [SDK] | **context** via `additionalContext`; `updatedToolOutput` for result | no (tool ran) | exit2 ignored | fires on Agent tool completion in main |
| **SubagentStart** | `agent_id`, `agent_type`, permission_mode — **NOT the dispatched prompt** [CC] | **context** via `additionalContext` → lands **inside the subagent's** context, pre-first-prompt. **Cannot rewrite the prompt** (`updatedPrompt` unimplemented, ISSUE-23885) | no | exit0→JSON | **runs in subagent scope** |
| **SubagentStop** | agent_id, agent_type, `agent_transcript_path` [CC] | **context** via `additionalContext` → lands in **MAIN thread** at end of turn | no | exit0→JSON | subagent lifecycle |
| **Stop** | session_id, cwd [SDK] | context | **yes** — `{"decision":"block","reason":...}` / exit 2 keeps turn going | JSON | main |
| **PreCompact** | session_id, cwd [SDK] | context (preserve before compaction) | yes (exit 2) [CC] | JSON | main |
| **Notification** | message, notification_type [SDK] | observe (can run async side-effects) | no | ignored; supports `async:true` | — |
| **InstructionsLoaded** | session_id, cwd, load_reason [MEM-ANCHOR] | context via `additionalContext` | yes (exit 2) | JSON | main |
| **PostCompact** | session_id, cwd [MEM-ANCHOR] | context (post-compaction) | no (done) | exit2 ignored | main |
| **SessionEnd** | session_id, cwd [SDK] | output ignored (side-effects only) | no | ignored | main |

### The load-bearing question — resolved across all three rungs

- **(a) PreToolUse rewrite of the Agent prompt → PLAUSIBLE [G, spike OQ-1].**
  `updatedInput` rewriting tool input generally is doc-backed [G]; that
  `updatedInput.prompt` on an `Agent`/`Task` dispatch reaches the subagent is
  gist-sourced and inferred. Intended shape: match `tool_name == "Agent"` (or
  `"Task"`), read `tool_input.prompt`, return `hookSpecificOutput.updatedInput`
  with the augmented `prompt` + `permissionDecision:"allow"`. If it holds, this
  is a **push** lever — auto-inject, no instance cooperation. Verify before build.
- **(b) SubagentStart injects into the subagent, not the prompt → PARTIAL
  [L wired, G placement].** `additionalContext` reportedly lands inside the
  spawned subagent's context (advisory); the dispatch prompt cannot be rewritten
  here (`updatedPrompt` is an open feature request, ISSUE-23885 [G]). Placement
  (subagent vs main) is guide-only → OQ-3. The hook itself is **already wired**
  and verified live (`yadgar-subagent-start.py` [L]).
- **(c) Deny is the fallback → friction only [G].** PreToolUse
  `permissionDecision:"deny"` + reason blocks the dispatch; the main thread must
  re-issue a corrected prompt. No auto-correction. Use only as a validation
  backstop, not the primary lever.

**Verdict:** (a) is the primary push lever for #82 and (b) is the complementary
per-subagent context vector. They stack: PreToolUse rewrites the prompt for the
main thread's dispatch; SubagentStart guarantees the subagent sees the contract
even for prompts the main thread constructs some other way.

---

## 2. Existing precedent — what's already wired (grounded, file-cited)

Live wiring is **per-event `yadgar-*.py` scripts** registered in the nix-generated
`settings.json`. The `hook_runner.py` dispatcher in `.claude/hooks/` (dated today)
is an **alternative implementation that is NOT registered** — do not assume it's
live. All context-returning hooks talk to the daemon over HTTP (`/hooks/*`) and
print `{"text": "..."}` to stdout; the daemon side owns the recall/throttle.

| Event | Script | Injection | yadgar call | Latency budget | Freeze-relevant? |
|---|---|---|---|---|---|
| SessionStart | `yadgar-session-start-context.py` | stdout `{"text":...}` | HTTP GET `/hooks/session-context` | 2s, fire-and-forget | daemon-side recall |
| UserPromptSubmit | `yadgar-prompt-recall.py` | stdout `{"text":...}` | HTTP GET `/hooks/prompt-recall` → `retriever.recall()` | **0.5s script / 2s daemon** | **ADR-0022 CULPRIT — semantic recall on hot path; now bounded by the 2-worker `_HOOK_RECALL_POOL`** |
| PostToolUse | `yadgar-post-tool-capture.py` | no stdout (HTTP 200/429) | HTTP POST `/hooks/auto-capture` (5-item batch flush) | 1s | write-back, off-path |
| PreCompact | `yadgar-pre-compact-drain.sh` | — | HTTP POST `/hooks/pre-compact` | 1s | drain |
| SessionStart(compact) | `yadgar-post-compact-rehydrate.sh` | stdout | HTTP GET `/hooks/post-compact` → restore() | 1s | matcher=compact |
| Stop | `yadgar-stop-memory-checkpoint.py` | **JSON `{"decision":"block","reason":"prompt"}`** | local; adr_add/anchor when threshold | sync BLOCK, every ~25 msgs | **the working PUSH precedent** |
| SubagentStart | `yadgar-subagent-start.py` | stdout `{"text":...}` into subagent | HTTP POST `/hooks/subagent-start` (agent-prompt + recall hint) | 2s | daemon recall |
| SubagentStop | `yadgar-subagent-stop.py` | HTTP POST findings | regex-extracts `## Yadgar findings` | 3s | write-back |
| InstructionsLoaded | `yadgar-instructions-loaded.py` | stdout | HTTP GET `/hooks/instructions-loaded` | 2s, throttled to session_start/compact | — |
| FileChanged | `yadgar-file-changed.py` | — | HTTP POST `/hooks/file-changed` (team_inbox/*.jsonl, docs/plans/*.md) | 3s | write-back |
| SessionEnd | `yadgar-session-end-capture.py` | sentinel JSON file → `~/.local/state/yadgar/session-ends/` | none (imported next SessionStart) | sync file write | write-back |
| **PreToolUse (Bash only)** | `yadgar-db-lockdown-check.py` | JSON `permissionDecision` | none | sync | **the only existing PreToolUse hook — NOT on Agent** |

Two facts that shape the whole design:
- **The push precedent works.** `stop-memory-checkpoint` reliably fires because it
  BLOCKS (`decision:block`), exactly ADR-0021's proof that push beats pull.
- **Deterministic pattern lookup is already freeze-safe.**
  `agent_dispatch_prelude`→`_read_agent_prompt(slug)` is an exact-key wiki read
  (`dispatch_helper.py:139`), NOT `recall()`. Semantic library discovery
  (`recall(type="wiki", tags=["agent-prompt"])`) is user-initiated, never
  hook-fired. Pattern storage: wiki page `agent-prompt-<pattern>`, tags
  `["agent-prompt","task:<pattern>"]` (`agent_prompts.py:189`); TOC page
  `agent-prompt-toc` upserted per save (`agent_prompts.py:86`).

---

## 3. Yadgar feature set → push-ability

| Feature | Kind | Push-able by a hook? | Freeze cost if hot-path |
|---|---|---|---|
| `recall` | read/context | already pushed (session-start, prompt-recall, subagent-start) — **semantic, expensive** | HIGH (the ADR-0022 vector) |
| `project_brief` | read/context | pushable; used by session-start + stop-checkpoint | MED (curated, still a query) |
| `agent_dispatch_prelude` / `_read_agent_prompt` | read/context | **pushable + CHEAP** (exact-key wiki read) | **LOW — freeze-safe** |
| `wiki_read` (exact slug) | read | pushable, cheap | LOW |
| `wiki_query` / `recall(type=wiki)` | read | semantic — avoid on hot path | HIGH |
| `restore` | read/context | pushed on post-compact | MED |
| `recent_memories`, `bookmark_list`, `block_get/list`, `get_rules` | read | pushable, cheap-ish | LOW–MED |
| `memorize` | write | pushable off-path (PostToolUse capture batch) | write path (async) |
| `wiki_add`, `adr_add`, `anchor`, `checkpoint`, `agent_prompt_save`, `block_*` | write | pushable off-path; `adr_add`/`anchor` already fire from Stop | write path (async) |
| `consolidate_now`, `vacuum_now`, `reembed_all` | maintenance | out-of-band only (systemd) | N/A |

**Design principle from this table:** hooks should push the CHEAP reads
(exact-key wiki/agent-prompt, precomputed briefs) synchronously, and route the
EXPENSIVE reads (`recall`, `wiki_query`) through the bounded pool with a strict
timeout or — better — off a **precomputed cache** refreshed by consolidation.
Writes are always off-path (batch/fire-and-forget), which they already are.

---

## 4. Integration matrix — feature × hook × role × action

Cells: **[W]** already wired · **[G]** gap · **[G!]** gap = highest-value ·
`inj`=inject-context · `push`=rewrite/force · `cap`=write-back capture.

| Moment (hook) | Main thread | Subagent | Feature → action |
|---|---|---|---|
| **SessionStart** | [W] session-context inj | n/a | project_brief + recall digest `inj` |
| **UserPromptSubmit** | [W] prompt-recall inj | n/a | recall(prompt) `inj` — *expensive, must stay bounded/cached* |
| **PreToolUse(Agent/Task)** | **[G!] rewrite dispatch prompt** | — (this is where the subagent is born) | inject agent-prompt contract + exact-key pattern via `updatedInput` `push` |
| **SubagentStart** | — | [W] agent-prompt + recall hint inj | project_brief(dir) + pattern contract `inj` (belt-and-suspenders with PreToolUse) |
| **PostToolUse(Agent)** | [W] capture | — | capture dispatch shape `cap` |
| **SubagentStop** | [W] findings→main inj | [W] findings extracted | write-back `## Yadgar findings` `cap` + surface to main `inj` |
| **Stop** | [W] checkpoint/adr push (block) | n/a | adr_add/anchor/checkpoint `cap` (working push) |
| **PreCompact / PostCompact** | [W] drain / rehydrate | — | checkpoint `cap` / restore `inj` |
| **SessionEnd** | [W] sentinel | — | session summary `cap` |
| **InstructionsLoaded** | [W] rules reinforce | — | get_rules `inj` |

The matrix is mostly **wired** except one **[G!]**: PreToolUse on Agent/Task.
That single gap is the seed task and the highest-leverage build.

---

## 5. Recommendation — prioritized, freeze-safe, risk-rated

### P0 — PreToolUse Agent-hook: CONTRACT injection only (the #82 seed, independent)
- **What:** new `yadgar-pretooluse-agent.py` bound to PreToolUse matcher
  `Agent|Task`. Reads `tool_input.prompt`, returns `updatedInput` with the prompt
  prepended by the CONTRACT ONLY (recall-first + `## Yadgar findings` footer).
  `permissionDecision:"allow"`. No pattern matching in P0 — that is P1.
- **Why P0 is standalone:** it flips pull→push at the exact point ADR-0021
  identified and delivers value even with zero pattern lookup. Deliberately
  scoped to avoid the P0/P1 inversion: matching a *specific* pattern requires the
  P1 index; the contract needs nothing.
- **Freeze-safety:** injecting a static contract string is a local operation —
  zero daemon call, zero `recall()`, structurally freeze-immune.
- **Risk:** MED, gated on OQ-1. Must fail-open: on any error →
  `permissionDecision:"allow"` with the ORIGINAL input, never deny/hang (OQ-4).

### P1 — Add pattern MATCHING via a precomputed index (builds on P0)
- **What:** consolidation writes `~/.local/state/yadgar/agent-pattern-index.json`
  (pattern → keywords/embeddings-summary). The P0 hook matches the task shape
  against this static file locally (substring/keyword; optional tiny local
  cosine over cached vectors), then resolves the winner via exact-key
  `_read_agent_prompt(agent-prompt-<winner>)` and adds it + the TOC line to the
  injected prompt. Zero live MCP on the hot path.
- **Why:** the ONLY freeze-safe way to pick the *best-matching* (not a fixed)
  pattern. A per-dispatch semantic `recall()` to choose the pattern is exactly
  the ADR-0022 freeze and is rejected. Matching lives HERE, not in P0.
- **Risk:** LOW (read a local file + exact-key wiki read). Staleness acceptable —
  index refreshes on the consolidation cycle.

### P2 — SubagentStart contract reinforcement (belt-and-suspenders)
- **What:** extend the existing `yadgar-subagent-start.py` to also inject the
  contract + (if resolvable from whatever the SubagentStart payload actually
  carries — see OQ-6) a pattern via `additionalContext`, so subagents whose prompt
  was built outside the P0 path still get the contract.
- **Payload conflict (OQ-6):** the two research agents DISAGREE on SubagentStart's
  stdin. claude-code-guide [G] says it receives `agent_id`/`agent_type` but NOT
  the dispatched prompt; Explore [L] reports the live hook POSTs a "task
  description" to `/hooks/subagent-start`. Resolve empirically before relying on
  prompt/task text at this hook — pattern resolution here depends on it.
- **Why:** subagents reset per spawn — this guarantees the contract lands in the
  subagent context even when PreToolUse rewrite didn't (e.g. nested dispatch).
- **Freeze-safety:** keep it to project_brief/exact-key reads through the bounded
  pool; do NOT add a second semantic recall per spawn (spawn bursts were the
  freeze trigger). Prefer the precomputed brief.
- **Risk:** LOW-MED (already wired; extension only).

### P3 — Write-back completeness (capture without the instance remembering)
- **What:** SubagentStop already extracts `## Yadgar findings`; add
  main-thread-side surfacing of those findings via `additionalContext` and ensure
  PostToolUse(Agent) captures the dispatch shape for later pattern mining.
- **Why:** closes the loop — findings/decisions captured as push, not pull.
- **Risk:** LOW (write path, async, already the pattern).

### Explicitly REJECTED (reintroduces the freeze)
- Per-dispatch semantic `recall()` in PreToolUse/SubagentStart to pick the best
  pattern. This is the ADR-0022 cascade (uncancellable ~1.5s threads × spawn
  burst → loop starvation → SIGKILL). Use the precomputed index instead.
- A blocking/deny-based #82 (rung c) as the PRIMARY mechanism — friction without
  auto-correct; keep deny only as a validation backstop behind fail-open.

---

## 6. Open feasibility questions — need a code spike before building

- **OQ-1 (blocks P0):** Verify on the *installed* Claude Code version that a
  PreToolUse hook returning `hookSpecificOutput.updatedInput` actually rewrites
  the Agent/Task `tool_input.prompt` (field name + that the subagent runs with the
  rewritten prompt). One primary citation was a community gist; the docs example
  is "modify tool input" generally. Spike: a trivial PreToolUse hook that appends
  a sentinel to a dispatched prompt and confirm the subagent echoes it.
- **OQ-2:** Confirm the PreToolUse matcher accepts `Agent|Task` (or which exact
  `tool_name` the dispatch surfaces as — `Agent` vs `Task`) on this version.
  MEM-ANCHOR gives matcher grammar; the tool name string is version-dependent.
- **OQ-3 (P2 placement):** Confirm SubagentStart `additionalContext` lands in the
  SUBAGENT's context (not the main thread) empirically — docs say so but placement
  differs per event and is load-bearing for "subagents leverage yadgar."
- **OQ-4 (fail-open):** Confirm that on hook error / malformed JSON, Claude Code
  proceeds with the original input (does not hard-fail the dispatch). P0's
  fail-open safety depends on this.
- **OQ-5 (freeze re-check):** Confirm the P1 hook path never transitively calls
  the daemon `recall` route; if it must hit the daemon for the exact-key read,
  measure the latency and confirm it's served without the `_HOOK_RECALL_POOL`
  saturating under a spawn burst. (P0 is contract-only, so it has no daemon
  dependency to check.)
- **OQ-6 (P2 payload conflict):** Determine what SubagentStart's stdin payload
  ACTUALLY contains on the installed version. The two research agents disagree
  (guide: no dispatched prompt; Explore: live hook posts a task description).
  P2 pattern resolution depends on the answer.
- **OQ-7 (confabulation scrub):** Before wiring any hook JSON, re-verify the exact
  `hookSpecificOutput` field names against the installed version — the guide
  output included at least two suspected confabulations (`permissionDecision:
  "defer"`, `ConfigChange` hook) not present in the verified anchor.

---

## 7. What NOT to do (constraints, restated)
- No hot-path `recall()` / `wiki_query()` in any per-dispatch or per-spawn hook.
- Every injecting hook must **fail open** (error → allow original, never deny/hang).
- Reuse exact-key `_read_agent_prompt` + a precomputed index; do not invent a new
  semantic matcher on the hot path.
- Do not assume `hook_runner.py` is live — the per-event `yadgar-*.py` scripts are.
- Implement nothing until OQ-1..OQ-5 spikes pass.
