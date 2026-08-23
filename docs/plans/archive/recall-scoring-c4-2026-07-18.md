# Recall scoring C4 — ranking-miss + wiki-KB SNR (SCOPING → PARTIALLY BUILT)

**Status:** DECISIONS LOCKED — C4.0 + C4.1 + C4.3 BUILT (train `feat/recall-scoring-c4`, v5.151.0); 1b-fix + S3 DEFERRED
**Date:** 2026-07-18
**Task:** #62 (improvement-train #29, car C4)
**Theme:** recall / retrieval quality. **Scope (original):** SCOPING ONLY — investigate → diagnose → options.
**Author note:** read-only investigation. Every code claim below cites `file:line` on branch `feat/bug-train`.

> **BUILD UPDATE (2026-07-18):** the C4 recall-scoring train shipped the three
> NON-scoring cars from §6 — C4.0 (tie-break), C4.1 (1b diagnostic), C4.3 (S1
> thin-content guard). The five §7 decisions are now LOCKED (see §7). 1b's real
> fix (§6 C4.2) and S3 (§6 C4.5) are DEFERRED. The C4.1 diagnostic VERDICT is
> **FAIL** → 1b is a genuine abbreviation hard-miss, parked (xfail, #62).

---

## 0. Reconciliations (read first — the task brief carries two stale premises)

Two things in the C4 assignment do not survive contact with the tree. Fix them before anyone builds:

1. **Task numbering could not be confirmed — resolve by NAME, not number.** The number "#34" is
   ambiguous across sources: `docs/plans/improvement-train.md:35` uses #34 for the *Prometheus
   scrape 2s→5s* item; ADR-0035 uses #34 for a *recall re-measure* task; the C4 brief uses #34 for
   the fusion tie-break. Task **#62** (this car) is absent from `improvement-train.md` entirely — so
   that file is *not* the authoritative tracker for these numbers (the real tracker is likely the
   GitHub issue list, `github.com/m-agahi/yadgar`, not local plan docs). **This doc does not
   stake a claim on the number.** The substantive foundation is the **fusion-tiebreak plan
   (ADR-0108 Option A)**, captured in `docs/plans/fusion-tiebreak-determinism-2026-07-14.md` — referred
   to by name throughout, never by number. Whatever #34 resolves to in the user's tracker, the
   sequencing recommendation in §4 is unaffected.

2. **The concern-1 wording inherits ADR-0108's stale "test intermittently fails" premise.**
   Per **ADR-0110** (accepted, empirical: 40/40 fresh-engine iterations), the test
   `test_specific_detail_preserved` **passes deterministically** and must NOT be described as
   quarantined/failing. It is ACTIVE, un-skipped (`test_memory_behavior.py:87-98`), single-candidate
   (stores ONE memory → nrows=1 → no ties possible → tie-break is a structural no-op *for this test*).
   The flake it once had was a session-scope fixture leak, fixed by `6aff1909` a month before the test
   was un-skipped (`#124`, `6e1629cb`). **The ranking miss is real only in multi-candidate / production.**

**Consequence of #2:** concern 1 is actually **two distinct root causes** that the single-candidate
test collapses into one. Keep them separate (see §1).

---

## 1. Concern 1 — the ranking miss (split into 1a + 1b)

### 1a — nondeterministic tie order (already scoped; NOT C4's to design)

- **Root cause:** fusion builds its candidate set by iterating a `set[int]`
  (`yadgar/backend/retrieval/fusion.py:106-108`: `all_mids: set[int]` … `all_mids.update(...)`),
  then every sort site uses a **score-only** key with no deterministic tiebreak:
  `fusion.py:76`, `:115`, `:190`, `:199`, `:275-279`, and `providers/fusion.py:272`
  (all `sorted(..., key=lambda x: x[1], reverse=True)` / `.sort(...)`, confirmed HEAD).
- **Effect:** equal fused-score rows land in run-varying order; a target tied at the top-N boundary
  crosses/falls below the cutoff nondeterministically.
- **Owner:** the fusion-tiebreak plan (ADR-0108 Option A) — add `(score, id)` desc at every sort site
  + a synthetic ≥2-equal-score regression test. **Small, ordering-only, no scoring-weight change → no
  LongMemEval run needed.** This is a **prerequisite** for C4, not part of it (see §4).

### 1b — abbreviation / specific-detail hard-miss (this IS the open C4 ranking concern)

- **Root cause hypothesis:** the fixture memory stores `"Codeberg PAT is stored in 1Password…"`; the
  query is `"codeberg personal access token"`. **"PAT" has zero FTS word-overlap with "personal access
  token"** (abbreviation ↔ expansion). No signal in the pipeline rewards abbreviation expansion:
  - FTS/BM25 is lexical (`storage.search_memories_fts`) — structurally cannot bridge PAT↔expansion.
  - vector cosine (`all-MiniLM-L6-v2`) *may* partially bridge it, but weakly for a 3-letter acronym.
  - CE rerank (GTE-ModernBERT) is the one layer that could — but only if the target is already in the
    CE candidate pool; if the fused pre-CE score buries it below the pool cutoff, CE never sees it.
- **Evidence it is a genuine hard-miss, not jitter:** ADR-0108 itself states option A (tie-break) "may
  not suffice" and flags hard-miss; the FTS-overlap gap is structural, not a boundary tie.
- **Why the current test can't catch it:** single-candidate (`test_memory_behavior.py:87-98`) → the
  target is the sole recall candidate → it always ranks #1 regardless of absolute score. **The gap is
  the absence of a multi-candidate ranking test**, not a wrong test.

---

## 2. Concern 2 — wiki/recall SNR (auto-gen noise out-ranks curated knowledge)

### Live evidence (this session's own recall output)

Querying recall about "recall fusion scoring", the top hits included:
- auto-abstracted memory **id 531839** — `"Recurring pattern across 7 observations: recall memory
  branch only memories new default none retrieval helper tests shipped"`, tags `[semantic,
  auto-abstracted]`, **heat 1.0, `_retrieval_score 5.25`, CE 0.68** — semantically near-empty, yet
  top-ranked.
- repo_wiki auto-gen page `mod-retrieval-fusion` (`_retrieval_score 0.409`) **out-ranked** the curated
  architecture page `yadgar-recall-pipeline-multi-signal` (0.407) and the relevant ADRs.

### Root-cause hypotheses (which signal lifts the noise)

- **H2-corpus (primary):** auto-abstracted memories are *created thin and match meta-queries by
  construction*. Creation: `cls_store/patterns.py:379` (`schema = f"Recurring pattern across
  {n} observations: {key_phrase}"`) → inserted at `cls_store/promotion.py:59-72` with tags
  `["semantic","auto-abstracted"]` and **`heat=0.8` seed** (higher than episodic). Because they are
  abstracted *from recall-related observations*, their token bag is dense with meta-tokens
  ("recall", "memory", "retrieval") → they **legitimately win vector+FTS** on any query *about the
  memory system itself*. This is a **corpus** problem: low-value pages that are real lexical matches.
- **H2-heat (secondary, weaker than it looks):** heat does **NOT** feed the fusion score — in
  `fusion.py` heat is only a **gate** (`fusion.py:284,325`: `mem["heat"] >= min_heat`), not a scoring
  term (confirmed: no provenance/store_type weighting anywhere in `fusion.py`; priors at
  `:193-256` are cofire/graph, not heat). So the `heat=0.8` seed's effect is *indirect*: it keeps
  auto-abstracted memories **above the min_heat floor** so they enter the pool — it does not multiply
  their rank. **Implication: a "down-weight heat" fix would not work; heat isn't in the score.**
- **H2-no-filter:** there is **no recall-side demote/filter** for auto-abstracted / derived / repo_wiki
  content anywhere in `yadgar/backend/retrieval/*.py` (grep clean). The v4.9 PR #60 guard
  (`_is_degenerate_auto_abstracted`, cls_store) only suppresses *degenerate* patterns
  ("frequently modified together"-class) at **write/consolidation time** — the semantically-thin-but-
  non-degenerate "Recurring pattern across N: recall memory branch…" survives that guard and reaches
  recall.
- **Derived tags** exist (`curation/strengthen.py:96,218`: `["derived","auto-generated"]`) but are not
  consulted at recall. Repo_wiki pages tag `["code-structure","module","pkg-*"]`
  (`core/repo_wiki/generator.py:175-190`) — note the `generated_by: repo_wiki` marker seen in page
  *content* is frontmatter the generator emits into the body, **not a queryable tag** — so a
  tag-based recall filter would need a new tag added at generation time.

**Net:** concern 2 is **more corpus-side than score-side.** The noise wins because it is a *real*
lexical/semantic match for meta-queries, floated into the pool by a generous heat seed and never
demoted. Score-side down-weighting is possible but fights the fact that these are legitimate matches.

---

## 3. Options menu (pros / cons per concern)

### Concern 1a (tie order) — NOT a menu; it is owned by ADR-0108 A. See §4 sequencing.

### Concern 1b (abbreviation hard-miss)

| Option | What | Pros | Cons |
|---|---|---|---|
| **B1 — add a multi-candidate ranking test (make the assertion realistic)** | New test: seed the PAT memory + N distractors, assert PAT beats distractors for the expansion query. Do NOT touch the existing passing test. | Honest; surfaces whether recall *actually* guarantees this today; zero prod risk; may reveal vector layer already bridges it (→ done). | If it fails, motivates B2/C but doesn't fix; "realistic k" could hide a real gap. |
| **B2 — query-side expansion** | Expand acronyms/abbreviations at query analysis (`analyze_query`) or index-side synonym injection so PAT↔"personal access token" overlap exists for FTS. | Targets the actual gap (lexical bridge); no fusion-weight change → lighter LongMemEval exposure. | Where do synonyms come from? Hand-list is brittle; generic acronym expansion is a research task; scope creep risk. |
| **C — recall re-weight / CE-pool change** | Raise vector weight, or widen the CE candidate pool so a low-FTS target still reaches CE (which *can* bridge PAT). | Directly addresses "buried below CE pool cutoff"; principled. | **Changes scoring → mandatory LongMemEval recall@k gate** (~0.868 baseline); pool-widening costs latency (CE ≈25% of recall wall, ADR-0105 supersedes ADR-0035's "CE ~70-90%" — the CE metric was dead since ADR-0078); **highest regression risk.** |

### Concern 2 (SNR)

| Option | What | Pros | Cons |
|---|---|---|---|
| **S1 — corpus-side: extend the write-time guard** | Broaden `_is_degenerate_auto_abstracted` (or add a min-information-content gate at `promotion.py:59-72`) so semantically-thin "Recurring pattern across N: <meta-tokens>" is never promoted; optionally back-prune existing ones. | Attacks root cause (thin content shouldn't exist); no recall-path change → no LongMemEval gate; reuses proven PR #60 machinery. | Defining "thin" is fuzzy; risk of suppressing a genuinely useful abstraction; back-prune is a one-off migration. |
| **S2 — corpus-side: lower the auto-abstracted heat seed** | Drop `heat=0.8` at `promotion.py` toward episodic level so thin patterns fall below `min_heat` and never enter the pool. | One-line; uses the existing gate rather than adding a filter. | Blunt — demotes ALL auto-abstracted, including good ones; heat also drives decay/consolidation elsewhere (verify blast radius before touching). |
| **S3 — score-side: provenance demote in fusion** | Add a `store_type`/tag-based penalty (e.g. auto-abstracted/derived → ×0.85) at fusion or a post-fuse soft-rule. | Recall-time, reversible, tunable; leaves corpus intact. | **Fights that these ARE real matches**; changes scoring → LongMemEval gate; needs a new queryable tag on repo_wiki pages first; risk of over-penalizing. |
| **S4 — recall-side hard filter (opt-in)** | Filter auto-gen/derived from recall unless the query is explicitly about them (or a flag is set). | Cleanest signal for user-facing recall. | Loses recall of legitimately-useful abstractions; "is the query about the memory system" is itself a classifier problem. |

---

## 4. Sequencing vs the fusion-tiebreak plan (ADR-0108 A)

**Recommendation: the fusion-tiebreak plan lands FIRST, as C4's foundation. C4 is neither a superset
nor disjoint from it.**

- **Primary discriminator (cheapest-first):** the tie-break (ADR-0108 A) is ordering-only, no
  scoring-weight change, **no LongMemEval run needed**, low-risk, and already scoped in
  `fusion-tiebreak-determinism-2026-07-14.md`. It is free and safe → it goes first on its own merits.
- **Secondary (measurement hygiene):** nondeterministic ordering bites only on exact score ties (not
  the whole ranking), but those ties are exactly what a boundary-sensitive recall@k measurement is most
  fragile to. Landing the tie-break first removes that jitter from the baseline before any measured
  re-weight (1b option C, concern-2 option S3) runs.
- After it lands: C4's non-scoring options (B1 test, S1/S2 corpus) can proceed in parallel; C4's
  scoring options (1b-C, S3) run against a now-stable baseline behind the LongMemEval gate.

---

## 5. GATES (mandatory)

- **G1 — LongMemEval recall@k regression check is mandatory before ANY re-weight.** Applies to
  1b-option-C AND concern-2 option S3 (and S2 if heat-gate blast radius touches recall). Baseline
  ~0.868 recall@k (ADR-0108). No scoring change ships without a green LongMemEval delta.
- **G2 — HARD USER-JUDGMENT GATE: DO NOT OVERFIT FUSION TO ONE SYNTHETIC FIXTURE.**
  The single PAT case is a *symptom*, not the objective. Tuning fusion weights until the one PAT
  test greens — while LongMemEval regresses or holds flat — is a **net loss** disguised as a fix.
  Any 1b scoring change must improve (or hold) the *aggregate* benchmark, not just the fixture.
  This gate is specifically about 1b-C and S3. **State explicitly at build time; a builder that greens
  the fixture but can't show aggregate parity must STOP and report.**
- **G3 — the tie-break lands and is confirmed deterministic (xdist + random-order) BEFORE any
  measured re-weight** (per §4).

---

## 6. Proposed build cars + follow-up tasks (for the user to create)

Ordered:

1. **Car C4.0 (foundation, pre-req):** ship the fusion-tiebreak plan (ADR-0108 A) — `(score,id)` desc
   at fusion.py:76/115/190/199/275-279 + providers/fusion.py:272, + synthetic equal-score regression
   test. No LongMemEval. *(This may be dispatched independently of C4 — it is the gate for everything
   below.)*
2. **Car C4.1 (diagnose 1b):** add a multi-candidate ranking test (option B1) that seeds PAT + N
   distractors and asserts rank. **Diagnose-first:** its pass/fail decides whether 1b is already
   handled (vector bridges it → close 1b) or genuinely open (→ C4.2).
3. **Car C4.2 (fix 1b, conditional on C4.1 failing):** choose B2 (query expansion) or C (re-weight /
   CE-pool). If C → LongMemEval-gated (G1+G2).
4. **Car C4.3 (SNR, corpus-side):** option S1 (extend write-time guard for thin auto-abstracted) +
   optional back-prune. No recall-path change.
5. **Car C4.4 (SNR, score-side, optional/deferred):** option S3 provenance demote — only if C4.3 leaves
   residual noise; requires adding a queryable `generated_by`/`auto` tag to repo_wiki generation first;
   LongMemEval-gated.

Follow-up housekeeping: add task #62 (and the tie-break) as rows in `docs/plans/improvement-train.md`;
decide ADR-0108 disposition (see §7).

---

## 7. USER DECISIONS — LOCKED (2026-07-18)

All five are settled; the C4 train (`feat/recall-scoring-c4`, v5.151.0) was built against them.

1. **ADR-0108 disposition → SUPERSEDE.** ADR-0108 is superseded by a NEW C4 ADR (recorded at ship)
   that captures the 1a/1b split + the built cars. (ADR add is flagged for the main thread to run
   post-merge — the train did not `adr_add` mid-build.)
2. **Concern-1 priority → 1b is DIAGNOSED, its FIX is DEFERRED.** C4.1 adds the multi-candidate
   ranking test (option B1) as a DIAGNOSTIC only. VERDICT: **FAIL** — the "Codeberg PAT" memory ranked
   below 5 distractors for the expansion query, a genuine abbreviation hard-miss. Its real fix
   (semantic abbreviation bridging) is research-sized and PARKED (#62); the test is `xfail(strict=True)`.
   Fusion was deliberately NOT overfit to green it (gate G2).
3. **SNR strategy → CORPUS-SIDE S1 only.** C4.3 broadens the write-time guard (new
   `_is_thin_auto_abstracted`) so meta-token-dense auto-abstracted schemas are not promoted. S3
   (score-side provenance demote) is DEFERRED to task #65. Back-prune of existing thin rows is OUT of
   scope (write-time guard only; no migration this train).
4. **Re-weight appetite → NO re-weighting this train, NO LongMemEval run.** C4 stayed entirely to
   non-scoring changes (tie-break + corpus guard + diagnostic test). No fusion weights changed, so the
   G1 LongMemEval gate did not need to run.
5. **Tie-break direction → `(score, id)` DESC (newer-wins-ties) CONFIRMED.** Built into C4.0 across all
   6 sort sites + the `set[int]` union ordering.

---

## Yadgar findings

- **Task numbering is ambiguous across sources** — "#34" means Prometheus scrape in
  `improvement-train.md:35`, recall-remeasure in ADR-0035, and the tie-break in the C4 brief; task #62
  is absent from `improvement-train.md` (so local plan docs are NOT the authoritative tracker — likely
  Codeberg issues). Resolve the tie-break by NAME (ADR-0108 A / `fusion-tiebreak-determinism-2026-07-14.md`),
  not number; the §4 sequencing holds regardless.
- **Concern 1 is TWO root causes** (1a nondeterministic tie order = ADR-0108 A's job; 1b abbreviation
  hard-miss = the real open C4 concern) — the single-candidate `test_specific_detail_preserved`
  (`test_memory_behavior.py:87-98`, ACTIVE, passing) collapses them.
- **SNR is corpus-side, not heat-side:** in `fusion.py` heat is a *gate only* (`:284,:325`), never a
  score term — so "down-weight heat" would not fix it. Noise wins because auto-abstracted memories are
  created thin + meta-token-dense (`cls_store/patterns.py:379`, `promotion.py:59-72`, heat=0.8 seed)
  and never demoted at recall (no filter in `retrieval/*.py`; PR #60 guard is write-time + degenerate-only).
- These are candidate wiki/ADR updates but I did **not** write them (read-only scoping task, per
  instructions) — surfaced here for the main thread.
