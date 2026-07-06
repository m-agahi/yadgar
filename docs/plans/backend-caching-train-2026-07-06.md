# Backend Caching Train — Unified Backend Cache (2026-07-06)

Shave ms/seconds off backend recall stages via ONE unified backend `Cache` class
(N namespaces), mirroring the core `yadgar/cache.py` design. Fold the existing
backend LRU caches into it AND add the new query-independent data caches. Rule of
thumb: **shave whenever we can** — not only the big CE win.

## Why (measured, live 5.111/5.16)

Warm recall = 19.3 s. CE = 15.2 s (79 %), sub-CE stages ≈ 4 s.

- **CE is query-specific** (scores query↔candidate *pairs*) → not cross-query
  cacheable — EXCEPT the 3 CE passes **overlap**: `crossfuse` (6 s,
  `fusion.py:56`) and `cross_encoder` (8 s, `reranking.py:137`) re-score
  overlapping `(query, text)` pairs within one request. `multi_passage`
  (1.5 s, `_reranking_multi_passage.py:16`) is mostly disjoint (synthetic
  cluster texts).
- **Sub-CE stages re-fetch query-INDEPENDENT stable data every recall** →
  cacheable cross-query (every recall, not just repeats).
- **Recall-OUTPUT cache (old Car 3) is DEAD**: shadow hit-rate on organic
  `source="tool"` traffic = **0 %** (memorize epoch-churn invalidates keys
  before identical tool recalls repeat). Explicitly OUT of scope — see #88 / #5.
- Proven the pattern works: core read-tool caches give project_brief 466 ms→2 ms
  (233×), wiki_query 401 ms→0.77 ms (518×).

Cross-query stable-data + within-request CE dedup ≈ **0.5–0.75 s + seconds
(CE dedup)** shaved off *every* recall. Grows in relative value after Ettin (#32)
shrinks CE and the sub-CE floor dominates.

## Design — one unified backend `Cache`

Extend the existing `yadgar/backend/cache.py` `LRUCache` into a unified `Cache`
class, same shape as core `yadgar/cache.py`:

- fields: `name`, `max_entries`, `invalidation` (`ModelCkpt | DataEpoch | TTL |
  Manual`), `key_fn`, `deep_copy`, `obs_tier` (obs-by-construction).
- N named instances via a `_REGISTRY` (namespaces). Policy bound at construction
  → zero per-call dispatch overhead (CE/embed `get` stays hot-path-cheap).
- msgpack snapshot I/O + per-instance hit/miss/evict counters carried over from
  `LRUCache`.

### Namespaces

| namespace | key | value | invalidation | source | est. shave |
|---|---|---|---|---|---|
| `ce` | `query_sha:text_sha:ckpt_sha` | CE score | ModelCkpt | **MOVE** existing `_ce_cache` (`embed_service.py:224`) + **WIRE recall CE path** | seconds (within-req dedup, #41) |
| `embed` | `text_sha:ckpt_sha` | vector | ModelCkpt | **MOVE** existing `_embed_cache` (`embed_service.py:223`) | already-served warm |
| `memory_doc` | `memory_id:data_epoch` | full doc dict | DataEpoch | **NEW** — `build_results` (`fusion.py:316` `get_memories_by_ids`) | 150–200 ms |
| `engram_slot` | `slot_index:data_epoch` | `[memory_id,…]` | DataEpoch | **NEW** — `engram_links` (`engram.py:128`) | 100–150 ms |
| `graph` | `entity_id:data_epoch` | `[neighbor,…]` | DataEpoch | **NEW** — spreading+ppr (`knowledge_graph.py:455`, `core.py:125`) | 300–400 ms |

`ce` and `embed` move in **behavior-neutrally** (same keys, same values) — the
win is one class, one obs surface, one snapshot path.

### Key insight — `#41` is just "let recall consult `ce`"

Recall's CE calls (`_rerank_cross_encoder`, `_score_candidates_ce`,
`multi_passage_rerank`) call `score_cross_encoder` **directly**, bypassing the
`ce` cache — by design today (only the `/rerank` HTTP endpoint uses it,
`embed_service.py:722`). Route recall's CE path through the `ce` namespace and:
- `crossfuse` scores `(query, text)` → stored in `ce`.
- `cross_encoder` looks up the overlapping `(query, text)` → **HIT** (same
  request, same query, same ckpt) → skips re-scoring.
That is the within-request dedup — **all queries, not repeat-gated.** Cross-request
repeat hits come free on top (rare, but non-negative).

## Invalidation — cross-service data-epoch

- **Model-stable** (`ce`, `embed`): `ckpt_sha` already in key → model swap busts.
  No change.
- **Data-stable** (`memory_doc`, `engram_slot`, `graph`): need a **write-bust**.
  Backend doesn't see memory/entity writes today (they enter via core
  `memorize`). Solution mirrors core: a **global `data_epoch`** bumped by core on
  writes (memorize/forget/entity/relationship — the core already runs
  `_recall_shadow.bump_epoch`), **passed to the backend on each recall request**
  (header/param) and **embedded in the data-namespace keys**. Stale keys then
  naturally miss — epoch-in-key, no explicit cross-service bust call.
  - `memory_doc` special case: memory **content is immutable** after write →
    content can be cached ~bust-free; only heat/metadata is stale-tolerant →
    short TTL layer or accept staleness for ranking-neutral fields.

