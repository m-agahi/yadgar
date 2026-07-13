# Agent-Brain Learning Loop — No-Memorize Rule + SubagentStop Auto-Ingest

**Status:** DRAFT — awaiting audit
**Task:** #39 — "Agent-brain learning loop: rethink no-memorize rule + stop-hook capture automation (opus design)"
**Author:** design agent (opus), 2026-07-13
**Core version at authoring:** 5.132.0
**Scope:** design only — NO code changes made producing this doc.

---

## BLUF

**The task premise is stale.** Task #39 describes the loop as: "subagents do NOT
`memorize()`; they emit a `## Yadgar findings` footer; the MAIN thread writes the
memories back before closing the task; a stop-hook nags. Problem: main thread
forgets → lossy; capture is manual."

**Observed state contradicts this.** The auto-ingest already ships and is live:

- The `SubagentStop` hook (`yadgar/core/hooks/subagent_stop.py`) parses the
  `## Yadgar findings` footer from the subagent's own transcript and POSTs the
  bullets to the daemon.
- The daemon endpoint `/hooks/subagent-stop`
  (`yadgar/core/server/http.py:1086-1187`) calls `memorize()` **once per bullet**,
  with `provenance_agent=<agent_type>` and tags `from-subagent` +
  `agent-type:<agent_type>`.
- The contract wiki (`agent-prompt-contract`, id 6821) already documents this:
  *"This footer IS memorized verbatim — one memory per bullet."*

