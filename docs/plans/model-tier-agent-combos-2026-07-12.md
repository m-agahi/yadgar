# Model-Tier Agent-Combo Strategy (task #41)

**Status: REVIEW — user confirmation pending, no implementation.**
Date: 2026-07-12. Author: Fable planning agent (fresh-context dispatch). Advisor: independent Opus adversarial audit (see §6 — no `advisor` MCP tool exists in the dispatch environment; an Opus plan-audit agent was substituted and that substitution is itself a data point for the matrix).

---

## 0. BLUF — Dispatch matrix

Core principle (the cost lesson): **Fable's premium is per-token on context, not per-thought.** Fable-as-main-thread was expensive because every wake re-read the entire growing session uncached (cache TTL ~5 min, wake gaps ~25 min ⇒ 100k+ uncached tokens per ping). A dispatched agent starts on a small fresh curated brief ⇒ Fable-as-agent is bounded and cheap. Therefore: **route by (context size × reasoning density), not by task prestige.**

| Task shape | Tier | Why (leverage vs cost) |
|---|---|---|
| Planning / plan-writing / feasibility-design | **fable\*** (provisional — see §5 falsification) | Decision-shaping; errors cascade into every downstream build. Bounded curated brief ⇒ premium is a few ¢/plan. Highest $/insight leverage. |
| Adversarial plan-audit / design review | **fable\*** (provisional) | This session: fable/opus audits caught 3 pre-build blockers (transformers-5.x load wall, CE `_ckpt` split-brain cache bug, hf-xet resolve blocker) — each a mid-build STOP or silent prod corruption avoided. Reasoning-dense, small input. Advisor caveat: the catches confound tier × fresh-context × skeptic-structure; fable-over-opus at this row is unproven until the §5 A/B tally runs. |
| RCA / crash debug | **opus** | Evidence-driven tool-loop work; most tokens go to tool churn, not reasoning — Fable premium mostly wasted. Opus did RCA cleanly this session. Escalate to fable only if opus stalls twice on *reasoning* (not mechanics), with a re-distilled brief. |
| Complex multi-seam code build | **opus** | Needs sustained tool use + solid reasoning; the mechanical majority of build tokens doesn't benefit from Fable. |
| Simple/mechanical build: nix bumps, version syncs, codemods, test migration, feature-kill closeouts | **sonnet** | Proven clean this session across all mechanical work. Cheap + fast; correctness comes from the pattern discipline, not the model. |
| Measurement / perf protocol runs | **sonnet** | Protocol-following: numbers in, numbers out. |
| Search / explore / code-location | **sonnet** (haiku for pure listing sweeps) | Retrieval, not reasoning. |
| Orchestration / routing / watcher wakes / main thread | **opus — NEVER fable** | The wake-tax mechanism above. Fable main-thread on a long orchestration session is the one known catastrophic cost mode. |
| Report compilation / status sweeps | **haiku/sonnet** | Aggregation only. |

**Mechanical clause (kept from the old rule):** every `Agent(...)` call sets `model` explicitly. Omission (= inherit main-thread model) is a violation — this is what prevents accidental Fable inheritance if the main thread is ever Fable again.

## 1. Combos

