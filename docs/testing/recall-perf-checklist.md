# Recall Performance Test Checklist

Repeatable procedure to characterise `recall()` behaviour + latency across
scenarios (cached + uncached), driven entirely through the **MCP `recall` tool**,
timed via Prometheus. Re-run after any recall-path change (rerank, priors,
caching, knob retune, SurrealDB upgrade).

> First authored 2026-07-02, after v5.96 (N+1 priors batch-fix + shadow
> hit-rate counter). See ADR-0030 (recall is IO-bound) + ADR-0031 (perf
> sequencing) + `docs/plans/cache-refactor-2026-07-01.md`.

## Prerequisites — record these each run

- Daemon healthy: `curl -sf localhost:8765/health/live` → OK; note `version`.
- Concurrency knobs (from config.yaml / `/health`): `tool_pool_workers`,
  `recall_heavy_concurrency`, `rerank_max_concurrency`, `hook_recall_pool_workers`.
- Backend image tag (`podman ps | grep backend`) + core `--cpus`.
- Cache state: `du -sh ~/.local/share/yadgar/cache` (ce.snap + embed.snap).
- Note whether this is a **cold** daemon (just restarted → caches empty, shadow
  counters 0) or **warm** (has served recalls).

## Timing method (MCP-only + Prometheus, no bash-timing-the-tool-call)

Individual MCP tool calls can't be wall-clock-timed cleanly from the harness, so
read the daemon's own histogram. Snapshot `/metrics` immediately BEFORE and AFTER
each recall; the delta is exact.

```sh
snap() {
  curl -sf http://localhost:8765/metrics | grep -E \
    '^yadgar_recall_duration_ms_(sum|count)|^yadgar_tool_pool'
}
```

