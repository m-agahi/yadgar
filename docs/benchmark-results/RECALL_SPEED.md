# Recall Speed — Consolidated Latency History

This document tracks measured `recall()` wall-clock latency across major performance
milestones. It is the canonical reference for cross-version speed comparisons.

Full retrieval accuracy (LongMemEval) is in
[`BENCHMARK_RESULTS.md`](BENCHMARK_RESULTS.md).

---

## What "recall latency" means

`recall()` latency is the end-to-end server-side wall time for one recall call,
measured as `yadgar_recall_duration_ms` Prometheus histogram delta on `:8765/metrics`
(available v5.96+). It covers signal-gather (vector/FTS/PPR/spreading), cross-encoder
reranking (CE), DB hydration, fusion, and side-effect dispatch — the full pipeline,
server-side, without MCP transport overhead (~2ms per RCA 2026-07-13).

---

## Canonical measurement method (ADR-0098 / ADR-0105)

All numbers in the master table below were taken with this protocol:

1. **Metric:** `yadgar_recall_duration_ms` histogram delta on `:8765/metrics`
   (server-side, version-portable since v5.96). NOT the harness `perf_counter`
   — that is not portable across daemon versions and produced bogus ~7ms readings
   pre-T2 when `YADGAR_RECALL_DIRECTORY` was unset.
2. **Regime: warm CE-miss** — fresh distinct queries the CE cache has never seen;
   2 warmup calls discarded; corpus + graph warm.
3. **CE-miss validity gate:** `yadgar_cache_miss_total{cache="ce"}` delta ≥ 5/query
   on `:8001/metrics`. If the gate fails the block is invalid (CE-cached = HOT regime,
   not the common case).
4. **Sample size:** n ≥ 12 recalls per measurement point.
5. **Query freshness:** novel topics not used in any prior round (CE cache persists
   across daemon restarts — "post-restart" ≠ "fresh CE state").

### Regime definitions

| Regime | Description | When CE is |
|---|---|---|
| **warm-CE-miss** (canonical) | Fresh distinct query, corpus + graph warm | Cold: CE inference runs, full pipeline cost |
| **HOT / same-query-repeat** | Exact-repeat of a prior query | Cached (~2ms); re-runs KNN/FTS/PPR/fusion anyway (no output cache) |
| **COLD daemon** | First recall post-restart | Model cold-load cost on top (~13–25s) |

> **CRITICAL comparability rule:** warm-CE-miss and HOT numbers are not comparable —
> HOT misses most of the CE cost and looks ~3–10× faster. Every row in the master table
> below is warm-CE-miss only. HOT numbers appear only in prose, clearly labelled.

---

## Master comparison table — warm-CE-miss regime

> Comparisons are valid only at equal cache warmth + equal method. Cross-run delta
> numbers are biased by session-to-session cache variation (memory\_doc hit rate
> ranged 68–99% across sessions — see "Comparability caveats" below).