1. **planner → independent-auditor gate** (two separate fresh-context dispatches). The auditor gets its own brief + the plan file, NOT the planner's transcript — fresh context is what makes the audit independent. Gate triggers on **blast radius, not duration** (advisor fix): mandatory for plans touching schema/migrations, data-destructive ops, public API/tool surface, or multi-seam architecture; optional for long-but-mechanical work. Auditor tier: fable\* provisional; opus is the proven-adequate floor. Payoff: every catch is a mid-build STOP avoided.
2. **opus-orchestrator-of-sonnet-workers** for build trains (stacked cars, mechanical chunk refactors). Opus main thread routes/rebases/synthesizes; sonnet agents build in worktrees. Already the de-facto proven shape (T2/hardening trains).
3. **fable + advisor** for planning when an advisor mechanism is available: fable plans, an independent second model pressure-tests. When no advisor tool exists, substitute an opus plan-audit dispatch (done for this very plan).
4. **divergence-panel (fable + opus, independent, main thread synthesizes)** — reserved, NOT default, and REDESIGNED per advisor: the two agents MUST get **deliberately different briefs/framings** (e.g. one gets the design + code citations, the other gets the problem statement + constraints and re-derives). Same-brief panels buy correlated answers — the dominant planning failure is a gap in the brief, which both inherit identically. Justified only when ALL of: (a) decision is irreversible or expensive to reverse (schema migration, data-destructive op, public API), (b) a single audit already produced disagreement or low confidence, (c) two genuinely different framings of the problem exist. Expected use: a few times per quarter, not per week.
5. **Escalation ladder** (replaces static assignment when a dispatch fails): sonnet fails twice → opus; opus stalls twice on reasoning → fable with a re-distilled ≤4k brief. Never "same model, try again harder" a third time.

## 2. How it lives in Yadgar

### Chosen mechanism: `DISPATCH:` convention line in pattern content + one global matrix wiki page. Zero code changes.

**Per-pattern hint.** First line of each agent-prompt pattern's `## Prompt` body:

```
DISPATCH: model=fable (fallback=opus) — decision-shaping, bounded brief
```

Why in content, not a structured field:
- `agent_prompt_save(pattern, content, purpose)` has no metadata slot, and `_load_starter_prompts()` (yadgar/core/server/tools/agent_prompts.py:207) reads only `e["pattern"], e["purpose"], e["content"]` — a `model_tier:` yaml key would be silently dropped. A schema change (new column/field + prelude plumbing + seeder change) buys nothing over a convention line the prelude already delivers verbatim.
- `agent_dispatch_prelude` injects the pattern snippet as-is (`## Agent-prompt [pattern vN]` block, dispatch_helper.py:429) — the orchestrator reads the hint at exactly the moment it chooses `model=`. Self-surfacing, no new code path.
- Wiki-versioned like the rest of the pattern body; tier revisions are ordinary `agent_prompt_save` updates.

**Matrix source of truth.** One global wiki page `model-tier-dispatch` (slug already forward-referenced by the memory rule) containing §0 + §1 of this plan. Orchestrator consults it when no pattern fits; the CLAUDE.md rule points at it.

**Drift control (advisor Q5).** Two places stating a tier = guaranteed drift if both are canonical. Resolution: the wiki `model-tier-dispatch` page is CANONICAL; the per-pattern `DISPATCH:` line is a cached hint that keeps the tier value inline (the prelude reader needs the value without a second lookup) but ends with `— canonical: model-tier-dispatch`. On conflict, wiki wins and the pattern page gets updated via `agent_prompt_save`. Optional cheap backstop: a periodic `drift-audit`-pattern sweep comparing `DISPATCH:` lines against the matrix page (no new lint code).

**ADR-precedent check (per-module versioning rejection).** ADR-0088 rejected per-car/per-module version numbers because they fragment release coherence — a version is a *contract* that must stay globally consistent. A per-pattern `DISPATCH:` hint is advisory *routing metadata*: nothing else must stay in sync with it, and a stale hint degrades gracefully to orchestrator judgment. Different category; the rejection does not apply. (It DOES argue against a structured, validated, lint-enforced `model_tier` field — which is exactly why the convention-line form is proposed instead.)

**Explicitly rejected:** per-pattern structured field + prelude schema change (over-engineering, ADR-0088-adjacent); hint in the *contract* genesis (contract is injected into the SUBAGENT, but model choice happens in the ORCHESTRATOR before dispatch — wrong reader); TOC-column rendering change (code change for a duplicate of the pattern-page line).

## 3. Rule updates (diff-grade — for user confirmation, NOT applied)

### 3a. Memory rule rewrite — `/home/max/.claude/projects/-home-max-git-yadgar/memory/no-fable-for-agents.md`

Propose: rename file to `model-tier-dispatch.md` and replace content wholesale. Also update the index line in `/home/max/.claude/projects/-home-max-git-yadgar/memory/MEMORY.md`.

