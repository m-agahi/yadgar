# Fusion score-tie determinism — deferred fix (ADR-0108 Option A)

**Status:** DEFERRED — ready for a future car. Surfaced 2026-07-14 by the recall-content-integrity-flake audit (that flake was OBVIATED + archived; THIS is a separate, genuine bug the audit found on the side).
**Theme:** recall / retrieval determinism. **Scope:** core `fusion.py` sort keys. Small, low-risk, well-localized.

## TL;DR
Production recall fusion sorts candidates by **score only**. When ≥2 candidates share an equal fused score, their relative order is decided by the iteration order of a Python `set[int]` — which is not stable run-to-run. Result: for multi-candidate recall with score ties, the top-k ordering (and thus what crosses a top-N cutoff) is **nondeterministic**. Fix = add a deterministic secondary sort key `(score, id)` descending at every fusion sort site.

## Why it exists / why it matters
- `fusion.py:106-111` builds the `combined` candidate collection by iterating `all_mids: set[int]`. Set iteration order over ints is not guaranteed stable across processes.
- That set-ordered sequence is then fed to `sorted(..., key=lambda x: x[1])` at `fusion.py:115` and `:199` — **score-only** sort keys (confirmed: no tie-break key exists anywhere in git history).
- Equal fused-score rows therefore land in run-varying order. For a query where the target ties on score with other rows near the top-N boundary, the target can fall below the cutoff intermittently — the exact failure signature that once made `test_specific_detail_preserved` flaky (before its own fixture fix `6aff1909` made it single-candidate and thus tie-free).
- The single-candidate test can no longer catch this (nrows=1 → no tie). So the determinism bug is **live and untested** in the multi-candidate production path.

## The fix (ADR-0108 Option A)
Add `(score, id)` descending as the sort key at every fusion sort/hygiene site:
- `fusion.py:115` (primary fuse sort)
- `fusion.py:199` (second fuse sort)
- `fusion.py:76`, `:190`, `:275-279` (hygiene/ordering sites — audit-flagged)
- `providers/fusion.py:272`

Use memory id (stable, unique) as the deterministic tiebreak so equal-score rows always resolve the same way. Descending id is a defensible default (newer wins ties); confirm the desired tie direction when building.

## TDD
- RED: construct a fusion input with ≥2 rows sharing an identical fused score; assert the output order is stable across repeated runs AND matches the `(score, id)` desc contract. Without the fix this is flaky/wrong; with it, deterministic.
- Consider a property test: shuffle input order → fused output order invariant.
- Guard against regression: a test that fails if any fusion sort key is score-only (grep-style or behavioral).

## Verify
Re-run the recall suite; the multi-candidate ordering is stable across `-n 4` parallel + random-order runs. No live-daemon needed — pure in-process fusion logic.

## Notes
- Do NOT reopen the recall-content-integrity-flake plan (archived, OBVIATED) — this is a distinct bug that shares an ancestor cause.
- Sizing: one small commit + tests. Good standalone car or rider on a future recall-quality train.
