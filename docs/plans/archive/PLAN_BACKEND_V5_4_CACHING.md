# PLAN — Backend v5.4.0: Recall Hot-Path Caching

> **STATUS: SHIPPED backend v5.4.0 (2026-05-27)**

**Status:** drafted 2026-05-27, scope locked via interactive Q&A. Pending implementation.

**Master at draft time:** yadgar-core v5.7.9 live, yadgar-backend v5.3.0 live, PLT/OTLP verified.

---

## Why

Post-v5.6.7 Tempo analytics + 2026-05-27 live trace inspection: cross-encoder
rerank is **88-91% of recall latency** on every subagent_start + most
explicit-recall calls.

| Stage | % of typical 10770 ms subagent_start trace |
|---|---|
| `retrieval.cross_encoder_rerank` (single batched call to `/rerank/ce`) | 88.9% |
| 51× `storage.memory.get_memory` (N+1 candidate hydration) | 9.7% |
| BM25 + HNSW + embed combined | <1% |

v5.7.2 already cut `CROSS_ENCODER_TOP_K` 20 → 10 → halved CE latency.
Diminishing returns from further TOP_K cuts. **Caching the CE result is
the next lever.** Embedding cache piggybacks for marginal extra effort.

---

## What ships (v5.4.0 backend release)

### Cache 1: CE score cache

- **Key:** `(query_text_sha256, candidate_memory_id, ce_checkpoint_sha256)`.
- **Value:** rerank score (float).
- **Layer:** in-memory `OrderedDict` LRU + periodic disk snapshot.
- **Cap:** 100K entries × ~1KB = 100MB.
- **Invalidation:** model-checkpoint-hash in key. New CE checkpoint → all
  old entries unreachable (not stale — keyed out). No explicit invalidation
  call needed.
- **Hit-path:** `_reranking_cross_encoder.py` checks per-candidate; partial
  hits + partial misses cohabit in one CE batch (uncached candidates go to
  CE, results merged + cached).
- **Expected hit-rate at steady state:** medium-high. Same Claude session
  re-querying related contexts. Live measurement TBD; baseline target 30%
  → 5× recall speedup on hits.

### Cache 2: Embedding cache

- **Key:** `(text_sha256, embed_checkpoint_sha256)`.
- **Value:** vector (384 dims × float32 = 1.5KB).
- **Layer:** same LRU pattern.
- **Cap:** 100K entries × ~1.5KB = 150MB.
- **Hit-path:** wrap embed call sites in `embed_service.py`.
- **Expected win on hit:** 20-50ms per query → 0ms.

### Storage layer

- In-memory `OrderedDict`-backed LRU (cache.py — new module).
- Periodic disk snapshot every `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC` (default
  600s = 10min) to `/data/cache/{ce,embed}.snap`.
- Snapshot format: **msgpack** (chosen over Python's serialize-anything
  module to avoid eval-on-load class — msgpack is data-only, type-safe).
  Length-prefixed framing, magic header `YADCACHE\0` + version byte +
  checkpoint-hash.
- Restore on backend startup: if checkpoint-hash matches current model →
  load; else discard + start empty.

### Knobs (registered in BOTH yaml + registry)

| Env var | yaml key | default | role |
|---|---|---|---|
| `YADGAR_CE_CACHE_MAX_ENTRIES` | `cache.ce_max_entries` | 100000 | CE cache cap |
| `YADGAR_EMBED_CACHE_MAX_ENTRIES` | `cache.embed_max_entries` | 100000 | Embed cache cap |
| `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC` | `cache.snapshot_interval_sec` | 600 | Snapshot cadence |
| `YADGAR_CACHE_SNAPSHOT_DIR` | `cache.snapshot_dir` | `/data/cache` | Snapshot path |
| `YADGAR_CE_CACHE_ENABLED` | `cache.ce_enabled` | true | Kill switch |
| `YADGAR_EMBED_CACHE_ENABLED` | `cache.embed_enabled` | true | Kill switch |

**`0` for entry-cap disables that cache.** Kill switches preferred for
config rollback without nix-update.

### Telemetry (~10 new series, all I23-compliant)

Per cache (`ce` and `embed`):

- `yadgar_embed_<cache>_cache_hits_total` counter
- `yadgar_embed_<cache>_cache_misses_total` counter
- `yadgar_embed_<cache>_cache_evictions_total` counter
- `yadgar_embed_<cache>_cache_size_entries` gauge
- `yadgar_embed_<cache>_cache_size_bytes` gauge

Plus shared:

- `yadgar_embed_cache_snapshot_age_seconds{cache}` gauge

---

## What does NOT ship in v5.4.0

