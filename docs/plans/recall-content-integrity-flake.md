# PLAN — unquarantine recall content-integrity test (#21)

Created 2026-06-25 (improvement-train #29, group C). theme: recall / retrieval /
test-flake. priority: medium (a quarantined behavior test is a blind spot in the
recall contract).

## Problem (root-caused 2026-06-25)

`yadgar/tests/test_memory_behavior.py::TestContentIntegrity::test_specific_detail_preserved`
(lines ~87-103) is hard-skipped:
```python
@pytest.mark.skip(reason="pre-existing recall-content flake, unrelated to obs-train
PR#122 … failed 6/6 CI reruns on a branch 0-behind master")
```
The test stores a memory and queries for it:
```python
content = "Codeberg PAT is stored in 1Password item zqq55bz2qi53gw375jlm2sh4jq"
mid = memorize_sync(content, "/home/user", ["codeberg", "secrets"])["id"]
hits = server.recall("codeberg personal access token", directory="/home/user")
match = next((h for h in hits if h["id"] == mid), None)
assert match is not None                                  # ← THE FAILING ASSERT
assert "zqq55bz2qi53gw375jlm2sh4jq" in match["content"]   # (content check, not reached)
```

### Root cause: retrieval RANKING miss, NOT content truncation
- The failure is at `assert match is not None` (line ~100): the stored memory does
  **not surface in recall's top-k** for the query. The content-preservation assert
  below it is never reached.
- **Content integrity itself is fine** — proven by the sibling, non-skipped
  `test_exact_content_preserved` (same class). The recall path returns `content`
  verbatim: `MemoryProvider` passes `m.get("content","")` unchanged
  (`retrieval/providers/memory.py:~86`); `Retriever.recall` pops only `embedding`, not
  `content` (`retrieval/fusion.py:~298-302`). 62-char content is far below any
  truncation threshold. So the quarantine reason "content flake" is a misnomer — it is
  a **ranking** flake.
- **Why it misses:** lexical/embedding gap. Stored uses the abbreviation "PAT"; the
  query expands it to "personal access token". The fused score (vector + FTS + CE
  rerank) does not reliably rank the target into the default top-5 (`max_results=5`,
  recall path) across runs → 6/6 CI failures, environmental-flake-flavored because tie
  ordering / embedding nondeterminism shifts it just under the cutoff.

## Fix approach (investigate → then choose)

This is a real recall-quality question, not a test-only patch. Order:

1. **Reproduce deterministically.** Un-skip locally, run under a seed/repeat loop
   (`pytest -p no:randomly … --count=20` or a manual loop) to confirm the miss rate
   and whether it's hard (always just-below-cutoff) or jittery (tie ordering).
2. **Diagnose the gap.** Log the target's per-signal scores (vector cos, FTS/BM25,
   CE rerank) vs the top-5 it loses to. Determine which signal fails:
   - FTS: "PAT" vs "personal access token" share no token → BM25 ~0; expected.
   - Vector: does the embedding bridge the abbreviation? If cos is mid-pack, the
     fused weight or the CE cut (`CROSS_ENCODER_TOP_K=10`) drops it.
3. **Choose the fix by what the data shows** (decide AFTER step 2 — do not pre-commit):
   - If vector cos is actually high but fusion weighting buries it → a weighting/fusion
     question (careful: don't regress the recall benchmark to pass one test).
   - If the gap is genuinely an abbreviation-expansion miss → this is a known hard case
     for pure embedding recall; the honest fix may be to **make the test assertion
     realistic** (query with overlapping terms, or assert top-k membership with a
     larger k) rather than chase a fusion change that helps one synthetic case and
     hurts the LongMemEval baseline. **[FLAG: this is a judgment call — surface the
     score data to the user before changing recall weights for a single fixture.]**
   - Tie/nondeterminism only → add a deterministic tie-break in fusion ordering (safe,
     local) and the test stabilizes without touching weights.
4. **Unskip.** Remove the `@pytest.mark.skip` once it passes deterministically; gate
   the recall-weight option (if any) behind the LongMemEval recall@k regression check.

## TDD outline
- The test IS the failing test — un-skip it (red), drive it green via the chosen fix.
- Add a tie-break / abbreviation regression case only if the diagnosis points there.
- **Guardrail:** any recall-scoring change MUST keep `make longmemeval` recall@k from
  regressing (the 0.868-class baseline). A test that passes by degrading the benchmark
  is worse than a skip. Run the benchmark before/after.

## Config / contracts
- Potentially touches recall fusion weights / `CROSS_ENCODER_TOP_K` (config.py:164) —
  if so, I25 three-way for any changed default. Lean: NO config change; prefer
  deterministic tie-break or test-realism fix.
- Recall BCs (BC-U* unified recall) must stay green — this test is an
  outcome-level content-integrity guard adjacent to them.

## Risks
- **Overfitting recall to one synthetic fixture** is the main risk — the test queries
  an abbreviation-expansion pair that is genuinely hard for embedding recall. Resist
  re-weighting fusion to pass it if that costs benchmark recall. The defensible
  outcomes are: (a) deterministic ordering fix, or (b) make the assertion test what
  recall actually guarantees (content fidelity when retrieved) + a separate realistic
  retrieval query. Decide with score data + user input.

## Related
- `yadgar/tests/test_memory_behavior.py` (the test + sibling
  `test_exact_content_preserved`), `yadgar/retrieval/fusion.py`,
  `yadgar/retrieval/providers/memory.py`, `config.py:162-165` (CE knobs),
  benchmark-runbook wiki (`make longmemeval`).
