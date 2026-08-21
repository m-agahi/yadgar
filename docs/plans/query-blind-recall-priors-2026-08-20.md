# Car 8 — auto-recall is query-blind: additive priors outrank the query signal

> Status: PLANNED (car of the 2026-08-20 train)
> Ledger: task 283. Related: 22 (SNR/ranking, eval-gated), 49 (context budget), 94.
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch.

## 1. The complaint, and the measurement behind it

User, 2026-08-20: auto-recall "returns the same rows over and over again instead of
returning useful info based on the prompt".

Confirmed by measurement, not inference.

**The arithmetic.** `backend/retrieval/fusion.py::_wrrf_fuse` uses `k=60`, so a rank-1 hit
contributes `w / (k + rank + 1) = w/61`:

| contribution | value |
|---|---|
| vector signal, weight 1.0, rank 1 | `1.0/61` = **0.0164** |
| fts signal, weight 0.5, rank 1 | `0.5/61` = **0.0082** |
| best case, rank 1 on both | **0.0246** |

Then `fusion.py:213 _apply_prior_boost` adds, with **no query term at all**:

```python
fused_scores[mid] = fused_scores[mid] + weight * prior_val
```

with `WRRF_GRAPH_PRIOR_WEIGHT = 0.2` and `WRRF_COFIRE_PRIOR_WEIGHT = 0.15`
(`_shared/config/config.py:231`/`:234`), both documented "additive boost in **ALL**
profiles (including fast)" — and prompt-recall runs `profile="fast"`.

**Live corpus, measured 2026-08-20** (`db_inspect`; priors are normalized to `[0,1]`,
`max(graph_prior) = max(cofire_prior) = 1.0` across 2180 rows):

| row | cofire | graph | boost | outcome |
|---|---|---|---|---|
| `memory:531268` "RECALL FEATURE IS COMPLETE" | **0.7407** | 0.0145 | **+0.1140** | returned FIRST for two unrelated queries |
| `memory:534854` fresh, near-verbatim match | **0.0** | 0.0121 | **+0.0024** | never surfaced at any `max_results` |

`531268` carries a **+0.1140 query-independent head start — 4.6x the entire maximum query
signal (0.0246)**. Even if `534854` ranked #1 on *both* signals and `531268` matched
nothing whatsoever, `531268` still wins by 0.087. The theoretical ceiling is `+0.35`, or
**14x** the query signal.

**The stated invariant is violated.** `config.py:229`, verbatim:

> Weight 0.2 is a secondary nudge; must not dominate vector(1.0)/fts(0.5).

That compares `0.2` against the **signal weights** `1.0`/`0.5` — but those weights are
divided by `~k` before contributing. The weight was calibrated against the wrong scale,
off by a factor of ~61. Nothing tests the invariant.

**Positive feedback loop.** The priors derive from graph / co-recall structure — rows
previously surfaced together. `recall()` bumps `access_count` on every hit, and
`backend/curation/strengthen.py:27` strengthens rows with `access_count > 5`. So:
surfaced -> access_count up -> strengthened -> higher prior -> surfaced more. Fingerprint:
`531268`'s `access_count` climbed **87 -> 99 inside a single session**. The set converges
and new rows can never enter it.

## 1b. The bigger fix (user, 2026-08-20): prompt-recall must not re-inject anchors

> "prompt recalls should not bring anchored ones and actually use the keywords of the
> prompt to recall and bring back useful info. anchored ones and such are already part of
> the context by project brief or other shit."

This is correct and it is a LARGER win than re-weighting, because it removes the
duplication rather than reordering it.

**Measured**: every one of the five top-ranked rows returned during the 2026-08-20
investigation was `_anchor`-tagged — `531268`, `522026`, `522027`, `523183`, `529839`.
All five are `is_protected`, `heat = 1.0`, `access_count` 48-140.

**And they are already in the context window.** `project_brief(mode="restore")` returns
`top_anchors` + `hot_memories`, and the SessionStart hook injects it. So prompt-recall is
spending its entire 3000-char budget, on every prompt, re-stating context the model
already has — which is the definition of the "context garbage" the user reported.

That also explains why the additive prior hurts so much HERE specifically: anchors are the
rows most co-recalled, so they carry the highest `cofire_prior`, so the query-independent
term selects precisely the rows that are already redundant.