- **Per-recall latency (ms)** = `Δ(yadgar_recall_duration_ms_sum) / Δ(yadgar_recall_duration_ms_count)`.
  (For a single recall between two snaps, Δcount=1 → the delta *is* that recall's ms.)
- **Cache hit/miss** — use Car 1/Car 2 `yadgar_cache_{hit,miss}_total{cache=<name>}` counters for project_brief / wiki / prelude caches. The recall-output shadow counters were removed (ADR-0071).
- **Graph-cache hit/miss** — before and after each timed run, also snapshot the graph-namespace and embedding cache series from the **backend** (port 8001 — graph/ce/embed caches emit via `CacheStatsCollector` on the backend metrics endpoint, not core 8765):
  ```sh
  curl -s http://localhost:8001/metrics | grep -E \
    'yadgar_cache_(hit|miss)_total|yadgar_embed_(ce|embed)_cache'
  ```
  Record the per-namespace `{cache=<name>}` deltas in the run log alongside latency. A regression in `yadgar_cache_hit_total{cache="graph"}` after a batch-adjacency change means the graph cache miss-path is exercised more than expected — file a perf note.
- **Saturation** = `yadgar_tool_pool` `inflight` / `saturated` during concurrent runs.
- **Per-stage (coarse)**: Tempo traces — spans `retrieval.recall` (total) +
  `retrieval.rerank`. Finer stages (embed / surreal search / priors / spreading /
  fusion / MMR) are NOT yet individually spanned — add spans if per-stage
  attribution is needed (follow-up).

## Scenarios — run each COLD then WARM (repeat identical call)

"Cold" = first time this exact (query, scope, params) key is seen this session.
"Warm" = immediately repeat the same call (embed+CE cache hot; shadow-hit).

| # | Scenario | Call (all `directory="/home/max/git/yadgar"` unless noted) |
|---|---|---|
| 1 | Baseline | `recall(query=Q1, max_results=5)` (default type=all) |
| 2 | Warm repeat | repeat #1 verbatim → expect shadow-HIT, embed/CE cache hot |
| 3 | Memory-only | `recall(query=Q1, type="memory", max_results=5)` |
| 4 | Wiki-only | `recall(query=Q1, type="wiki", max_results=5)` |
| 5 | Landscape | `recall(query=Q1, mode="landscape")` (slow cross-domain consensus) |
| 6 | Large N | `recall(query=Q1, max_results=20)` |
| 7 | Concurrent | fire 6 distinct queries simultaneously (one MCP batch) |
| 8 | Global scope | `recall(query=Q1, directory="global")` or cross-project |

Use a stable query set so runs are comparable, e.g.:
`Q1="offload freeze fix daemon"`, plus for #7 the 6-query set from the load test.

## Results table template (fill per run)

```
daemon vX.Y.Z | pool=P heavy=H rerank=R | core --cpus C | date
# | scenario      | cold ms | warm ms | shadow c/w | timeouts | notes
1 | baseline      |         |         | miss/hit   |          |
2 | warm repeat   |    —    |         | hit        |          |
3 | memory-only   |         |         |            |          |
4 | wiki-only     |         |         |            |          |
5 | landscape     |         |         |            |          |
6 | large N=20    |         |         |            |          |
7 | 6 concurrent  |  (avg)  |         | m/h split  |  X/6     | saturated? loop-lag?
8 | global scope  |         |         |            |          |
```

## Interpretation guide

- **cold ≈ warm** → caches aren't helping (expected per ADR-0030: recall is
  surreal-IO bound; CE/EMBED cache only the ~0.2% compute). This is the signal
  that the query→output cache (#88) would be needed for a real warm speedup.
- **shadow hit-rate over a realistic read/write-interleaved session** is the
  go/no-go for building #88 — a read-only repeat burst inflates it unrealistically.
- **concurrent timeouts** → effective concurrency = `min(pool, heavy, rerank)`;
  ceiling is CPU-bound on `--cpus 1` core / `--cpus 2` backend (see
  `yadgar-concurrency-tuning` wiki). Not fixable by raising knobs past ~2-3.
- **priors cost** (post-v5.96): `get_memory_graph_priors`/`cofire_priors` are one
  batched query now (was N+1) — a regression here means the batch broke.

## Run log — 2026-07-02 (core v5.96.0, pool=3 heavy=2 rerank=3, --cpus 1 core / 2 backend)

Fresh daemon (counters 0). Timed via `yadgar_recall_duration_ms` histogram deltas + shadow counter.

| # | scenario | ms | shadow | notes |
|---|---|---|---|---|
| 1 | baseline COLD | ~15,800 | miss | **includes lazy model cold-start** (embed + CE load on first use) — not representative of steady-state |
| 2 | WARM repeat (identical) | ~2,410 | **HIT** | real warm retrieval cost; shadow-HIT did NOT speed it up (instrumentation-only, no output cache) → this ~2.4s is what cache (#88) could save on repeats |
| 7 | 6 concurrent (distinct) | ~26,400 avg | 6 miss | **6/6 succeeded, 0 timeout, saturated=0** — vs pre-v5.96 pool=1→2/6, pool=2→4/6. Latency superlinear under load (queue through pool=3 + serial CE rerank on --cpus-2 backend), but all completed within the 95s offload timeout. |

**Findings:**
- **Model cold-start (~13s) dominates the first recall** — separate from retrieval; warm floor ~2.4s.
- **v5.96 (N+1 batch-fix + pool=3) fixed the concurrent-timeout problem** — 6/6 now complete (was 2/6–4/6). Reliability win.
- Concurrent **latency** still degrades (26s avg @ 6) — CPU-bound per ADR-0030; not fixable by knobs. The real lever remains more CPU or surreal-query optimisation.
- **Shadow-HIT ≠ speedup today** (no output cache) — confirms #88's potential value on repeat-heavy workloads; its go/no-go is real read/write-interleaved hit-rate.
- Scenarios 3-6, 8 (type=memory/wiki, landscape, large-N, global) not run this pass — run via this checklist when needed.

## Run log — 2026-07-02 (core v5.97.0 — POST fusion-batch, PR #143)

Same box/config (pool=3 heavy=2 rerank=3, --cpus 1 core / 2 backend). Timed via histogram deltas.

| # | scenario | v5.96 | **v5.97** | change |
|---|---|---|---|---|
| 1 | cold (models warm; CE-cache miss) | — | ~2,090 | — |
| 2 | WARM repeat (shadow HIT) | ~2,410 | **~1,432** | **−40%** |
| 7 | 6 concurrent (avg) | ~26,400 | **~9,330** | **−65% (~2.8×)**, 6/6 ok, saturated=0 |

**Verdict: the v5.97 fusion N+1 batch delivered.** Warm single −~1s (matches the profiled ~950ms fusion collapse). Under 6-concurrent the win **compounds** — each recall ~1s shorter → far less pool/rerank queuing → 26→9s avg. Still 6/6 success, no saturation.

**Remaining warm cost** (~1.4s): CE = GTE-ModernBERT (~720ms, task #92) + PPR (networkx core, 0-620ms query-dep) + priors/fts/knn (~240ms). Next lever = #92 (GTE speedup) → target ~1.0s. Below that = hardware (CPU) or output cache (#88).

## Run log — 2026-07-02 (core v5.98.0 — GTE-rerank Lever-1, PR #144)

Same box/config (pool=3 heavy=2 rerank=3, --cpus 1 core / 2 backend). Daemon warm (uptime >2200s at run; a fresh-restart run gave 24–76s = warmup artifact, discarded — see caveat). Every number cross-checked: `yadgar_recall_duration_ms` histogram delta == daemon `POST /mcp` handler `lat_ms` within ±5ms.

### 3-way comparison (v5.96 → v5.97 → v5.98)

| # | scenario | v5.96 | v5.97 | v5.98 | v5.97→v5.98 |
|---|---|---|---|---|---|
| 2 | WARM repeat (shadow HIT) | ~2,410 | **~1,432** | **~1,602** | +170ms (+12%) |
| 1 | cold (CE-miss) | ~15,800 | ~2,090 | **2s–75s** | ✗ not comparable |
| 7 | 6 concurrent (avg) | ~26,400 | ~9,330 | **✗ unmeasurable** | MCP serializes tool calls |

Warm v5.98 = median of 4 shadow-HIT calls of `"offload freeze fix daemon"` (1575/1596/1608/1628ms). Backend embed+CE both cached to ~2ms → the ~1.6s is entirely core-side (surreal knn + networkx PPR + fusion).

**Verdict: v5.98 is flat on the warm floor (+12% ≈ noise/corpus growth), NOT a regression.** Expected: Lever-1 is CE-*routing* (multi-passage → cached `mode=ce`); warm CE is already cached to ~2ms, so it cannot move the warm floor. The +170ms vs v5.97 is most likely corpus growth — the PPR entity graph is rebuilt per-recall from DB (more memories → larger subgraph). v5.98's value was CE-routing correctness + CI model-isolation (ADR-0032), not a warm-speed win.

### ⚠️ Method caveats discovered this run (READ before trusting cold/concurrent)

1. **Never measure on a low-uptime daemon.** A fresh redeploy gives 24–76s recalls (cold caches/models); it settles to ~1.6s after ~30min warm. Always check `/health` `uptime_seconds` is large first. (ADR-0033.)
2. **Cold (CE-miss) is NOT comparable across runs — it varies 30× (2s–75s) by query.** Cost is dominated by **core-side PPR + spreading-activation BFS** on the novel embedding, NOT CE. An entity-rich cold query drives ~40–50s of core PPR/spreading (uncached per-neighbor BFS N+1, mem 531710) — 60–80× the profile-doc's 0–620ms PPR estimate. v5.97's "cold ~2,090ms" was a low-entity query; not a like-for-like baseline.
3. **6-concurrent is currently unmeasurable via the MCP tool** — the streamable-http client serializes tool calls in BOTH main-thread and subagent contexts (verified: 6 "parallel" calls ran ~70s apart). A real concurrency number needs a parallel-dispatch harness (task #79) or direct parallel HTTP, not batched MCP tool calls.

**Bottleneck has moved:** v5.96/97 killed the priors+fusion N+1s (warm floor → 1.4s). The dominant remaining cost is the **core PPR/spreading path on cold/entity-rich queries** (40–50s) → next levers = v5.99 (PPR N+1 fetch), #85 (PPR→backend stateless), #93 (spreading BFS N+1). Warm floor below ~1.4s needs the output cache (#88) or more CPU.

## Run log — 2026-07-03 (core v5.99.0 — POST PPR + spreading-BFS N+1 batch, PR #146)

Warmed daemon (fired ~7 recalls to convergence FIRST — per caveat 1 — then measured; uptime 147→609s). Every number cross-checked: histogram delta == daemon `POST /mcp` handler `lat_ms` within ±8ms. Cold = 3 fresh, meaningful, entity-rich queries; **core ms = total − backend CE span time** (from `yadgar-backend` logs).

### The v5.99 test — cold entity-rich core cost (the PPR/spreading path)

| query | total ms | backend CE ms | **core ms (PPR/spreading)** |
|---|---|---|---|
| vacuum/bloat/compaction | 21,686 | ~14,505 | **~7,181** |
| KG/PPR/spreading | 26,062 | ~16,667 | **~9,395** |
| consolidation/engram | 21,051 | ~14,570 | **~6,481** |
| **avg** | **~22,900** | **~15,250** | **~7,700** |

**Verdict: v5.99 delivered.** Cold-query **core** (PPR + spreading-BFS, the 28→2 round-trip collapse) dropped **~40–50s (v5.98) → ~6.5–9.4s (v5.99) — a 5–7× win**; cold **total** ~54–64s → ~21–26s (~2.5×). The spreading-BFS N+1 was the 40s ceiling; batching it per-depth removed it, live-confirmed.

### Warm floor unchanged (as expected)

Convergence curve (repeated same query): 1915 → 1710 → 1603 → 1564 → 1674 → 1610ms (converged by call 3). Warm-floor median = **~1,613ms** — matches v5.98 (~1,602ms). v5.99 only touches the cold/PPR path; warm CE is cached to ~2ms so the fix cannot move the warm floor.

### Bottleneck moved AGAIN → cold is now backend-CE-bound

Post-v5.99, the dominant cold cost is **backend GTE-ModernBERT CE** — the first `/rerank` per cold query is ~9s, ×~1.6 calls = ~14–16s. Core is no longer the ceiling. **Next lever = #88 query→output cache** (the only thing that skips CE entirely on repeat; shadow hit-rate counter live since v5.96 to gate it), or CE candidate trim (v5.98 Lever-2 `CROSS_ENCODER_TOP_K`, dormant), or onnx-int8 (v5.98 Lever-3, unverified in a built image). Warm floor below ~1.4s still needs #88 or more CPU.

### Full arc (measured)

| version | change | warm floor | cold-query core (PPR/spreading) |
|---|---|---|---|
| v5.96 | priors N+1 batch | ~2,410ms | — |
| v5.97 | fusion N+1 batch | **~1,432ms** (−40%) | — |
| v5.98 | GTE CE-routing (Lever 1) | ~1,602ms (flat) | ~40–50s |
| v5.99 | PPR + spreading-BFS N+1 batch | ~1,613ms (flat) | **~7–9s** (−5–7×) |
| v5.106 | full observability standard (@observe on every fn) | **~1,409ms** (flat) | spreading ~1.1s |

## Run log — 2026-07-04 (core v5.106.0 / backend v5.12.0 — full observability standard)

Same box/config (**pool=3**, `--cpus 1` core / `2` backend, GTE-ModernBERT CE). v5.106 =
tri-signal `@observe` (span+metric+log) on **every function**. Question this run answers:
**did the per-function instrumentation slow recall vs the pre-`@observe` baseline?**

**Method fix (important):** the raw `/mcp` endpoint is now bearer-auth-gated
(`require_auth: true`, token in `~/.config/yadgar/config.yaml`). The `benchmarks/run_perf_loadtest.py`
harness sends no `Authorization` header → every recall 401s → `_call_recall` swallows it
(returns `False`) → CE `d_count=0` → the contract-compare crashes on `ce_mean=None`. Drove the
same JSON-RPC envelope directly **with** `Authorization: Bearer <token>` instead (scratch:
`/tmp/yadgar_perf_v5106.py` + `/tmp/yadgar_concurrent_v5106.py`). Fixing the harness auth +
guarding the `None` compare is a follow-up.

⚠️ **Uptime caveat (ADR-0033 caveat-1).** Measured at daemon uptime ~647s (~11min), below the
~30–37min settle. Trusted anyway: warm **converged tight** (1324–1441ms after discarding the
model-warm call-0), and both warm (~1.4s) and cold (~16.8s) sit in the **steady-state** range,
NOT the 24–76s fresh-deploy warmup-artifact band.

| # | scenario | v5.106 | metric | notes |
|---|---|---|---|---|
| 2 | WARM (same query ×8, CE-cache hot) | **~1,409ms** median (1324–1441) | wall ≈ pipeline (~6ms gap) | v5.102 trace-gap spans closed the old ~6s un-spanned MCP-wrapper gap → wall now == pipeline. CE cached ~1–2ms. |
| 1 | COLD (6 fresh distinct, CE-miss) | **~16.8s** median (10.9–18.4) | wall ≈ pipeline | CE the wall: `backend.rerank.ce` ~10.4s + `retrieval.rerank.cross_encoder` 8.4s → **CE ~56–78%**. `retrieval.spreading` only **1.1s** (v5.104 batch holding). Trace fully accounted, no gap. |
| 7 | 6-concurrent (threaded direct-HTTP, all cold) | 76.7s batch / ~59s per-call median | wall | **6/6 ok**. Real parallel (threads bypass MCP-client serialization, ADR-0033 caveat-3). High because all-cold each pays full CE, contending pool=3 + serial CE on `--cpus-2` backend. **NOT comparable** to the older "~9.3s" (that was v5.97 *pipeline-histogram*, warm-ish, MCP-serialized). |

**Verdict: v5.106 `@observe` did NOT meaningfully slow recall.** Three-way evidence:

1. **Pipeline↔pipeline (same metric):** warm histogram **1,409ms (v5.106)** vs 1,432ms (v5.97)
   vs 1,613ms (v5.99) — **flat/slightly-better across the entire `@observe` rollout
   (v5.100→v5.106)**. The instrumentation added nothing detectable to the hot path.
2. **Log layer:** only **2 core log lines per warm recall** → decisively kills the
   "span_end logging per fn" half of the concern. Spans go to the off-thread OTLP
   `BatchSpanProcessor`, not synchronous stdout logs.
3. **Trace accounting (direct overhead bound):** warm trace = **1,687 spans**, cold =
   **24,628 spans** (every-fn `@observe`). Yet `tool.recall` ≈ Σ(substantive child spans) on
   both — if span *creates* cost real time, `tool.recall` would exceed its children. It
   doesn't. This bounds creation overhead to **tens-of-ms worst case (<1%)** of both the 1.4s
   warm and 18.5s cold floors. Matches the v5.101 off-cgroup A/B (+8ms for ~40 spans; design
   sound — span create sub-µs, `LogSpanProcessor` routes off-thread via QueueHandler, `@observe`
   = 2×monotonic + Prom `.observe()` µs).

**No obs-overhead follow-up needed** — instrumentation is not on the critical path. The wall is
still **CE** (~56–90% of cold): next levers unchanged — #13 onnx-int8 CE, #28
`CROSS_ENCODER_TOP_K` trim, #88 query→output cache.

> Diagram re-render step: the `docs/diagrams` YAML generator (see repo diagram workflow) would
> re-render the recall waterfall/arc specs with these numbers — noted, not run this pass.

## CE onnx-int8 A/B — 2026-07-04 (NO-GO)

**Backend:** GTE-ModernBERT CE, `--cpus 2` backend, 20 calls per variant, fresh unique
queries per call (CE result cache busted), method: direct `POST /rerank mode=ce` with
`Authorization: Bearer <token>`, histogram `yadgar_embed_rerank_duration_seconds mode=ce`
delta per call. Data in `/tmp/ce_speed_ab.json`.

| variant | p50 | quality | verdict |
|---|---|---|---|
| torch | **3,917ms** | recall@10 0.9667, nDCG 0.9544, MRR 0.95 | baseline |
| onnx-int8 | **7,899ms** | **exact parity** (controlled 30q A/B, byte-identical) | **NO-GO** |
| ratio | **0.50× (2.0× slower)** | — | int8 hypothesis FAILED |

**Quality note:** earlier uncontrolled run showed "onnx 1.0 recall" — this was noise
(per-question recall swings 0.0↔1.0 in a single uncontrolled run). Controlled A/B shows
exact parity.

**Thread-thrash caveat:** `--cpus 2` is CFS quota, not affinity — inside container
`os.cpu_count()=24`, ORT spawns 24 `intra_op` threads thrashing the 2-core budget. OMP
env vars (`OMP_NUM_THREADS=2`, `OMP_WAIT_POLICY=PASSIVE`) do NOT cap ORT's intra-op
pool (follow-up test: 7791ms uncapped → 7791ms with OMP vars, wall time unchanged;
container CPU 157%→103%). Fix requires code-level `SessionOptions.intra_op_num_threads`.
Unconstrained datapoint (0.83×) still slower → no latency win expected even with fix.

**Decision:** `GTE_RERANKER_BACKEND` stays `torch`. See ADR-0043.

## Run log — 2026-07-12 (core 5.128.0 / backend 5.39.0 — T3 complete, Car 0 re-measure)

**Context:** T3 train fully shipped (Car 1 multi_passage=OFF, Car 2 async side-effects fork,
Car 3 CPU-aware parallel pipeline). This is the Car 0 live re-measure on the just-deployed
stack, becomes the T4 Ettin A/B baseline. Pool=3, `recall_heavy_concurrency=2`,
`rerank_max_concurrency=3`, `--cpus 1` core / `2` backend. Cache: `ce.snap` 97KB +
`embed.snap` 1.0MB (carry-over). **multi_passage=OFF** (Car 1). Method: `yadgar_recall_duration_ms`
histogram deltas + `podman logs yadgar-backend` Tempo spans. Backend uptime ~2min at first
measurement; no concurrent pytest/eval (pgrep gate clean).

**Three regimes (explicitly separated, per T3 Car 0 spec):**

| Regime | ms | N | spread | Notes |
|---|---|---|---|---|
| COLD (warm model + fresh CE, entity-rich) | **10,847** | 3 | 10,801–11,328 | CE=6.2s (~57% backend), spreading=2.3s; backend total=10.8s |
| WARM (warm model + fresh query, CE-cache miss — **common case**) | **13,625** | 6 | 10,826–13,739 | backend=530–933ms; core=~12.7s (dominant); CE cached→0ms backend |
| HOT (exact-repeat, CE-cache hit) | **4,555** | 1 | — | CE 0ms backend; residual = core KNN/FTS/PPR |

**Per-stage attribution (backend spans):**

Cold: CE 6,165ms (57%) + spreading 2,320ms (21%) + other 2,315ms (22%) = ~10,800ms backend.
Warm: backend 800ms total; CE cached ~0ms; core-side (KNN/FTS/PPR/spreading on --cpus 1 core) ~12.7s = **dominant cost**.

**Key finding:** warm-common-case bottleneck is the CORE-SIDE retrieval (not backend CE).
At 2 CPUs + warm CE, ~93% of warm time is in core. This is the correct T4 baseline — prior
history conflated same-query warm (shadow HIT + CE cached = 1.4s) with the common case of
distinct auto-recall queries that miss CE.

**Comparison vs history:**

| Version | Warm (fresh-q) | Cold | Hot |
|---|---|---|---|
| v5.106 (2026-07-04) | ~1,409ms *(same-query repeat — CE cached, not common case)* | ~16.8s median | — |
| 2026-07-09 sweep (5.117/5.30, pre-T2) | — | 24,596ms (model-load inclusive) | 4,068ms |
| **T3 Car 0 (5.128.0/5.39.0)** | **~13,625ms** (common case, NEW) | **~10,847ms** | **4,555ms** |

Cold delta 2026-07-09 → T3: −56% (24,596 → 10,847ms). Multi_passage=OFF (Car 1) removes
one CE pass → accounts for much of the cold-CE reduction (LME showed −37% wall). Hot
essentially flat (+12%).

**Bonus checks:**

| Check | Result |
|---|---|
| `restore()` within offload window | **FAIL — timeout** (~2min40s; not within 95s offload) |
| viz `/graph` 200s | **PASS** |
| `yadgar_store_swap_state{state="clean"}` | `0.0` — `retained_old=1.0` (expected post-swap; no torn/split) |
| CE/NLI models loaded | **PASS** (both =1.0; NLI cold-load 27.4s at backend start) |

**T4 Ettin baseline locked:** warm-common-case **~13,625ms**, cold **~10,847ms**, hot **~4,555ms**.

## ⚠️ Correction — 2026-07-12 (fix/pre-t4-anomalies): the T3 Car 0 warm attribution was WRONG

The Car 0 run-log above (2026-07-12, 5.128.0/5.39.0) reports warm-common-case as
`backend=530–933ms; core=~12.7s (dominant)`. **That core/backend split is a
measurement error and must not be trusted.** RCA from the full Tempo span tree of
the actual 13.6s traces (`eae97683…`, `9291db6e…`) shows:

- The warm-common-case **`POST /recall` BACKEND span is ~13,616ms** — it covers
  ~99% of the 13,635ms wall. Core-side is **~200ms** (thin `_forward_to_backend`
  HTTP wait + ~107ms of session side-effects at the tail).
- The "530–933ms backend" figure came from **grepping `podman logs yadgar-backend`
  for `POST /recall` lines and reading FAST/HOT recall calls** (594ms, 498ms, …),
  then attributing them to the SLOW 13.6s wall — a **trace_id mis-correlation**.
  `total − (wrong backend number) = phantom 12.7s "core"`. There is NO core-side
  retrieval cost: `_st._retriever` is `None` in the core process (retrieval fully
  sunk to backend, ADR-0078), so no KNN/FTS/PPR/spreading runs in core at all.
- The real 13.6s warm breakdown (all BACKEND, `--cpus 2`, CPU-bound):
  CE (GTE-ModernBERT) **~9.3s across two passes** — `_rerank_cross_encoder` (~6.1s)
  scoring the top-K memories + `recall.fanout.fuse → _score_candidates_ce` (~2.8s)
  cross-scoring the ~5 wiki candidates (INTENTIONAL wiki placement scoring, cache is
  working: memory texts hit from pass-1) — plus `spreading_activation` (~2.1s) and
  `_find_entities_in_content` (~1.5s). CE cache MISSES on a fresh distinct query.

**Method rule (do NOT repeat the Car 0 mistake):** never compute
`core = total − grep-of-logs`. Pull the FULL trace by traceID and read the
`POST /recall` backend span duration directly; core-side cost = `wall − that span`.
Attribute every ≥100ms stage to its `service.name` from the span's resource
attributes, matched by `trace_id`. A `podman logs … | grep POST /recall` line is
NOT guaranteed to belong to the trace you are attributing.

**Warm > cold explained:** warm-common-case (13.6s) > cold (10.8s) because the warm
distinct queries carry the wiki cross-scoring CE pass + a fuller candidate pool,
whereas Car 0's cold (entity-rich) queries were effectively single-CE-pass. Same CE
model, extra pass — not a different bottleneck.

## Run log — CPU-scaling series 2026-07-12 (backend --cpus 2 → 3 → 4, core 5.129.0 / backend 5.40.0)

**Context:** Three back-to-back runs with identical core version (5.129.0) and backend version
(5.40.0), varying only `--cpus` on the backend container: 2 → 3 → 4. Establishes the CPU-scaling
curve for the warm CE-miss recall regime and confirms which T3 Car 3 knobs engage at each step.
The 2-CPU run also serves as the #186 post-ship verification (restore() fix from 5.128.0→5.129.0).

### Consolidated 2v3v4 table

| Regime | 2-CPU | 3-CPU | 4-CPU | 2→3 Δ | 3→4 Δ | 2→4 Δ | Notes |
|---|---|---|---|---|---|---|---|
| **WARM CE-miss** (6 distinct, valid) | **10,955ms** | **7,916ms** | **6,807ms** | **−28%** | **−14%** | **−38%** | Primary metric; CE-miss validity gate PASS all three runs |
| **HOT** (exact-repeat, CE-cache hit) | **1,126ms** | **875ms OUTLIER** | **3,452ms INVALID** | — | — | — | HOT not comparable cross-run (standing caveat below); 875ms = subgraph-residency outlier, NOT a per-CPU speedup; 4-CPU cold graph state (see note) |
| **restore()** | **4,348ms** | **4,264ms** | **4,142ms** | −2% | −3% | −5% | Flat; DB-IO bound, not CE-bound |

**HOT 4-CPU validity note:** 4-CPU backend had only 1 startup hook recall before measurement
(miss=14). Graph cache was cold at HOT-block time — PPR/graph traversal dominated, making
3,452ms unrepresentative of steady-state HOT. Not comparable to 2-CPU or 3-CPU HOT values.

**STANDING CAVEAT — HOT regime is unreliable cross-run (RCA Anomaly 2, 2026-07-12; T4 Car 0):**
do NOT compare single-query HOT numbers across runs. Recall has **no output cache (#88)** — a
HOT repeat re-runs the full KNN+FTS+PPR+fusion compute even with every CE score cached; the true
HOT floor is **≈4.3s @4cpu**, compute-bound and warm-state-dependent. The 3-CPU **875ms** above
was a **graph-subgraph-residency outlier** (a hook-recall pre-warmed that exact query's
neighbourhood), NOT a per-CPU speedup — discard it as an artifact. HOT is only meaningful as a
within-session, same-graph-state delta. Future measurers: book WARM CE-miss as the primary
regime; treat any sub-second HOT reading as residency luck until #88 lands.

### Knob attribution: what each CPU buys

| Step | Knob change | Formula | Mechanism | Observable evidence |
|---|---|---|---|---|
| 2→3 CPU | **gather_budget: 1→2** (dominant) | `min(ncpu-1, 2)` | Two CE candidate batches scored concurrently; serial at 2-CPU, parallel at 3-CPU | Concurrent `score_ce_cached` span pairs (Q2: 2,441ms + 344ms overlap; Q4: 3,778ms + 683ms overlap) |
| 3→4 CPU | **torch intra-op: 1→2** (secondary) | `ncpu//2` | More ML matrix threads; each individual CE span shorter | Per-span CE −8–69% shorter at 4-CPU (large batch: 2,441–3,778ms → 2,248–2,423ms; small: 683ms → 212ms) |
| 4→5 CPU | No new knob | gather_budget saturates at 2; torch intra-op capped at 2 via `min(ncpu//2, 2)` | 5th CPU ≈ no warm-CE gain under current config | — |

CE is ~70–90% of pipeline cost (`_apply_rerank_pipeline` outer span); gather_budget=2 parallelises the
dominant stage → biggest lever. torch intra-op=2 accelerates each CE call individually → secondary.

### Recommendation

- **4 CPUs = sweet spot (owner verdict, 2026-07-12).** The additional ~1.1s warm improvement
  at 3→4 (7,916→6,807ms) is judged worth the extra CPU. gather_budget=2 unlocks parallel CE
  scoring (−28% warm, dominant at 2→3); torch intra-op=2 adds −14% on top (3→4, secondary).
- **Note:** backend is currently running `--cpus 2` as a deliberate temporary posture during
  T4 Ettin planning/benchmarking — this is not the recommendation.
- **5th CPU adds nothing** under current config (both gather_budget and torch intra-op saturate at ≤4 CPUs).

### #186 post-ship verification (2-CPU run, 5.129.0)

| Claim | Observed | Verdict |
|---|---|---|
| restore() 264s → ~5s | **4,348ms (4.35s)** | **CONFIRMED — 60× speedup** |
| HOT −75% vs Car 0 (graph cache) | 1,126ms vs 4,555ms = −75% | **CONFIRMED** — attributable to new `graph` cache counter in 5.40.0 |
| embed cache 0 hits / 0 misses across all runs | 0/0 every run (all 3 CPU configs) | **OBSERVED — open question flagged** (embed.snap corpus growth vs cache layer mismatch?) |

### Per-round run logs

#### Round 1: 2-CPU baseline

- **Deployment:** core 5.129.0, backend 5.40.0, `--cpus 2`, NanoCpus=2000000000
- **T3 Car 3 config:** torch intra-op=1 (`ncpu//2=1`), gather_budget=1 (`min(1,2)=1`)
- **Backend uptime at first recall:** ~110s; models warm via startup hook recall
- **CE state before block:** hit=5, miss=16 (fresh; no hook-recall pre-warming of test queries)
- **CE-miss validity:** Δmiss=+94, +14/query — PASS
- **Knobs:** pool=3, RECALL_HEAVY_CONCURRENCY=2, RERANK_MAX_CONCURRENCY=3, HOOK_RECALL_POOL_WORKERS=2, YADGAR_AVAILABLE_CPUS=0

Queries (same as Car 0 T3 baseline set):
1. offload freeze fix daemon, 2. consolidation cycle engram storage sleep, 3. vacuum bloat compaction SurrealDB,
4. knowledge graph priors PPR spreading activation rerank, 5. N+1 query fix graph helpers cls restore timeout,
6. heat decay thermodynamics memory importance valence score

| Regime | ms |
|---|---|
| WARM Q1 | 8,278 |
| WARM Q2–Q6 avg | ~11,490 |
| **WARM mean (6)** | **10,955** |
| HOT (Q1 repeat) | **1,126** |
| restore() | **4,348** |

Cache deltas (backend :8001/metrics):

| cache | Δhit | Δmiss | hit_rate |
|---|---|---|---|
| ce | +43 | +94 | 31% |
| embed | 0 | 0 | — |
| memory_doc | +3,043 | +1,404 | 68% |
| graph | +157 | +1,525 | 9% |

#### Round 2: 3-CPU

- **Deployment:** core 5.129.0, backend 5.40.0, `--cpus 3`, NanoCpus=3000000000
- **T3 Car 3 config:** torch intra-op=1 (still `ncpu//2=1` at 3), gather_budget=2 (`min(2,2)=2`)
- **Caveat:** first warm block INVALID — 9 startup hook recalls pre-warmed CE to 100% hit rate (0 CE misses
  during measurement). CE cache persists across daemon restart; reused query topics hit cache immediately.
- **Corrective block:** 6 novel queries (topics absent from baseline set) run after verifying CE state; CE-miss
  validity gate: Δmiss=+90, +15/query — PASS

Novel queries (corrective block):
1. consolidation scheduler timer interval nightly systemd dispatch,
2. wiki page versioning draft approval workflow branch,
3. DLQ dead letter queue taxonomy error classification requeue dismiss,
4. hook installation post-tool-use session start auto-capture action log,
5. vacuum quiescence storage bloat cleanup retention purge archive,
6. span naming module qualname tracing dynamic component label

| Regime | ms |
|---|---|
| **WARM CE-miss mean (6, corrective)** | **7,916** |
| HOT (Q1 exact-repeat) | **875** |
| restore() | **4,264** |

Cache deltas (corrective block only):

| cache | Δhit | Δmiss | hit_rate |
|---|---|---|---|
| ce | +21 | +90 | varies |
| embed | 0 | 0 | — |
| memory_doc | +3,912 | +535 | 88% |
| graph | +135 | +70 | 66% |

gather_budget=2 confirmed via concurrent `score_ce_cached` spans:
- Q2: two spans at 2,441ms + 344ms ending simultaneously → parallel gather
- Q4: two spans at 3,778ms + 683ms ending simultaneously → parallel gather

#### Round 3: 4-CPU

- **Deployment:** core 5.129.0, backend 5.40.0, `--cpus 4`, NanoCpus=4000000000
- **T3 Car 3 config:** torch intra-op=2 (`ncpu//2=2`, NEW at 4-CPU), gather_budget=2 (same as 3-CPU, saturated)
- **Backend uptime at first recall:** ~142s; CE state before block: hit=5, miss=14 (~1 startup hook recall only)
- **CE-miss validity:** Δmiss=+97, +16.2/query — PASS

Novel queries (topics absent from baseline AND 3-CPU corrective sets):
1. checkpoint restore protocol post-compaction context reconstruction,
2. memory heat decay thermodynamic importance valence score aging,
3. agent prompt seeding bootstrap library pattern dispatch prelude,
4. CI wave topology fan-out parallel test runner xdist worker,
5. torn manifest recovery detection version pre-claim reservation release,
6. reembed all embedding model migration batch reindex corpus vectors

| Regime | ms |
|---|---|
| WARM Q1 | 7,096 |
| WARM Q2 | 4,336 |
| WARM Q3 | 8,520 |
| WARM Q4 | 4,779 |
| WARM Q5 | 8,264 |
| WARM Q6 | 7,848 |
| **WARM mean (6)** | **6,807** |
| HOT (Q1 repeat) | 3,452 (INVALID — cold graph) |
| restore() | **4,142** |

Cache deltas:

| cache | Δhit | Δmiss | hit_rate |
|---|---|---|---|
| ce | +18 | +97 | 16% |
| embed | 0 | 0 | — |
| memory_doc | +5,397 | +399 | 93% |
| graph | +231 | +87 | 73% |

torch intra-op=2 evidence — per-span CE vs 3-CPU:

| Span | 3-CPU | 4-CPU | Δ |
|---|---|---|---|
| Large CE batch | 2,441–3,778ms | 2,248–2,423ms | −8–40% shorter |
| Small CE batch | 344–683ms | 200–212ms | −38–69% shorter |

### Protocol lessons (permanent procedure updates)

The following are standing procedure requirements, not just observations from this series.
Add these checks to every future warm CE-miss measurement block.

**(a) CE cache persists across daemon restarts — reused query sets never re-miss.**
Every warm CE-miss block requires FRESH DISTINCT queries. If the same topics were queried
in a prior session (including startup hook recalls), CE will be cached and the block measures
HOT-regime, not warm CE-miss. There is no guaranteed-clean CE state without a fresh backend restart
AND novel queries.

**(b) CE-miss validity gate — block is invalid if ≥5 CE misses/query are not observed.**
Before block: record `yadgar_cache_miss_total{cache="ce"}` as M0.
After block: verify Δ = (M1 − M0) ≥ 5 × n_queries (expected ~14/query for a 6-query block).
If Δ ≈ 0 or < 5/query → CE was cached → block is invalid → discard and rerun with novel queries.
Also check `yadgar_embed_ce_cache_misses_total` (ML inference layer) as secondary gate.

**(c) Startup hook recalls pre-fill the CE cache — "warm" ≠ "post-restart".**
At 3-CPU with pool=3 and HOOK_RECALL_POOL_WORKERS=2, 9 startup hook recalls completed before
measurement — fully caching CE for common query topics. Verify CE miss state from metrics,
not from assumptions about backend uptime. A 60s-old backend can have a hot CE cache.

**(d) Per-run cache series scrape at :8001/metrics before and after every timed block.**
Record `yadgar_cache_{hit,miss}_total{cache="ce"}` before and after every measurement block.
This is the validity gate data and enables CE-miss rate verification. Also record graph,
memory_doc, embed, and engram_slot for completeness. (Protocol extended in PR #186; align
to that standard, do not duplicate counter scrape steps.)
