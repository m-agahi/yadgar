# Recall Wall-Clock Attribution RCA — yadgar 5.132.0 / backend 5.43.0

**Date:** 2026-07-13
**Env:** Ettin-32m reranker, --cpus 2, single-backend in-process pipeline.
**Method:** MCP recall (read-only) → grab trace_id from `podman logs yadgar-backend`
(read-only) → reconstruct span tree from `span_end` events (span_id / parent_span_id
/ duration_ms) + live `curl GET :8001/metrics` and `:8765/metrics` + code reads.
No writes, no exec.

---

## TL;DR (verdict)

1. **The histogram reads 0 because recall stopped feeding it.**
   `yadgar_embed_rerank_duration_seconds{mode="ce"}` is emitted **only** by the
   embed-service `POST /rerank` HTTP endpoint (`embed_service.py:1034`,
   `RemoteMLClient` path). The current architecture runs retrieval + CE
   **in-process** in the backend via `LocalMLClient.score_cross_encoder` →
   `_try_gte_reranker` → `CrossEncoder.predict()` — which **never touches
   `/rerank`** and **never observes that histogram**. Live: `count=0.0` on :8001.
   Dead for recall.
   **[HIGH CONFIDENCE — verified live + in code.]**

2. **The harness `ce_mean_ms` was therefore broken → reported `None`, not a
   number.** `run_perf_loadtest.py` took the before/after `_sum`/`_count` delta of
   that histogram; `d_count` was always 0 → `agg["ce_mean_ms"] = None` (silently,
   with no warning). The "CE is 7-8.5s of recall wall" number came from the OLD
   split-container era where core used `RemoteMLClient` and POSTed `/rerank`. It is
   stale on this stack.
   **[HIGH CONFIDENCE — verified in code. FIXED in this PR: harness now detects
   d_count==0 and emits explicit `ce_metric_status` + WARNING.]**

3. **CE is NOT the dominant term of the one cold recall we fully captured.**
   In trace `06d8111b` (6219ms), CE wall ≈ **1.5s** (all passes; ~0.6s of it
   one-time model load) vs **1.44s** DB hydration (`get_memories_by_ids`) vs a
   **~2.8s head segment** (query embed + vector + FTS + PPR + spreading + fusion —
   see caveat). CE is **~25% of cold wall**, not 100%.
   **[MEDIUM-HIGH — the CE / hydration numbers are solid; the 2.8s head is
   bounded by wall math but its per-signal split is NOT captured (see below).]**

4. **The real, working recall-wall signal already exists.** Core-side
   `yadgar_recall_duration_ms` (port :8765) — a dedicated histogram, live,
   with MCP transport overhead ≈ 0 (~2ms/call framing as confirmed by
   `yadgar_mcp_request_duration_ms{tool=recall}`). Use this for all
   version-portable recall-wall measurements.
   **[HIGH CONFIDENCE.]**

---

## CRITICAL MEASUREMENT CAVEAT — span logs are flush-truncated

The `span_end` events reach `podman logs` via a **BatchSpanProcessor** that
flushes on a timer, so a single recall's spans are split across log-flush windows.
Consequence, verified this session:

- Of ALL recalls captured, exactly **ONE has a complete tree with a `POST /recall`
  root**: the cold `06d8111b` (6219ms). Every other recall trace in the logs is a
  **partial fragment**.
- The earlier "warm 222ms, CE=0" trace `7469068b` is a **truncated fragment** —
  it has NO `POST /recall` root (only a 0ms `POST /recall http receive` sub-span);
  its "222ms" is a `ppr_retrieve` CHILD, not the wall. It CANNOT be used to claim
  "warm recall = 222ms" or "warm CE = 0". **That claim is retracted.**
- Disjoint coverage proves the flush artifact: the **cold** trace (4153 spans)
  contains **zero** gather-signal spans (ppr/vector/fts/spreading/encode all
  ABSENT) yet HAS the CE + hydration tail; the **warm** fragment has the
  gather-signal spans (ppr_retrieve, search_vectors, _collect_scores_dispatch) but
  NOT the CE/hydration tail. Neither is whole.

So the cold trace's "2806ms before the first child span" is **NOT genuinely
unspanned code** — the gather IS instrumented (the warm fragment proves
ppr/vector/fts emit spans); those spans simply did not land in the same log-flush
window. Do not read "2.8s unspanned" as "2.8s of code with no spans." Read it as
"2.8s head segment whose internal spans were dropped by log-flush truncation."
A clean per-signal split requires **Tempo** (which reassembles the full trace
across flushes), not `podman logs`. **This is the one open gap in the attribution.**

A warm-recall wall number could not be captured cleanly this session: two
identical-query probes (to force CE cache hits) did not flush a `POST /recall`
root within a 60s poll. The steady-state number rests on
`yadgar_recall_duration_ms` (mean ≈ 5.0s at the time, cold-outlier-inclusive),
NOT on a warm span tree.

---

## COLD recall span tree (trace 06d8111b, 6219ms)

