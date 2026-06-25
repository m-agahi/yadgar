# CPU/Fan Burst Root-Cause Validation + Consolidation Embedding-Scan Efficiency Fix

Investigation date: 2026-06-25. Read-only investigation + design. No code edited.

> **AUDIT 2026-06-25 (improvement-train #29, group A car #30–33).** Part 2 cites
> re-verified against current code: **31/33 line-refs exact**, two minor drifts +
> one path drift recorded, no functions moved files or were deleted. Plan is
> CURRENT and ready to build. Corrections:
> - `yadgar/server/lifecycle.py` "v5.7.0 PR-0: consolidation daemon removed"
>   comment is now at **line 396** (plan/§Part-1 said 404).
> - `yadgar/storage/migrations.py` no-partial-index comment now at **256–257**
>   (plan said 257) — present + accurate.
> - **Path drift:** the nightly process is `yadgar/scripts/nightly_cycle.py`
>   (NOT `scripts/nightly_cycle.py`); the consolidation step is
>   `_step_consolidation()` at **line 298** (creates its own `StorageEngine` —
>   confirms the "nightly = separate short-lived process, can't see an in-RAM
>   matrix" argument that kills option A). Console script:
>   `yadgar-nightly-cycle = "yadgar.scripts.nightly_cycle:main"` (pyproject.toml:76).
> - All hot call sites (`storage/memory.py:388/394/398/410-419` `SELECT *`,
>   `dream.py:23/28`, `community.py:162/179/231`, `cls.py:266/350/525/653/517/640`,
>   `heat_decay.py:94`, `vector.py:42-83/112/146`) and all test refs
>   (`test_sleep_compute.py:596/499`, `test_merge_duplicates_perf.py:214`,
>   `test_no_per_pair_anti_pattern.py:69`, `test_heat_single_writer_e2e.py`)
>   confirmed present at the cited (or ±1) lines. Part 1 (host-side fan burst)
>   unchanged — still OPEN, hand to user via MIGRATION_NOTES.

## TL;DR — two separate things were conflated

The task asked to validate one finding: *"the ~20-min idle CPU/fan burst was traced to consolidation
re-reading the ENTIRE embedding set from SurrealDB every cycle, saturating surreal."*

After tracing the code, the systemd timers, **Yadgar's own episodic memory of this 11-attempt hunt**,
and the **overnight burst capture log** (`/tmp/overnight_burst_20260625.log`, 8h), the finding is **two
claims welded together, one of which does not hold**:

1. **The fan burst itself is NOT confirmed to be the consolidation embedding scan.** It is OPEN. The
   "~20 min" cadence is REAL (surreal bursts on a clean 1200 s pacer in deep idle), but a Prometheus
   read proves the idle burst is **not a yadgar-issued SELECT** (the `op=SELECT` count + duration are
   flat at idle) — so it is surreal-process-internal and/or observer-effect, and the consolidation loop
   that the finding implies no longer has an idle trigger (removed in v5.7.0). The high-`SELECT`-time
   evidence the finding cited turns out to be **morning recall/rerank activity**, not idle consolidation.
2. **The `SELECT *` full-embedding scans in consolidation ARE real and worth fixing** — as a
   nightly-surreal-load + RAM efficiency improvement. But selling them as "the fix for the fan burst"
   is unsupported by the idle data.

Treat (1) and (2) as independent deliverables. The verification metric the task wanted ("show the burst
gone") only works if (1) and (2) are the same thing — and the data says they are not.

---

## Part 1 — Fan-burst root cause: CORRECTED

### What the finding claimed vs. what the code shows

| Finding claim | Verdict | Evidence |
|---|---|---|
| Consolidation re-reads ALL embeddings every cycle at idle | **Not at idle** | The idle/daemon consolidation loop was REMOVED in v5.7.0. `yadgar/consolidation/orchestrator.py:3-6` + `yadgar/server/lifecycle.py:404` ("v5.7.0 PR-0: consolidation daemon removed; `_st._consolidation.start()` intentionally removed"). Consolidation now runs only via MCP `consolidate_now` or the nightly systemd timer. |
| Bursts every ~20 min at idle | **Cadence REAL, driver wrong** | Overnight log deep-idle window (02:00–07:00): surreal spikes recur on a clean **1200 s (=20 min)** period (1200,1200,1200,1005,195 — the 1005+195 sum to 1200; the burst splits across two 5 s samples). |
| 4 astrocyte OS processes each re-scan | **False — not multiprocess** | `yadgar/astrocyte_pool.py` keeps `_processes: dict` of DB rows in the `astrocyte_process` table; `yadgar/consolidation/__init__.py:195-206` iterates 4 domains in a single sequential for-loop. Zero `multiprocessing.Process`/`ProcessPool` in production code. |
| `op`-labelled metric proves the embedding scan | **Too coarse to attribute** | `yadgar/storage/client.py:281-302`: `op` = the first SQL keyword only (SELECT/UPDATE/…). "99.9% SELECT time" proves *a* slow SELECT, not *which* SELECT. |
| Burst = surreal saturating (1120 ms/s) | **Saturation only during ACTIVITY** | Overnight log: surreal >100 % (188–208 %) appears ONLY at 08:47–08:51 = Max resuming (recall→CE rerank traffic). At true idle, surreal bursts are a modest ~37–42 % on the 1200 s period — not saturation. |

### What the overnight log actually shows (`/tmp/overnight_burst_20260625.log`)

- SPIKE process distribution (112 spikes): `surreal 43`, `node_ex+ 29`, `brave 14`, `tempo 9`,
  `.claude+ 7`, `nix 5`, `grafana 4`.
- In deep idle BOTH `surreal` AND `node_exporter` burst on the same ~1200 s period — a **shared
  external pacer** (a 20-min scrape/housekeeping tick or surrealkv's own LSM compaction), not a
  yadgar Python loop.
- The 4 instrumented yadgar daemon loops (`metrics_sampler`, `model_unload`, `queue_drainer`,
  `sse_event_stream`) advance on their *own* steady cadence (~60 s heartbeats) and **do not jump at
  burst timestamps** — corroborated by Yadgar memory **531374** ("the 4 yadgar daemon loops did NOT
  advance abnormally at burst times… strengthens OBSERVER-EFFECT, weakens yadgar-loop hypothesis").
- Yadgar memory **531373** ("10th attempt, OPEN/unresolved") + **531374** are the authoritative state:
  ruled out (with evidence) orphan-pytest-surreal (mem 6671), dbsize os.walk, circuit-breaker probes,
  auto-capture hooks. Two live hypotheses remained: observer effect (node_exporter `--collector.systemd`
  on 395 units + cadvisor `--docker_only=false`, scraped every 15 s) and model evict→reload thrash.

### Prometheus check (read-only) — the decisive evidence

The finding's strongest claim is the metric: *"99.9% SELECT time, 216 s/hr, bursts to 1120 ms/s."*
That metric, `yadgar_surrealdb_query_duration_ms{op="SELECT"}`, is **emitted only by yadgar Python**
after each `self._q()` call (`storage/client.py:291-302`). surrealkv's internal compaction never goes
through `_q()`, so it cannot appear there. So the metric directly tests "is the burst a yadgar-issued
SELECT?" I queried Prometheus (`:9090`) over last night's exact idle window:

| Window | `increase(..._count{op=SELECT}[5m])` | `rate(..._sum{op=SELECT}[5m])` | surreal CPU (proc log) |
|---|---|---|---|
| **Idle 02:00–05:00** | **flat ~25 / 5m, NO jumps at 1200 s burst times** | **flat ~7 ms/s** | 37–42 % on 1200 s pacer |
| **Activity 08:50–08:52** (Max resumed) | — | **566 ms/s (burst)** | 188–208 % |

Two facts fall out:
1. **At idle the yadgar SELECT count + duration are dead flat** — they do NOT jump at the 1200 s surreal
   bursts. If the idle burst were a yadgar full-embedding `SELECT`, both would spike. They don't. So the
   **idle 20-min surreal CPU burst is NOT a yadgar-issued SELECT** — it is surreal-process-internal
   (surrealkv background work) and/or observer-effect, **invisible to the `op=SELECT` metric**.
2. **The finding's "216 s/hr / 1120 ms/s SELECT" burst is the MORNING ACTIVITY window** (566 ms/s at
   08:50, exactly when surreal hit ~200 %). That is **recall → CE cross-encoder rerank traffic** (Max
   resuming work), not idle consolidation. The finding measured an *activity* burst and mis-attributed
   it to an *idle* consolidation scan.

### Conclusion for Part 1

The fan burst is **not** the consolidation full-embedding scan, and the high-`op=SELECT`-time evidence the
finding cited is **activity-driven recall/rerank**, not idle consolidation. No autonomous ~20-min embedding
scan exists in the daemon (the idle consolidation loop was removed in v5.7.0). There are **two distinct
idle signals**, neither yadgar-SELECT: a **~1200 s surreal-process burst** (surreal-internal background
work — surrealkv is the prime suspect, but this is NOT provable from the yadgar SELECT metric and must be
confirmed at the process level) and a **~75 s node_exporter burst** (observer-effect; mem 531374). Both
pace independently of the 4 instrumented yadgar loops.

**To actually close Part 1 (host-side, NOT done here — read-only + No-Apply rule). Hand to Max via
MIGRATION_NOTES:**
- Confirm the surreal-process attribution at the process level (the yadgar query metric can't see it):
  `py-spy dump` / `perf` on the surreal PID at a burst, and inspect surrealkv compaction/GC settings for a
  ~20-min cycle. Prometheus `rate(container_cpu_usage_seconds_total{name=~"yadgar-backend|surreal"}[1m])`
  at the 1200 s timestamps will show whether the surreal container CPU integral at idle is material or a
  5 s-sampling artifact.
- Decisive A/B for observer-effect: briefly stop `node_exporter` + `cadvisor`, watch if the idle burst
  vanishes (observer) or persists (surreal-internal).
- These are infra mutations / process-level probes — out of scope for this read-only investigation.

---

## Part 2 — The real, fixable inefficiency: `SELECT *` full-embedding scans in consolidation

Independent of the fan burst, the consolidation/sleep path has genuinely wasteful full-table reads.
Worth fixing for nightly surreal load and peak RAM, and to remove a latent scaling cliff at 100k+ memories.

### Confirmed hot call sites (corrected from the finding's `vector.py:114/148`)

The finding pointed at `yadgar/storage/vector.py:114` (`get_memories_needing_reembedding`) and `:148`
(the `recreate_vector_table` backup). Those are rare (reembed / manual index rebuild). The **real**
per-cycle full scans are in `yadgar/storage/memory.py`:

| Method (storage/memory.py) | SQL | Caller | Cadence | Need |
|---|---|---|---|---|
| `get_all_memories_with_embeddings` (`:394`) | `SELECT * FROM memory WHERE embedding IS NOT NONE AND heat > 0` (no limit) | `sleep_compute/dream.py:23`; `sleep_compute/community.py:179`; `curation/strengthen.py:174` | nightly + 6 h-gated sleep | dream needs only ~40 vectors; community needs per-cluster only |
| `get_memories_with_embeddings(limit≤4000)` (`:398`) | `SELECT * … ORDER BY last_accessed DESC LIMIT $lim` | `consolidation/cls.py:266,350,525,653` | nightly | already capped; builds N×N matmul |
| `get_all_memories_for_decay` (`:388`) | `SELECT * FROM memory WHERE heat > 0 AND (is_protected=false OR NONE)` | `consolidation/heat_decay.py:94`; `community.py:162,231`; `embed_compress.py:56`; `metacognition/gap_detection.py:20` | nightly | decay is `heat *= factor` — pure server-side UPDATE |

Two reasons these are expensive:
- **`SELECT *` pulls full content + metadata + embedding** for every row. 2951 rows ≈ 11 MB (vectors
  alone ≈ 4.4 MB at 384-dim f32) — confirms the finding's ~11 MB figure is content-inflated, not vectors.
- **`WHERE embedding IS NOT NONE AND heat > 0` is NOT index-backed.** SurrealDB v3.0.5 has no partial
  index (`yadgar/storage/migrations.py:257`). So it is a genuine **full-table scan**, regardless of how
  many rows match. The HNSW index (`memory_embedding_idx`, `migrations.py:1123`) is used ONLY by the
  `<|K,EF|>` KNN operator on the recall path (`storage/vector.py:43-83`), never by these bulk reads.

### What each computation actually needs (correctness constraints)

- **`dream_replay` (`dream.py:14-60`)** loads ALL memories-with-embeddings (`:23`) only to sample
  `DREAM_REPLAY_PAIRS` (=20) random pairs and call `self._embeddings.similarity()` per pair (`:48`).
  It needs ~40 vectors but pulls ~3000. **Fix: sample IDs from the id list first, then fetch only those
  rows** (preserves exact random-pair semantics — strictly better than a "recent-N window," which would
  change behavior).
- **`generate_cluster_summaries` (`community.py:175-204`)** loads ALL, then per-cluster `np.mean` centroid
  (`_compute_centroid`, `community.py:61-71`). Centroid is per-cluster and bounded by cluster size — needs
  cluster members only, not a global pull.
- **`_link_similar_memories` / `_merge_duplicates` (`cls.py:517,640`)**: build `matrix @ matrix.T` over a
  ≤4000-row cap (`SIMILARITY_MATRIX_MAX_CANDIDATES`, config default 4000) and write `memory_similarity_link`
  / delete dups. Already capped. The all-pairs comparison must stay global across the candidate set (cross-
  domain dup/link detection is the point) — but can move to **incremental-by-time**: KNN each *new* memory
  against the global HNSW index, rather than rebuilding the full N×N each cycle. (Existing↔existing pairs
  were linked in prior cycles, so nothing is missed — this is incremental-by-time, NOT the domain-partitioned
  incremental that WOULD miss cross-domain pairs.)
- **heat decay (`heat_decay.py`)** pulls all rows to multiply `heat`. This is a pure scalar update — push
  into surreal as `UPDATE memory SET heat = ... WHERE heat > 0` and stop materializing rows in Python.
- **reembed_stale / compress_old** already filter to stale/old subsets — fine.

### No in-process store to reuse (option B is dead)

There is **no in-RAM embedding matrix anywhere** in the daemon. Recall delegates to surreal's HNSW KNN
(`retrieval/scoring.py:199`, `retrieval/fusion.py:333` → `storage.search_vectors`). The `EMBED_CACHE`
(`backend/embed_service.py:195-202`, 100k LRU) caches **text→vector computation**, not DB reads — the bulk
scans bypass it. So option (A) "hold a matrix across cycles" would be a *new* component, and it is
**undermined by the process model**: the nightly cron runs consolidation in a **separate short-lived
process** (`scripts/nightly_cycle.py:298-334`) with its own `StorageEngine` — it cannot see a matrix held
in the long-lived core process. An in-process cache would optimize only the interactive `consolidate_now`
path, not the nightly path. Reject (A).

### Recommended fix (beats the alternatives)

**Column projection + server-side decay + sample-then-fetch + incremental linking.** Concretely, in
priority order:

1. **Stop `SELECT *`; project only needed columns.** New storage helpers returning `(id, embedding)` or
   `(id, heat)` only. Eliminates the ~11 MB content pull on every scan. Pure win, cadence-independent,
   zero behavior change. (Biggest single reduction in surreal egress + Python RAM.)

   > **CORRECTION (2026-06-25, commit `63f1d4f`):** car **C1 as shipped is SCAFFOLDING ONLY.** It
   > added the three projected helpers (`iter_embeddings_minimal()`, `get_embeddings_by_ids()`,
   > `get_ids_with_heat()`) + a `SELECT *` lint-guard + unit tests, but switched **ZERO call sites** —
   > all 8 current callers need content/metadata/datetime columns beyond `id+embedding` / `id+heat` and
   > cannot migrate without a behavior change (see the deferred-caller table in
   > `test_no_select_star_in_bulk_scans.py`). So the "biggest single reduction / zero behavior change"
   > framing above describes the *helpers' potential*, not C1's realized effect: **C1 moved no data and
   > changed no perf on its own.** The actual perf comes from **C2** (server-side heat decay) and **C3**
   > (dream two-phase sample-then-fetch), which *use* these helpers at their call sites. C1 is the
   > foundation the wins are built on, not a win by itself.
2. **Server-side heat decay.** Replace `get_all_memories_for_decay()` → Python loop → batch UPDATE with a
   single `UPDATE memory SET heat = math::max(...)` style server statement. Stops materializing every row.
3. **`dream_replay`: sample IDs, fetch only sampled rows.** ~3000-row pull → ~40-row fetch by id.
4. **Incremental-by-time similarity linking.** KNN new memories against the HNSW index instead of rebuilding
   the full ≤4000 N×N matmul each cycle. (Bounds the Python matmul spike that was the other half of the
   finding — the python ~255 % numpy matmul.) **"New" must include memories whose embedding *changed*
   since last cycle** (`reembed_stale`, reconsolidation), not just brand-new rows — track a
   `dirty`/`embedding_updated_at` set so updated-embedding memories are re-linked against the index too.

Why not the others:
- **(B)** no store exists. **(A)** invisible to the nightly process; rejected.
- **(C) push all-pairs into surreal**: pushing similarity-linking into the DB = N KNN round-trips into the
  very store you're trying to unload — likely worse than one in-RAM matmul, and the maintainer already
  deferred it ("eventual target but not implemented here", `cls_store/clustering.py:237-241`). (C) fits the
  *sampled* dream case only, where it is just a per-vector KNN — but #3 (sample-then-fetch) is simpler.
- **(E) paginate/stream**: band-aid; doesn't remove the scan, and #1 (projection) + #2 (server decay)
  dominate it.

---

## TDD implementation plan (Part 2 only — Part 1 is host-side ops)

Application code → tests-first (per repo discipline). All paths absolute under `/home/max/git/yadgar`.

### Files to change
- `yadgar/storage/memory.py` — add projected helpers: `iter_embeddings_minimal()` → `(id, embedding bytes)`;
  keep `get_all_memories_with_embeddings` as a thin shim during migration. Add `get_embeddings_by_ids(ids)`.
- `yadgar/storage/vector.py` — (optional) a server-side decay statement helper.
- `yadgar/consolidation/heat_decay.py` — switch to server-side UPDATE (or batched projected read).
- `yadgar/sleep_compute/dream.py` — sample ids first, then `get_embeddings_by_ids`.
- `yadgar/sleep_compute/community.py` — centroid path fetches per-cluster ids, not global.
- `yadgar/consolidation/cls.py` — incremental-by-time linking (new-memory KNN) behind a flag.
- Config (I25 three-way, see below) for any new knob.

### Failing tests to write FIRST
- `yadgar/tests/test_dream_replay_*` — assert dream replay fetches only ~`DREAM_REPLAY_PAIRS`-worth of rows,
  NOT all (spy on storage call / assert row count fetched ≤ 2·pairs). Mirror the existing perf guard
  `test_sleep_compute.py::test_dream_replay_under_5s_at_60_memories` (line ~596).
- `test_heat_decay_server_side` — assert decay issues a bounded number of statements (server UPDATE), not a
  full materialization; reuse the single-writer contract guard `test_heat_single_writer_e2e.py`
  (BC-CSW1: exactly one `batch_writes` for heat).
- `test_no_select_star_in_consolidation_scans` — lint-style guard (mirror
  `tests/test_no_per_pair_anti_pattern.py:69-100`) failing if these call sites use `SELECT *`.
- Incremental linking: extend `tests/test_merge_duplicates_perf.py` (`test_500_memories_under_100ms`, line ~214)
  and a correctness test that incremental-by-time produces the same links as the full N×N on a seeded set.
- Keep green: `test_consolidation.py`, `test_consolidate_now.py`, `test_astrocyte_pool.py`,
  `test_sleep_compute.py` (`test_detect_communities_under_5s_at_100_entities`,
  `test_dream_replay_under_5s_at_60_memories`), `test_embedding_upgrade.py::test_get_memories_needing_reembedding`.

### Config knobs (I25 three-way sync — enforced by pre-commit + `test_config_three_way_sync.py`)
A new knob (e.g. `CONSOLIDATION_INCREMENTAL_LINK_ENABLED`) must be added in lockstep to all three:
1. `yadgar/config.py` — `Settings` field (default).
2. `yadgar/config_yaml.py` — `FIELD_META` entry (`desc`, `section`).
3. `yadgar/config_registry.py` — `_REGISTRY` `ConfigEntry` (`YADGAR_`-prefixed env name).
Do NOT add to `config_env_only_allowlist.txt` (that is for true env-only exceptions).

### Contracts / invariants touched
- **BC-C2** (heat decay lowers heat → archive), **BC-CSW1** (single heat writer) — server-side decay must
  preserve both. **BC-C4** (nightly sleep phases run / produce dream insight) — dream sampling change must
  keep producing insights. **BC-C5a** (astrocyte domain consolidation) — unchanged. Verify these e2e tests
  stay green; no new BC entry needed unless a phase is added.
- I25 config sync (above). No change to the astrocyte model (it is single-process logical domains).

### Metrics / how to verify Part 2 worked
- `yadgar_surrealdb_query_duration_ms{op="SELECT"}` — `_sum` and `_count` for the consolidation window
  should drop after projection (less data marshalled). NOTE: still coarse (verb-only); for attribution,
  also watch `yadgar_consolidation_duration_seconds{phase=...}` (`metrics.py:67`) per phase.
- Peak RSS during a nightly cycle should drop (no 11 MB content pull).
- **This does NOT verify the fan burst** — see Part 1; the burst metric is the host-side surreal-container
  CPU integral at idle, which Part 2 is not expected to move.

### Risks
- Server-side decay must replicate the importance-weighted decay exactly (`IMPORTANCE_DECAY_FACTOR`); pin
  with a characterization test before switching.
- Incremental-by-time linking can drift from full N×N if the HNSW EF is too low — keep behind a flag,
  validate parity on a seeded corpus, default off until proven.
- Embedded-mode (MTREE) vs server-mode (HNSW) branch (`migrations.py`) — incremental KNN must work in both
  or fall back to the capped matmul in embedded mode.

---

## Honest framing for Max
- The fan burst is still OPEN; the overnight data points at **observer-effect / surrealkv compaction on a
  20-min idle pacer**, not consolidation. The "11th attempt root cause = consolidation embedding scan" is a
  hypothesis the idle data does not support (no idle consolidation loop exists post-v5.7.0).
- The embedding `SELECT *` scans are real and worth fixing for nightly load + RAM + 100k-scale headroom —
  but do not expect fixing them to silence the fan.