| Item | Why deferred |
|---|---|
| BM25 / HNSW result caches | Stages already <50ms. Write-invalidation cost > cache benefit. Skip indefinitely unless they become the bottleneck post-CE-cache. |
| Full recall-pipeline cache | Freshness-sensitive. User memorizes → expects immediate visibility in next recall. TTL trade-off ugly. |
| CE batched inference | **Already batched** — confirmed via code (`score_cross_encoder(query, texts: list[str])` takes a list). No change needed. |
| N+1 `get_memory` hydration batching | Real win (1s/recall) but independent of caching. Deferred to **backend v5.4.1**. Overlaps with core roadmap item #14 (consolidation SQL batching). |
| SurrealKV-backed cache | DB roundtrip per lookup defeats speed win. In-memory only. |
| CE model preload | Already shipped — v5.6.7 PR-G (`YADGAR_MODEL_IDLE_EVICTION_SECONDS=0`). |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_cache.py`:
   - LRU semantics (get/put/eviction)
   - Snapshot round-trip (write → restart → restore identical state)
   - Checkpoint-hash mismatch → empty cache
   - Cap by entries
   - Concurrent put/get (asyncio safety — async lock if shared state crosses await)
2. **`yadgar/cache.py`** — new generic `LRUCache` class. ~200 LOC. msgpack snapshot.
3. **CE cache integration** in `_reranking_cross_encoder.py`. Per-candidate
   key lookup before CE batch call; partial hits short-circuit; misses go
   to CE; results merged + back-filled.
4. **Embedding cache integration** in `embed_service.py` embed call sites.
5. **Periodic snapshot task** in backend `lifespan` — asyncio background
   task at interval, ExceptionGroup-safe.
6. **Restore on startup** in same `lifespan`, BEFORE serving first request.
7. **Metrics** in `embed_service_metrics.py` — 10 declarations + writers (I23 passes).
8. **YAML registration** in `yadgar/config_yaml.py` — 6 entries in new
   `cache` section.
9. **Registry registration** in `yadgar/config_registry.py` — same 6 keys.
10. **`server.json::backend_version`** bump 5.3.0 → 5.4.0. `docker-compose.yml`
    backend tag bump. Nix `yadger_backend_version` bump.
11. **`MIGRATION_NOTES.md`** — v5.4.0 entry with deploy guide + verification
    steps + expected hit-rate ramp.
12. **Wiki page 6163** update — mark backend caching item DONE.

---

## Acceptance criteria

- `pytest yadgar/tests/test_cache.py yadgar/tests/test_embed_service*.py` green.
- I23 + I24 lints exit 0.
- `python scripts/check_versions.py` exit 0.
- After backend restart with traffic: hit + miss counters both > 0 within
  60s.
- After `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC` elapses: snapshot files exist
  under `/data/cache/`. `snapshot_age_seconds` gauge resets to ~0.
- After backend restart with valid snapshot: cache size jumps to
  pre-restart size on startup (visible via `_size_entries` gauge).
- Live trace via Tempo: at least one `rpc.rerank.ce` span with attribute
  `cache.hit_count > 0` (need to add span attribute as part of impl).

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| msgpack snapshot version drift | Magic header + version byte. Reject + restart empty on mismatch. |
| Memory accounting drift (Python dict overhead != serialized size) | Cap by entry count primary; `sys.getsizeof()` polling secondary for `_size_bytes` gauge. Adjust default cap if soak shows real memory > 250MB. |
| Cache poisoning if checkpoint hash collides | SHA256 truncated to 16 bytes. Collision-resistant for this domain. |
| Snapshot dir interaction with vacuum | `/data/cache/` is outside `surreal_db/`. vacuum's `swap surreal_db` step does not touch it. Verified. |
| Concurrency: race between put + snapshot | asyncio lock on the cache's serialize-snapshot path. Reads stay lock-free. |
| Kill switch needed for emergencies | `YADGAR_CE_CACHE_ENABLED=false` env override returns nothing-from-cache; CE pipeline behaves pre-v5.4.0. |

---

## Estimate

~600 LOC implementation + ~250 LOC tests. Single agent dispatch (Sonnet,
worktree-isolated, mandatory rebase prelude). One backend image rebuild.
Single backend release commit. No core image change.

---

## Open / parked questions

- **Hit-rate baseline** — speculative until live data. After v5.4.0 deploy
  + 24h soak, measure: target ≥30% CE cache hit-rate, ≥50% embed cache
  hit-rate. Re-tune caps if hit-rate cliff at lower entry count.
- **Snapshot during heavy traffic** — does 600s snapshot pause visible to
  recall? Should be sub-second on 100K-entry dict. Confirm via Tempo span
  added around snapshot write.
- **Eviction signal to dashboard** — `_evictions_total` rising fast means
  cap too low. Worth a Grafana alert at >100/min sustained.

---

## v5.4.1 follow-up (deferred work, NOT part of v5.4.0)

N+1 `get_memory` hydration batching. Replace 51× sequential reads with
single `WHERE id IN $ids` query. 1s win per recall. Overlaps with core
roadmap item #14. Ship after v5.4.0 cache hit-rate baseline established
— makes scope of the win precise.