Current (full file):

```markdown
---
name: no-fable-for-agents
description: "User rule 2026-07-11 — never dispatch subagents on Fable; opus for complex builds/design, sonnet for simple"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 855fb12e-aa0d-4b4c-9fa0-235ef4de65bd
---

User directive (2026-07-11, yadgar session): "dont run agents with fable". Agents must NOT inherit the main-loop Fable model — always set an explicit `model` on Agent dispatches: `opus` for complex builds, multi-seam refactors, RCA/design; `sonnet` for simple bounded fixes.

**Why:** cost — user judged Fable-tier builders overkill even for the complicated T2/hardening cars ("opus would probably match results"). Main thread stays Fable; delegation is where the spend multiplies.

**How to apply:** every `Agent(...)` call carries `model: "opus"` or `model: "sonnet"` explicitly; omitting model (= inherit Fable) is a violation. Graded rule refinement pending in [[model-tier-dispatch-rule]] (yadgar task #41, enforced later by the pretooluse hook, task #11).
```

Proposed replacement (`model-tier-dispatch.md`):

```markdown
---
name: model-tier-dispatch
description: "Model tier per task-shape — fable for bounded-brief reasoning (planning/audit), opus RCA+complex builds+main thread, sonnet mechanical. Supersedes no-fable-for-agents 2026-07-12."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 855fb12e-aa0d-4b4c-9fa0-235ef4de65bd
---

SUPERSEDES no-fable-for-agents (2026-07-11). That rule's rationale was inverted by the cost RCA: Fable-as-main-thread was expensive because every wake re-read the whole growing session uncached — NOT because Fable-thinking is dear. A dispatched agent starts on a small fresh brief, so Fable-as-agent is bounded and cheap.

**Route by (context size × reasoning density):**
- `fable` — planning, adversarial plan-audit, design review (provisional pending fable-vs-opus A/B tally; opus is the proven floor). REQUIRES a single-fresh-read curated brief (target ≤10k tokens, never inherited session history) and a bounded deliverable (one doc/verdict).
- `opus` — RCA/debug, complex multi-seam builds, synthesis, MAIN THREAD. Fable is NEVER the main thread for long orchestration/watcher sessions (wake-tax).
- `sonnet` — mechanical builds, nix/version edits, codemods, test migrations, measurements, explore.
- `haiku` — listing sweeps, report aggregation.

Escalation: sonnet fails 2× → opus; opus stalls 2× on reasoning → fable with re-distilled brief.

**Mechanical clause (unchanged):** every `Agent(...)` call sets `model` explicitly; omission (= inherit) is a violation. Enforcement hook pending (task #11).

Full matrix + combos: wiki `model-tier-dispatch`; plan docs/plans/model-tier-agent-combos-2026-07-12.md.
```

MEMORY.md index line, current:

```markdown
- [No Fable for agents](no-fable-for-agents.md) — always explicit model on dispatches: opus complex, sonnet simple
```

Proposed:

```markdown
- [Model-tier dispatch](model-tier-dispatch.md) — explicit model always; fable = bounded-brief planning/audit agent, opus = RCA/builds/main-thread, sonnet = mechanical
```

### 3b. Nix-managed CLAUDE.md — `/home/max/git/nix/dotfiles/common/claude.md`

**Edit 1 — section title (line 274).** Current:

```markdown
## HARD RULE — Orchestrator Mode (Sonnet Delegation)
```

Proposed:

```markdown
## HARD RULE — Orchestrator Mode (Tiered Delegation)
```

**Edit 2 — Mechanism block (lines 289–293).** Current:

```markdown
**Mechanism:**

- Reads/search: `Agent(subagent_type="Explore", run_in_background=true)`
- Code work: `Agent(subagent_type="general-purpose", model="sonnet", run_in_background=true)`
- Multi-agent repo writes: `isolation: "worktree"` per agent — prevents collisions.
```

Proposed:

```markdown
**Mechanism:**

- Model tier per task-shape (full matrix: wiki `model-tier-dispatch`; pattern pages carry a `DISPATCH:` line the prelude surfaces):
  - `fable` — planning, adversarial plan-audit, design review. ONLY with a single-fresh-read curated brief (target ≤10k tokens); never inherits session history.
  - `opus` — RCA/debug, complex multi-seam builds, synthesis of divergent agent outputs.
  - `sonnet` — mechanical builds, nix/version/git edits, codemods, test migrations, measurements, reads/search.
  - `haiku` — listing sweeps, report aggregation.
- Reads/search: `Agent(subagent_type="Explore", run_in_background=true)`
- Code work: `Agent(subagent_type="general-purpose", model=<tier>, run_in_background=true)`
- EVERY dispatch sets `model` explicitly — omitting it (= inherit main-thread model) is a violation.
- Escalation: sonnet fails 2× → opus; opus stalls 2× on reasoning → fable with a re-distilled brief.
- Multi-agent repo writes: `isolation: "worktree"` per agent — prevents collisions.
```

**Edit 3 — "Stay on Opus" block (lines 306–310).** Current:

```markdown
**Stay on Opus (main thread):**

- Architecture, design, cross-cutting reasoning
- Synthesis of agent outputs, user-facing replies
- Ambiguous tasks (unclear requirements, design tradeoffs)
```

Proposed:

```markdown
**Main thread (Opus — NEVER Fable for long orchestration/watcher sessions; wake-tax: every notification re-reads the full growing context uncached):**

- Orchestration, routing, synthesis of agent outputs, user-facing replies
- Ambiguous tasks (unclear requirements, design tradeoffs)
- Deep architecture/design that FITS a curated brief → dispatch a `fable` agent with a single-fresh-read brief instead of grinding it in-thread
```

**Edit 4 — Agent-Prompt Library rule (append to the paragraph ending line 327, after "…`recall(type=\"wiki\", tags=[\"agent-prompt\"])`.").** Insert:

```markdown
Pattern pages open with a `DISPATCH: model=<tier> (fallback=<tier>) — <why>` line;
honor it when choosing `model=` unless the task at hand contradicts the pattern's
assumptions (then override AND update the pattern page via `agent_prompt_save`).
```

### 3c. Seeding materials — `/home/max/git/yadgar/yadgar/core/seed/materials/agent_prompts.yaml`

No loader change needed (hint lives in `content`, which the seeder passes through verbatim). Add a `DISPATCH:` first line to each starter's `content` block. NOTE: the two `model=fable` lines below (plan-audit, feasibility-design) are provisional pending the §5 A/B tally — if the user prefers, seed them as `model=opus (escalate=fable)` now and flip after the tally:

| pattern | proposed line |
|---|---|
| plan-audit | `DISPATCH: model=fable (fallback=opus) — adversarial audit is peak $/insight leverage` |
| feasibility-design | `DISPATCH: model=fable (fallback=opus) — pre-build feasibility shapes everything downstream` |
| crash-rca | `DISPATCH: model=opus — evidence/tool-loop heavy; escalate fable only on 2× reasoning stall` |
| debug-investigate | `DISPATCH: model=opus (fallback=sonnet for shallow bugs)` |
| perf-anomaly-metrics | `DISPATCH: model=opus — attribution reasoning over noisy metrics` |
| code-review | `DISPATCH: model=opus (fallback=sonnet for small mechanical diffs)` |
| implement-tdd | `DISPATCH: model=sonnet (fallback=opus for multi-seam scope)` |
| plan-executing-build | `DISPATCH: model=opus for multi-seam cars, sonnet for mechanical cars — plan states which` |
| stacked-car-parallel-build | `DISPATCH: model=sonnet — seams pre-cut by the plan` |
| mechanical-refactor-chunk-commit-early | `DISPATCH: model=sonnet — chunked mechanical codemod` |
| dispatch-fix-test-migration | `DISPATCH: model=sonnet` |
| feature-kill-closeout | `DISPATCH: model=sonnet` |
| drift-audit | `DISPATCH: model=sonnet (fallback=opus if drift classification is judgment-heavy)` |
| explore-codebase | `DISPATCH: model=sonnet (haiku for pure listing)` |
| plan-corpus-status-sweep | `DISPATCH: model=haiku` |

