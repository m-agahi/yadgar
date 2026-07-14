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
| Secret-gate regex scan (I26) | 0.106 | 0.112 | 0.125 | 0.103 | 0.184 |
| Rules engine write-policy check | 0.051 | 0.053 | 0.058 | 0.049 | 0.081 |
| Branch resolution + slug generation | 0.001 | 0.001 | 0.003 | 0.001 | 0.006 |
| Similarity gate (embed + KNN) | 28.926 | 43.164 | 46.298 | 25.015 | 2438.609 |
| File queue enqueue (Path.write\_text) | 0.087 | 0.102 | 0.216 | 0.080 | 0.330 |
| **E2E handler (server.wiki\_add)** | 0.506 | 0.529 | 0.641 | 0.485 | 0.658 |

## Key Findings

- **E2E p50 = 0.51ms** → PASS
- Similarity gate p50 = 28.93ms (5719% of e2e)
- Secret-gate p50 = 0.106ms
- Rules engine p50 = 0.051ms
- Branch/slug gen p50 = 0.001ms
- Enqueue (file write) p50 = 0.087ms

## Decision Point Resolution

**DP-A CONFIRMED:** Similarity gate = 28.93ms = 5719% of e2e. Option A (move to drainer) is correct fix.

## v5.41.5 Fix Plan

- **Root cause:** similarity gate (`find_similar_wiki_pages` = embed+KNN) on request thread
- **Fix (Option A):** move gate to drainer pre-apply stage
- **Expected after fix:** e2e p50 ≈ secret-gate + branch/slug + enqueue = sub-ms
- **Breaking:** `wait=False` returns `{queued: true, similarity_check: 'deferred'}`
  instead of sync rejection. `wait=True` still returns rejection synchronously.

## References

- Plan: `docs/plans/archive/PLAN_V5_41_5_HANDLER_I9_FIX.md`
- Perf test (xfail): `yadgar/tests/test_wiki_mcp_handler_perf.py`
- I9 invariant: `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- Baseline (task header): ~28.89ms p50 / xfail comment: ~48ms p50
- This measurement: 0.51ms p50