## Cars (one branch, one PR at train end)

- **Car 0 — unified `Cache` class + fold-in.** `LRUCache`→`Cache` (namespaces,
  `DataEpoch`/`ModelCkpt`/`TTL`/`Manual`, obs-by-construction). MOVE `_ce_cache`
  + `_embed_cache` in. Behavior-neutral. Foundation.
- **Car 1 — #41 CE dedup.** Route recall CE passes through the `ce` namespace →
  within-request dedup. Biggest shave. Quality gate: deduped scores must be
  **identical** (same key = same score), recall results byte-identical.
- **Car 2 — `memory_doc` cache.** `build_results` id→doc. Low risk
  (content-immutable). ⭐ safe first data-cache.
- **Car 3 — `engram_slot` cache.** slot→ids.
- **Car 4 — `graph` cache + cross-service `data_epoch` infra.** spreading+ppr;
  highest complexity (the write-bust plumbing lands here, or earlier if Car 2/3
  need it — likely hoist the `data_epoch` signal into Car 2).

## Obs (total-visibility, non-negotiable)

Every namespace emits hit/miss/evict metrics (`yadgar_cache_*{cache=…}`) + spans
+ logs by construction — same contract as the core train. A repeated-recall trace
must SHOW `{cache=memory_doc}` / `{cache=graph}` hits.

## Verification (per car)

- Targeted tests, behavior-neutral on miss (#52 no-weakening).
- Measure **hit-vs-miss latency** per namespace (histogram deltas, as done for
  project_brief) — prove the ms shaved.
- **Quality-neutral**: #41 dedup + data caches must return identical recall
  results (same ids, same order, same scores). Run the recall quality harness (#31).
- Trace a warm recall → confirm the new cache spans + the CE-dedup drop.

## Risks / open questions

- **#41 overlap is partial** — `cross_encoder` may expand with implied-fact
  variants (different texts) that miss the memo. Measure the real dedup rate;
  worst case it still shaves the non-expanded overlap.
- **Cross-service `data_epoch`** adds a core→backend signal on the recall path —
  keep it a cheap header/int, not a round-trip.
- **`graph` invalidation** is the hardest — entity/relationship writes must move
  the `data_epoch`; verify no stale-graph recall.
- Interaction with **Ettin (#32)**: independent (Ettin swaps the CE model; #41
  dedups CE calls). Both compose. Do the cache train and Ettin in either order.

## Out of scope

- Recall-output cache (old Car 3 / #88) — data-killed (0 % organic hit).
- Core-side caches — already shipped (#164, unified core `Cache`).