Example diff (plan-audit entry; same shape for the rest):

```yaml
- pattern: plan-audit
  purpose: 'Adversarially audit an implementation plan BEFORE building — …'
  content: |-
    DISPATCH: model=fable (fallback=opus) — adversarial audit is peak $/insight leverage

    You are an INDEPENDENT skeptic. The plan may be wrong; its prose is not evidence. …
```

Optionally (cheap, recommended): add one sentence to the `contract:` genesis so subagents that THEMSELVES dispatch sub-subagents inherit the discipline — append to the contract content:

```
5. Dispatching a sub-agent yourself? Honor its pattern's DISPATCH: model tier; set model explicitly.
```

Live wiki pages for already-deployed patterns get the same line via ordinary `agent_prompt_save` updates (seed backflow direction per ADR-0091 — genesis and live stay in sync).

### 3d. New wiki page (post-confirmation): `model-tier-dispatch` (global)

Content = §0 matrix + §1 combos + §4 guardrails of this plan. Saved with `wiki_add(..., category="convention", directory="global")`. The memory rule and CLAUDE.md both point at it.

## 4. Guardrails (Fable-cost regression prevention)

1. **Fable never the main thread** for long orchestration/watcher sessions. The wake-tax is the ONLY demonstrated catastrophic Fable cost mode. (If a future session needs Fable-grade main-thread judgment, run it as a short bounded session and hand off.)
2. **Fable agents get a SINGLE FRESH READ — never inherited session history.** (Advisor correction: the cost pathology is *repeated uncached re-reads of growing context*, not brief size. A one-shot 10k brief is bounded and cheap; a 4k cap starves planners of integration-seam context and pushes curation cost back to the main thread.) Rule: curated brief, read once, target ≤10k tokens; if you're pasting >20k you're dumping the repo — distill first (distillation is opus/sonnet work) or the task is tool-loop-shaped and belongs on opus.
3. **Fable deliverable is bounded:** one document, one verdict, one design. No open-ended "keep iterating until done" Fable dispatches — iteration loops are tool-churn, i.e. opus/sonnet territory.
4. **No Fable in watch/poll/monitor loops** of any kind (the agent-side mirror of guardrail 1).
5. **Explicit `model=` on every dispatch** — the inheritance foot-gun stays banned regardless of what the main thread runs.
6. **Divergence-panel requires the 3-condition test** (§1.4) — the default remains ONE auditor.
7. **Review trigger:** if a month of usage shows Fable spend >~15% of total agent spend, or fable plan/audit catch-rate indistinguishable from opus (§5 falsification), demote planning/audit rows to `opus (fallback=fable)` and keep fable only for escalation + panels.
8. **Orthogonal follow-up (advisor Q6b, NOT solved by this plan):** the main-thread wake-tax persists on Opus at a per-token discount — the disease is context growth × uncached re-reads, and the cure is main-thread context hygiene (checkpoint + /clear cadence between wake batches, watcher-notification batching). Separate task; tier routing does not substitute for it.

## 5. Evidence honesty (n=1 caveat)

This matrix generalizes ONE session: 3 audit catches, clean sonnet mechanicals, one opus RCA. Cells strongly supported: sonnet-for-mechanical (many independent executions), never-Fable-main-thread (measured cost mechanism, not anecdote). Cells on thinner ice: fable-over-opus for planning/audit — this session's catches came from fable AND opus agents, so the marginal value of fable over opus at the top of the matrix is plausible but unproven. Cheap falsification (advisor-endorsed): for the next ~5 plan-audits, alternate fable and opus auditors and tally blocker catches; if opus matches fable, demote planning/audit to `opus (fallback=fable)` and keep fable only for the escalation ladder and divergence panels.

## 6. Advisor reconciliation