So "auto-ingest the footer" is **not** a design option to weigh — it is the
shipped status quo. The real design question is: **harden the existing hook path
(A), tear it out in favour of relaxed direct-memorize (B), or hybrid (C)** — and,
orthogonally, **kill the double-write contract** (CLAUDE.md still says "main thread
writes back", which, combined with the live hook, double-writes every finding).

**Recommendation:** **Option A — harden the existing hook auto-ingest**, keep the
no-direct-memorize rule (the single gate-able choke point is the design's whole
value), fix the documented double-write contract, and add the missing telemetry +
decay-based quality gate. Reject Option B.

**Biggest risk:** memory pollution from unreviewed subagent bullets, made
invisible today by **dead capture-rate telemetry** (the `yadgar_subagent_capture_rate`
gauge is defined, init to 0, and never updated — loss is *unmeasured*, not proven).

---

## Current capture-loop mechanics (verified, file:line)

### 1. Contract (what subagents are told)

- `agent-prompt-contract` wiki (id 6821), injected as prelude before every
  dispatch. Verbatim: subagents "do NOT call `memorize()` directly — emit findings
  in report instead. Exception: long_running agents may call memorize with
  provenance_agent set." REQUIRED footer:
  ```
  ## Yadgar findings
  - <fact/anchor/insight> or "none"
  ```
  and: *"This footer IS memorized verbatim — one memory per bullet."*
- `~/.claude/agent-instructions.md` (global, nix-managed) instructs subagents to
  end with a `## Yadgar findings` section.

### 2. Hook script → module (the auto-ingest reader)

- Registered script: `.claude/hooks/yadgar-subagent-stop.py`, which imports
  `yadgar.core.hooks.subagent_stop:main` (falls back to an inline duplicate if the
  package is not importable — `.claude/hooks/subagent-stop.py:29-32`).
- `yadgar/core/hooks/subagent_stop.py`:
  - `main()` (line 276): reads stdin payload → `agent_type`, `cwd`,
    `transcript_path`; detects branch via `_detect_branch_from_cwd` (line 252).
  - `_get_report_text(data)` (line 182): opens `transcript_path`, iterates the
    JSONL, keeps the **last assistant-role message** as the report text. Comment
    at line 183 asserts SubagentStop payload does not carry report text directly —
    it reads the transcript.
  - `_extract_findings(text)` (line 116): lenient — matches any H1-H6 heading whose
    text contains both "yadgar" and "find"; collects `- ` bullets; skips `<!-- -->`
    and the literal `none`.
  - `_post_findings(...)` (line 211): POSTs
    `{agent_type, cwd, findings[], _subagent_writeback: true, branch_hint}` to
    `http://127.0.0.1:$PORT/hooks/subagent-stop`, **3s timeout, `except: pass`**.

### 3. Daemon endpoint (the writer)

- `yadgar/core/server/http.py:1086` `hook_subagent_stop(request)`:
  - validates `agent_type` against `^[A-Za-z0-9_-]{1,64}$` (else `general-purpose`);
  - `tags = ["from-subagent", f"agent-type:{agent_type}"]`;
  - for each finding string: `sanitize_log_field(..., max_len=32_768)` then
    `memorize(content=finding, context=cwd, tags=tags, is_protected=False,
    provenance_agent=agent_type, branch_hint=...)` on a worker thread
    (line 1164-1172);
  - counts `stored` (queued counts as stored); returns `{status, stored, agent_type}`.
- `memorize()` (`yadgar/core/server/tools/memorize.py:31`): standard pipeline —
  secret gate + policy in `phase_validate`, then enqueue; embed / contradiction /
  store / post-write run in the backend drainer. No subagent-specific quality gate.

### 4. Provenance / audit trail (already exists on the hook path)

- Every auto-ingested memory carries `provenance_agent=<agent_type>` and tags
  `from-subagent` + `agent-type:<agent_type>` → gives an audit trail and a bulk
  `forget`/filter handle without any new work. This is a guardrail we already have.

### Answer to task Q "Is there ANY automation that reads the footer and memorizes?"

**Yes.** It is not 100% main-thread discipline. The SubagentStop hook →
`/hooks/subagent-stop` → `memorize()` path is fully wired and registered
(`.claude/settings.json` and `~/.claude/settings.json` both contain the
`SubagentStop` command). The task's "main thread writes back" model is the *old*
mental model; the code moved past it.

---

## The loss problem (evidence — and what's actually assertion)

The task asserts loss because "main thread forgets to write the footer back." Given
auto-ingest exists, that specific loss mode is largely obsolete. The **real** loss
modes, ranked by evidence:

1. **Unmeasured capture rate (the big one).** `yadgar_subagent_capture_rate`
   (`yadgar/_shared/observability/metrics.py:572`) is a Gauge, `.set(0)` at import
   (line 579), and **never updated anywhere** (grep: no `.set(`/`.inc(` outside the
   definition). `yadgar_subagent_dispatch_count` **is** incremented
   (`http.py:1709`, subagent-start). So we count dispatches but not captures →
   **loss is not observable.** "Lossy" is currently an assertion, not a measurement.
   *This is itself a finding and drives open-question #3.*
2. **Silent drop on daemon-down / slow.** `_post_findings` uses a 3s timeout and
   swallows all exceptions (`subagent_stop.py:244-248`). If the daemon is busy or
   down, findings vanish with zero signal.
3. **Footer not in the last assistant turn.** `_get_report_text` keeps only the
   *last* assistant message. If the subagent emits tool-use or a trailing
   non-footer message after the footer, or splits the footer across turns, capture
   silently fails. (See open-question #1 — platform behaviour.)
4. **Hook unregistered / wrong interpreter.** Purely install-state dependent; no
   telemetry to detect.
5. **Double-write (opposite of loss, still a defect).** CLAUDE.md + agent-instructions
   still say "main thread writes back." If a human/agent obeys CLAUDE.md AND the
   hook fires, each finding is memorized twice (dedup only catches it later in
   consolidation, and only above the similarity threshold). Two contracts describe
   one ingress; one must die.

**Local config-hygiene note (NOT a shipped bug):** this dev's
`.claude/settings.json` (gitignored; `git check-ignore` matches) currently has
**3 duplicate `SubagentStop` entries** → findings POSTed 3× on this machine. The
installer uses `_append_if_absent` (`install_hooks_lib.py:315`) which dedupes, so
the installer does **not** produce this — it's hand-edited local cruft (two entries
differ only by `python3` vs `python`). Fix the local file; do not treat as a design
bug. Flagged because it inflates this dev's pollution today.

---

## Design options

### Option A — Harden the existing hook auto-ingest (RECOMMENDED)

Keep no-direct-memorize + footer + hook. Fix the gaps: wire capture telemetry,
add a decay-based quality gate, resolve the double-write contract, decide the fate
of the dead directive grammar.

- **Pros:** single inspectable/gate-able ingress (`/hooks/subagent-stop`);
  provenance already solved; smallest blast radius; the choke point is exactly what
  lets us gate/tag/bulk-forget.
- **Cons:** still depends on the platform contract (SubagentStop fires, transcript
  is the sub's, footer is last turn) — must be verified, not assumed; footer is
  free-text so quality varies.

### Option B — Relax no-memorize; let subagents call `memorize()` directly (REJECT)

Drop the rule; every subagent writes memories inline with `provenance_agent`.

- **Pros:** no dependence on transcript parsing / SubagentStop platform quirks;
  richer control (tier, tags) at the point of insight.
- **Cons — why this is the wrong answer:** it **destroys the single gate-able
  choke point.** Today every subagent write funnels through one endpoint we can
  inspect, tag, rate-limit, quality-gate, and bulk-forget. Direct memorize
  multiplies write paths across every agent, re-scatters provenance enforcement,
  and maximizes pollution and similarity-gate load — the exact opposite of what
  memory discipline wants. The no-memorize rule's *whole value* is that one ingress.
  **Do not adopt B as the primary mechanism.**

### Option C — Hybrid: hook auto-ingest for the default footer + a narrow, gated direct path

Keep the footer/hook as the default; allow `long_running` / explicitly-privileged
agents (the contract already carves this exception) to call `memorize()` directly
**with mandatory `provenance_agent`**, but route even those through a shared
`_ingest_finding()` helper so the quality gate + tagging + telemetry are identical
regardless of ingress.

- **Pros:** covers the legitimate case (long agents that must persist mid-run,
  before any SubagentStop fires) without opening the floodgates; one shared gate.
- **Cons:** two ingress paths to keep in sync; needs discipline that the direct
  path stays narrow (privileged agents only). Acceptable because the exception
  already exists in the contract and the shared helper prevents divergence.

**Verdict:** ship **A** now; treat the narrow direct path of **C** as a follow-up
only if long-running agents demonstrably lose findings before SubagentStop fires.
Never **B** as the primary path.

---

## Recommended design + guardrails

**Design = Option A, hardened, with a Option-C-shaped shared ingest seam for the future.**

Flow: subagent emits `## Yadgar findings` footer → SubagentStop hook extracts
bullets → POST `/hooks/subagent-stop` → **`_ingest_finding()`** (new shared helper)
→ quality gate → `memorize()` with provenance + tier.

### Guardrails

1. **Single choke point preserved.** All subagent-originated writes go through
   `_ingest_finding()`. Even a future privileged direct path (C) calls the same
   helper. No second raw write path.
2. **Decay-based quality gate (cheap, no LLM judge).** Auto-ingested findings
   default to **`tier="ephemeral"` (14d TTL)** unless a bullet is explicitly marked
   durable (see #3). Rationale: unreviewed subagent junk decays automatically unless
   heat (re-access) proves it useful. Quality-gating-by-decay costs nothing and
   needs no classifier. Anchors/durable facts survive only if promoted.
3. **Resolve the dead directive grammar — wire it or delete it.**
   `subagent_stop.py` already ships `_parse_directive` supporting typed
   `memorize:` / `wiki_add:` / `anchor:` bullet prefixes (lines 39-88), but
   `http.py` **never calls it** — every bullet is memorized raw. Decision the plan
   forces:
   - **Preferred: wire it.** Typed routing is the natural quality/gating seam: a
     bare bullet → `memorize(tier=ephemeral)`; `anchor: content=... reason=...` →
     durable anchor; `wiki_add: title=... content=...` → curated wiki (with the
     similarity gate). This gives subagents a controlled way to say "this is
     durable" without opening direct memorize.
   - **Fallback: delete it.** If typed routing is out of scope, remove the dead
     parser to stop half-built code rotting. Do not leave it half-wired.
4. **Kill the double-write contract.** Update CLAUDE.md + `agent-instructions.md`:
   the footer is auto-ingested by the hook; main thread does **NOT** re-memorize
   footer bullets. Main thread's job becomes *review/curate*, not *transcribe*.
   (User decision — see open-questions.)
5. **Wire the capture telemetry.** Increment `yadgar_subagent_capture_rate`
   (or a new `yadgar_subagent_findings_stored_total` counter) in
   `hook_subagent_stop`, so capture rate = stored / dispatched becomes a real,
   alertable number. Without this, every pollution/loss claim stays unfalsifiable.
6. **Bounded ingest per subagent.** Cap findings per SubagentStop event
   (e.g. ≤ 20 bullets, already loosely bounded by `max_len` per bullet) to stop a
   runaway agent from flooding memory in one call.
7. **Provenance stays mandatory** (already true on the hook path). Any future
   direct path must set `provenance_agent`; reject writes that don't.
8. **Similarity-gate load awareness.** Ephemeral default + per-event cap keep the
   consolidation-time dedup load bounded; measure with the new counter before
   loosening.

### Migration

1. Add `_ingest_finding()` helper wrapping the current per-bullet loop; route the
   endpoint through it (no behaviour change first — characterization test pins
   current output).
2. Add the capture counter; deploy; observe real capture rate for a soak window
   **before** changing defaults (evidence-first, per repo's soak discipline).
3. Flip auto-ingest default to `tier=ephemeral`; add directive routing (or delete
   the dead parser).
4. Update CLAUDE.md + agent-instructions to remove "main thread writes back"; update
   `agent-prompt-contract` wiki to state the hook auto-ingests and specify the
   optional `anchor:`/`wiki_add:` directive syntax if wired.
5. Local hygiene: dedupe this dev's `.claude/settings.json` SubagentStop entries.

---

## Acceptance criteria

- **[unit]** `_extract_findings` extracts bullets from a footer, skips `none`/comments
  (regression pin of current behaviour).
- **[unit]** `_parse_directive` round-trips `anchor:`/`wiki_add:`/`memorize:` bullets
  into typed dicts; malformed → `None` (already has partial coverage — extend).
- **[unit]** `_ingest_finding()` routes: bare bullet → `memorize(tier=ephemeral)`;
  `anchor:` → durable; `wiki_add:` → wiki path. (Only if directive routing is wired.)
- **[unit]** endpoint increments the capture counter by the number of stored findings.
- **[unit]** per-event finding cap enforced (21st bullet dropped + logged).
- **[e2e]** dispatch a real subagent that emits a `## Yadgar findings` footer →
  assert N memories appear with `provenance_agent=<type>`, tags `from-subagent` +
  `agent-type:<type>`, and `tier=ephemeral` — within one drain cycle.
- **[e2e]** daemon-down path: POST fails, subagent still completes (hook never
  blocks), and the drop is counted (a `capture_failed` counter), not silent.
- **[e2e]** double-write guard: with contract updated, main thread does not
  re-memorize; assert no duplicate-content pair for a footer bullet.

---

## Test plan

1. Characterization test on the current endpoint output BEFORE refactor
   (deepdiff / exact-match on stored memory fields) — red-green safety for the
   `_ingest_finding()` extraction.
2. Extend existing `test_subagent_stop_hook.py` /
   `test_v5_46_9_subagent_stop_findings.py` for the new tier default + counter.
3. New directive-routing tests (if wired) covering all three prefixes + malformed.
4. Metric-emission integration test: assert the capture counter moves on a
   representative POST (mirrors the I8/P11 "each metric emits on its path" convention).
5. e2e smoke: 5-dispatch run asserting capture count > 0 (the same EXIT criterion
   the v5.3.8 plan used — reuse it as the loop's health check).

---

## Risks

- **Memory pollution (primary).** Free-text footers vary in quality; auto-ingest
  writes them all. Mitigation: `tier=ephemeral` default (decay-gate) + per-event
  cap + provenance for bulk-forget. Residual: genuinely-wrong findings that get
  re-accessed will stick — no automated correctness gate (would need an LLM judge,
  out of scope; v6 curator territory).
- **Provenance.** Already solved on the hook path (`provenance_agent` + tags).
  Risk only reappears if Option B / a direct path is added without enforcing it —
  guardrail #7 covers it.
- **Similarity-gate / dedup load.** More writes → more consolidation-time dedup
  work. Currently unquantified (no counter). Mitigation: land telemetry first,
  ephemeral default reduces long-lived pressure, per-event cap bounds burst load.
- **DLQ.** `memorize` rejections (secret gate, branch resolution) can DLQ. Auto-ingest
  runs unattended, so DLQ entries from subagent findings accrue silently. Mitigation:
  count `capture_failed`; surface subagent-origin DLQ entries in the existing DLQ
  alert text.
- **Platform contract fragility.** The whole loop rests on SubagentStop firing +
  transcript-path semantics + footer-in-last-turn (open-question #1). If a Claude
  Code version changes this, the loop silently degrades — the capture counter is the
  early-warning system, which is why guardrail #5 is not optional.

---

## Scope

**IN:**
- Harden the existing SubagentStop → `/hooks/subagent-stop` → `memorize` path.
- Wire capture telemetry; add decay-based quality gate (ephemeral default) + per-event cap.
- Resolve dead directive grammar (wire or delete).
- Fix the double-write contract (docs).
- Shared `_ingest_finding()` seam.
- Prefer payload `last_assistant_message` over transcript-file parsing (fall back to
  transcript when absent) — removes the "which transcript?" ambiguity + a fragile
  file read. Gated on open-question #1 (may we depend on that field).

**OUT:**
- Option B (direct subagent memorize as the primary path) — rejected.
- LLM-judge / correctness quality gate — v6 curator territory, not this loop.
- Real-time synthesis / `ask()` (v7).
- Wiki auto-ingest of findings beyond the optional `wiki_add:` directive.
- Rewriting the consolidation/dedup pipeline.

---

## Open questions (user decisions)

1. **[VERIFY — platform] SubagentStop reliability + the `last_assistant_message`
   opportunity.** `claude-code-guide` verification (2026-07-13, against current
   Claude Code docs) returned:
   - **Fires for all subagent types** (`general-purpose`, `Explore`, `Plan`, custom):
     confidence HIGH for CLI subagents, MEDIUM for API-level Task orchestration
     (docs don't explicitly cover the API-parallel path).
   - **Payload now carries `last_assistant_message` directly** (plus `agent_id`,
     `agent_type`, `session_id`, `prompt_id`, `transcript_path`, `cwd`). SubagentStop
     was added ~v2.1.198 (June 2026).
   - **`transcript_path` target (sub vs parent) is NOT documented** — only inferred
     from "each subagent runs in its own context window." Our code reads
     `transcript_path` and keeps the last assistant turn (`subagent_stop.py:182`).
   - **Whether the footer is reliably the last assistant message: undocumented.** No
     guarantee about trailing tool-use / summary blocks.
   - Historical evidence FOR the loop working: the v5.3.8 plan recorded "SubagentStop
     capture >0 from 5-dispatch smoke" as a met EXIT criterion — smoke-tested green
     **once**, at v5.3.8, not re-verified at the current CC version.

   **Design implication (new):** the hook currently parses the transcript JSONL to
   recover the report text, but the platform now provides `last_assistant_message`
   in the payload. We should **prefer `last_assistant_message` when present, fall
   back to transcript parsing** — simpler, avoids the "which transcript?" ambiguity,
   and removes a fragile file-read. This is a low-risk hardening item to add to
   Scope IN. **Decision for user:** confirm we may depend on `last_assistant_message`
   (it is undocumented whether it is truncated/summarized vs. the full final turn).
   If the platform contract is unreliable, the loop needs a fallback (e.g.
   main-thread confirmation) and Option A's confidence drops.
2. **Kill "main thread writes back"?** The double-write contract must die on one
   side. Recommendation: hook is authoritative; main thread reviews, does not
   transcribe. Confirm so CLAUDE.md + agent-instructions + contract wiki can be
   updated consistently.
3. **Is loss actually happening?** Capture rate is unmeasured today (dead gauge).
   Do we (a) land telemetry and soak-measure before changing anything, or (b) trust
   the anecdotal "lossy" claim and change defaults immediately? Recommendation: (a) —
   evidence-first, matches repo soak discipline.
4. **Directive grammar: wire or delete?** Wiring gives subagents a gated
   durable/wiki path (nice, more work); deleting removes rot (cheap). Pick one.
5. **Ephemeral default acceptable?** Making auto-ingested findings decay in 14d
   unless re-accessed is the core quality gate. Confirm the TTL and the promotion
   path (heat threshold? explicit `anchor:` directive?).

---

## Version impact

- No public API / MCP tool signature change (memorize signature is frozen; the
  endpoint contract is internal). New internal counter + optional directive routing.
- Target: a **minor** core bump (behaviour change: ephemeral default + telemetry).
  If directive routing is wired and changes the endpoint's stored-memory shape,
  still minor (internal endpoint). Backend bump only if the drainer path changes.
- Docs bump (CLAUDE.md is nix-managed, out of this repo — hand the diff to the user
  via the migration note; `agent-prompt-contract` wiki update is in-repo via `wiki_add`).

---

## AUDIT (2026-07-13)

**Auditor:** adversarial review agent (opus). Read-only; no code changed. Verified
every load-bearing file:line claim against source at core 5.132.0 / this working tree.

### Verdict Status: **PROCEED WITH ONE CORRECTION — reframe "already LIVE".**

The plan's code-path claims are almost all **VERIFIED** — the SubagentStop →
`/hooks/subagent-stop` → `memorize()` chain is genuinely wired end-to-end, per-bullet,
with provenance + tags. Option A (harden, keep no-direct-memorize) is the right call and
the recommendation **stands**. BUT the plan's framing — "auto-ingest is ALREADY LIVE …
it is the shipped status quo" — is **overstated on the empirical axis**: I found **zero
`from-subagent`-tagged memories in the live DB** (see row [E1]). The path is *live in
code*, *inert in effect*. That does not flip the design choice, but it **reorders the
work**: the #1 task is not "harden a working loop," it is "prove the loop captures
anything at all" — which is exactly what the plan's own dead-telemetry finding predicts,
now confirmed by direct evidence. Elevate open-question #3 to the critical path.

### Per-claim verification table

| # | Claim (plan) | Verdict | Evidence (file:line) |
|---|---|---|---|
| C1 | Hook script imports `yadgar.core.hooks.subagent_stop:main`, inline fallback if unimportable | **VERIFIED** | `.claude/hooks/yadgar-subagent-stop.py:29-32` (import), `:155-168` (inline dup). Plan cited `.claude/hooks/subagent-stop.py:29-32`; actual filename is `yadgar-subagent-stop.py` — **minor path typo in plan**. |
| C2 | `main()` reads stdin → agent_type/cwd/transcript_path; detects branch via `_detect_branch_from_cwd` | **VERIFIED** | `subagent_stop.py:276` main, `:285-291` fields, `:295` branch detect, `:252` `_detect_branch_from_cwd`. |
| C3 | `_get_report_text` opens `transcript_path`, keeps **last** assistant-role message; comment asserts payload lacks report text | **VERIFIED** | `subagent_stop.py:182-208`; comment `:186-187`; last-wins loop `:201-204`. Reads `transcript_path` ONLY — does **not** read `last_assistant_message` (see C11). |
| C4 | `_extract_findings` lenient — any H1-H6 heading containing "yadgar"+"find"; collects `- ` bullets; skips `<!-- -->` and `none` | **VERIFIED** | `subagent_stop.py:116-149`; heading test `:130`; skip `:145`. |
| C5 | `_post_findings` POSTs `{agent_type,cwd,findings,_subagent_writeback,branch_hint}`, 3s timeout, `except: pass` | **VERIFIED** | `subagent_stop.py:211-248`; payload `:230-238`; timeout+swallow `:246-248`. NOTE: inline-fallback `_post_findings` (`yadgar-subagent-stop.py:137-140`) does **NOT** forward `branch_hint` or `_subagent_writeback` — silent divergence between the two copies. |
| C6 | Endpoint `hook_subagent_stop` validates agent_type `^[A-Za-z0-9_-]{1,64}$` else `general-purpose`; tags `from-subagent`+`agent-type:<t>`; memorize per bullet with provenance + branch_hint on worker thread; queued=stored | **VERIFIED** | `http.py:1088` def, `:1129-1131` regex, `:1144` tags, `:1157-1174` per-bullet `asyncio.to_thread(_memorize, …, provenance_agent=agent_type, branch_hint=_branch_hint)`, `:1173` queued-counts-as-stored. Plan cited `1086-1187` / `1164-1172` — accurate. |
| C7 | `memorize()` = standard pipeline; no subagent-specific quality gate | **VERIFIED** | endpoint calls the frozen `memorize` (`http.py:1140-1142` import, `:1164`). No gate branch in the loop. |
| C8 | Provenance already on hook path: `provenance_agent=<type>` + tags → audit/bulk-forget handle | **VERIFIED** | `http.py:1144`, `:1170`. |
| C9 | `yadgar_subagent_capture_rate` gauge `.set(0)` at import, **never updated**; only `dispatch_count` incremented | **VERIFIED** | `metrics.py:572-579` (def + lone `.set(0)` at `:579`, comment "no in-process capture tracking yet"); grep confirms no other `.set(`/`.inc(` on it. `dispatch_count.inc()` at `http.py:1709` (subagent-**start** endpoint, `:1703-1711`). |
| C10 | `_parse_directive` (typed `memorize:`/`wiki_add:`/`anchor:`) exists but endpoint never calls it → every bullet memorized raw | **VERIFIED** | parser `subagent_stop.py:39-88`; `main()` POSTs raw bullets (`:325`) — never invokes `_parse_directive`; `http.py:1157-1174` loops raw strings. Genuinely dead code on both sides. |
| C11 | (Scope IN / OQ#1) Prefer `last_assistant_message` over transcript parsing — currently transcript-only | **VERIFIED as future work** | Confirmed code reads only `transcript_path` (C3). The claim that the *payload now carries* `last_assistant_message`, and all SubagentStop platform behaviour in OQ#1, **rests on the plan's cited `claude-code-guide` result — NOT re-verified by this audit** (no-fabricate constraint; I did not independently probe CC hook schema). Treat OQ#1 platform facts as plan-asserted, not audit-confirmed. |
| C12 | Double-write: CLAUDE.md **and** `agent-instructions.md` say "main thread writes back" → each finding memorized twice | **PARTIALLY VERIFIED — quote inaccurate + one source unsupported** | The contract substantively exists: the session-governing (nix-managed/injected) CLAUDE.md says *"main thread writes from agent report before closing task"* — that is the operative text; **"main thread writes back" is the plan's paraphrase, not verbatim.** The disk copy `/home/max/.claude/CLAUDE.md` is a DIFFERENT auto-generated section (header "Yadgar v5.133.0", generic "call memorize after significant task") — likely rewritten by `sync_instructions`; the plan's cited `CLAUDE.md:34` does not contain that phrase. **`agent-instructions.md` attribution is UNSUPPORTED**: it carries agent-side write-back *triggers* (`:117`, `:130` "end with `## Yadgar findings`"), NOT a main-thread re-memorize instruction. So double-write is a *real latent risk from one source*, not the two-source certainty the plan implies. Corrects the row; does **not** flip Status. |
| C13 | Local `.claude/settings.json` has duplicate SubagentStop entries → findings POSTed multiple× | **VERIFIED (count off)** | `.claude/settings.json` has **3** command entries (`:103` `python3`, `:112` `.venv/bin/python3`, `:124` `.venv/bin/python`), not "two differ by python3 vs python" — it's 3×. File is gitignored (`git check-ignore` matches) → local cruft, correctly flagged as not-a-design-bug. |
| C14 | `_append_if_absent` installer dedupes (`install_hooks_lib.py:315`) | **NOT INDEPENDENTLY VERIFIED** | Did not open `install_hooks_lib.py`; out of the hot path. Plausible, unconfirmed. |
| C15 | Referenced test files exist (`test_subagent_stop_hook.py`, `test_v5_46_9_subagent_stop_findings.py`) | **VERIFIED** | `yadgar/tests/hooks/test_subagent_stop_hook.py`, `yadgar/tests/scripts/test_v5_46_9_subagent_stop_findings.py`. |
| E1 | **(Audit-added empirical check)** Is the live path actually *producing* captures? | **CONTRADICTS "already LIVE" framing** | `recall(tags=["from-subagent"], type="memory", max_results=15)` on `/home/max/git/yadgar` → **0 memories with `from-subagent` / `agent-type:*` tags**; every hit is `provenance_agent="default"` cochange/auto-abstracted noise. 40 recent memories (14d) incl. Agent-dispatch sessions → still **0** footer captures. The loop is **wired but empirically inert in this DB.** Consistent with C9 (loss unmeasured) + the silent-drop modes (C5, `except:pass`) + likely footer-not-in-last-turn (OQ#1). |

### What this changes for the design

1. **Reframe the BLUF.** "Auto-ingest is the shipped status quo" is true only in the
   *code-path* sense. Empirically (E1) it captures nothing here. The honest framing:
   *"the loop is fully coded and registered but has never been shown to capture a single
   finding in production; telemetry to prove capture is itself missing (C9)."* This
   **strengthens** the plan's own argument (dead telemetry → unfalsifiable) but weakens
   the rhetorical "not a design option to weigh — it's already live."
2. **Reorder the migration.** The plan's step 2 (add capture counter, soak-measure)
   should be step 1 and is now the **acceptance gate for everything else**. Do not flip
   defaults (ephemeral tier), wire directives, or kill the double-write contract until the
   counter proves capture > 0 on a real 5-dispatch smoke. If capture is genuinely 0, the
   bug to fix first is *why the footer never lands* (transcript target? last-turn
   assumption? daemon-down swallow?), and Option A's hardening list is correct but must be
   validated against a **now-passing** smoke, not the stale v5.3.8 green.
3. **Option A vs B vs C:** design reasoning is **sound**. B correctly rejected (choke-point
   argument holds). C (shared `_ingest_finding()` seam) correctly deferred. The decay-TTL
   (`ephemeral`, 14d) + per-event cap guardrails are cheap and sound **given the choke
   point exists** — but they only matter once capture works; today they gate an empty
   stream. Ephemeral default also has a subtle risk: if capture is bursty-then-silent, 14d
   TTL may expire genuinely-useful findings before any human reviews them — pair with the
   promotion path (OQ#5) before shipping the TTL flip.
4. **Directive grammar (C10):** wire-or-delete framing is right. Given E1, **delete-first**
   is the lower-risk move — do not build typed routing atop an unproven ingress. Wire it
   only after capture > 0 is demonstrated and there is real demand for a durable subagent
   path.

### User-decision items (audit-surfaced, additive to plan's OQ)

- **[D1 — CRITICAL, new]** Empirical capture is **0** today (E1). Before ANY hardening,
  confirm: land the capture counter (C9 fix) + run a 5-dispatch smoke. If smoke shows 0,
  the task pivots from "harden" to "diagnose why the footer never lands." Approve this as
  the first, gating step?
- **[D2]** Accept the C12 correction: double-write is a single-source latent risk
  (session/nix CLAUDE.md only), not a two-source certainty, and the quoted phrase is a
  paraphrase. Still worth resolving, but lower urgency than the plan implies. OK to
  down-rank it below D1?
- **[D3]** OQ#1 platform facts (`last_assistant_message` presence, SubagentStop firing for
  all agent types, transcript target) are **plan-asserted via claude-code-guide, not
  audit-verified**. Confirm you want to depend on that verification, or re-run a fresh
  `claude-code-guide` pass before coding the `last_assistant_message` preference.
- **[D4]** Inline-fallback divergence (C5): the standalone copy drops `branch_hint` +
  `_subagent_writeback`. Fold both copies into one importable path, or accept the drift?
  (Relevant only for machines where the yadgar package is unimportable.)
- **[D5]** Fix the 3× duplicate local `SubagentStop` entries (C13) before any capture
  measurement — otherwise the counter triple-counts on this dev's box and corrupts the
  first soak reading.

### Bottom line

Design is fundamentally right; **ship Option A**. But correct the BLUF from "already
live" to "coded but unproven," make capture-telemetry + a passing smoke the **gating first
step** (not step 2), delete the dead directive grammar rather than wire it (for now), and
downgrade the double-write claim to a single-source latent risk with a paraphrased quote.
Status: **PROCEED with corrections above.**
