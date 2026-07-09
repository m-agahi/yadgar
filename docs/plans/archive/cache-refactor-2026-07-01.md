> ARCHIVED 2026-07-09 — train shipped #164/#165 (Car 1 project_brief, Car 2 wiki/prelude, Car 3 killed ADR-0071 — 0% tool-path hit-rate). Shadow surface removed.

# Cache Refactor — the query→ranked-output cache (and what should ship first)

**Status:** PLAN / INVESTIGATION ONLY. No code changed. **Date:** 2026-07-01.
**Author:** agent (bot). **Branch:** `docs/cache-refactor-plan`.
**Context:** v5.95.0 shipped. ADR-0030 (`wiki:yadgar-adr-log`) verdict: recall is
**IO-bound, not compute-bound** — ~99.8% of recall wall-time is surreal query
execution + backend rerank HTTP; movable core-CPU is ~0.2%. This plan evaluates the
user's hypothesis and designs the cache they asked for.

---

## TL;DR — the recommendation up front

1. **The user's mechanism is backwards, but their conclusion is accidentally right.**
   Hypothesis: *"raw DB queries are FAST — the cost is the RANKING; cache query→output."*
   Measurement (ADR-0030 cProfile) says the opposite about the *mechanism*: the DB
   queries are the ENTIRE cost (~99.8%), ranking *compute* is ~0.2%. **But** the
   expensive DB queries ARE the ranking-signal queries (graph/cofire priors, spreading
   activation) — so "caching the ranked output skips the slow part" is true. The two
   framings agree on *what* to skip; they disagree on *why it's slow* (it's IO, not CPU).

2. **The direct fix for slow DB queries is to make them fast — not to cache around them.**
   Two of the three big cost terms are a plain **N+1 anti-pattern over precomputed
   scalars** (`get_memory_graph_priors`, `get_memory_cofire_priors`) that a single
   `WHERE id IN [...]` collapses. That is **lever (c)** — stateless, zero invalidation,
   strictly-better, saves ~1.5s on **every** recall (not just cache hits).