No `advisor` MCP tool exists in this dispatch environment; per the fable+advisor combo definition (§1.3), an independent Opus adversarial plan-audit agent was dispatched with a self-contained brief. Its pushback and my reconciliation:

**Advisor verdict:** "one genuinely load-bearing insight (fresh-context single-read is cheap) welded to an unproven tier boundary (fable-vs-opus) it derives from n=1. Ship the sonnet/opus mechanical-vs-RCA split; gate everything Fable behind the falsification test."

| # | Advisor pushback | Disposition |
|---|---|---|
| Q1 | The 3 audit catches confound tier × fresh-context × skeptic-structure; fable-over-opus is unattributable. Falsify by re-running the same audit structure on opus. | **ACCEPTED.** Planning/audit rows marked `fable*` provisional; §5 A/B tally (alternate fable/opus auditors over next ~5 audits) is the gate. Opus declared the proven floor. Partial reservation: the sonnet-auditor arm is skipped — this session's audit quality signal came only from fable/opus, and a 3-arm test triples the sample needed. |
| Q2 | Same-brief divergence panel = fraudulent independence (correlated brief gaps); synthesis burns main-thread tokens. | **ACCEPTED.** Panel redesigned to require deliberately different briefs/framings (§1.4c); same-brief form cut. |
| Q3 | Evidence-backed cells: sonnet-mechanical, sonnet-measurement, opus-RCA. Vibes: fable planner/auditor, mandatory double-fable gate, panel, haiku-listing. | **ACCEPTED** for fable cells (provisional markers added). Haiku-listing kept as a low-risk default — worst case is a cheap retry on sonnet; not worth a falsification protocol. |
| Q4 | ≤4k brief cap optimizes the wrong variable — cost pathology is repeated re-reads, not read size; 4k starves planners of seam context. "10k breaks nothing; 4k breaks planning." | **ACCEPTED — sharpest catch.** Guardrail rewritten: single fresh read + no inherited history is the hard rule; ≤10k target, >20k = distill-first heuristic (§4.2, §3a, §3b updated). |
| Q5 | DISPATCH line + matrix page = two sources of truth, guaranteed drift; nothing validates the line. | **PARTIALLY ACCEPTED.** Wiki matrix page made canonical; DISPATCH line demoted to cached hint carrying a canonical pointer; optional drift-audit sweep as backstop (§2). Rejected the structured-field alternative: a validated field needs schema + prelude + seeder changes for a hint whose staleness degrades gracefully — ADR-0088-style over-coherence for advisory metadata. |
| Q6a | Mandatory fable→fable gate "before any multi-day build" is a routine 2× Fable cost regression. Gate on blast radius. | **ACCEPTED.** §1.1 gate now triggers on blast radius (schema/data-destructive/public-API/multi-seam), not duration. |
| Q6b | Main-thread Fable→Opus is a per-token discount, not a fix — the 100k-uncached-per-wake pathology persists on Opus. Real cure = main-thread context curation/checkpointing. | **ACCEPTED as out-of-scope follow-up.** Recorded in §4.8; candidate mechanisms (aggressive checkpoint+/clear cadence between wake batches, watcher notification batching) belong to a separate task — this plan governs dispatch tiers only. |

## 7. Files proposed for editing (none touched yet)

| File | Change |
|---|---|
| `/home/max/.claude/projects/-home-max-git-yadgar/memory/no-fable-for-agents.md` | Replace → `model-tier-dispatch.md` (§3a) |
| `/home/max/.claude/projects/-home-max-git-yadgar/memory/MEMORY.md` | Index line swap (§3a) |
| `/home/max/git/nix/dotfiles/common/claude.md` | 4 edits to Orchestrator-Mode + Agent-Prompt-Library rules (§3b) |
| `/home/max/git/yadgar/yadgar/core/seed/materials/agent_prompts.yaml` | `DISPATCH:` line per starter + contract sentence (§3c) |
| Yadgar wiki (runtime, not a file) | New global page `model-tier-dispatch`; `agent_prompt_save` updates adding `DISPATCH:` lines to live pattern pages (§3d) |
