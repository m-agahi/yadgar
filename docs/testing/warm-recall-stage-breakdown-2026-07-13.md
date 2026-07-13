# Warm per-stage recall breakdown (Tempo-free, P-SB #6/#50)

**Date:** 2026-07-13
**Ships with:** P-SB recall-observability car (`car/psb-observability`).
**Prereq:** the deployed stack carries this car — core 5.133.0 / backend 5.44.0
(the P0 leaf-registry fix lights up `@observe`; the backend bridge exposes
`yadgar_observe_*` on :8001; the two tier promotions add
`retrieval.vector.encode_query` + `retrieval.ce.score_ce_cached` stages).

## Why

Per-stage recall timing previously existed only as `span_end` structured logs,
which the BatchSpanProcessor flush-truncates — warm attribution needed Tempo, and
only ONE complete cold trace was ever reconstructed from `podman logs`. Prometheus
histograms are cumulative in-process counters, immune to that flush truncation, so
a warm per-stage breakdown is obtainable by histogram delta with **no Tempo
dependency**. This is an aggregate distribution (not single-trace attribution) —
Tempo remains the optional tool for trace-shaped questions.

## Procedure

1. `curl -s $BACKEND_METRICS_URL` → snapshot **A** (e.g.
   `http://127.0.0.1:8001/metrics`).
2. Drive **N ≥ 6** identical WARM recalls (MCP `recall` tool, or
   `benchmarks/run_perf_loadtest.py` with `YADGAR_PERF_WARMUP` ≥ N so the
   measured calls are warm). Use the same query set + same box.
3. `curl -s $BACKEND_METRICS_URL` → snapshot **B**.
4. Per stage label, from `yadgar_observe_stage_duration_seconds{stage=...}`:
   - `mean_ms      = (B.sum − A.sum) / (B.count − A.count) × 1000`  (per invocation)
   - `per_recall_ms = (B.sum − A.sum) / N × 1000`                    (per-recall wall share)
5. Attribute the wall against the **stage TREE** below — do NOT sum siblings with
   ancestors, and do NOT add the cross-cut CE stage to the tree sum.

The harness already emits `ce_mean_ms` + `ce_wall_ms_per_recall` from the CE stage
(P-SB #50). Extending it to dump every stage delta is one scrape away; this doc is
the manual/diagnostic procedure.

## Stage tree (nesting — for correct wall attribution)

All labels are `yadgar_observe_stage_duration_seconds{stage="<label>"}`. The
boundary denominator for the "≈90% covered" sanity check is the RED boundary
histogram `yadgar_observe_request_duration_seconds{name="retrieval.recall"}` —
NOT a stage histogram.

```
retrieval.recall                              (boundary RED — denominator)
├── retrieval.resolve_query_and_candidate_k
├── retrieval.fts
├── retrieval.vector
│   └── retrieval.vector.encode_query         (P-SB promotion: query-embed split from KNN)
├── retrieval.ppr
├── retrieval.spreading
├── retrieval.temporal
├── retrieval.fusion
├── retrieval.build_results                   (≈ hydration; get_memories_by_ids is ~99% per RCA)
└── retrieval.rerank                          (whole rerank pipeline)
    ├── retrieval.cross_encoder_rerank         (CE pass #1)
    └── retrieval.score_documents              (CE pass #2)

retrieval.ce.score_ce_cached                  (CROSS-CUT — NOT a tree leaf; see below)
```

### Cross-cut CE stage — do NOT add to the tree sum

`retrieval.ce.score_ce_cached` is the single funnel ALL 2-3 CE passes go through
(it fires inside both `retrieval.cross_encoder_rerank` and the fanout
`retrieval.crossfuse.fuse_candidates`). Its `d_sum` is the **total CE wall per
window** — exactly the #50 signal — but because it is already counted INSIDE
`retrieval.rerank` (and crossfuse), adding it to the tree sum double-counts CE.
Treat it as a cross-cut readout, never a tree leaf. There is deliberately NO
`retrieval.ce.total` metric family (out of scope per §7 — avoids a new family).

## Excluded

`retrieval.pipeline.*` (`stages/knn.py`, `stages/fts.py`, …) belong to the DEAD
`Retriever.recall_via_pipeline` path — zero production callers (the fanout uses
`Retriever.recall`). Those spans still fire in CI test call sites but are NOT on
the live recall path. Do NOT query `retrieval.pipeline.*` in dashboards or the
harness.

## Sanity check (heuristic, NOT a gate)

The tree-sum of per-recall stage means should cover ≈90% of the
`retrieval.recall` boundary duration; the remainder is un-instrumented glue. Do
NOT fail anything on the exact number — buckets default to a 10s ceiling (accepted
for this car), so `sum/count` means are exact but >10s quantiles are unresolvable.

## Properties

Flush-immune (histograms are cumulative counters, not flushed span logs),
Tempo-free, and portable to any deploy carrying this car.