```
POST /recall .................................................. 6219ms   (embed_service route boundary)
└─ recall_route ............................................... 6218ms
   └─ Retriever.recall ........................................ 5702ms   ← backend retrieval core
      │  (== MemoryProvider.candidates 5702ms; single-provider path)
      │
      ├─ [HEAD segment — spans flush-truncated] ............. ~2806ms   ← query embed + vector + FTS +
      │     from recall t0 to first landed child span.              PPR + spreading + fusion scoring.
      │     Its internal spans (ppr_retrieve etc.) are             The gather IS instrumented (warm
      │     INSTRUMENTED but did NOT land in this log-flush         fragment shows ppr_retrieve /
      │     window (see caveat). Bounded by wall math only;        search_vectors); those spans just
      │     per-signal split needs Tempo. This is what prior       missed this flush window. NOT
      │     agents mislabeled the "7-13s unspanned layer" —        genuinely uninstrumented, and
      │     it is INSIDE backend recall, not MCP/hook, and         INSIDE backend recall, not
      │     it is cold-cache inflated.                             MCP/hook overhead.
      │
      ├─ _build_initial_results ............................... 1442ms
      │  └─ get_memories_by_ids (storage hydration) .......... 1439ms   ← DB round-trip, N rows
      │
      └─ _apply_rerank_pipeline ............................... 1453ms
         ├─ _rerank_cross_encoder ............................ 1051ms   ← CE PASS #1 (retrieval CE)
         │  └─ cross_encoder_rerank .......................... 1051ms
         │     └─ score_ce_cached ............................ 1051ms
         │        └─ LocalMLClient.score_cross_encoder ....... 1049ms   (in-process; NOT /rerank)
         │           └─ _try_gte_reranker ................... 1049ms
         │              └─ _load_gte_reranker ................. 613ms   ← ONE-TIME model load (first call only)
         ├─ _rerank_engram_links ............................. 261ms
         │  └─ get_temporally_linked ×10 .................... 255ms
         ├─ _rerank_multi_passage ............................ 88ms
         ├─ _rerank_profile_belief_merge ..................... 35ms
         └─ _rerank_{rules,metacog,heuristic,nli,mmr} ........ <15ms each

   (sibling of Retriever.recall, in recall_route:)
   └─ recall.fanout.fuse ...................................... 390ms
      └─ fuse_candidates → _score_candidates_ce .............. 389ms   ← CE PASS #3 (cross-type fusion CE)
         └─ score_ce_cached → score_cross_encoder ............ 389ms
```

CE fires up to **3×** in one cold recall:
`1051ms` (main rerank incl. 613ms model load) + `87ms` (score_documents) + `389ms`
(cross-type fanout fuse) = **~1527ms CE wall**, ~613ms of which is one-time.
Steady-state CE ≈ **0.9s** across the 2–3 passes.

### Cold recall wall attribution (6219ms, trace 06d8111b)

| stage | ms | confidence |
|---|---|---|
| Head: query embed + vector + FTS + PPR + spreading + fusion (internal spans flush-dropped) | ~2806 | wall-bounded; per-signal split NOT captured — needs Tempo |
| CE rerank (all 2-3 passes) | ~1527 | solid (spans present); incl. ~613ms one-time model load |
| DB hydration (`get_memories_by_ids`) | ~1439 | solid (span present, sequential child) |
| Engram links | 261 | solid |
| Multi-passage + profile/belief + rules/etc | ~135 | solid |
| Route/boundary overhead (6219−5702−390) | ~127 | solid |

**CE ≈ 25% of cold wall.** The head signal-gather (~45%) and DB hydration (~23%)
are larger cold terms. CE is not the single dominant term even cold.

---

## WARM recall — NOT cleanly captured (claim retracted)

The earlier "warm 222ms / CE=0" reading was a **flush-truncated fragment** (trace
`7469068b`, no root span; "222ms" was a `ppr_retrieve` child, not the wall).
**Retracted.** Two identical-query re-probes did not flush a complete
`POST /recall` root within a 60s poll. So there is NO trustworthy warm span tree
from this session.

What IS defensible about warm recall, from the Prometheus histogram:

- `yadgar_recall_duration_ms` shows sub-second and low-second recalls occur
  (warm-ish), alongside a 5-10s cluster and one >25s cold outlier.
- Whether warm recall SKIPS CE inference or just CACHE-HITS it faster is
  **unconfirmed** (the fragment is unreliable). Code says `score_ce_cached` still
  runs on a warm query but reuses stored scores for repeat (query,text) pairs —
  so warm CE is a fast cache lookup, likely tens of ms. Confirm via Tempo.

---

## Core-side / MCP / hook gap — resolved (from Prometheus, not spans)

- **Core-side recall boundary** (`yadgar_recall_duration_ms`, :8765): mean ≈ 5.0s.
- **Full MCP boundary** (`yadgar_mcp_request_duration_ms{tool=recall}`): mean ≈ 4.9s.
- **MCP transport overhead ≈ 0** (~2ms/call framing).

The "7-13s unspanned layer" a prior agent attributed is NOT a persistent
core/MCP/hook cost. It is **cold-recall backend compute** (cold caches + one-time
model load), which shows as the >25s outlier + the 5-10s cluster in the histogram.

---

## Was CE EVER 7-8.5s? Is it now?

- **Was:** plausibly yes, in the split-container era (core `RemoteMLClient` →
  `/rerank` HTTP, CE inference "8-46s on CPU" per the `RemoteMLClient` timeout
  comment, un-batched, un-cached). Those numbers fed the embed_rerank histogram
  and the "CE dominant across 30 versions" claim.
- **Now:** no. In-process, batched, LRU/ckpt-cached, Ettin-32m (tiny reranker).
  Measured CE wall on the cold recall ≈ **1.5s** (with model load) / **~0.9s**
  steady across 2-3 passes. CE is not the recall bottleneck on this architecture.

---

## Open gap

Use **Tempo** (not `podman logs`) to pull a complete warm + cold recall trace by
trace_id — Tempo reassembles across the BatchSpanProcessor flush boundaries.
That yields: (i) the per-signal split of the ~2.8s cold head (spreading vs vector
vs FTS vs PPR vs graph); (ii) a trustworthy warm recall wall + whether warm CE
skips or cache-hits. Everything else here is settled.
