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
| Secret-gate regex scan (I26) | 0.003 | 0.003 | 0.005 | 0.003 | 0.018 |
| Rules engine write-policy check | 0.000 | 0.000 | 0.001 | 0.000 | 0.005 |
| Branch resolution + slug generation | 0.001 | 0.001 | 0.003 | 0.001 | 0.009 |
| Similarity gate (embed + KNN) | 27.735 | 73.016 | 92.667 | 24.089 | 99.217 |
| File queue enqueue (Path.write\_text) | 0.027 | 0.035 | 0.136 | 0.025 | 0.257 |
| **E2E handler (server.wiki\_add)** | 27.092 | 43.465 | 48.838 | 24.651 | 50.640 |

## Key Findings

- **E2E p50 = 27.09ms** → FAIL — 5.4x over budget
- Similarity gate p50 = 27.73ms (102% of e2e)
- Secret-gate p50 = 0.003ms
- Rules engine p50 = 0.000ms
- Branch/slug gen p50 = 0.001ms
- Enqueue (file write) p50 = 0.027ms

## Decision Point Resolution

**DP-A CONFIRMED:** Similarity gate = 27.73ms = 102% of e2e handler. Option A (move to drainer) is correct fix.

## v5.41.5 Fix Plan

- **Root cause:** similarity gate (`find_similar_wiki_pages` = embed + KNN) runs on request thread
- **Fix (Option A):** move gate to drainer pre-apply stage
- **Expected after fix:** e2e p50 ≈ secret-gate + branch/slug + enqueue = sub-ms
- **Breaking change:** `wait=False` returns `{queued: true, similarity_check: 'deferred'}`
  instead of sync rejection. `wait=True` still returns rejection synchronously.

## References

- Plan: `docs/PLAN_V5_41_5_HANDLER_I9_FIX.md`
- Perf test (xfail): `yadgar/tests/test_wiki_mcp_handler_perf.py`
- I9 invariant: `docs/ARCHITECTURE_INVARIANTS.md`
- Baseline (v5.41.2): ~28.89ms p50 (task header) / ~48ms p50 (xfail comment)
- This measurement: 27.09ms p50