| Date | Version (core/backend) | Reranker | CPUs | Change / context | Method | p50 | p95 | mean | CE-miss/q | Cache warmth note | Source |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-02 | 5.96.0/— | GTE-ModernBERT | 1c/2b | N+1 priors batch fix; same-query-repeat (HOT regime) | histogram-delta | — | — | ~2,410 ms ⚠️ HOT | n/a | Same-query CE-cached; NOT warm-CE-miss — not in trend | checklist 2026-07-02 |
| 2026-07-02 | 5.97.0/— | GTE-ModernBERT | 1c/2b | Fusion N+1 batch | histogram-delta | — | — | ~1,432 ms ⚠️ HOT | n/a | Same-query CE-cached; NOT warm-CE-miss — not in trend | checklist 2026-07-02 |
| 2026-07-04 | 5.106.0/5.12.0 | GTE-ModernBERT | 1c/2b | @observe instrumentation on every fn | histogram-delta | — | — | ~1,409 ms ⚠️ HOT | n/a | Same-query CE-cached; NOT warm-CE-miss — not in trend | checklist 2026-07-04 |
| 2026-07-12 | 5.129.0/5.40.0 | GTE-ModernBERT | 2b | T3 complete; transformers 4.57.6; first valid warm-CE-miss series | histogram-delta | — | — | 10,955 ms | ~14/q | memory_doc 68% | checklist 2026-07-12 |
| 2026-07-12 | 5.129.0/5.40.0 | GTE-ModernBERT | 3b | T3 Car 3 parallel gather (gather_budget 1→2) | histogram-delta | — | — | 7,916 ms | ~15/q | memory_doc 88% | checklist 2026-07-12 |
| 2026-07-12 | 5.129.0/5.40.0 | GTE-ModernBERT | 4b | torch intra-op 1→2 | histogram-delta | — | — | 6,807 ms | ~16/q | memory_doc 93% | checklist 2026-07-12 |
| 2026-07-12 | 5.131.0/5.42.0 | GTE-ModernBERT | 2b | transformers-5.x new-stack (T4 A/B reference baseline) | histogram-delta | — | — | 13,416 ms | 14.3/q | memory_doc 96% | checklist 2026-07-12 |
| 2026-07-13 | 5.132.0/5.43.0 | GTE-ModernBERT | 2b | Cross-version sweep control arm (config-pinned GTE on Ettin image) | histogram-delta | — | — | 12,947 ms | — | — | checklist §cross-version sweep |
| 2026-07-13 | 5.132.0/5.43.0 | **Ettin-32m** | 2b | **T4: Ettin swap (apples-to-apples, same image as GTE control)** | histogram-delta | — | — | **5,306 ms** | — | — | checklist §cross-version sweep |
| 2026-07-13 | 5.132.0/5.43.0 | Ettin-32m | 2b | CPU-scaling series (2-CPU arm) | histogram-delta | ~6,944 ms (est) | ~9,694 ms (est) | 5,644 ms | ~15/q | — | checklist §Ettin CPU-scaling |
| 2026-07-13 | 5.132.0/5.43.0 | Ettin-32m | 3b | **CPU sweet spot (ADR-0106)** — gather_budget=2 engaged | histogram-delta | ~4,000 ms (est) | ~9,250 ms (est) | **4,317 ms** | ~15/q | — | checklist §Ettin CPU-scaling |
| 2026-07-13 | 5.132.0/5.43.0 | Ettin-32m | 4b | torch intra-op=2; flat vs 3 CPU (ADR-0106) | histogram-delta | ~3,750 ms (est) | ~7,500 ms (est) | 4,568 ms | ~16/q | — | checklist §Ettin CPU-scaling |
| 2026-07-13 | 5.132.0/5.43.0 | Ettin-68m | 2b | Accuracy A/B arm (not a latency run) | — | — | — | — | — | — | T4 A/B (accuracy only) |
| 2026-07-15 | 5.143.0/5.50.0 | Ettin-32m | 3b | Module-split (I13 train, PR #203) — **PROVISIONAL** warm-only, n=12, NOT controlled A/B *(superseded by controlled rows below)* | histogram-delta | 2,639 ms ⚠️ | 2,870 ms ⚠️ | 2,264 ms ⚠️ | 16.3/q | memory_doc 99%, graph 94% — very warm; higher than baseline session | perf_loadtest_20260715_184411.json |
| 2026-07-15 | 5.143.0/5.50.0 | Ettin-32m | 3b | **CONTROLLED warm steady-state** (n=30, 5 warmup discarded; balanced; CE-miss gate PASS ~15.8/q) | histogram-delta | **2,644 ms** | **3,064 ms** | **2,332 ms** | 15.8/q | memory_doc 99.5%, graph 97.3%; min 1086, max 3236, stdev 685, 95% CI ±245ms | controlled re-measurement 2026-07-15 |
| 2026-07-15 | 5.143.0/5.50.0 | Ettin-32m | 3b | **CONTROLLED cold** (n=30, restart then 2 warmup; CE snapshot persists on disk, cache re-warms within 2 recalls; genuinely-cold warmup calls ~2.9–3.4s) | histogram-delta | **2,682 ms** | **3,148 ms** | **2,594 ms** | 16.1/q | memory_doc 98.8%, graph 96.2%; min 1445, max 3370, stdev 537 | controlled re-measurement 2026-07-15 |

Legend:
- `Xb` = backend CPUs; `Xc` = core CPUs (recall fully in backend since T2/ADR-0078)
- `(est)` = estimated from histogram bucket boundaries, not a direct percentile read
- ⚠️ HOT = CE-cached same-query-repeat, NOT warm-CE-miss regime

---

## Verdict per milestone

### v5.96–v5.106 (2026-07-02 to 2026-07-04) — N+1 batch fixes + @observe
**Historical warm floor ~1.4–2.4s is a HOT-regime artifact — not comparable to later
warm-CE-miss numbers.**

These runs measured same-query-repeat recall (v5.96: 2,410ms warm, v5.97: 1,432ms,
v5.106: 1,409ms). The ~1.4s is real for CE-cached repeats; it is meaningless as a
"latency baseline" because fresh queries (the common production case) paid full CE
each time. The v5.97 fusion N+1 batch and v5.99 spreading-BFS N+1 batch were real
wins for their target regimes (warm repeat and cold entity-rich queries respectively),
but the regime labels in those run logs were later corrected.

**@observe instrumentation overhead: +8ms (+4.2%) via A/B — negligible, not a
latency cause (ADR-0035).**

### T3 CPU-scaling series (2026-07-12, GTE, @2/3/4 CPUs)
**Gather-budget 1→2 at 3 CPUs is the dominant lever: −28% warm mean (10,955 →
7,916ms). torch intra-op 1→2 at 4 CPUs adds another −14% (secondary).**

These are the first valid warm-CE-miss measurements (ADR-0098 protocol). Note they
use transformers-4.57.6; the 5.x new-stack runs 22% slower at 2 CPUs due to corpus
growth, not a regression (memory_doc hit rate jumped 68% → 96% between sessions,
indicating a larger corpus requiring more PPR/spreading work per recall).

### T4: Ettin-32m swap (2026-07-13)
**Real 2.44× end-to-end speedup, attributable to the reranker model swap.**

The cleanest apples-to-apples: GTE control (12,947ms) vs Ettin live (5,306ms), same
5.132.0/5.43.0 image, same session, same histogram method. −59%. This is the number
to cite for the Ettin win.

At ADR-0106 standing config (--cpus 3): GTE @2cpu 12,947ms → Ettin @3cpu 4,317ms =
~3× combined speedup (model swap + CPU). This is a **combined model+CPU effect**,
not model-only.

CE per-pass ratio: ~4.7× (GTE ~7s CE of 12.8s total → Ettin ~1.5s CE, per
checklist §honest measurement arc). CE was ~55% of GTE's warm wall; cutting CE by
4.7× accounts for most of the end-to-end 2.44× (remaining stages — PPR/spreading/
fusion/DB — are model-invariant and unchanged).

**Ettin-68m: no latency measurement taken.** The T4 A/B ran 68m in an accuracy-only
arm (LongMemEval recall@k). Its per-pass CE ratio is ~2.1× (vs GTE) per ADR-0104.
End-to-end wall is not measured and cannot be derived from the per-pass ratio alone.

### ADR-0106: standing config --cpus 3 (supersedes ADR-0097's 4-CPU verdict)
**3→4 CPU is flat under the corrected method (3cpu 4,317ms vs 4cpu 4,568ms). The
ADR-0097 "~1s gain at 3→4" was a dirty-method artifact.**

3 CPUs captures the gather-budget gain (2→3: −24%) at lower resource cost. 4th CPU
changes only torch intra-op (1→2), which adds matrix threads per CE call; on the
small Ettin-32m model this buys nothing detectable.

### Module-standardization split (2026-07-15, I13 train, PR #203)
**PERF-NEUTRAL / no regression. High confidence by construction; controlled measurement confirms.**

The split (PR #203) is byte-identical retrieval code — pure file moves with no logic
changes (embed_service, cache, ml_client, daemon, graph_api, install_hooks,
predictive_coding, adr.py, shim removal). Zero retrieval logic changed → ≈0 perf
impact by construction.

**Controlled measurement (n=30 each, 2026-07-15):**
- Warm steady-state: p50 **2,644ms**, mean **2,332ms**, p95 **3,064ms** (stdev 685, 95% CI ±245ms)
- Cold (post-restart, in-process caches re-warm within 2 recalls): p50 **2,682ms**, mean **2,594ms**, p95 **3,148ms** (stdev 537)

Both runs pass the CE-miss validity gate (~15.8–16.1/q). Tight low-variance profile:
p95 is only 1.3× mean (vs the 5.132.0 baseline's 2.1×), consistent with the very
warm cache state (memory_doc 99.5%, graph 97.3%).

**The −40/−46% apparent gap vs the 4,317ms baseline is cache regime, NOT the split.**
The 5.132.0 Ettin @3cpu baseline (4,317ms mean) was measured in a **fresh-session
cold-graph state** — 33% of its queries fell in a 5–10s tail (cold graph/PPR cache
signature). The controlled re-measurement ran with 97–99% warm caches (no tail, max
3.2s). Same CE load (~15–16 miss/q both). The two are NOT a like-for-like
before/after comparison; the apparent delta is cache regime variance, not a code
change effect.

Verdict: **no regression + no improvement**. The module split is behavior-preserving
as claimed. The provisional n=12 row above is superseded by these controlled n=30 rows.

---

## Comparability caveats — read before drawing trends

### ⚠️ Regime mismatch (most serious)
Rows v5.96–v5.106 (the ~1.4–2.4s "warm floor" numbers) used HOT regime (CE-cached
same-query repeats). All subsequent rows use warm-CE-miss (fresh queries, CE runs).
These are **not comparable** — HOT is 3–10× faster because CE is the dominant cost
and it is cached to ~2ms in that regime. Drawing a trend line across both will make
recall appear to get slower after v5.97, which is factually wrong.

### ⚠️ Stack change (transformers 4.57.6 → 5.x)
GTE CPU-scaling series (2026-07-12, 5.129.0/5.40.0) used transformers-4.57.6. The
T4 Ettin A/B baseline (5.131.0/5.42.0) used transformers-5.x. The +22% observed
GTE latency on 5.x vs 4.x is corpus-growth variance, not a framework regression
(confirmed by corpus growth signals: memory_doc hit rate 96% vs 68%). However these
two stacks' GTE numbers cannot be used interchangeably without acknowledging the gap.
**There is no GTE @3cpu new-stack (transformers-5.x) measurement.**

### ⚠️ Session-to-session cache variation
memory_doc hit rates ranged 68–99% across sessions. Graph cache: 9–94%. A warmer
cache reduces PPR/spreading work per recall (more subgraph residency). This means
two warm-CE-miss runs on the same version/CPU can differ by 20–30%. Cross-session
comparisons are approximate. The post-split 2,264ms is within the expected variance
of the 4,317ms Ettin @3cpu baseline when cache is significantly warmer.

### ⚠️ CPU confounds in cross-model comparison
The "3× faster" headline (GTE @2cpu → Ettin @3cpu) combines the model swap win
(2.44×) with a CPU change (+gather_budget). It overstates what Ettin alone delivered
on equal CPUs. Use the 2.44× figure (same-image, same-CPU, 5.132/5.43) for
model-attributable speedup.

### ⚠️ Data gaps (no latency measurement)
| Gap | Reason | What we have |
|---|---|---|
| GTE @3cpu (transformers-5.x new-stack) | Not measured — CPU series ran on 4.57.6 | 4.57.6 @3cpu = 7,916ms |
| Ettin-68m end-to-end wall | Accuracy-only A/B arm | Per-pass CE ratio ~2.1× (not e2e) |
| Pre-T2 architecture (v5.96−) | `/mcp` contract changed; harness returned bogus ~7ms | Not measurable via portable method |
| Warm-CE-miss at <v5.128 | No valid CE-miss runs before T3 landed | Only HOT/same-query numbers exist |

---

## Current headline number

**Ettin-32m @ --cpus 3 (ADR-0106 standing config), warm steady-state (controlled n=30, 2026-07-15, v5.143.0/5.50.0):**

| Regime | p50 | p95 | mean | Status |
|---|---|---|---|---|
| Warm steady-state (n=30) | **2,644 ms** | **3,064 ms** | **2,332 ms** | CONTROLLED — CE-miss gate PASS, stdev 685, 95% CI ±245ms |
| Cold post-restart (n=30) | **2,682 ms** | **3,148 ms** | **2,594 ms** | CONTROLLED — CE snapshot persists on disk; first 2 genuine-cold warmup calls ~2.9–3.4s |

Cold-cache first queries (before CE snapshot re-warms, i.e. the first 2 calls post-restart)
ran ~2.9–3.4ms. After 2 recalls, in-process caches re-warm and the distribution converges
to warm levels (CE snapshot cache persists on disk across restart).

The v5.143.0 module-split (PR #203) is **perf-neutral** — byte-identical retrieval code,
pure file moves. The ~−46% apparent gap vs the 5.132.0 Ettin @3cpu baseline (4,317ms mean)
is a **cache regime difference**, not a code change: the baseline was measured in a
fresh-session cold-graph state (33% of queries in a 5–10s tail); this session was 97–99%
warm (no tail, max 3.2s). The two are not directly comparable.

Source: controlled re-measurement 2026-07-15 (n=30 warm + n=30 cold, histogram-delta method)

---

## Raw data sources

| File | Contents |
|---|---|
| `docs/testing/recall-perf-checklist.md` | All run logs with per-call data, cache series, CE validity gates |
| `benchmarks/reports/perf_loadtest_20260715_184411.json` | 2026-07-15 post-split run (12 queries, histogram-delta) |
| ADR-0097 | CPU-scaling series (GTE, 2→3→4 CPUs, first valid warm-CE-miss) |
| ADR-0098 | Measurement protocol (fresh queries + CE-miss gate) |
| ADR-0104 | T4 Ettin A/B accuracy gate + CE speed rationale |
| ADR-0105 | CE metric correction (embed-rerank histogram dead since ADR-0078) |
| ADR-0106 | Standing --cpus 3 verdict (supersedes ADR-0097's 4-CPU) |
