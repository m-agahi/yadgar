# Workflow Codification — ADR-0107 (task #55)

**Status:** DRAFT — awaiting audit
**Date:** 2026-07-13
**ADR:** ADR-0107 (accepted 2026-07-13)
**Scope:** doc + wiki edits that codify the standard train workflow. NO code changes.

---

## BLUF

ADR-0107 decided the standard train workflow: one `feat/<train>` branch + worktree-parallel
cars + **stacked-rebase ff-only** integration (retiring independent-merge) + **unconditional
per-car audited plans** + ONE PR + ONE version. This plan specifies the exact edits that
codify that decision, split into two channels:

- **FILE edits** (in the git tree, ship in the car's PR, docs-only → no version bump):
  `docs/claude-workflow.md` (full rewrite around train/car vocab), `docs/RELEASE.md:110-112`
  (vocab fix on the pointer, pointer stays).
- **WIKI writes** (out-of-tree, executed via MCP tools at build time, NOT a PR diff):
  `model-tier-dispatch` Combos §1 amendment; a new `agent-prompt-car-plan-audit-gate` pattern;
  one-line precondition-gate additions to `agent-prompt-plan-executing-build` and
  `agent-prompt-stacked-car-parallel-build`.

Explicit non-goal: NO addition to `docs/ARCHITECTURE_INVARIANTS.md` (I1-I34 are lint/import-linter
enforced code properties; a git-process rule is a category mismatch — ADR-0107 says so).

---

## ADR-0107 recap (verbatim decision)

1. A train = one `feat/<train>` branch off latest `origin/master`; ONE PR at the end; ONE version
   claim (per ADR-0088).
2. Parallel cars build in isolated worktrees on distinct branches with disjoint seams; integration
   to the feat branch is **STACKED-REBASE ff-only** (each car rebases onto the latest feat tip
   before its ff-merge) — canonical for future trains, **retiring the independent-merge
   (`merge --no-ff`) model** in `docs/claude-workflow.md`.
3. **EVERY car MUST have its own plan doc that passes an independent adversarial audit**
   (verdict: BUILD / BUILD-WITH-CHANGES / DO-NOT-BUILD) BEFORE its build dispatch — unconditional,
   regardless of blast radius. Build starts only on an AUDITED-ready plan.
4. Plan lifecycle per ADR-0081/0082 (final car archives via first-commit `git mv`).

Consequences (ADR-0107's own list, = this plan's task set): refresh `claude-workflow.md`;
fix `RELEASE.md:112` pointer; amend `model-tier-dispatch` Combos §1 (planner→auditor gate
mandatory-every-car); add agent-prompt `car-plan-audit-gate` pattern; do NOT add to
ARCHITECTURE_INVARIANTS.

---

## Per-target edit spec

| # | Target | Channel | Current state (verified) | Change |
|---|--------|---------|--------------------------|--------|
| 1 | `docs/claude-workflow.md` | FILE | Describes the STALE pre-ADR-0088 model: numbered sub-branches `feat/vX.Y/NN-<topic>`, `git merge --no-ff`, "long-lived integration branch", periodic master rebase, 3-pass audit only at final PR. Header "Yadgar v5 Integration Model", captured 2026-05-15. | Full rewrite around ADR-0088/0107 vocab (train/car, stacked-rebase ff-only, unconditional per-car audit gate, one PR/one version, plan lifecycle ADR-0081/0082). Section-level stays-vs-rewrites map below. |
| 2 | `docs/RELEASE.md` :110-112 | FILE | Section "## When using the long-lived feature-branch workflow" points at `claude-workflow.md` as canonical, with vocab `feat/vX.Y → master`. Pointer is CORRECT (claude-workflow.md stays canonical); only the framing vocab is stale. | Vocab fix, pointer stays. Verbatim old→new below. |
| 3 | `model-tier-dispatch` (id 6825) Combos §1 | WIKI | Gate is blast-radius-CONDITIONAL: "Gate triggers on blast radius: mandatory for plans touching schema/migrations, data-destructive ops, public API/tool surface, or multi-seam architecture." | Keep blast-radius clause for non-train one-off plans; ADD: unconditional for EVERY train car (audit tier may drop to a lighter check for purely-mechanical cars, but is never skipped). Verbatim old→new below. |
| 4a | `agent-prompt-car-plan-audit-gate` (NEW) | WIKI | Does not exist (confirmed absent in tagged recall). | Create via `agent_prompt_save`. Encodes the WHEN (no car build dispatches until its plan is AUDITED-ready); composes the HOW (`agent-prompt-dispatch-plan-audit` + `agent-prompt-plan-audit`). Content below. |
| 4b | `agent-prompt-plan-executing-build` (id 6814) | WIKI | No precondition gate present. | Add one-line precondition gate near top of the prompt block. Verbatim insert below. |
| 4c | `agent-prompt-stacked-car-parallel-build` (id 6815) | WIKI | No precondition gate present. | Add one-line precondition gate near top of the prompt block. Verbatim insert below. |
| 5 | `docs/ARCHITECTURE_INVARIANTS.md` | — | I1-I34, import-linter 4-contract + code-review enforced (header confirms). | **NON-GOAL — no edit.** Rationale below. |

### 1. `docs/claude-workflow.md` — section stays-vs-rewrite map

Read in full (248 lines). Per-section disposition:

| Section | Disposition |
|---------|-------------|
| Title "Yadgar v5 Integration Model" + intro | **REWRITE** — retitle "Standard Train Workflow (ADR-0088/0107)"; drop v5-specific framing. |
| "When to use this model" | **REWRITE** — keep the "cohesive release / parallelisable" gate; drop "long-lived integration branch" language. |
| "Branch topology" | **REWRITE** — replace numbered sub-branches `feat/vX.Y/NN-<topic>` diagram with train/car topology: `feat/<train>` + per-car stacked branches, cars in isolated worktrees. |
| "Workflow" §0 setup | **KEEP (light edit)** — `feat/<train>` off latest `origin/master` is already correct; rename `feat/vX.Y` → `feat/<train>`. |
| "Workflow" §1 dispatching an agent | **REWRITE** — car dispatch instruction; reference the per-car audited-plan precondition (ADR-0107 decision 3) + the agent-prompt library patterns. |
| "Workflow" §2 integration cycle | **REWRITE (core change)** — replace `git merge --no-ff origin/feat/vX.Y/NN` with **stacked-rebase ff-only**: each car rebases onto latest feat tip, then `git merge --ff-only`. This is the load-bearing edit. |
| "Workflow" §3 periodic master sync | **KEEP (light edit)** — rebase discipline still applies; rename branch var. |
| "Workflow" §4 final master PR | **KEEP (light edit)** — one PR / one version already correct; rename branch var. |
| "CI on the feature branch" | **PARTIAL** — mechanism stays; rename `feat/vX.Y` → `feat/<train>`; the `paths-ignore: ['feat/**']` example stays valid. |
| "Safeguards" | **REWRITE** — replace "numbered sub-branches / resolve in numeric order" + "light audit at integration point / skip full 3-pass" with: unconditional per-car audited plan BEFORE build; stacked-rebase conflict front-loading; ff-only merge. Add the "build only on AUDITED-ready plan" gate. |
| "Anti-patterns" | **REWRITE** — drop sub-branch-specific items; add "❌ dispatching a car build before its plan is AUDITED-ready", "❌ independent-merge (`merge --no-ff`) of parallel car branches". |
| "Lessons from v4.9" | **KEEP** — historical rationale, still valid (maps pain → model). Light: note the model has since evolved to stacked-rebase (ADR-0107). |
| "Doc-update gate (before opening final PR)" | **KEEP** — still correct; canonical-doc list may need refresh but out of this task's scope. |
| "Branch cleanup" | **KEEP (light edit)** — rename branch var; matches HARD RULE Post-Merge Cleanup. |
| "Benchmarks (LoCoMo / LongMemEval)" | **KEEP** — unrelated to train mechanics. |

### 2. `docs/RELEASE.md` :110-112 — verbatim old→new (FILE edit)

`old` (line 110 heading + line 112 body):
```
## When using the long-lived feature-branch workflow

See `claude-workflow.md`. The final integration PR (`feat/vX.Y → master`) ships the version bump in the same PR. The `no-release` label exists only for direct-to-master PRs that touch `yadgar/**` but legitimately do not ship a release (doc-only, test-only fixes that don't warrant a version bump).
```
`new`:
```
## When using the standard train workflow

See `claude-workflow.md` (canonical). A train = one `feat/<train>` branch, parallel cars integrated stacked-rebase ff-only, ONE PR (`feat/<train> → master`) that ships the version bump in the same PR (ADR-0088/0107). The `no-release` label exists only for direct-to-master PRs that touch `yadgar/**` but legitimately do not ship a release (doc-only, test-only fixes that don't warrant a version bump).
```

### 3. `model-tier-dispatch` Combos §1 — verbatim old→new (WIKI via `wiki_replace_text`)

`old_text` (exact, from wiki_read of id 6825):
```
1. **planner → independent-auditor gate** (two separate fresh-context dispatches). The auditor gets its own brief + the plan file, NOT the planner's transcript — fresh context is what makes the audit independent. Gate triggers on blast radius: mandatory for plans touching schema/migrations, data-destructive ops, public API/tool surface, or multi-seam architecture.
```
`new_text`:
```
1. **planner → independent-auditor gate** (two separate fresh-context dispatches). The auditor gets its own brief + the plan file, NOT the planner's transcript — fresh context is what makes the audit independent. For **every train car** (ADR-0107): the gate is **mandatory and unconditional** — no car build dispatches until its plan passes an independent adversarial audit (verdict BUILD / BUILD-WITH-CHANGES / DO-NOT-BUILD). The audit tier may drop to a lighter check for purely-mechanical cars, but is **never skipped**. For non-train one-off plans, the gate still triggers on blast radius: mandatory for plans touching schema/migrations, data-destructive ops, public API/tool surface, or multi-seam architecture.
```

Call: `wiki_replace_text(slug="model-tier-dispatch", old_text=<above>, new_text=<above>, occurrences=1, directory="/home/max/git/yadgar", branch_hint="master")`.

### 4a. NEW pattern `agent-prompt-car-plan-audit-gate` (WIKI via `agent_prompt_save`)

`agent_prompt_save(pattern="car-plan-audit-gate", purpose="Precondition gate: no train-car build dispatches until its plan is AUDITED-ready (ADR-0107)", content=<below>, directory="/home/max/git/yadgar", branch_hint="master")`.

Content:
```
DISPATCH: (orchestrator gate — not itself a dispatch) — canonical: model-tier-dispatch

## Purpose
The WHEN of the per-car audit gate (ADR-0107): under the standard train workflow, NO car build
is dispatched until that car's plan doc has passed an independent adversarial audit and carries an
AUDITED-ready status. Unconditional — every car, regardless of blast radius.

## Gate (orchestrator checklist, before ANY car build dispatch)
1. Car has its own plan doc (docs/plans/<car>-<date>.md, or a per-car section of the train plan).
2. That plan has been audited by an INDEPENDENT fresh-context dispatch — the HOW is
   [[agent-prompt-dispatch-plan-audit]] (writes the `## AUDIT` section + Status line IN PLACE) or
   [[agent-prompt-plan-audit]] (returns the BUILD / BUILD-WITH-CHANGES / DO-NOT-BUILD verdict).
3. Verdict is BUILD or BUILD-WITH-CHANGES (changes folded into the plan) — DO-NOT-BUILD blocks.
4. Only then dispatch the build via [[agent-prompt-plan-executing-build]] or
   [[agent-prompt-stacked-car-parallel-build]].

The audit tier may drop to a lighter check for purely-mechanical cars, but is NEVER skipped
(ADR-0107; mirrored in model-tier-dispatch Combos §1).

## Composes
- [[agent-prompt-dispatch-plan-audit]]
- [[agent-prompt-plan-audit]]
```

### 4b. `agent-prompt-plan-executing-build` — one-line precondition insert (WIKI)

Insert a precondition line at the top of the "## Prompt block (prepend to build dispatches)" body,
before "PLAN LIFECYCLE:". Verbatim line to add:
```
PRECONDITION (ADR-0107): this plan MUST already be AUDITED-ready before this dispatch — see [[agent-prompt-car-plan-audit-gate]]. Do not build an un-audited plan.
```
Executed by re-saving the page via `agent_prompt_save(pattern="plan-executing-build", content=<full updated body>, ...)` — `agent_prompt_save` is upsert/versioned, so re-save the whole content with the line inserted (safer than a surgical wiki_replace on a pattern page).

### 4c. `agent-prompt-stacked-car-parallel-build` — one-line precondition insert (WIKI)

Same treatment. Verbatim line to add at the top of the prompt body (before "You are in an isolated worktree."):
```
PRECONDITION (ADR-0107): your car's plan MUST be AUDITED-ready before this dispatch — see [[agent-prompt-car-plan-audit-gate]]. An un-audited car does not get built.
```
Re-save via `agent_prompt_save(pattern="stacked-car-parallel-build", content=<full updated body>, ...)`.

### 5. `docs/ARCHITECTURE_INVARIANTS.md` — NON-GOAL

I1-I34 are lint-enforced code properties (import-linter 4-contract boundary + code-review; header
confirms "docs-only" additions all map to code-shape rules). A git-process rule (how trains
integrate, when cars are audited) is a category mismatch — it constrains human/agent workflow,
not code structure, and nothing lints it. ADR-0107 states this explicitly. No edit.

---

## File-vs-wiki split — how wiki writes fit a code PR

**Key fact (the crux):** wiki content lives in the yadgar SurrealDB store, NOT in the git tree.
Wiki writes therefore CANNOT appear in a PR diff and are NOT committed to the repo. Two distinct
channels:

| Channel | Targets | Mechanism | Lands where | Verified how |
|---------|---------|-----------|-------------|--------------|
| **FILE** | #1 claude-workflow.md, #2 RELEASE.md | `Edit`/`Write` in a worktree, committed | The car's PR diff (docs-only) | `git diff`, PR review |
| **WIKI** | #3 model-tier-dispatch, #4a/b/c agent-prompt pages | `wiki_replace_text` (#3) + `agent_prompt_save` (#4a/b/c) MCP calls | Out-of-tree (SurrealDB); no commit, no diff | read-back via `wiki_read` / `recall(type="wiki", tags=["agent-prompt"])` |

**How the wiki writes fit the train:** the FILE edits form ONE docs-only car in the train PR
(or a standalone docs PR if no train is in flight). The WIKI writes are executed as MCP tool
calls **in the same work session** that lands the PR — either by the building agent (agents may
call `wiki_add`/`agent_prompt_save` directly per project rules) or as a main-thread step right
after merge. Recommended sequencing: **wiki writes AFTER the FILE-edit PR merges** — so the
codified docs and the codified wiki reflect the same landed decision, and a rejected/revised PR
doesn't leave the wiki ahead of the tree. They are never blocked ON the PR (no diff dependency),
but coherence is best if they land together in the session.

Acceptance for the wiki channel is a **read-back**, not a diff (see below).

---

## Acceptance criteria

Docs (FILE) consistent:
- [ ] `claude-workflow.md` contains NO `merge --no-ff`, NO numbered-sub-branch (`feat/vX.Y/NN`) refs; uses train/car + stacked-rebase ff-only + unconditional per-car audit gate + one PR/one version vocab.
- [ ] `claude-workflow.md` "Anti-patterns" lists independent-merge and un-audited-car-build as anti-patterns.
- [ ] `RELEASE.md:110-112` heading + body updated to train vocab; pointer to `claude-workflow.md` retained as canonical.
- [ ] `grep -rn "merge --no-ff\|feat/vX.Y/NN" docs/claude-workflow.md docs/RELEASE.md` → zero hits.

Wiki (read-back):
- [ ] `wiki_read("model-tier-dispatch")` Combos §1 shows the mandatory-every-car clause AND retains the blast-radius clause for non-train plans.
- [ ] `recall(type="wiki", tags=["agent-prompt"])` surfaces `agent-prompt-car-plan-audit-gate`; it composes `dispatch-plan-audit` + `plan-audit`.
- [ ] `agent-prompt-plan-executing-build` and `agent-prompt-stacked-car-parallel-build` each carry the PRECONDITION (ADR-0107) line pointing at `car-plan-audit-gate`.

Reachability:
- [ ] The audit gate is reachable from a builder's dispatch: build patterns → PRECONDITION line → `car-plan-audit-gate` → auditor patterns.

No stale independent-merge refs remain **in canonical docs** (see Scope OUT for historical stragglers).

---

## Scope

**IN:**
- Rewrite `docs/claude-workflow.md`; fix `docs/RELEASE.md:110-112`.
- Amend `model-tier-dispatch` Combos §1.
- Create `agent-prompt-car-plan-audit-gate`; add precondition lines to `plan-executing-build` + `stacked-car-parallel-build`.

**OUT:**
- `docs/ARCHITECTURE_INVARIANTS.md` (explicit non-goal, category mismatch).
- Historical `merge --no-ff` / sub-branch references in `docs/CHANGELOG.md` and archived
  `docs/plans/archive/PLAN_V5_2.md` — these are historical record, NOT canonical workflow docs;
  leaving them is correct. Acceptance is scoped to canonical docs only.
- The `claude-workflow.md` "Doc-update gate" canonical-doc list refresh (separate concern).
- Any code / lint / test changes.

---

## Version impact

- **FILE edits are docs-only** → NO version bump. If they ride an in-flight train PR, the train's
  claimed version stands unchanged (docs don't force a bump). If shipped standalone, apply the
  `no-release` label (per RELEASE.md convention) — direct-to-master doc-only PR, no release.
- **WIKI writes are out-of-tree** → no version semantics at all; not part of any release artifact.

---

## Open questions

1. **Combos §1 scope (resolved reading, confirm at audit):** ADR-0107 decision (3) mandates
   unconditional per-car audit "regardless of blast radius"; mission text says "mandatory for EVERY
   train car... never skipped". Reading adopted here: **keep** the blast-radius clause for non-train
   one-off plans, **add** the unconditional-for-every-car rule. Alternative (full replacement of the
   blast-radius clause) would over-reach onto non-train one-off plans. Confirm the intended split at audit.
2. **Which auditor pattern the new gate composes.** The "AUDITED — ready" *Status-line* mechanism
   lives in `agent-prompt-dispatch-plan-audit` (id 6827, writes `## AUDIT` + Status in place); the
   BUILD/BUILD-WITH-CHANGES/DO-NOT-BUILD *verdict* form is `agent-prompt-plan-audit` (id 6787). The
   gate composes BOTH (6827 produces the AUDITED-ready status the gate checks; 6787 is the verdict
   contract). Confirm this is the intended composition, or narrow to one.
3. **Wiki-write executor + timing.** Building agent vs main-thread step; before/with/after PR merge.
   This plan recommends after-merge, same session. Confirm.
4. **Precondition-insert mechanism for #4b/#4c.** Recommended full-content re-save via
   `agent_prompt_save` (upsert, versioned) rather than surgical `wiki_replace_text`, to avoid a
   fragile anchor match on a long pattern-page body. Confirm acceptable.

---

## AUDIT (2026-07-13)

Independent adversarial audit. Verified against the live git tree and the yadgar wiki store as of this date. **Status: AUDITED — ready.** Every load-bearing claim checks out; the file-vs-wiki split is sound; the four open questions are all resolvable from evidence already in hand (answered below, not punted).

### Verification table (load-bearing claims)

| # | Claim | Verdict | Evidence |
|---|-------|---------|----------|
| A | `claude-workflow.md` is the STALE model (merge --no-ff, numbered sub-branches, long-lived integration branch) | **VERIFIED** | `docs/claude-workflow.md` (247 lines): title `# Yadgar v5 Integration Model` (L1), `long-lived integration branch` (L26), `feat/vX.Y/NN-<topic>` (L62/73/76), `git merge --no-ff origin/feat/vX.Y/NN-<topic>` (L79). Genuinely the retired model. (Plan says 248 lines; actual 247 — off by one, immaterial.) |
| B | `model-tier-dispatch` Combos §1 captured `old_text` matches live wiki | **VERIFIED verbatim** | `wiki_read(model-tier-dispatch)` id 6825, Combos §1 body is byte-identical to the plan's `old_text`. The `wiki_replace_text(occurrences=1)` will match. |
| C | `RELEASE.md:110-112` old text + pointer-stays framing | **VERIFIED** | `sed -n '108,114p'` matches the captured `old` block exactly; pointer to `claude-workflow.md` is correct (it stays canonical); only vocab is stale. |
| D | `agent-prompt-car-plan-audit-gate` does NOT already exist | **VERIFIED — no dup** | Tagged recall `type=wiki tags=[agent-prompt]` (20 results) surfaced no `car-plan-audit-gate`. Closest are `plan-audit` (6787, verdict form) and `dispatch-plan-audit` (6827, in-place AUDITED-status form) — the two the new gate *composes*, not dups. |
| E | Composed/target patterns exist as cited | **VERIFIED** | `plan-audit` id 6787 ✓, `dispatch-plan-audit` id 6827 ✓, `plan-executing-build` id 6814 ✓ (no precondition gate present — confirmed), `stacked-car-parallel-build` id 6815 ✓ (no precondition gate; body opens `"You are in an isolated worktree."` — matches the §4c insert anchor). |
| F | ADR-0107 recap is faithful | **VERIFIED** | ADR-0107 read from `yadgar-adr-log` wiki: status accepted, date 2026-07-13, decisions (1)-(4) match the plan's recap word-for-word; alternatives explicitly reject blast-radius-conditional *for trains* and independent-merge. |
| G | ARCHITECTURE_INVARIANTS non-goal (category mismatch) | **VERIFIED** | `docs/ARCHITECTURE_INVARIANTS.md` is I1-I34 lint/import-linter-enforced code properties; a git-process rule is not lintable. ADR-0107 alternatives echo this. Non-goal correct. |

### Answers to the plan's open questions (resolved, not punted)

- **OQ #1 (Combos §1 scope):** **RESOLVED — the plan's split reading is faithful.** ADR-0107's `alternatives` field rejects blast-radius-conditional audit *"for trains"* and is **silent on non-train one-off plans**. So: keep the blast-radius clause for non-train one-offs, ADD unconditional-for-every-train-car. Full replacement would over-reach onto non-train plans the ADR never spoke to. Adopt the split. Not a user decision.
- **OQ #2 (which auditor the gate composes):** **RESOLVED — compose BOTH, as written.** `dispatch-plan-audit` (6827) produces the `## AUDIT` + `Status: AUDITED — ready` line the gate *checks*; `plan-audit` (6787) is the BUILD/BUILD-WITH-CHANGES/DO-NOT-BUILD *verdict* contract. They are complementary (status-mechanism vs verdict-form), not redundant. Keep both `[[…]]` links.
- **OQ #6 (should a docs ADR file also be written?):** **RESOLVED — NO, non-goal correct, no gap.** ADRs are wiki-native (`yadgar-adr-log` via the `adr_add` tool); there is **no `docs/adr/` convention** in the tree (confirmed: no ADR docs file exists, only wiki + `docs/DECISIONS.md` narrative). Writing a docs ADR would invent a convention that doesn't exist. ADR-0107 living in the wiki is the correct and only authority. Close it.

### File-vs-wiki split + wiki-write timing (the mission's explicit ask)

The split is **sound**: wiki content lives in SurrealDB, not the git tree, so wiki writes genuinely cannot appear in a PR diff — the plan's crux fact is correct. Recommended **after-merge, same session** is defensible: it prevents the wiki running ahead of a rejected/revised PR.

**Brutal-honesty caveat the plan under-weights:** after-merge has an *invisible-debt* failure mode. Nothing in the PR diff, and nothing in the git tree, tracks the pending wiki writes — so a session that dies (crash, overload, `/clear`) between merge and the wiki writes leaves **merged docs + stale wiki with no reminder anywhere**, and the read-back acceptance criteria never get run. **Recommendation: keep after-merge/same-session, but add a durable tracking anchor** (e.g. `checkpoint`/`update_active_work`, or a `MIGRATION_NOTES`-style one-liner) enumerating the 4 wiki writes as a pending checklist, cleared only when the read-back acceptance passes. That closes the silent-drop hole without moving the timing. This is the one substantive addition; it is not a blocker.

### Minor notes (non-blocking)

- Line count of `claude-workflow.md` is 247, not 248 (plan §1). Immaterial to the rewrite.
- §4b/§4c full-content re-save via `agent_prompt_save` (over surgical `wiki_replace_text`) is the right call — `agent_prompt_save` is upsert/versioned and a long pattern-page body is a fragile anchor target. Endorsed (OQ #4 resolved: acceptable).
- Acceptance criterion `grep -rn "merge --no-ff\|feat/vX.Y/NN" docs/claude-workflow.md docs/RELEASE.md → zero hits` is a good machine-checkable gate. Confirm `RELEASE.md` currently has zero such hits already (its stale content is vocab-only, no `merge --no-ff`), so the gate keys on `claude-workflow.md`.
- Scope-OUT correctly leaves historical `merge --no-ff` refs in `CHANGELOG.md` / archived plans — those are record, not canonical workflow. Correct.

### Status

**AUDITED — ready.** All 7 load-bearing claims VERIFIED; the new pattern does not dup; ADR-0107 recap faithful; non-goal correct. Buildable as written.

**User-decision items:**
1. Confirm the after-merge wiki-write timing PLUS the added **durable pending-write tracking anchor** (audit's one substantive add — closes the invisible-debt hole). This is the only real decision.
2. (Informational — already resolved by audit, no action) OQ #1/#2/#6 are answered above; the plan's readings hold. OQ #3 timing folds into decision 1; OQ #4 (re-save mechanism) endorsed.
