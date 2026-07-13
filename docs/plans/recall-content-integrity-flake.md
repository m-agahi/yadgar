# PLAN — stabilize recall content-integrity flake (#12)

Status: AUDITED-ready
Created 2026-06-25 (improvement-train #29, group C). Refreshed 2026-07-13 to
current reality + ADR-0108 (status: open, 2026-07-13); reworked 2026-07-13 after
audit. Theme: recall / retrieval / test-flake.
Priority: medium (an intermittently-failing behavior test is a blind spot in the
recall contract and a CI-noise source).

## BLUF (current state)

The behavior test `test_specific_detail_preserved` is **LIVE (unskipped)** — it
was un-skipped in #124 (`6e1629cb`). It fails **intermittently**, not always. The
failure is a **retrieval RANKING miss**, not content truncation: the target
memory does not reliably land in recall's default top-5 for the query. Content
passthrough is verified clean end-to-end.

The fix is **ADR-0108 (status: open, 2026-07-13): option A = deterministic
tie-break** in fusion ordering, **behind a diagnose-first stop-gate**. The
tie-break direction is now locked: **recency-wins — `(score, id)` descending,
i.e. the higher (newer) memory id wins a fused-score tie.** The builder MUST
diagnose per-signal scores before shipping — A only closes the flake if the
cause is an **exact fused-score tie** near the cutoff. If it is a near-tie score
straddle or a hard miss, A is a no-op and the car STOPS and escalates. Do not
ship a no-op.

## Root cause (verified 2026-07-13)

The test at `yadgar/tests/core/test_memory_behavior.py:87-98`
(`TestContentIntegrity::test_specific_detail_preserved`) stores a memory and
queries for it:
```python
content = "Codeberg PAT is stored in 1Password item zqq55bz2qi53gw375jlm2sh4jq"
result = memorize_sync(content, "/home/user", ["codeberg", "secrets"])
mid = result["id"]
hits = server.recall("codeberg personal access token", directory="/home/user")
match = next((h for h in hits if h["id"] == mid), None)
assert match is not None                                    # ← THE FAILING ASSERT (line 95)
assert "zqq55bz2qi53gw375jlm2sh4jq" in match["content"]     # (content check, not reached on failure)
```

- The failure is at `assert match is not None` (line 95): the stored memory does
  **not surface in recall's top-k** for this query. The content-preservation
  assert below it (line 96) is never reached — so "content integrity flake" is a
  **misnomer**; it is a **ranking** flake.
- **Content integrity itself is fine** — proven by the sibling non-flaky
  `test_exact_content_preserved` (same class, line 73): the recall path returns
  `content` verbatim. 62-char content is far below any truncation threshold.
- **Why it misses (abbreviation gap):** the stored text uses the abbreviation
  "PAT"; the query expands it to "personal access token". Those share **zero FTS
  word-overlap** → BM25 contribution ≈ 0. The fused score (vector + BM25 + CE
  rerank) does not reliably rank the target into the default top-5
  (`max_results=5`), and tie ordering / embedding nondeterminism can shift it just
  under the cutoff run-to-run → the intermittent CI failures.
- **The confirmed nondeterminism source (verified):** at `_convex_fuse` the
  `combined` dict is built by iterating `all_mids`, a **`set`**
  (`fusion.py:106-111`). Set iteration order over ints is not insertion-stable,
  so equal-score rows enter `sorted()` in a run-varying order and Python's stable
  sort preserves that varying order. Exact fused-score ties are therefore
  resolved by nondeterministic input order — precisely the case a stable
  secondary key fixes.

## Fix — ADR-0108 option A (deterministic tie-break) + diagnose-first gate

**Decision (ADR-0108):** fix with a **stable secondary sort by memory `id`** in
fusion ordering so that exact score ties resolve deterministically instead of
depending on set/dict iteration order.

### The two REAL sort sites (mandatory — the live default path)

Default fusion is **CONVEX, not WRRF** (`config.py:278` → `FUSION_METHOD =
"convex"`), so the WRRF sites are inactive by default. The true final ordering
the test sees is:

1. **`fusion.py:115`** — module-level `_convex_fuse`,
   `return sorted(combined.items(), key=lambda x: x[1], reverse=True)`. The
   default fuse.
2. **`fusion.py:199`** — `_apply_prior_boost`,
   `return sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)`. Prior
   boosts are **ON by default** (`WRRF_GRAPH_PRIOR_WEIGHT=0.2`,
   `WRRF_COFIRE_PRIOR_WEIGHT=0.15`), so this is the **last sort to run** and it
   determines final top-5 membership.

**Both 115 AND 199 must get the tie-break.** Fixing 115 alone lets the
prior-boost re-sort at 199 re-introduce nondeterministic ties. These two are
mandatory and on the live default path.

The sorted element at both sites is `tuple(memory_id:int, score:float)`, so the
DB id is directly reachable as `x[0]`.

**The tie-break expression (direction locked — recency-wins):**
```python
sorted(items, key=lambda x: (x[1], x[0]), reverse=True)
```
`x[1]` = fused score, `x[0]` = memory id. `reverse=True` makes it score-descending
then **id-descending**: on an exact score tie, the **higher (newer) id wins**.

**Direction rationale (policy, not fixture-overfit):** recall favors recency —
when two memories are equally relevant by fused score, the **newer** memory is
the better answer (more likely current, less likely superseded). This is a
defensible global ordering policy for a recency-aware memory system; it is stated
as the policy **first**, and the fact that it greens this particular fixture (the
target is a freshly-`memorize_sync`'d highest-id row) is a **consequence**, not
the reason. The alternative — lowest-id-wins (`(score, -id)`) — would leave the
test deterministically RED and force escalation to B; it is rejected on policy
grounds (stale-wins-ties is wrong for recall), not because it fails the fixture.

**Safety invariant (explicit):** a strict secondary key only reorders rows with
**identical** primary fused scores. Strictly-ordered rows are unaffected —
lexicographic tuple compare on `(score, id)` never changes the relative order of
two rows whose scores already differ. So **A cannot change ranking for any
non-tied item** and cannot regress a benchmark: it touches ordering only, zero
scoring-weight change. **No LongMemEval run is needed for A** (nor for B).

### Non-blocking hygiene sites (apply the same tie-break, do not block on them)

The audit enumerated four more score-only sorts. They are inactive on the default
path but should get the same secondary key for consistency (hygiene, non-blocking):

- `fusion.py:76` — module-level `_wrrf_fuse` (dead under convex default).
- `fusion.py:190` — class-method `_wrrf_fuse` on `_FusionMixin` (dead under convex default).
- `fusion.py:275-279` — `_inject_ce_diversity`, score-only.
- `providers/fusion.py:272` — `fuse_candidates`, in-place `.sort()`; element is
  `(Candidate, score)` → tie-break on `x[0].id`.

Do not silently drop these — they are named so a later blind sweep does not
reintroduce a nondeterministic sort. They are hygiene, not the fix.

### The diagnose-first stop-gate (non-negotiable — ADR-0108)

Before writing the fix, the builder DIAGNOSES the target's per-signal scores —
vector cosine, BM25/FTS, CE rerank — against the top-5 rows that beat it, over a
**repeat loop** (not one pass). There are **three** cases, and the gate must
distinguish (a) from (b) by **exact float equality**:

- **(a) exact fused-score tie** — target's fused score is **equal to full float
  precision** to the row it flips with at the top-5 boundary; ordering flips
  run-to-run only because of set-iteration order → **A fixes it → ship.**
- **(b) near-tie score straddle** — embedding/CE nondeterminism yields scores
  that are *close but not equal*, straddling the cutoff run-to-run. A secondary
  key on `(score, id)` **never activates** (scores are unequal) → **A is a
  NO-OP.** This presents identically to (a) as "intermittent flip near the
  cutoff," so the gate MUST check equality, not proximity. → **STOP, escalate to
  B. Do NOT ship.**
- **(c) hard miss** — target genuinely scored *below* the top-5 cutoff, not a tie
  → A no-op → **STOP, escalate.**

**The gate's classifier is exact-float-equality at the top-5 boundary**, run over
the repeat loop and inspecting equality (not "tied/near"). Per-signal scores are
surfaced in the recall row (`_retrieval_score`, `_cross_encoder_score`, etc.), so
this is mechanically executable. The investigation leaned toward hard-miss /
near-tie (FTS structurally cannot bridge abbreviation↔expansion), so A may not
suffice — that outcome must surface **before** ship, not after.

## Escalation ladder (A → B → C)

1. **A — deterministic tie-break** (this plan's primary). Ordering-only,
   recency-wins `(score, id)` desc, at sites 115 + 199. No benchmark. Ships **iff**
   diagnosis shows an **exact fused-score tie** at the boundary (case a).
2. **B — test-realism fix** (if A is a no-op: case b or c). Make the test assert
   what recall actually guarantees: query with an overlapping-term phrase, or
   assert top-k membership with a larger `k`. Arguably the more honest fix —
   abbreviation↔expansion is a job for the semantic layer, not FTS. Still no
   scoring-weight change → no benchmark. **If diagnosis lands on case (b), the
   escalation-to-B decision is pre-approved (D2 below) so the car does not stall.**
3. **C — recall re-weight / CE-pool change** (last resort). Adjust fusion weights
   or `CROSS_ENCODER_TOP_K`. **Rejected for now**: regression risk against the
   LongMemEval recall@k ~0.868 baseline. If ever taken: I25 three-way for any
   changed default, and `make longmemeval` recall@k MUST NOT regress
   (run before/after; a test that passes by degrading the benchmark is worse than
   the flake).

## Acceptance criteria [unit]

- `test_specific_detail_preserved` passes **deterministically** — the target
  memory (`mid`) lands in recall's top-5 for the query on every run (repeat-loop
  clean, e.g. `pytest -p no:randomly --count=20` or equivalent).
- **Regression test (mandatory for A):** a new test pins the tie-break
  **direction** on synthetic **equal-fused-score** rows — assert that with two
  rows of identical fused score the **higher id sorts first** at both `_convex_fuse`
  and `_apply_prior_boost`. This encodes the recency-wins policy so a later change
  cannot silently flip it, and it proves the safety invariant (non-tied rows
  unaffected) by including a strictly-ordered pair that must not reorder.
- This unit test (target-in-top-5) is the **pre-deploy tripwire**: it must be
  green under repeat before ship.
- Recall BCs (BC-U* unified recall) stay green — this test is an outcome-level
  guard adjacent to them.
- A touches ordering only — verify the diff changes no scoring weight/config.

## Test plan

- The test IS the failing test — drive it green under the chosen fix, verified via
  a seed/repeat loop (not a single pass) to prove determinism, not luck.
- A: the synthetic equal-score regression test (above) pins direction +
  invariant; the behavior test proves top-5 membership is stable across runs.
- B (if escalated): the reworked query/k becomes the new assertion; keep the
  content-fidelity assert (`zqq55bz2qi53gw375jlm2sh4jq in content`) intact.
- C (if escalated): `make longmemeval` recall@k before/after; gate on no regression.

## #57 remeasure linkage

ADR-0108 pairs the pre-deploy unit tripwire with a **post-deploy remeasure
(task #57)**: after ship, remeasure recall on the abbreviation-class query to
confirm the fix holds in the deployed daemon, not just in the unit harness. The
unit test proves ordering is deterministic; #57 confirms real-world recall
behavior. A hard-miss / near-tie diagnosis (A insufficient) is exactly what #57
would catch post-deploy if it slipped past the gate — so the gate is the primary
defense and #57 is the backstop.

## Scope

- **In:** the fusion ordering tie-break (A) at the two real sites
  `yadgar/backend/retrieval/fusion.py:115` (`_convex_fuse`) + `:199`
  (`_apply_prior_boost`), recency-wins `(score, id)` desc; the same tie-break at
  the four non-blocking hygiene sites; the diagnosis pass; the synthetic-tie
  regression test; stabilize the behavior test. If escalated: the test-realism
  rewrite (B) or a benchmark-gated re-weight (C).
- **Out:** any scoring-weight change for A/B; broad recall-quality rework; the
  semantic-abbreviation-expansion feature (a real fix for abbreviation recall is a
  separate, larger effort — this plan stabilizes the test, it does not teach
  recall to expand abbreviations). Editing ADR-0108 (a wiki page) — the direction
  decision should be folded into the ADR later, but that is out of this plan's
  file scope.

## Version impact

- Core-only change (`yadgar/backend/retrieval/fusion.py` + test).
- **A and B: no benchmark** (ordering-only / test-only, zero scoring-weight
  change) → no LongMemEval gate.
- **C: benchmark-gated** (`make longmemeval` recall@k, ~0.868 baseline) if ever
  taken.

## Related

- ADR-0108 (`yadgar-adr-log` wiki, status: open, 2026-07-13) — the decision this
  plan encodes. Fold the recency-wins direction into the ADR later (out of this
  plan's file scope).
- #124 (`6e1629cb`) — the commit that un-skipped the test.
- #57 — post-deploy recall remeasure (the backstop paired with this fix).
- `yadgar/tests/core/test_memory_behavior.py:87-98` (the test) + sibling
  `test_exact_content_preserved:73`.
- `yadgar/backend/retrieval/fusion.py:115` + `:199` (the two real sort sites),
  `config.py:278` (`FUSION_METHOD="convex"`), CE knobs (`CROSS_ENCODER_TOP_K`) —
  only relevant if escalated to C.
- benchmark-runbook wiki (`make longmemeval`).

---

## AUDIT (2026-07-13)

**Status: NEEDS-REWORK (small).** The plan is directionally sound and the fix
location is on the live path — but it fixes the **wrong-labeled sort**, leaves
the tie-break **direction unspecified** (which decides pass/fail), and its
diagnose-first gate has a **hole that can green a no-op**. All fixable with a
targeted refresh; the core approach (deterministic tie-break + diagnose gate)
survives.

### Verified findings (file:line, read on master)

**The named sort sites exist, but the plan mis-prioritizes them.**

- `fusion.py:76` — module-level `_wrrf_fuse`, score-only. ✅ exists.
- `fusion.py:115` — module-level `_convex_fuse`, score-only. ✅ exists.
- `fusion.py:190` — class method `_wrrf_fuse` on `_FusionMixin`, score-only. ✅ exists.
- `fusion.py:199` — `_apply_prior_boost`, score-only. ✅ exists.
- Plus TWO the plan did not list: `fusion.py:275-279` (`_inject_ce_diversity`) and `providers/fusion.py:272` (`fuse_candidates`).

**Which sort is the real final ordering — the load-bearing answer:**

- **Default fusion is CONVEX, not WRRF.** `config.py:278` → `FUSION_METHOD = "convex"`.
  **The WRRF sites (76, 190) are INACTIVE by default.**
- **The true final sort the test sees is `fusion.py:115`, then re-sorted by
  `_apply_prior_boost` at `fusion.py:199`.** Prior boosts are ON by default, so
  **line 199 is the last sort to run** and determines top-5 membership. **Line 115
  AND line 199 must both get the tie-break.**
- Corrected target set: **115 + 199 (mandatory, live default path); 76 + 190 +
  279 + providers/fusion.py:272 as hygiene, non-blocking.**

**Nondeterminism source confirmed, exact-tie-compatible.** `combined` is built by
iterating `all_mids`, a **`set`** (`fusion.py:106-111`) — run-varying order,
stable sort preserves it. ✅

**Stable secondary key reachable at every site** (`x[0]` = int id; `x[0].id` at
`providers/fusion.py:272`). ✅

**Test verified.** `test_memory_behavior.py:87-98`, assert at `:95`; both sibling
tests UNSKIPPED; the `recall_backend_bypass` fixture (conftest.py:1299) routes
`_forward_to_backend` → `_fanout_recall` in-process, so `fusion.py` IS under test. ✅

**ADR-0108 status "open"** correctly reflected. **#124 un-skip + #57 linkage** accurate. ✅

### Defects to fix before build — RESOLVED below

1. **Tie-break DIRECTION unspecified** (decides pass/fail): target is freshly
   `memorize_sync`'d → highest id. Must name direction AND justify it as policy,
   not fixture-fit; add a regression test pinning direction on synthetic
   equal-score rows.
2. **Diagnose gate conflates two failure modes; one is a silent no-op**: (a)
   exact ties → A fixes; (b) near-tie score straddle → A no-op but looks like
   jitter; (c) hard miss → STOP. Gate must require **exact fused-score equality at
   the boundary** over a repeat loop, not "tied/near."
3. **Safety invariant** ("strict secondary key only reorders identical-primary
   rows") — state explicitly.
4. **Fix must cover 115 AND 199** — enumerate both as mandatory.

### REWORK (2026-07-13): applied — RECENCY-WINS `(score, id)` desc (user decision D1)

All four defects resolved in the plan body above:

- **Defect 1 (direction):** locked to **recency-wins — `(score, id)` descending,
  higher/newer id wins ties.** Justified as a recall-recency *policy* (stated
  first; greening the fixture is a consequence, not the reason). Lowest-id-wins
  rejected on policy grounds. Mandatory synthetic-equal-score regression test
  added to acceptance, pinning both direction and the safety invariant.
- **Defect 2 (gate hole):** the diagnose-first gate now classifies by **exact
  float equality** at the top-5 boundary, over a repeat loop — case (a) exact tie
  → ship A; case (b) near-tie straddle → A is a no-op → STOP/escalate to B; case
  (c) hard miss → STOP. B is pre-approved for case (b) (D2) so the car does not
  stall.
- **Defect 3 (invariant):** stated explicitly — strict secondary key reorders
  only identical-primary-score rows; non-tied rows unaffected; zero
  scoring-weight change; no benchmark for A.
- **Defect 4 (sites):** the two REAL sites `fusion.py:115` + `:199` are enumerated
  as mandatory on the live convex default path; 76/190/279 + `providers/fusion.py:272`
  are called out as non-blocking hygiene (76/190 dead under convex default).

Kept: the A→B→C escalation ladder + #57 remeasure linkage. Version core-only, no
benchmark for A. ADR-0108 not edited (wiki page, out of file scope) — direction to
be folded in later. **Status → AUDITED-ready.**

### User decisions — RESOLVED

- **D1** (direction): **recency-wins / higher-id-first** — chosen as policy. ✅
- **D2** (case-b escalation): escalate to B pre-approved if diagnosis shows
  near-tie straddle. ✅
