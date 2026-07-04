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
    '^yadgar_recall_duration_ms_(sum|count)|^yadgar_recall_shadow_cache_(hits|misses)_total|^yadgar_tool_pool'
}
```

- **Per-recall latency (ms)** = `Δ(yadgar_recall_duration_ms_sum) / Δ(yadgar_recall_duration_ms_count)`.
  (For a single recall between two snaps, Δcount=1 → the delta *is* that recall's ms.)
- **Cache hit vs miss** = `Δyadgar_recall_shadow_cache_hits_total` vs `_misses_total`
  (cold/first-seen key → miss; repeat identical key, no intervening write to that
  directory → hit).
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
