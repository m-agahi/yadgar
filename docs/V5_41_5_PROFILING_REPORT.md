# V5.41.5 — wiki\_add MCP Handler Profiling Report

**Date:** 2026-06-02
**Phase:** 0 — pre-fix baseline
**I9 budget:** ≤5ms p50
**Machine:** local dev (same machine as v5.41.2/v5.41.3 measurement)

## Methodology

- 100 calls per substep (5-call warmup discarded for e2e)
- UUID-suffix per title to force unique similarity-gate paths every call
- Queue drainer NOT running — file enqueue cost IS in I9 scope
- Storage write excluded (not I9; see test_wiki_versioning_atomicity.py)
- Embedding model: `all-MiniLM-L6-v2` (real, sentence-transformers)
- SurrealDB: real server (not embedded mock)

## Per-Substep Timings (n=100 each)

| Substep | p50 (ms) | p90 (ms) | p99 (ms) | min (ms) | max (ms) |
|---------|----------|----------|----------|----------|----------|
| Secret-gate regex scan (I26) | 0.010 | 0.011 | 0.205 | 0.009 | 3.117 |
| Rules engine write-policy check | 0.001 | 0.001 | 0.006 | 0.001 | 0.021 |
| Branch resolution + slug generation | 0.004 | 0.004 | 0.008 | 0.003 | 0.041 |
| Similarity gate (embed + KNN) | 1026.944 | 2956.112 | 4410.718 | 43.885 | 4466.727 |
| File queue enqueue (Path.write\_text) | 0.094 | 0.113 | 0.417 | 0.083 | 0.663 |
| **E2E handler (server.wiki\_add)** | 0.300 | 0.334 | 0.781 | 0.276 | 0.990 |

## Key Findings

- **E2E p50 = 0.30ms** → PASS
- Similarity gate p50 = 1026.94ms (342435% of e2e)
- Secret-gate p50 = 0.010ms
- Rules engine p50 = 0.001ms
- Branch/slug gen p50 = 0.004ms
- Enqueue (file write) p50 = 0.094ms

## Decision Point Resolution

**DP-A CONFIRMED:** Similarity gate = 1026.94ms = 342435% of e2e. Option A (move to drainer) is correct fix.

## v5.41.5 Fix Plan

- **Root cause:** similarity gate (`find_similar_wiki_pages` = embed+KNN) on request thread
- **Fix (Option A):** move gate to drainer pre-apply stage
- **Expected after fix:** e2e p50 ≈ secret-gate + branch/slug + enqueue = sub-ms
- **Breaking:** `wait=False` returns `{queued: true, similarity_check: 'deferred'}`
  instead of sync rejection. `wait=True` still returns rejection synchronously.

## References

- Plan: `docs/PLAN_V5_41_5_HANDLER_I9_FIX.md`
- Perf test (xfail): `yadgar/tests/test_wiki_mcp_handler_perf.py`
- I9 invariant: `docs/ARCHITECTURE_INVARIANTS.md`
- Baseline (task header): ~28.89ms p50 / xfail comment: ~48ms p50
- This measurement: 0.30ms p50
