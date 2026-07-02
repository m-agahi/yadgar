# Warm Recall Per-Stage Profile & Tuning Plan — 2026-07-02

> **UPDATE 2026-07-02 (post PR #143, v5.97.0):** Fixes 1 (fusion N+1 batch) + 2
> (MMR fold-in) SHIPPED → warm floor ~2.74s → **~1.6-1.8s**. **Fix 3 (onnx-int8 CE)
> was WRONG for prod and NOT shipped:** the ~720ms CE hot-path is **GTE-ModernBERT**
> (`GTE_RERANKER_ENABLED=true` default), not the ST CrossEncoder; `CROSS_ENCODER_BACKEND=onnx-int8`
> only gates the 3rd-priority ST fallback that GTE preempts → flipping it is a no-op.
> Speeding the real CE (GTE-ModernBERT) is tracked as **task #92**. The "Fix 3"
> section below is retained for the record but its premise is superseded.

Goal: attribute the warm steady-state recall latency ("~2.4s floor") to per-stage
components on THIS box (core `--cpus 1`; backend `--cpus 2` running SurrealDB +
rerank; constrained RAM), separate the **reducible** overhead from the
**irreducible hardware floor**, and give a prioritized, advisor-gated tuning plan
to reach "can't improve more without adding CPU/RAM."

Method: standalone profiler OFF the daemon cgroup, read-only against the live
backend (`YADGAR_DB_URL=http://127.0.0.1:8000`, `YADGAR_EMBED_URL=http://127.0.0.1:8001`),
constructing `Retriever(...)` and calling the write-free `recall_via_pipeline()`
(NOT the MCP `recall()` tool, which does heat-boost writes). Sequential
single-query only — never the concurrent-6 burst that risks the health-kill.
Pre-warmed (models + CE/embed caches), then ≥10 timed runs per variant; medians
reported. cProfile used only for call-count and within-stage attribution.

Profiler scratch scripts: `/tmp/yadgar_warm_profile.py`, `/tmp/yadgar_warm_profile2.py`.
Reproduce:
```
PYTHONPATH=<repo> \
YADGAR_DB_URL=http://127.0.0.1:8000 YADGAR_EMBED_URL=http://127.0.0.1:8001 \
YADGAR_DB_USER=yadgar YADGAR_DB_PASS=<pass> \
YADGAR_OTLP_ENDPOINT="" \
.venv/bin/python /tmp/yadgar_warm_profile.py
```

> **Measurement caveat — OTEL retry pollution.** `YADGAR_OTLP_ENDPOINT` MUST be
> empty for the profiler. When set but unreachable (`host.containers.internal:4318`
> is not resolvable from a standalone process), the OTEL BatchSpanProcessor blocks
> synchronously on the calling thread and injects 5–180 s spikes into arbitrary
> stages. All 9 s / 58 s / 156 s outliers observed were OTEL artifacts, not
> pipeline cost. (The live daemon points OTLP at a real Tempo instance, so this
> is a profiler-only concern.)

---

## 1. Per-Stage Table

Two warm variants, `profile="balanced"`, `max_results=5`, `min_heat=0.0`:
- **cache-MISS warm** — fresh distinct query each run (embed + CE inputs miss caches).
- **cache-HIT warm** — same query repeated (embed + CE caches hot). The HIT column
  is the most stable measure (n=10, ~±120 ms wall) and the basis for attribution.

| stage | miss ms (p50) | hit ms (p50) | side | reducible? |
|---|---|---|---|---|
| query_analysis | 0.1 | 0.1 | core (Python NLP) | no — negligible |
| fts | 21.8 | 18.6 | backend (1 Surreal FTS round-trip) | no — single query, near floor |
| knn | 63.4 | 44.8 | backend (embed + HNSW) | marginal — embed cached on HIT (0 ms) |
| ppr | 167.9 | 620.9 | **core** (networkx pagerank, in-process) | no — CPU-bound floor, query-dependent |
| spreading | 0.0 | 0.0 | backend (BFS) | n/a — did not fire on test queries (see §note) |
| temporal | 0.0 | 0.1 | core | no — effectively disabled |
| fusion | 1233 | **1100** | **backend N+1** — 52–55× serial `get_memory` | **YES — batchable, ~950 ms** |
| ce_rerank | 2906 | **903** | backend CE inference ~720 ms + MMR embed re-fetch ~328 ms (**overlaps** — see note) | **partial** — MMR fold-in ~183 ms marginal; CE via onnx-int8 |
| nli / mmr / adversarial / rules | 0.0 | 0.0 | — | dispatched inside `ce_rerank` composite (not separately timed) |
| **TOTAL wall** | **~3934** (clean) | **~2739** | | |
| sum(stages) | 4392 | 2688 | | |
| unaccounted | ~290 | ~52 | Python dispatch / span setup | no |

Sum-vs-wall reconciles tightly on HIT (2688 + 52 = 2740 ≈ 2739 wall). The
oft-quoted "2.4s" is really **~2.74 s** on the HIT path here; the true figure is a
*range* driven by query entity-richness (see §4).

### Notes on the surprising stages

- **Fusion is the #1 cost, not CE rerank.** `_build_initial_results`
  (`yadgar/retrieval/fusion.py:297-305`) loops `for mid, score in fused:
  mem = self._storage.get_memory(mid)` — **52–55 serial HTTP round-trips** per
  recall (cProfile call count), each ~18.7 ms (matches the isolated by-id RTT
  floor exactly). This is the *same N+1 shape* the v5.96 priors batch removed —
  the bottleneck simply **relocated** from priors into the final result fetch.
- **CE rerank ~903 ms (HIT)** = ~720 ms backend CE inference (`_rerank_rpc`,
  ~271 ms/call × ~2.7 calls) plus MMR's ~328 ms candidate-embed re-fetch — but
  these **overlap inside the 903 ms composite stage**: 720 + 328 = 1048 > 903, so
  ≥145 ms is concurrent (backend I/O waits overlap). MMR's *marginal* wall cost is
  therefore only ~903 − 720 ≈ **~183 ms**, not the full 328 ms. This distinction
  matters for the tuning-list math (§3 #2, §4): MMR and CE fixes both draw from the
  *same* 903 ms stage and cannot be subtracted independently from the grand total.
- **PPR swings 0 → 620 ms** purely on query entity-richness: entity-poor queries
  early-return (0 ms); the entity-rich HIT query drove a full `networkx.pagerank`
  walk. This is in-process CPU on the `--cpus 1` core (`core.py:100-164`).
- **Spreading measured 0 ms** — NOT disabled. `balanced` includes it
  (`WRRF_SPREADING_WEIGHT=0.3`, `config.py:160`); it early-returns when there are
  no vector seeds / no graph neighbors (`scoring.py:236-256`). ADR-0030's
  "non-batchable N-round-trip BFS" premise still stands as a *latent* cost: on
  entity-dense queries with graph edges it will fire and add per-round-trip cost.
  It did not fire on the test queries, so it is not a current line-item — it is a
  tail-risk (§3, item 6).
- **Priors batch (v5.96) confirmed landed & healthy** — `get_memory_graph_priors`
  + `get_memory_cofire_priors` together ~150 ms/recall (two single `WHERE id IN
  [...]` round-trips, `storage/memory.py:1029-1064` / `1080-1107`). No N+1 there.
- **Heat-boost writes are OFF the critical path.** `_apply_heat_boost_and_side_effects`
  runs in the MCP tool layer (`server/tools/recall.py:350-389`) *after*
  `recall_via_pipeline` returns; the write-free path never executes them. Out of
  scope for the warm floor.

---

## 2. Irreducible Floor vs Reducible Overhead

**Reducible overhead (removable by code changes, no HW change):**

| source | current ms (HIT) | mechanism |
|---|---|---|
| fusion N+1 (`fusion.py:297-305`) | ~1100 | 52 serial round-trips; (N−1)×18.7 ms = **~953 ms** collapsible by one `WHERE id IN [...]` |
| MMR redundant embed re-fetch (`_reranking_mmr.py:33`) | ~183 marginal (overlaps CE inside the 903 ms `ce_rerank`) | re-calls `get_memory` per candidate; already in the fused result dict |
| CE fp32 vs int8 (`config.py:191` default `st`) | ~720 → ~240–360 | onnx-int8 model built v5.85 but **not runnable on this box** (see #3): runtime broken |

**Irreducible floor on this hardware** (surreal exec + CE inference on `--cpus 2`,
networkx on the `--cpus 1` core):

| source | floor ms | why irreducible |
|---|---|---|
| CE inference (onnx-int8, backend) | ~240–360 | model forward pass on `--cpus 2`; only HW/model change reduces further |
| PPR networkx | 0–620 (query-dependent) | in-process CPU on `--cpus 1`; iterative pagerank, unavoidable when entities present |
| FTS + KNN (backend) | ~65 | single Surreal FTS + HNSW round-trips, already minimal |
| batched fusion fetch (after fix) | ~25–40 | one `WHERE id IN [...]` round-trip: 1×RTT + server exec |
| priors (v5.96, backend) | ~150 | two batched round-trips, already optimal |
| pipeline / dispatch overhead | ~50 | Python orchestration |

**Reducible-vs-floor math (fusion N+1):** N = 52 round-trips, RTT = 18.7 ms,
batched round-trip ≈ 25–40 ms. Reducible = (52−1) × 18.7 ≈ **953 ms** of pure
network round-trips; floor = one batched query exec (~25–40 ms). Cleanly
separates batchable network RTT from surreal exec (the HW floor).

---

## 3. Prioritized Tuning List

Ordered by ROI (ms saved ÷ risk). Each: fix | expected ms | risk | HW-limited after?

**1. Batch the fusion final-result fetch — `fusion.py:297-305`.**
- Expected: **−~950 ms** (HIT 2739 → ~1790; MISS 3934 → ~2980).
- Fix: replace the per-id `get_memory` loop with one `get_memories(ids)` batch
  (`SELECT * FROM memory WHERE id IN [...]`), mirroring the v5.96 priors template
  (`storage/memory.py:1029-1064`), preserving `heat >= min_heat` filter + fused
  ordering in Python.
- Risk: **LOW** — exact pattern already shipped & tested for priors (v5.96); pure
  read; ordering/filter reproducible in-memory. Needs a parity test (batched result
  == old per-id semantics) like `tests/test_v5_96_prior_batch.py`.
- HW-limited after? No — this removes pure network RTT, not HW work.

**2. Fold MMR embed re-fetch into the batched fusion result — `_reranking_mmr.py:17-43`.**
- Expected: **−~183 ms marginal** (FREE synergy with #1 — near-zero extra code).
  NOTE: this is *marginal wall time*, not 328 ms — MMR's re-fetch overlaps CE
  inside the 903 ms `ce_rerank` stage (§1 note), so it draws from the *same* stage
  as #3 and the two cannot be summed independently against the grand total.
- Fix: `_collect_candidate_embeddings` should read `mem.get("embedding")` from the
  already-fetched fused result dict instead of re-calling `storage.get_memory`.
  The row carries `embedding` (SELECT *), and #1's batch fetch already returns it,
  so the re-fetch is pure redundancy.
- Risk: **LOW** — read from an in-memory dict already present; guard for the
  rare candidate not in the fused set (fall back to single fetch).
- HW-limited after? No — removes redundant I/O.

**3. Flip CE to onnx-int8 — `CROSS_ENCODER_BACKEND=onnx-int8` (`config.py:191`).**
- Expected: **−~360–480 ms** (CE 720 → ~240–360; the v5.85 claim is 2–4× with ~1%
  accuracy hit).
- **Not a pure config flip on this box — runtime is BROKEN.** Verified 2026-07-02:
  the quantized artifact IS present (`~/.cache/huggingface/hub/models--cross-encoder--ms-marco-MiniLM-L-6-v2/snapshots/*/onnx/model_qint8_avx512.onnx`)
  and `optimum` imports, BUT `import onnxruntime` fails (`ImportError: import numpy
  failed` — numpy broken under the py3.14 venv). So `CROSS_ENCODER_BACKEND=onnx-int8`
  would fail at model-load today. This is **unfinished setup work**, not a flip.
- Fix: (a) repair the onnxruntime↔numpy install in the venv, verify
  `import onnxruntime` succeeds; (b) then set `CROSS_ENCODER_BACKEND=onnx-int8`;
  (c) measure golden-set recall-quality delta before adopting.
- Risk: **MEDIUM-HIGH** (was MEDIUM) — contingent on fixing the runtime first;
  ~1% rerank accuracy hit; onnx runtime RAM footprint on a RAM-constrained box
  (verify it fits). Reversible via the env var once the runtime works.
- HW-limited after? Yes for CE — post-flip CE inference is the model/HW floor.
  The ~1.0 s target floor is **contingent on this being fixed**; if not fixed, CE
  stays at ~720 ms and the floor is ~360 ms higher.

**4. (Conditional) Reduce CE candidate count.**
- Expected: linear in candidates — CE cost ≈ (candidates) × ~271 ms/pair-call at
  fp32 (uncertain: no k-scaling curve captured; treat as per-call × count). At
  `max_results=5` the multi-passage wrapper issues ~2.7 RPC calls; trimming
  candidate breadth cuts calls proportionally.
- Risk: **MEDIUM** — directly trades recall quality for latency; only pursue if
  #3's onnx win is insufficient. Quantify k=5/10/20 scaling first.
- HW-limited after? Partially — fewer inferences, same per-inference floor.

**Explicitly LOW priority / do NOT chase on this box:**

**5. Parallelize independent pipeline stages.** REJECT on this hardware. The core
is `--cpus 1` — CPU-parallelism buys nothing. Only I/O overlap could help, but
after #1 the remaining big costs are CE (serial, after fusion) and PPR (CPU-bound
networkx) which don't overlap usefully. **Batching beats parallelizing here.**
Not worth the concurrency-bug risk on a single core.

**6. Batch spreading-activation adjacency (per-depth not per-entity).** LATENT, not
current. Spreading measured 0 ms (untriggered). Per ADR-0030 it is a genuine
N-round-trip BFS and *is* the one non-batchable-into-one-query stage — but the
adjacency queries *within a BFS depth* could be batched per-depth (`WHERE in IN
[frontier]`) rather than per-entity. Defer until an entity-dense query set shows
it firing; instrument first. Risk: MEDIUM (traversal correctness). Not on the
current critical path.

**7. Query→ranked-output result cache (plan lever `a`).** Out of scope for the
"floor" (it *skips* work, doesn't make work faster) but it is the only lever that
removes CE + PPR + spreading entirely on a cache HIT. Shadow hit-rate counter is
already live (v5.96); gate a real output cache on measured interleaved hit-rate.

---

## 4. Verdict — How Close Is 2.4s to the Floor?

The "2.4s" figure is stale and mis-attributed. Reality:

- **Current warm HIT total ≈ 2.74 s**, and it is a **range, not a point** — PPR
  alone swings 0 → 620 ms on query entity-richness, so an entity-poor query is
  ~2.1 s and an entity-rich one ~2.7 s today.
- **~950 ms of the current total is pure reducible overhead** in the fusion N+1,
  plus a bounded `ce_rerank` reduction (MMR fold-in + CE onnx-int8, drawn from the
  *same* 903 ms stage), plus a shipped-but-not-yet-runnable CE quantization win.
- **Headline (accurate, not the mis-stated "relocated"):** v5.96 correctly removed
  the *priors* N+1 (~1.5 s per the cited plan). The fusion final-fetch N+1 (~1.1 s,
  `fusion.py:297-305`) is a **distinct, pre-existing** N+1 site — a *second*
  location of the same round-trip anti-pattern — now the dominant cost. (Pre-v5.96
  warm total was not re-measured — checkout was out of scope — so calling it "the
  same N+1 relocated" would be unverified; it is two separate sites.)

**Ordered plan to reach "can't improve more on this hardware":**

1. Batch fusion fetch (#1) → −~950 ms → **~1.79 s**
2. + Fold MMR into #1's result (#2) and flip CE onnx-int8 (#3) — **both reduce the
   SAME `ce_rerank` stage**, so bound them jointly, not additively. `ce_rerank`
   goes 903 ms → **~350–500 ms** (CE onnx floor ~240–360 ms + non-overlappable
   residual). Net from step 1: −~400–550 ms → **~1.24–1.39 s** (contingent on the
   onnx runtime being fixed — see #3; if unfixed, CE stays ~720 ms and this stage
   only drops to ~720 ms via MMR fold-in, landing ~1.6 s).

**Resulting hardware floor ≈ 0.9–1.4 s** (range; the low end requires the onnx
runtime repair), composed of:
- CE inference (onnx-int8) ~240–360 ms — backend `--cpus 2` model floor
- PPR networkx 0–620 ms — `--cpus 1` core CPU floor, query-dependent
- FTS + KNN ~65 ms + batched fusion fetch ~25–40 ms + priors ~150 ms — surreal
  exec floor on `--cpus 2`
- overhead ~50 ms

After steps 1–3, the remaining cost is **CE model inference + networkx PageRank +
minimal Surreal round-trips** — all bounded by CPU/RAM on this box. Reaching below
~0.9–1.4 s requires *either* more CPU/RAM *or* the output cache (lever `a`, which
skips the work rather than speeding the hardware). **At that point recall cannot be
made faster on the current constraint without adding CPU/RAM.**

---

## Appendix — Primitive Floors (measured)

| primitive | p50 ms | note |
|---|---|---|
| single `get_memory` by-id RTT | 18.69 | 1 HTTP POST to SurrealDB (backend) |
| batched priors, N=5 | 60.9 total (12.2/id) | one round-trip, confirms batch works |
| embed query MISS | 11.8 | backend inference |
| embed query HIT | 0.0 | in-process query cache |
| embed inference delta (miss−hit) | 11.8 | backend embed compute |
| CE `_rerank_rpc` per call | ~271 | backend CE fp32 inference (`st`) |
| CE total per recall | ~720 | ~2.7 calls at `max_results=5` |
| MMR `_collect_candidate_embeddings` | ~328 | redundant per-candidate re-fetch |

Provenance: standalone off-cgroup profiler, live backend, read-only, write-free
`recall_via_pipeline`, sequential. Stage timings from `state.stage_stats`
(`pipeline.py:175-187`); call counts + within-stage split from cProfile.