**Fix**: prompt-recall (`http.py:1733`) must exclude anchors — `_anchor` tag /
`is_protected` — from its own candidate set. Scope this to the PROMPT-RECALL hook, NOT to
`recall()` generally: an explicit `recall("what did we decide about X")` should still be
able to return an anchor, because there the model asked for it and it is not already
duplicated.

This is independent of the §4 weighting decision and can ship first. Do BOTH: excluding
anchors removes the duplication; fixing the additive term is what lets a genuinely
relevant fresh row rank at all (measured: a row written minutes earlier, matching the
query near-verbatim, could not surface at any `max_results`).

## 2. Scope

In scope: the fusion-time prior application, its weight calibration, the invariant test,
and the test that currently pins the defect as correct.

Out of scope (do NOT fold in): the prior COMPUTATION (`graph_prior` / `cofire_prior`
producers) — they are working as designed; the defect is how the result enters ranking.
Also out: task 22's broader SNR work, which is eval-gated.

## 3. Touched files

| path | why |
|---|---|
| `yadgar/backend/retrieval/fusion.py:213` | `_apply_prior_boost` — the additive term |
| `yadgar/backend/retrieval/fusion.py:255-276` | both prior application sites |
| `yadgar/_shared/config/config.py:229-236` | the weights + the false comment |
| `yadgar/tests/scripts/test_v5_54_2_cofire_prior.py:189` | pins the defect — MUST be rewritten |
| `yadgar/tests/scripts/test_v5_54_1_graph_prior.py` | same shape, check |

## 4. The decision to make FIRST

**Do not simply lower the constants.** A query-independent additive term is wrong in
KIND, not only in magnitude: at any non-zero weight it still orders same-query ties by
popularity, and the feedback loop still runs — just slower. Pick one:

- **(a) Rank-scaled** — fold the prior into the per-signal term *before* the `1/(k+rank)`
  division, so it can only reorder rows the query already matched.
- **(b) Multiplicative** — `score * (1 + w * prior)`. A row with no query match has
  `score = 0` and stays 0. Preserves "popular AND relevant beats relevant alone".
- **(c) Tie-breaker only** — priors enter `_tiebreak_key`, never the score.

(b) is the smallest change that restores the stated invariant and kills the loop; (c) is
the most conservative. Recommend (b), decide before building.

## 5. Build steps (TDD)

1. **Write the invariant test FIRST, red**: a row with `graph_prior=1.0, cofire_prior=1.0`
   and NO query match must NOT outrank a rank-1 query match with zero priors. This is
   `config.py:229`'s own claim, asserted for the first time.
2. **Rewrite `test_high_cofire_prior_ranks_higher`** (`test_v5_54_2_cofire_prior.py:189`).
   It currently asserts `ranked_ids[0] == 1` for a high-prior row — i.e. it encodes the
   dominating behaviour as the spec. The replacement must assert the correct, narrower
   claim: among rows with *comparable query relevance*, higher prior ranks higher.
3. Implement the chosen shape from §4.
4. Re-derive the weights against the post-division scale; record the derivation in the
   comment so the next reader can check it.
5. Verify on the live corpus: the `531268` vs `534854` pair must invert.

## 6. Acceptance gates

- The §5.1 invariant test passes, and MUTATING the fix (restoring the additive term) reds it.
- `531268` no longer returns first for an unrelated query.
- A memory written minutes ago and matching the query verbatim IS retrievable — this is
  the case that failed all night and blocked the tasks 246/262 reachability probe.
- `make longmemeval Q=N` does not regress. NOTE this eval has been measuring a
  query-blind ranker, so a *changed* number is expected; a WORSE number needs explaining.

## 7. Risks

- The eval baseline was established under the defect. Do not treat baseline movement as
  failure without inspecting which queries moved.
- `_apply_prior_boost` runs after BOTH fusion methods (`fusion.py:244` convex / `:253`
  wrrf). Check the convex path separately: `fused_scores` is assigned in the wrrf branch
  only, so in convex mode the boost may operate on a different or empty dict — verify
  whether priors apply at all there before changing shared code.

## 8. Adjacent, filed not fixed

`_build_dlq_alert_text` (`core/server/_helpers.py:67`) is prepended to the prompt-recall
injection AFTER the 3000-char budget is computed (`http.py:1829-1848`), so it sits
outside the cap. 1170 DLQ entries are on disk — a known frozen backlog of an
already-fixed defect. Measure what it renders on every prompt.
