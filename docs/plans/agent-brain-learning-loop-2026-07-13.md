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
