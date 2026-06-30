# PreToolUse Forcing-Function Hook: Force the Agent-Prompt Library at Dispatch Time

**Date:** 2026-06-30
**Status:** Design / feasibility — RECOMMEND BUILD (with a phased rollout)
**Author:** dispatched design pass (Opus orchestrator)
**Related:** ADR-0021 (agent-prompt library is write-only), #69 (project_brief read-nudge), the ADR-capture Stop hook (the proven forcing-function twin)

---

## 0. Problem (ADR-0021 restated)

The agent-prompt library is **write-only**. This session logged **zero** `agent_dispatch_prelude`
calls despite the instance building, *this same session*, a CLAUDE.md HARD RULE and a
`project_brief` read-nudge (#69) telling itself to call it before every dispatch.

Root cause is structural, not motivational: **PULL-based discipline fails, PUSH-based forcing
functions work.** Proof inside the same codebase — the ADR-capture **Stop** hook
(`yadgar-stop-memory-checkpoint.py`) emits `{"decision":"block","reason":...}` and is reliably
honored every checkpoint, while the soft library-nudge (the same instruction delivered as text)
is reliably skipped. The instruction is identical in content; only the *delivery mechanism*
differs. Mechanism is the variable that matters.

**Fix direction:** a dispatch-time forcing function — the twin of the ADR Stop hook, but firing
on `PreToolUse[Agent]` instead of `Stop`. Push the contract into the dispatch at the point of
action, so the model cannot dispatch a subagent without the agent-prompt contract attached.

---

## 1. FEASIBILITY VERDICT

**Framing: floor + ceiling. The build does NOT hinge on the one unverified claim.**

- **FLOOR (guaranteed mechanism):** `permissionDecision:"deny"` + `permissionDecisionReason` — the
  reason text reaches **the model** (doc-confirmed verbatim, §1.1), so the model reads it, calls
  `agent_dispatch_prelude`, and re-dispatches. This alone closes ADR-0021 and is independent of any
  mutation capability. **Build recommendation rests on the floor.**
- **CEILING (frictionless upgrade, if confirmed at runtime):** `hookSpecificOutput.updatedInput`
  auto-injects the prelude into the dispatch prompt so the model needn't act at all. Doc-indicated
  as general to PreToolUse, but **runtime-unverified for the `Agent` tool specifically** — confirm
  as build-step-zero (§1.1, §2.6). If it works, we get the ideal; if not, we ship the floor.

So: *the verdict is BUILD regardless; mutation only decides whether the push is frictionless
(inject) or one-bounce (deny→re-dispatch).*

| Capability | Supported? | Exact shape | Source |
|---|---|---|---|
| **Fires on subagent dispatch** | YES | matcher `"Agent"` | sub-agents docs ("deny the `Agent` tool"); confirmed locally — `post-tool-capture.py` already captures tool `Agent` |
| **Receives `tool_input`** | YES | stdin JSON `tool_input` = dispatch args | hooks ref Input Schema; locally `post-tool-capture.py:64` reads `prompt`/`description` from `Agent` `tool_input` |
| **MUTATE input (`updatedInput`)** | **DOC-INDICATED, runtime-unverified for `Agent`** | `hookSpecificOutput.updatedInput` "replaces a tool's arguments before it runs" | hooks ref (verbatim); only Bash example shown — confirm on `Agent` at build-step-zero |
| **DENY + reason to MODEL** | **YES (verbatim-confirmed)** | `permissionDecision:"deny"` + `permissionDecisionReason` → shown to **Claude (model)** | hooks ref: `permissionDecisionReason` is the reason shown to the model |
| **`systemMessage` (NOT the model channel)** | shown to **USER**, not model | top-level `systemMessage` | hooks ref: *"Warning message shown to the user"* — do NOT use for the deny reason |
| **Inject `additionalContext`** | YES (under `hookSpecificOutput`, PreToolUse) | `hookSpecificOutput.additionalContext` — appears "next to the tool result" | hooks ref: "Return `additionalContext` inside `hookSpecificOutput`" |
| **Exit 2** | blocks, stderr→model | lower-level than JSON deny | hooks-guide |
| **`"ask"`** | escalates to USER prompt | not useful here (we want the model to act, not the user) | hooks ref |

**Doc citations:** `https://code.claude.com/docs/en/hooks`, `https://code.claude.com/docs/en/hooks-guide`,
`https://code.claude.com/docs/en/sub-agents` (fetched 2026-06-30).

### 1.1 The one caveat (and why it does not block us)

The public hooks reference does **not** enumerate the `Agent` tool's `tool_input` field names, and
the `updatedInput` merge-vs-replace semantics ("does a partial `updatedInput` drop omitted
fields?") are **not documented**. Both are resolved empirically by **local precedent**, not docs:

- Field names: `yadgar/hooks/post-tool-capture.py` already pulls `prompt` and `description` out of
  the `Agent` tool's `tool_input` in production. So the field we must rewrite is `tool_input.prompt`.
- Replace-vs-merge: treat `updatedInput` as a **full replacement** (the safe reading — the docs'
  one example replaces the whole `tool_input`). The hook therefore echoes back **all** original
  fields (`prompt`, `description`, `subagent_type`, …) with only `prompt` modified. Never emit a
  partial `updatedInput`.
- **Runtime-unverified for `Agent`:** the docs describe `updatedInput` as general to PreToolUse but
  the only concrete example is Bash's `command`. Whether the `Agent` tool honors `updatedInput` is
  **not provable from docs**. This is the single claim the ceiling rests on — it is verified at
  **build-step-zero** (a one-line inject hook + PostToolUse capture confirming the sentinel landed in
  the child's recorded prompt), and the design degrades to the deny floor if it fails (§2.6). The
  feasibility verdict does NOT depend on resolving this.

### 1.2 Existing precedent in THIS setup (reuse, don't invent)

- **A `PreToolUse` hook already ships and works:** `settings.json` registers
  `PreToolUse[matcher:"Bash"] → yadgar-db-lockdown-check.py`. That script is the I/O template for the
  stdin parse + `allow|deny` shape + fail-soft-on-malformed-stdin discipline. **BUT one correction we
  must make when cloning it:** that script returns its deny text in top-level `systemMessage`, which
  the docs say is shown to the **user, not the model**. For our purpose the reason MUST reach the
  **model** so it acts on it — so we use `hookSpecificOutput.permissionDecisionReason` (verbatim
  doc-confirmed as the model-facing channel), not `systemMessage`. The lockdown hook is a hard-stop
  (it doesn't need the model to do anything), which is why its `systemMessage` choice is fine there
  but wrong for an elicitation hook.
- **The block-and-reason pattern is proven for the *Stop* event, not yet PreToolUse:** the Stop
  checkpoint hook uses `decision:block`+`reason` and is reliably honored — strong evidence that an
  injected reason changes model behavior. The PreToolUse analogue (`permissionDecision:deny` +
  `permissionDecisionReason`) is the *same class* of mechanism (reason fed back to the model) and is
  doc-confirmed to reach the model, but the "model reads deny reason → calls prelude → re-dispatches"
  behavior is **assumed by analogy, not locally proven on PreToolUse**. Build-step-zero smoke-tests it
  with a trivial always-deny `Agent` hook.
- **Install path exists:** `yadgar/install_hooks_lib.py` (`install_hooks_impl`, `_build_core_hooks`,
  `_install_append_hooks`), source scripts in `yadgar/hooks/`, both global and project scope. The
  existing PreToolUse entry is a **standalone** script (NOT routed through `hook_runner.py`) — the
  new hook must be standalone too, or it loses the required `hookSpecificOutput.hookEventName` field.
- **The TOC data source is cheap:** the pattern list lives in a wiki page slug `agent-prompt-toc`,
  rows formatted `` - `pattern` → purpose ``. Retrievable via `GET /api/wiki/read?slug=agent-prompt-toc`
  (a DB key-read, **no LLM, no embedding**). `agent_dispatch_prelude` itself does NOT match — it's an
  exact slug lookup, so the **hook must do the matching**.

**Verdict one-liner:** *Floor = deny+reason (guaranteed, model-facing, doc-confirmed); ceiling =
`updatedInput` auto-inject (frictionless, doc-indicated but runtime-unverified for `Agent`).* Build
on the floor; light up the ceiling if build-step-zero confirms it. BUILD either way.

---

## 2. DESIGN

### 2.1 Mechanism — ceiling: auto-inject (`updatedInput`)

> This is the *ceiling* path, used only when build-step-zero confirms `updatedInput` is honored for
> the `Agent` tool. If unconfirmed, the hook runs the §2.2 deny floor instead. The matcher (§2.3) and
> all anti-annoyance/loop-safety guards (§2.4–2.5) are shared by both paths.

On `PreToolUse[Agent]`, the hook (inject mode):

1. Reads `tool_input` (`prompt`, `description`, `subagent_type`) from stdin.
2. Runs the **fast-path gates** (§2.4) — if any says "skip", emit a bare `allow` (zero friction).
3. Matches the dispatch text to a library pattern (§2.3). If **no pattern** fits → bare `allow`
   (the legitimate bespoke case — never block bespoke dispatches).
4. If a pattern matches → fetch its contract block and **prepend** it to `prompt`, then emit:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {
      "subagent_type": "<unchanged>",
      "description": "<unchanged>",
      "prompt": "<INJECTED CONTRACT BLOCK>\n\n<original prompt>"
    }
  }
}
```

The injected block is exactly what `agent_dispatch_prelude(pattern=X)` would return: the Yadgar
contract (recall-first, observed-state-wins, mandatory `## Yadgar findings` footer) **plus** the
matched agent-prompt body. The model never has to call the tool — the prelude is already in the
subagent's prompt. **Zero friction, the ideal.** This is strictly better than the soft nudge: it
removes the human (model) step that was being skipped.

> Design note — we inject the **prelude content**, not a *demand to call the tool*. The tool was the
> bottleneck; bypassing it (while delivering its payload) is the whole point.

### 2.2 Mechanism — floor: deny + reason (the guaranteed path)

This is the **floor** — the mechanism that is doc-confirmed model-facing and works regardless of the
`updatedInput` question. It is used (a) whenever build-step-zero cannot confirm injection, and (b) on
the ambiguous-match path even in inject mode:

1. **Platform/version regression guard.** If a Claude Code build silently drops `updatedInput` for
   the `Agent` tool (the doc-acknowledged ambiguity in §1.1), the hook must degrade to a behavior
   that still works rather than silently no-op. A self-test on install (§2.6) picks the mode.
2. **Cases where we want the model to *choose* the pattern.** When the matcher is *ambiguous*
   (two patterns score within ε), injecting the wrong one is worse than asking. Then:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Agent-prompt library has candidate patterns for this dispatch: `code-review`, `debug-investigate`. Call agent_dispatch_prelude(pattern='<pick one>', task_topic='<topic>') and build the dispatch on the returned contract, then re-dispatch. (If none truly fit, re-dispatch unchanged — bespoke is allowed.)"
  }
}
```

The model reads the reason, calls the prelude, re-dispatches. Loop-safety (§2.5) ensures the
re-dispatch is not blocked again.

**Chosen mechanism:** auto-inject (`updatedInput`) as primary; deny+reason as the (a) regression
fallback and (b) ambiguous-match path. `additionalContext` is NOT used as the primary channel — it
appends a system reminder to the *parent's* context but does not alter the *child's* prompt, so it
does not actually carry the contract into the subagent. (It may optionally be used to leave a
one-line breadcrumb in the parent like "injected `code-review` prelude" for transparency.)

### 2.3 Task-shape → pattern matching (cheap, no LLM)

**Data source:** the `agent-prompt-toc` wiki page, fetched once per session and cached (§2.4).
Rows: `` - `pattern` → one-line purpose ``. Current built-ins: `code-review`, `debug-investigate`,
`explore-codebase`, `implement-tdd` (plus any `agent_prompt_save`-added customs).

**Matcher (keyword/token scoring, deterministic, sub-millisecond):**

1. Build a keyword set per pattern from (a) the pattern name tokens (`debug-investigate` →
   {debug, investigate}) and (b) a small curated synonym map per built-in, e.g.:
   - `code-review` ← review, audit, diff, PR, lint, security, vulnerabilit*
   - `debug-investigate` ← debug, investigate, root cause, why does, failing, reproduce, trace, bug
   - `explore-codebase` ← explore, find, locate, map, where is, search, list all, survey
   - `implement-tdd` ← implement, build, add feature, write test, TDD, red-green, failing test
2. Lowercase the dispatch `description` + first ~400 chars of `prompt`; count keyword hits per
   pattern (weight `description` 2×, prompt 1×).
3. **Decision rule:**
   - top score `== 0` → **no match** → bare `allow` (bespoke).
   - top score `≥ MIN_HITS` (e.g. 2) AND beats 2nd by `≥ MARGIN` → **inject** that pattern.
   - top two within `MARGIN` and both `≥ MIN_HITS` → **deny+reason** listing the candidates
     (ambiguous; let the model pick).

The synonym map is the only piece needing curation; it ships with the hook and is overridable via a
sidecar file (`~/.yadgar/agent_prompt_keywords.json`) so new `agent_prompt_save` patterns can register
keywords without a code change. If a saved pattern has no keywords, it simply never auto-matches
(safe — falls through to bespoke), which is the correct conservative default.

> Why not an LLM/embedding match in the hook? A `PreToolUse` hook runs **synchronously on the dispatch
> critical path**. An LLM call (hundreds of ms–seconds) would tax *every* dispatch and the user would
> disable the hook — the documented death of forcing functions. Keyword scoring is O(tokens) and
> needs no network. A future enhancement (§3) can offload matching to the daemon HTTP endpoint
> `/hooks/dispatch-prelude-check` with a tiny in-process cosine match over precomputed pattern
> embeddings — still no LLM — but v1 stays pure-Python/local.

### 2.4 Anti-annoyance / cost (the make-or-break constraint)

Forcing functions die when they add latency or fire spuriously. Guards, all cheap:

1. **Fast skip gates (checked first, before any matching):**
   - **Self-dispatch / library tools:** if `subagent_type` is an Explore-only or trivial type, or the
     prompt already contains the contract sentinel (§2.5), skip.
   - **Recency gate (the "called recently" rule):** if `agent_dispatch_prelude` was called in this
     session within the last N dispatches (read a tiny session-keyed state file, same pattern as the
     Stop hook's `stop-hook-state.json`), skip — the model is already in the contract groove. This is
     the direct analogue of the prompt-recall hook's "skipped if session-context ran recently."
2. **TOC cache:** fetch `agent-prompt-toc` once per session; cache to
   `~/.local/state/yadgar/agent-prompt-toc.cache.json` with a short TTL (e.g. 10 min) + the
   keyword map. The hot path does **zero network** after the first dispatch.
3. **Hard timeout / fail-soft:** every external read (TOC fetch) wrapped with a ≤300 ms timeout; any
   exception or malformed stdin → bare `allow`. The hook must NEVER block legitimate work on its own
   failure (same discipline as `db-lockdown-check.py`). A broken hook degrades to invisible, not to
   obstructive.
4. **Budget:** hot path = stdin parse + dict lookups + ≤a few hundred keyword comparisons. Sub-ms.
   No model, no embeddings, no DB on the cached path.

### 2.5 Loop-safety (must not block the post-prelude re-dispatch)

The failure mode to avoid: model gets deny+reason → calls prelude → re-dispatches → hook denies
*again* → infinite loop. **Critical insight (corrected from a naive design): on the deny path the
model rewrites the dispatch prompt building on the prelude, so the prompt HASH changes and the model
may NOT paste the verbatim sentinel.** Therefore hash-matching and sentinel-detection both *miss* on
the deny re-dispatch — the only guard that reliably fires is the recency marker:

1. **Recency / deny-issued state (PRIMARY — the load-bearing guard).** The moment the hook issues a
   deny for a given dispatch (or observes an `mcp__yadgar__agent_dispatch_prelude` call via the
   PostToolUse capture), it writes a session-keyed marker (same mechanism as the Stop hook's
   `stop-hook-state.json`). While that marker is fresh, the hook **suppresses all denies for the next
   N dispatches** (`allow`, optional inject). This breaks the loop independent of prompt content,
   because it does not rely on recognizing the re-dispatch — it simply stops gating for a window after
   the model has been told once. Mirrors the Stop hook's `stop_hook_active` one-shot guard.
2. **Sentinel detection (BEST-EFFORT — covers the inject path).** The injected contract block carries
   a stable marker `<!-- yadgar-prelude:vN pattern=X -->`. If the incoming `prompt` already contains
   it, skip re-injection. This reliably covers the **inject** path (we control the sentinel) but is
   only opportunistic on the **deny** path (the model may paraphrase rather than paste).
3. **Deny-once-by-hash (BEST-EFFORT).** Hash of `description`+`prompt` recorded on deny; identical
   re-submission is not re-denied. Catches the verbatim-retry case only; misses the (common) rewritten
   re-dispatch — hence demoted below the recency gate.

The inject path is inherently loop-safe (it always `allow`s). Loop-safety on the **deny** path rests
on guard #1; #2/#3 are belt-and-suspenders, not the mechanism.

### 2.6 Mode self-selection on install

The install step records `inject` as the default mode and writes a capability flag. First real
dispatch attempts `updatedInput`; the PostToolUse capture (which sees the *actual* subagent prompt)
can detect on the first run whether the injection landed (sentinel present in the child's recorded
prompt). If after K dispatches no injected sentinel is ever observed in children, a
consolidation-time check flips the hook to `deny` mode (writes `mode:"deny"` to the sidecar config).
This makes the regression in §1.1 self-healing rather than silently broken. (v1 may ship `inject`
hard-coded and defer auto-flip to v2.)

### 2.7 Scope & ship path

- **Scope: GLOBAL (`~/.claude`).** Subagent dispatch happens in every project; the library is global
  (patterns live in global wiki pages). Project-scoping would leave most dispatches ungated. Ship via
  `install_hooks(scope="global")`.
- **Files:**
  - New source script `yadgar/hooks/dispatch-prelude-gate.py` (standalone, clone of
    `db-lockdown-check.py` I/O contract + the matcher).
  - Register in `install_hooks_lib.py`: add a `PreToolUse[matcher:"Agent"]` entry alongside the
    existing `PreToolUse[matcher:"Bash"]` lockdown entry (the matcher is tool-name-scoped, so the two
    coexist without interference — settings.json supports multiple PreToolUse blocks with distinct
    matchers).
  - Optional daemon endpoint `GET /hooks/dispatch-prelude-check?desc=...` in `http.py` (v2, for the
    embedding match) — not required for v1.
- **Sidecar config** `~/.yadgar/agent_prompt_keywords.json` (`{pattern: [keywords], _mode: "inject"|"deny", _min_hits, _margin}`)
  so behavior is tunable without redeploying the script.
- **Settings example (added by installer):**

```json
"PreToolUse": [
  { "matcher": "Bash",  "hooks": [{ "type": "command", "command": ".../yadgar-db-lockdown-check.py" }] },
  { "matcher": "Agent", "hooks": [{ "type": "command", "command": ".../yadgar-dispatch-prelude-gate.py" }] }
]
```

---

## 3. Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| `updatedInput` not honored for `Agent` tool (doc-ambiguous, §1.1) | **HIGH (top risk)** | Self-test/auto-flip to deny mode (§2.6); deny fallback always designed; sentinel-in-child detection confirms injection empirically |
| Wrong-pattern injection pollutes a bespoke dispatch | MED | Conservative `MIN_HITS`+`MARGIN`; no-match → allow; ambiguous → deny+ask, never guess; bespoke always passes through |
| Latency → user disables hook | MED | TOC cache, recency gate, ≤300ms timeout, sub-ms hot path, fail-soft allow |
| Loop on deny path | MED | Triple guard: sentinel, recency state, deny-once hash (§2.5) |
| Keyword map rots as patterns grow | LOW | Sidecar override file; unmatched saved patterns safely fall through to bespoke |
| Two PreToolUse matchers interfere | LOW | Tool-name matchers are disjoint (`Bash` vs `Agent`); independent blocks |
| Partial `updatedInput` drops dispatch fields | MED | Always echo ALL original fields; treat updatedInput as full replace (§1.1) |

---

## 4. Build / no-build recommendation

**BUILD — phased.**

- **v1 (small, high-value):** standalone `dispatch-prelude-gate.py`, inject mode, local keyword
  matcher + curated synonym map, TOC cache, fail-soft, sentinel loop-guard, global install. This is
  ~one script modeled on `db-lockdown-check.py` + a matcher + a cache — bounded, testable, reversible
  (delete one settings block). It directly closes ADR-0021: the contract is pushed into every matched
  dispatch with zero model cooperation required.
- **v2 (optional):** daemon `/hooks/dispatch-prelude-check` with precomputed-embedding match,
  auto-flip mode self-selection, `additionalContext` parent breadcrumb.

**Why build, not just re-nudge:** the entire premise of ADR-0021 is that nudges fail and pushes work,
proven within this codebase by the Stop hook. Re-issuing the nudge in a new place repeats the failure.
The mutate-capable `PreToolUse` makes the push *frictionless* (auto-inject), removing even the model's
re-dispatch step — strictly dominant over the deny-only design we'd have settled for if mutation were
unsupported.

**Test-first (per repo discipline):** the matcher and the stdin→stdout contract are pure functions
(stdin JSON in, stdout JSON out) — unit-testable exactly like the existing hook tests
(`test_server.py` covers `db-lockdown-check`). Write failing tests for: no-match→allow,
single-match→inject(sentinel+full-fields), ambiguous→deny(reason lists candidates),
sentinel-present→allow (loop-safety), malformed-stdin→allow (fail-soft), timeout→allow.

---

## 5. Advisor review (incorporated)

A stronger reviewer read the full design pass. Four points, all incorporated above:

1. **Decouple the verdict from the unverified claim.** The build does not depend on `updatedInput`.
   Reframed to floor (deny+reason, guaranteed) + ceiling (inject, upgrade-if-confirmed). §1, §1.1.
2. **Two single-sourced claims were overstated; one discriminates.** (a) `updatedInput`-for-`Agent`
   is doc-indicated but runtime-unverified (only Bash example) → demoted to "confirm at
   build-step-zero." (b) The local "block+reason is proven" claim was **wrong**: the local
   `db-lockdown-check.py` returns its reason in top-level `systemMessage`, which the docs say is
   **user-facing, not model-facing** — so it would NOT elicit a re-dispatch. Corrected to use
   `permissionDecisionReason` (verbatim doc-confirmed as the model channel), and flagged that the
   re-dispatch behavior is proven on *Stop*, assumed-by-analogy on *PreToolUse*. A targeted WebFetch
   confirmed both field semantics verbatim. §1.1, §1.2.
3. **Loop-safety leaned on the wrong guard.** On the deny path the model rewrites the prompt, so the
   hash changes and the sentinel may be absent — both "primary" guards miss. Promoted the
   **recency / deny-issued marker** to the load-bearing guard; demoted sentinel/hash to best-effort.
   §2.5.
4. (Process) git divergence noted — handled at commit (reset to `origin/master`, the untracked doc
   survives, then push `HEAD:master`).

**Net:** the advisor did not change the BUILD recommendation; it tightened what the verdict is
allowed to *claim* and corrected one factual error about the local precedent's reason channel.