3. **Ship order: (c) → (b-residual) → (a).**
   - **(c) surreal N+1 fix** first — biggest guaranteed win, no correctness surface.
   - **(b) "per-memory priors cache"** as a *separate* lever is **subsumed by (c)**:
     priors are already materialized scalars on the row; there is nothing to compute,
     only a fetch to batch. It collapses into (c). (Kept in the comparison for the
     record.) Note (c) is low- but not *zero*-risk: the N+1 was a deliberate v2/v3
     SurrealDB-compat choice (source comment at `memory.py:1029`), so the `IN […]`
     rewrite must be validated across both modes; and the win *magnitude* is
     contention-dependent (ADR-0030's ms are contention-soft).
   - **(a) query→output result cache** LAST and **gated on a measured hit-rate**,
     because its win exists only on hits and it is the only lever that carries a
     staleness/correctness surface. Build a **shadow hit-rate counter** *before* the
     invalidation machinery to decide whether (a) is worth building at all.

4. **Spreading activation is the one cost only (a) can skip.** Unlike priors, spreading
   is a genuine per-query N-round-trip graph BFS (not a stored scalar, not batchable
   into one query). So after (c), spreading + the two CE rerank HTTP calls are the
   residual per-recall cost — and *that* residual is what an (a) cache-hit removes.
   This is the real, honest case for (a).

---

## 1. FLOW — the recall pipeline end to end, with every cache marked

Single entry point: **`recall()` — `yadgar/server/tools/recall.py:395`**. It validates
params, detects branch/directory, then dispatches to one of four paths. All the caches
listed below are *inside* the dispatch; `recall()` itself has **no result cache today**
— it is the natural home for one (see §4).

```
recall(query, directory, max_results, min_heat, type, mode, profile, tags, branch_hint)
  └─ validate dir/type/mode/profile                       recall.py:465-491
  └─ _detect_branch / _get_default_branch  (→ cache-key scope) recall.py:523-551
  │
  ├─ mode="landscape" ─────────────► _landscape_recall     recall.py:126 / 506
  │    └─ AstrocytePool.consensus_retrieve  (separate path — NO priors/spreading/CE)
  │
  ├─ UNIFIED_RECALL_ENABLED & profile is None ─► _fanout_recall  recall.py:179 / 567
  │    ├─ MemoryProvider.candidates → Retriever.recall  (the 4-signal core)
  │    │     ├─ query embed .............. [EMBED_CACHE hit]  (backend /embed)
  │    │     ├─ vector KNN search ........ surreal
  │    │     ├─ FTS BM25 search .......... surreal   (branch clause injected)
  │    │     ├─ graph_prior fetch ........ surreal  N+1  ← get_memory_graph_priors
  │    │     ├─ cofire_prior fetch ....... surreal  N+1  ← get_memory_cofire_priors
  │    │     ├─ spreading activation ..... surreal  N-round-trip BFS (_get_adjacent)
  │    │     └─ WRRF/convex fusion ....... in-process (tottime≈0)
  │    ├─ WikiProvider.candidates ....... wiki store (own vectors)
  │    ├─ fuse_candidates → CE rerank ... [CE_CACHE hit] (backend /rerank)
  │    ├─ get_memory in CE diversity .... surreal  N+1  ← _inject_ce_diversity
  │    └─ _dedup_by_content
  │
  ├─ profile set ─────────────────► Retriever.recall_via_pipeline  recall.py:593
  │    └─ RetrievalPipeline stages: QueryAnalysis→FTS→KNN[EMBED_CACHE]→PPR→
  │       Spreading→Temporal→Fusion→CEReRank[CE_CACHE]→(NLI,MMR,…)
  │
  └─ legacy (profile=None, flag off) ─► Retriever.recall + wiki blend  recall.py:603
  │
  └─ _apply_recall_side_effects(merged, query, storage)   recall.py:347
       ├─ per memory: update_memory_heat(+0.1)   surreal WRITE  (INLINE, N writes)
       ├─ per memory: update_memory_last_accessed surreal WRITE  (INLINE, N writes)
       ├─ SR transition record, action-log capture, auto-checkpoint tick
       └─ (wiki rows skipped)
```

**Two caches exist, both in the backend embed service, both keyed on pure compute
inputs (no scope):**

| Cache | Where | Key | Value | Invalidation |
|---|---|---|---|---|
| **EMBED_CACHE** | `backend/cache.py`, keyed in `embed_service.py:573` | `sha256(text)[:16] : mode : checkpoint_hash` (mode ∈ document/query/raw) | `list[float]` embedding | model-checkpoint hash mismatch → discard on load |
| **CE_CACHE** | `backend/cache.py`, keyed in `embed_service.py:637` | `sha256(query)[:16] : sha256(text)[:16] : checkpoint_hash` | `float` CE rerank score | model-checkpoint hash mismatch |

Both: OrderedDict LRU, `max_entries=100000` (0=disabled), msgpack snapshot every
**600s** to `/data/cache`, restored on startup, final snapshot on shutdown.
Config: `ce_cache_max_entries` / `embed_cache_max_entries` / `cache_snapshot_interval_sec`
(`config.py:802-806`, `config_yaml.py:642-654`).

---

## 2. What the current cache SOLVES

Both caches cache **pure, deterministic compute keyed only on the input text +
model checkpoint**:

- **EMBED_CACHE** — memoizes the embedding of a given text. Recompute of an identical
  query/passage embedding is skipped. Value depends only on (text, mode, model).
- **CE_CACHE** — memoizes the cross-encoder score of a given (query, passage) pair.
  Recompute of an identical pair rerank is skipped. Value depends only on
  (query, passage, model).

This keying is **why they need no invalidation logic**: the cached value is a pure
function of the key inputs, so it is *never stale* except when the model changes — and
the checkpoint-hash in the key handles exactly that (a snapshot with a mismatched hash
is silently discarded on load). No scope, no heat, no time enters the value, so no
scope/heat/time enters the invalidation.

**Utilization:** the caches are far from full — `embed.snap` is ~2.7 MB against a
~150 MB capacity (100k entries). Capacity is not the bottleneck; the caches are simply
not the hot cost (see §3).

---

## 3. What the current cache MISSES (and reconciling the two framings)

The CE/EMBED caches cache the **compute** (embedding vectors, rerank scores). Per
ADR-0030 the hot cost is **not** that compute — it is **surreal query execution**:

| Cost term | Where | Shape | Batchable? | Cached today? |
|---|---|---|---|---|
| `get_memory_graph_priors` ~800ms | `storage/memory.py:1029` | **N point-read SELECTs** of a *precomputed scalar* field | **YES** (`id IN […]`) | value pre-materialized on row; **fetch not cached** |
| `get_memory_cofire_priors` ~730ms | `storage/memory.py:1069` | same N+1 point-reads of a stored scalar | **YES** | same |
| `get_memory` N+1 | `storage/memory.py:286`, called `fusion.py:269` | full-row read per diversity candidate (pulls embedding blob) | **YES** | no |
| spreading activation (~42s **cumulative**, see caveat) | `retrieval/core.py:192`, `scoring.py:236` | **per-query N-round-trip BFS** `_get_adjacent` per frontier entity, depth 2, seeded from top-5 vectors | **NO** (traversal, seed-dependent) | no |
| CE rerank HTTP ×(1–2) | backend `/rerank` | per-pair scores | partial | **CE_CACHE** (per-pair) |
| heat-boost writes | `recall.py:367` | **N inline UPDATE writes** per recall | — | no (a *write*, not a read) |

**⚠️ cumtime caveat (do not overclaim):** the "~42s" for spreading is cProfile
**cumulative across the whole profile run**, NOT per-recall. Per-recall total wall-time
is ~2250ms (ADR-0030). The plan must NOT headline "saves 42s." The correct per-recall
decomposition (priors ~800+730ms, spreading = residual, CE = backend HTTP) is what the
sequencing below is built on; a clean-box per-recall re-measure is an open item (ADR-0030
already flags the absolute surreal ms as contention-soft).

**Reconciling the user's hypothesis with ADR-0030:**
- User: *"DB queries are fast; ranking is the cost; cache query→output."*
- ADR-0030: *"~99.8% of wall-time is surreal IO; ranking compute is ~0.2%."*
- **Resolution:** both point at the *same* stages to skip. The expensive surreal queries
  (priors + spreading) ARE the ranking-*signal* queries. The user calls them "ranking";
  ADR-0030 calls them "IO." The disagreement is only about the *cause of slowness*
  (the user assumes CPU-ranking; it is actually the fetch/traversal IO). This matters
  for the fix: **the mechanism-correct fix is to make the fetch fast (batch it), not to
  cache around a fetch that is only slow because it is a loop.** Caching the output
  skips the slow queries too — but on hits only, and at the cost of an invalidation
  surface. Hence the sequencing.

---

## 4. DESIGN — the query→ranked-output cache (lever a)

Placement: a thin wrapper **inside `recall()` (`recall.py:395`)**, consulted *after*
param validation + branch/directory detection (both are needed for the key) and
*before* the dispatch to `_fanout_recall` / `recall_via_pipeline` / `_landscape_recall`.
`_apply_recall_side_effects` stays outside the cache (see heat-boost below).

### KEY

Exact-string key on the *semantically load-bearing* inputs plus the scope + an epoch:

```
key = (
    normalize(query),        # trim + lowercase + collapse whitespace
    directory,               # REQUIRED, part of the scope filter
    branch_bucket,           # frozenset{current, default, None} → stable string
    type,                    # all | memory | wiki  (changes result shape, §fanout)
    mode,                    # None | landscape     (entirely different path)
    profile,                 # None | fast | …       (different pipeline)
    max_results,             # trims the list
    round(min_heat, 2),      # filter floor
    tuple(sorted(tags or [])),
    epoch[directory],        # per-directory structural-write epoch (see INVALIDATION)
)
```

- **Exact-string vs embedding-bucket keys.** Recommend **exact normalized string**,
  not an embedding-cosine bucket. Bucketing (hash the query embedding to a coarse
  cosine cell) raises hit-rate on near-duplicate phrasings but introduces **near-miss
  correctness risk**: two queries in the same bucket can legitimately want different
  results, and you would serve one for the other. Agent recalls are *often literally
  identical* (session-start `recall(project)`, `project_brief` catalog, repeated
  anchor lookups), so exact-string already captures most of the realistic hit-rate
  without the near-miss failure mode. Bucketing is a later, opt-in experiment gated on
  the shadow-counter data — not the v1.
- Scope (directory, branch) MUST be in the key: branch is a SQL `WHERE` clause
  (`storage/branch.py:31`) and directory is a Python post-filter
  (`storage/directory.py:53`); different scope → different result set.
- `type`, `mode`, `profile` change *which pipeline runs* and *the result shape*
  (fan-out interleaves memory+wiki with cross-type CE; landscape uses the astrocyte
  path with `consensus_score`/`voting_domains`; a profile routes the plugin pipeline) —
  all must be in the key or a hit would return the wrong shape.

### VALUE

The final ranked `list[dict]` exactly as `recall()` would return it (post dedup,
post directory/branch/quality-floor filter, trimmed to `max_results`). Store a
**deep copy**; never hand out the cached list object (callers and the heat-boost
mutate row dicts in place — `m["heat"] = new_heat` at `recall.py:370`).

### INVALIDATION — correctness is *easy*; hit-rate is the hard part

Reframe: with a **per-directory structural epoch embedded in the key**, stale reads are
**structurally impossible** — a write bumps `epoch[directory]`, every prior key for that
directory is now unreachable (a miss), never a wrong hit. So the classic "invalidation
is hard" is solved by construction. The *real* difficulty is elsewhere:

**Epoch bump policy (the load-bearing decision):**
- **Bump on STRUCTURAL change only:** `memorize`, `forget`, and the consolidation pass
  that **recomputes graph/cofire priors** (these change the candidate set and the prior
  scalars → the ranking genuinely changes). Reuse the invalidate-on-`memorize`-per-dir
  trigger already sketched in `hook-recall-cache-track-a-2026-07-01.md` (#3).
- **Do NOT bump on heat/decay drift.** Heat changes on *every recall* (the +0.1 boost)
  and decay ticks continuously. If heat drift bumped the epoch, **the cache would
  self-destruct — every recall invalidates its own directory → ~0% hit-rate.** Instead
  ride heat/decay staleness on a **short TTL backstop** (e.g. 60–300s): a hit older than
  TTL is refreshed. Small heat drift barely reorders the top-k, so a short TTL is an
  acceptable staleness/accuracy trade; the epoch guarantees *structural* freshness.
- Net: **epoch = structural correctness; TTL = heat/decay freshness.** Two mechanisms,
  each doing the job it is suited to.

**⚠️ The hit-rate trap (make-or-break, under-weighted by the load test).** The
invalidation trigger (any structural write in the directory) is **anti-correlated with
hit-rate in real read/write-interleaved sessions.** A working agent does
recall→memorize→recall: the memorize bumps the epoch, so the *next* recall misses. The
session's own load test showing "recalls are repetitive" is a **read-only burst** — not
representative. So the honest expected hit-rate for (a) is **unknown and possibly low**
in real sessions, high only in read-heavy phases (session-start context loads, repeated
`project_brief`/anchor lookups, benchmark loops). **Do not assume the win — measure it
(shadow counter below).**

**Alternatives considered:**
- *TTL-only (no epoch):* simplest, but serves structurally stale results (a just-added
  memory is invisible until TTL) — wrong for a memory system whose whole point is that a
  `memorize` is immediately recallable. Rejected as the primary mechanism; kept only as
  the heat backstop.
- *Write-through:* update the cached list on each write. Rejected — reconstructing a
  ranked list incrementally on write reimplements the pipeline; not worth it.

### HEAT-BOOST side effect on a cache hit

`_apply_recall_side_effects` (`recall.py:347`) is **inline/blocking** and does **N
surreal UPDATE writes** (heat +0.1, last_accessed) per recalled memory, plus SR
transition + action-log + checkpoint tick. It is cleanly separable — signature
`(merged, query, storage)` — so a cache hit *can* still fire it.

**Recommendation: on a hit, fire the heat-boost fire-and-forget (off the response
path), do NOT skip it.** Rationale:
- Skipping it silently breaks the heat-ranking model — cached recalls would stop
  reinforcing accessed memories (the exact regression `test_recall_boosts_heat`
  guards, per the v5.80 extraction comment at `recall.py:356`). SR transitions and the
  action log would also go dark on hits.
- But **a hit is not free:** the boost is 2N writes. Fire-and-forget keeps them off the
  response latency, but they still hit surreal. **Quantify in the build:** confirm the
  async heat-boost writes do not eat the read savings the hit bought (they are cheap
  point UPDATEs vs the priors+spreading reads a hit skips — expected net positive, but
  measure). Note the interaction with the epoch: heat writes must **not** bump the epoch
  (see above), or the fire-and-forget boost would invalidate the very entry it just
  served.

### Interaction with the other recall surfaces

- **Unified recall (`type=all`):** the fan-out interleaves memory+wiki via cross-type CE
  and applies per-type prior weights (`recall.py:305-317`). `type` is in the key, so
  `all` / `memory` / `wiki` cache independently — correct, since their result shapes
  differ (episodic-query wiki suppression at `recall.py:277` also folds into the cached
  value naturally).
- **Landscape mode:** separate astrocyte path, extra fields (`consensus_score`,
  `voting_domains`). `mode` in the key isolates it. Landscape is experimental (#67) and
  slower; a cache benefits it *most* per-hit but its queries are the least repetitive —
  low priority.
- **Branch-scoping:** `branch_bucket` in the key. A directory used on multiple branches
  keeps per-branch entries; the epoch is per-*directory* (a write on any branch of the
  dir bumps it) — slightly coarse but safe (over-invalidates, never under).

### Expected win vs hit-rate reality

- **Per hit:** skips the whole pipeline for that scope — priors N+1 (~1.5s if (c) not
  yet shipped), spreading BFS, and the CE rerank HTTP — returning in ~sub-ms.
  **After (c) ships, the per-hit win shrinks** to (spreading + CE HTTP + fusion),
  because (c) already removed the priors N+1 for *every* recall, hit or miss. This is
  the key sequencing consequence: **(c) reduces (a)'s marginal value.** That is a reason
  to do (c) first and re-measure before committing to (a).
- **Hit-rate:** genuinely repetitive in read-heavy phases (session bootstraps, repeated
  context loads, benchmark loops); likely low in write-interleaved work phases (the
  anti-correlation trap). **Net EV of (a) ≈ per-hit-win × hit-rate — and both terms are
  reduced by shipping (c) first.** This is why (a) is gated, not assumed.

### De-risk before building (a): a shadow hit-rate counter

Before writing any invalidation machinery, add a **shadow counter**: compute the cache
key and log would-be **hit/miss + would-be epoch-bump events** *without serving from
cache*. Run it across real sessions + the benchmark. This produces the one number that
decides whether (a) is worth building — measured hit-rate under realistic read/write
interleaving — for near-zero risk and no behavior change. If real hit-rate is low, (a)
is not worth its invalidation surface and (c) alone is the answer.

---

## 5. COMPARE the three levers + sequencing

| Lever | What it does | Win | Risk / correctness surface | Effort |
|---|---|---|---|---|
| **(c) surreal N+1 fix** | `get_memory_graph_priors` / `_cofire_priors` / `get_memory` → single `WHERE id IN […]`; trim the embedding blob in the diversity read | direction certain (50→1 round-trips, strictly better, helps *most* under contention); **magnitude contention-dependent** — the ~800+730ms is ADR-0030's contention-soft figure, not a clean-box number | **Low but not zero** — the N+1 was a *deliberate* v2/v3-compat choice (see caveat below); results unchanged, but the `IN […]` rewrite must be validated on both SurrealDB modes | **Low** — 3 batched queries; TDD against existing storage tests |
| **(b) per-memory priors cache** | cache graph/cofire prior *values* keyed by memory_id, slow TTL | ≈0 *additional* over (c) | low, but pointless | — |
| **(a) query→output cache** | cache the ranked list per (query+scope+params+epoch) | full pipeline skip **on hits only**; shrinks after (c) | **Highest** — staleness/epoch/heat interaction; hit-rate unknown | **Medium-High** — cache + epoch bus + heat interaction + shadow-counter first |

**⚠️ (c) caveat — the N+1 was deliberate, validate before batching.** The point-read
loop is intentional, per the verbatim source comment (`storage/memory.py:1029`):
*"Use individual point reads (one per ID) to stay compatible with both SurrealDB
embedded (v2) and server (v3) modes. The candidate set is bounded by the rerank_pool
cap (≤50 by default) so N round trips are fine."* So the `WHERE id IN […]` rewrite must
be validated on **both** SurrealDB v2-embedded and v3-server modes — it is not a free
mechanical change. Tie this to `docs/plans/surrealdb-3.1.5-upgrade-plan-2026-06-30.md`:
if v3-server is the target, the v2-compat constraint may already be dissolving, which
would make the batch rewrite unambiguously safe. Until then, treat cross-mode
validation as an acceptance criterion for (c), not an afterthought.

**(b) is subsumed by (c).** Priors are **already precomputed scalars stored on the
memory row** (`update_memory_graph_prior` runs in consolidation, `memory.py:1055`; the
request path only *reads* the field). There is nothing to compute, so "caching the
priors" adds a cache in front of a value that is already effectively cached (the row
field). The only cost is the **N+1 fetch pattern**, which (c) fixes directly. A separate
priors cache would add invalidation complexity (priors change on consolidation) for no
win over batch-fetching. **Drop (b) as a distinct lever; it lives inside (c).**

### Recommended sequence

1. **(c) FIRST.** Highest guaranteed EV, zero correctness surface, low effort. Fix the
   three N+1 sites. This alone likely captures the majority of the realizable latency
   win and helps *every* recall regardless of repetition.
2. **Shadow hit-rate counter** (part of, or immediately after, (c)). Cheap, no behavior
   change. Gathers the data that decides (a).
3. **Re-measure per-recall decomposition on a clean box** (ADR-0030 flagged surreal ms
   as contention-soft). Confirm what residual cost remains after (c) — this is the true
   ceiling for (a).
4. **(a) LAST, gated.** Build only if the shadow counter shows a hit-rate high enough
   that (per-hit-win-after-c × hit-rate) beats its invalidation-surface cost. If built:
   exact-string key + per-directory structural epoch + short TTL heat-backstop +
   fire-and-forget heat-boost on hit.

**Which combine:** (c) + shadow-counter always combine (ship together). (a) combines
with (c) but (c) *reduces* (a)'s marginal value — deliberately sequence (c) first so
(a) is evaluated against the *post-(c)* residual, not the current inflated cost.
(b) does not exist independently.

---

## Open questions for the user

1. **Hit-rate reality:** are your real sessions read-heavy (recall bursts) or
   read/write-interleaved (recall→memorize→recall)? The latter kills (a)'s hit-rate via
   the epoch-bump anti-correlation. The shadow counter answers this empirically — OK to
   ship the shadow counter first and decide (a) on the data?
2. **Is (c) alone acceptable?** If (c) removes ~1.5s from every recall and the shadow
   counter shows a low hit-rate, do we skip (a) entirely? (Recommended default: yes.)
3. **TTL length for the heat backstop** if (a) is built — 60s vs 300s trades hit-rate
   against heat-freshness. Your reinforcement cadence should set this.
4. **Clean-box re-measure:** ADR-0030 marks the absolute surreal ms as contention-soft.
   Do we want the perf-loadtest harness (`docs/plans/perf-loadtest-contract-2026-06-30.md`)
   to produce the per-recall decomposition *before* committing to (a)?

---

## Key file references

| Concern | File:line |
|---|---|
| recall entry (cache home) | `yadgar/server/tools/recall.py:395` |
| fan-out dispatch | `recall.py:179` / `:567` |
| landscape dispatch | `recall.py:126` / `:506` |
| profile pipeline dispatch | `recall.py:593` |
| heat-boost side effects (inline, N writes) | `recall.py:347-391` |
| EMBED_CACHE / CE_CACHE impl | `yadgar/backend/cache.py` |
| EMBED_CACHE key | `yadgar/backend/embed_service.py:573` |
| CE_CACHE key | `yadgar/backend/embed_service.py:637` |
| cache defaults (100000 / 600s) | `yadgar/config.py:802-806`; `config_yaml.py:642-654` |
| graph_prior N+1 (batchable) | `yadgar/storage/memory.py:1029` |
| cofire_prior N+1 (batchable) | `yadgar/storage/memory.py:1069` |
| prior VALUES precomputed in consolidation | `yadgar/storage/memory.py:1055`, `:1091` |
| get_memory N+1 in diversity | `yadgar/storage/memory.py:286`; `retrieval/fusion.py:269` |
| spreading BFS (per-query, not batchable) | `yadgar/retrieval/core.py:192`; `scoring.py:236` |
| branch WHERE clause | `yadgar/storage/branch.py:31` |
| directory post-filter | `yadgar/storage/directory.py:53` |
| ADR-0030 (IO-bound verdict) | `wiki:yadgar-adr-log` |
| prior related plan (invalidate-on-write #3) | `docs/plans/hook-recall-cache-track-a-2026-07-01.md` |
| perf harness (clean-box re-measure) | `docs/plans/perf-loadtest-contract-2026-06-30.md` |
