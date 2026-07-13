# PLAN — stabilize recall content-integrity flake (#12)

Status: DRAFT — awaiting audit
Created 2026-06-25 (improvement-train #29, group C). Refreshed 2026-07-13 to
current reality + ADR-0108 (status: open, 2026-07-13). Theme: recall / retrieval / test-flake.
Priority: medium (an intermittently-failing behavior test is a blind spot in the
recall contract and a CI-noise source).

## BLUF (current state)

The behavior test `test_specific_detail_preserved` is **LIVE (unskipped)** — it
was un-skipped in #124 (`6e1629cb`). It fails **intermittently**, not always. The
failure is a **retrieval RANKING miss**, not content truncation: the target
memory does not reliably land in recall's default top-5 for the query. Content
passthrough is verified clean end-to-end.

The fix is already decided in **ADR-0108 (status: open, 2026-07-13): option A =
deterministic tie-break** in fusion ordering, **behind a diagnose-first
stop-gate**. The builder MUST diagnose per-signal scores before shipping — A only
closes the flake if the cause is jitter (target tied near the cutoff). If it is a
hard miss, A is a no-op and the car STOPS and escalates. Do not ship a no-op.

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

## Fix — ADR-0108 option A (deterministic tie-break) + diagnose-first gate

**Decision (ADR-0108):** fix with a **stable secondary sort** (e.g. by memory
`id`) in fusion ordering so that score ties resolve deterministically instead of
depending on dict/insertion order or embedding jitter.

Fusion score ordering currently sorts on the fused score **only** (no tie-break),
in `yadgar/backend/retrieval/fusion.py` — e.g. `_wrrf_fuse` (line 76), the
module-level `_convex_fuse` (line 115), and the class methods `_wrrf_fuse`
(line 190) / `_apply_prior_boost` (line 199), each `sorted(..., key=lambda x: x[1],
reverse=True)`. A is: add a deterministic secondary key (memory id) to these
orderings so equal-score rows sort stably.

**Rationale A is safe:** it touches ordering only — **no scoring weights change**,
so **no LongMemEval run is needed** for A (and for B). Only option C (re-weight /
CE-pool change) is benchmark-gated.

### The diagnose-first stop-gate (non-negotiable — ADR-0108)

Before writing the fix, the builder DIAGNOSES the target's per-signal scores —
vector cosine, BM25/FTS, CE rerank — against the top-5 rows that beat it. Then:

- **Jitter** (target tied at/near the top-5 cutoff, ordering flips run-to-run):
  A applies → deterministic tie-break greens the unit test → **ship**.
- **Hard miss** (target genuinely scored *below* the top-5 cutoff, not a tie):
  A is a **no-op** (reordering ties can't promote a row that isn't tied at the
  boundary) → **STOP and report**. Do **not** ship a no-op that leaves the flake
  live. Escalate to B, then C (ladder below).

The investigation leaned toward hard-miss (FTS structurally cannot bridge
abbreviation↔expansion; that needs the semantic layer), so A may not suffice —
that outcome must surface **before** ship, not after.

## Escalation ladder (A → B → C)

1. **A — deterministic tie-break** (this plan's primary). Ordering-only. No
   benchmark. Ships iff diagnosis shows jitter.
2. **B — test-realism fix** (if A is a no-op). Make the test assert what recall
   actually guarantees: query with an overlapping-term phrase, or assert top-k
   membership with a larger `k`. Arguably the more honest fix — abbreviation↔
   expansion is a job for the semantic layer, not FTS. Still no scoring-weight
   change → no benchmark.
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
- This unit test (target-in-top-5) is the **pre-deploy tripwire**: it must be
  green under repeat before ship.
- Recall BCs (BC-U* unified recall) stay green — this test is an outcome-level
  guard adjacent to them.
- If A is chosen: no scoring-weight/config change (verify diff touches ordering
  only). If C is ever chosen: LongMemEval recall@k not regressed.

## Test plan

- The test IS the failing test — drive it green under the chosen fix, verified via
  a seed/repeat loop (not a single pass) to prove determinism, not luck.
- A: assert the tie-break makes top-5 membership stable across runs. Add a small
  regression case pinning deterministic ordering on equal-score rows if the
  diagnosis points there.
- B (if escalated): the reworked query/k becomes the new assertion; keep the
  content-fidelity assert (`zqq55bz2qi53gw375jlm2sh4jq in content`) intact.
- C (if escalated): `make longmemeval` recall@k before/after; gate the change on
  no regression.

## #57 remeasure linkage

ADR-0108 pairs the pre-deploy unit tripwire with a **post-deploy remeasure
(task #57)**: after ship, remeasure recall on the abbreviation-class query to
confirm the fix holds in the deployed daemon, not just in the unit harness. The
unit test proves the ordering is deterministic; #57 confirms real-world recall
behavior. A hard-miss diagnosis (A insufficient) is exactly what #57 would catch
post-deploy if it slipped past the gate — so the gate is the primary defense and
#57 is the backstop.

## Scope

- **In:** the fusion ordering tie-break (A) in
  `yadgar/backend/retrieval/fusion.py`; the diagnosis pass; unskip/stabilize the
  unit test. If escalated: the test-realism rewrite (B) or a benchmark-gated
  re-weight (C).
- **Out:** any scoring-weight change for A/B; broad recall-quality rework; the
  semantic-abbreviation-expansion feature (a real fix for abbreviation recall is a
  separate, larger effort — this plan stabilizes the test, it does not teach
  recall to expand abbreviations).

## Version impact

- Core-only change (`yadgar/backend/retrieval/fusion.py` + test).
- **A and B: no benchmark** (ordering-only / test-only, zero scoring-weight
  change) → no LongMemEval gate.
- **C: benchmark-gated** (`make longmemeval` recall@k, ~0.868 baseline) if ever
  taken.

## Related

- ADR-0108 (`yadgar-adr-log` wiki, status: open, 2026-07-13) — the decision this
  plan encodes.
- #124 (`6e1629cb`) — the commit that un-skipped the test.
- #57 — post-deploy recall remeasure (the backstop paired with this fix).
- `yadgar/tests/core/test_memory_behavior.py:87-98` (the test) + sibling
  `test_exact_content_preserved:73`.
- `yadgar/backend/retrieval/fusion.py` (fusion ordering — tie-break site),
  `config.py` CE knobs (`CROSS_ENCODER_TOP_K`) — only relevant if escalated to C.
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

- `fusion.py:76` — module-level `_wrrf_fuse`, `sorted(scores.items(), key=lambda x: x[1], reverse=True)`, score-only. ✅ exists.
- `fusion.py:115` — module-level `_convex_fuse`, `sorted(combined.items(), key=lambda x: x[1], reverse=True)`, score-only. ✅ exists.
- `fusion.py:190` — class method `_wrrf_fuse` on `_FusionMixin`, score-only. ✅ exists (distinct from the module-level one — plan's "duplicate _wrrf_fuse" note is correct).
- `fusion.py:199` — `_apply_prior_boost` (static on `_FusionMixin`), score-only. ✅ exists.
- Plus TWO the plan did not list: `fusion.py:275-279` (`_inject_ce_diversity`, score-only) and `providers/fusion.py:272` (`fuse_candidates`, in-place `.sort()` on wiki placement, score-only).

**Which sort is the real final ordering — the load-bearing answer:**

- **Default fusion is CONVEX, not WRRF.** `config.py:278` → `FUSION_METHOD = "convex"`.
  `_fuse_scores()` dispatches to `_convex_fuse()` (module-level, **line 115**) under
  default config. **The WRRF sites (76, 190) are INACTIVE by default** — fixing
  them alone would not touch the test's ordering.
- **The true final sort the test sees is `fusion.py:115`, then re-sorted by
  `_apply_prior_boost` at `fusion.py:199`.** Prior boosts are ON by default
  (`WRRF_GRAPH_PRIOR_WEIGHT=0.2`, `WRRF_COFIRE_PRIOR_WEIGHT=0.15` per the
  investigator), so **line 199 is the last sort to run** and determines top-5
  membership. **Line 115 AND line 199 must both get the tie-break** — fixing 115
  but not 199 would let the prior-boost re-sort re-introduce nondeterministic ties.
- So the plan's "fix the RIGHT sort, not all blindly" instinct is correct, but it
  named 76/190 (dead) as if co-equal and under-emphasized 199. **Corrected target
  set: 115 + 199 (mandatory, on the live default path); 76 + 190 + 279 +
  providers/fusion.py:272 as hygiene, non-blocking.**

**Nondeterminism source is confirmed and is exact-tie-compatible.** At
`_convex_fuse` the `combined` dict is built by iterating `all_mids`, a **`set`**
(`fusion.py:106-111`) — set iteration order over ints is not insertion-stable, so
equal-score rows enter `sorted()` in a run-varying order and Python's stable sort
preserves that varying order. This is precisely the "exact ties resolved by
nondeterministic input order" case that a secondary key fixes. ✅ Good news for A.

**Stable secondary key is reachable at every site.** The sorted element is
`tuple(memory_id:int, score:float)` at all five `fusion.py` sites → `x[0]` is the
immutable DB id, directly usable as tie-break. At `providers/fusion.py:272` the
element is `(Candidate, score)` → `x[0].id`. No structural blocker. ✅

**Test verified.** `test_memory_behavior.py:87-98`, assert at `:95` is
`assert match is not None`; query/content/`max_results=5` as described; sibling
`test_exact_content_preserved:73`; **both UNSKIPPED** (no `@pytest.mark.skip`).
The tests take a `recall_backend_bypass` fixture (conftest.py:1299) that routes
`_forward_to_backend` → `_fanout_recall` in-process — so the fanout+fusion path
(and thus `fusion.py`) IS the code under test. ✅ The fix location is on the
tested path.

**ADR-0108 status "open" is correctly reflected** (verified in `yadgar-adr-log`
wiki: status open, 2026-07-13, decision = option A + diagnose-first gate). ✅
**#124 (`6e1629cb`) un-skip and #57 remeasure linkage** are accurate. ✅

### Defects to fix before build

1. **Tie-break DIRECTION is unspecified and decides pass/fail.** The plan says
   "e.g. by id" but never states ascending vs descending. The target memory is
   freshly `memorize_sync`'d → **highest id**. With `key=(score, id),
   reverse=True` the target **wins** ties (greens); with `key=(score, -id),
   reverse=True` it **loses** (test goes deterministically RED). The plan MUST
   name the direction AND justify it as a real policy (e.g. "recency wins ties"),
   not pick the direction because it happens to green this one test — that is
   overfitting a global ordering rule to a single fixture. State the chosen
   semantics in the ADR/plan and add a regression test that pins the direction on
   synthetic equal-score rows.

2. **The diagnose-first gate conflates two failure modes; one is a silent
   no-op.** The gate's binary is "jitter → ship A, hard-miss → STOP." But there
   are **three** cases, and the middle one is misfiled as shippable:
   - (a) **exact fused-score ties**, ordered by nondeterministic set/dict
     iteration → **A fixes** (this is the confirmed mechanism above).
   - (b) **near-tie score jitter**: embedding/CE nondeterminism yields scores that
     are *close but not equal*, straddling the top-5 cutoff run-to-run. A
     secondary key on `(score, id)` **never activates** because the scores are
     unequal → **A is a NO-OP**, yet this presents identically to (a) as
     "intermittent flip near the cutoff." The gate as written would call this
     "jitter" and **ship a no-op**.
   - (c) **hard miss**: target genuinely below cutoff → A no-op → STOP (gate
     handles this).
   **Fix:** the gate must require the diagnosis to confirm **exact fused-score
   equality at the boundary** (target score == the row it flips with, to full
   float precision), not merely "tied/near the cutoff." If scores are unequal, it
   is case (b) → A is a no-op → escalate to B, do NOT ship. Per-signal scores are
   surfaced in the recall row (`_retrieval_score`, `_cross_encoder_score`, etc.),
   so this exact-equality check is mechanically executable — but run it over a
   **repeat loop**, not one pass, and inspect equality, not proximity.

3. **Safety claim "secondary key can't change ranking for non-tied items" —
   holds, state it explicitly.** Adding `x[0]` as a strict secondary key only
   reorders rows with **identical** primary scores; strictly-ordered rows are
   unaffected (lexicographic tuple compare). True at every site. The plan asserts
   this implicitly ("touches ordering only") — make it an explicit invariant with
   the regression test in defect 1.

4. **Fix must cover 115 AND 199 (see above).** The plan's scope line
   ("the fusion ordering tie-break in fusion.py") is too vague given two sorts run
   in sequence on the default path. Enumerate 115 + 199 as mandatory.

### User decisions needed

- **D1:** Tie-break direction/semantics — "highest id (recency) wins ties" vs.
  "lowest id (stable/oldest) wins." Recency-wins greens the test; lowest-id is
  arguably the more conventional "stable sort" choice but leaves the test RED and
  forces escalation to B. Pick the *policy*, then let the test outcome follow —
  do not reverse-engineer the direction from the test.
- **D2:** If diagnosis lands on case (b) near-tie-straddle (plausible — the ADR
  itself leans hard-miss/abbreviation-gap), confirm the escalation-to-B decision
  up front so the car does not stall. B (test-realism: overlapping-term query or
  larger k) is arguably the honest fix and is also benchmark-free.
