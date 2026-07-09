> ARCHIVED 2026-07-09 — train shipped #164/#165 (Car 1 project_brief, Car 2 wiki/prelude, Car 3 killed ADR-0071 — 0% tool-path hit-rate). Shadow surface removed.

# Caching Opportunities — where else caching helps, and in what order

**Status:** PLAN / INVESTIGATION ONLY. No code changed. **Date:** 2026-07-05.
**Author:** agent (bot). **Scope:** A deep map of (a) what yadgar caches today and
(b) where else caching would help — a ranked opportunity list with honest
value/risk + an invalidation design per candidate, to queue a "caching train" in
the right order.

**Sibling docs (read first — this plan does NOT relitigate them):**

- `docs/plans/cache-refactor-2026-07-01.md` — the full design for the **recall
  query→ranked-output cache (lever a)**: exact-string key + per-directory
  structural epoch + short TTL heat-backstop + fire-and-forget heat-boost, plus
  the hit-rate-trap analysis. Lever **(c)** (surreal N+1 batch) from that doc
  **already shipped** (v5.96, PR #139). The **shadow hit-rate counter** also
  shipped (v5.96). This doc reuses that design by reference and adds the numbers.
- `docs/plans/hook-recall-cache-track-a-2026-07-01.md` — background-refreshed
  **query-agnostic hot-context** for the lifecycle hooks (prompt-recall /
  subagent-start / instructions-loaded). Parked-then-un-parked; design ready.
- `docs/plans/recall-3-train-overhaul-2026-07-04.md` +
  `docs/plans/recall-forward-only-2026-07-05.md` — the **recall pipeline is
  mid-overhaul** (core→backend forward-only, Ettin CE swap, bounded-parallel
  restructure). This is the load-bearing train-ordering constraint (see Risks).

---

## TL;DR — the ranked opportunities

1. **project_brief per-(directory, branch, mode) cache** — the strongest new
   candidate. Query-AGNOSTIC (every agent in a dir uses the SAME key), so it
   sidesteps lever (a)'s per-prompt hit-rate trap. Per hit saves ~6–8 DB
   round-trips + a 479-page wiki-catalog scan + (signals mode) an O(n²) anchor
   cosine + ~100–200ms of git-subprocess spawn overhead. **Catalog/restore have
   high staleness tolerance (nudge data → cache whole payload); signals mode does
   NOT (it drives stop-hook write actions → cache only the query-agnostic
   sub-pieces).** **Same car as the track-a hook hot-context** — one build serves
   both.
2. **Recall output cache (lever a)** — already fully designed
   (`cache-refactor-2026-07-01.md`) and instrumented. Shadow data now exists:
   the **hook/session-start lane hits ~74%**, the **real-work tool lane hits ~0%**
   so far. So build it for the *repetitive lane*, gate it on the *tool lane*.
   **Do NOT slot it until the recall pipeline overhaul (forward-only + Ettin)
   settles** — it caches a moving target.
3. **Hook hot-context (track-a)** — un-parked, design ready; folds into #1.
4. **wiki_read / wiki_get per-slug content cache** — small, safe, repetitive
   (agents re-read the same slugs); low risk, low-medium value.
5. **agent_dispatch_prelude per-pattern cache** — small, safe; value depends on
   dispatch frequency.

**Do NOT cache (premature/unsafe):** query embeddings and CE scores (already
cached backend-side), per-query PPR/spreading (that IS lever a, not a separate
win), branch detection (already cached), config (already cached), and any
write-interleaved recall result until the shadow tool-lane hit-rate justifies it.

---

## 1. Existing-cache inventory

Everything cached today (source-cited; from a full sweep of `yadgar/`). The two
"star" caches (CE, EMBED) are pure-compute, keyed on text+model-checkpoint, so
they need **no scope/heat/time invalidation** — the checkpoint hash in the key
handles the only staleness (model change).

| # | Cache | What | Where (file) | Key | Bound / evict | Invalidation | Hit-rate signal |
|---|---|---|---|---|---|---|---|
| 1 | **CE score cache** | float CE (query,passage) score | `backend/cache.py:43` (`LRUCache`), keyed `backend/embed_service.py:637` | `sha256(query)[:16]:sha256(text)[:16]:ckpt_hash` | 100k entries LRU (`ce_cache_max_entries`) | checkpoint-hash mismatch → snapshot discarded on load | `yadgar_cache_hit/miss_total{cache="ce"}` |
| 2 | **Embedding cache** | embedding vector | `backend/cache.py:43`, keyed `embed_service.py:573` | `sha256(text)[:16]:mode:ckpt_hash` | 100k LRU (`embed_cache_max_entries`) | checkpoint-hash mismatch | `yadgar_embedding_cache_hits/misses_total` |
| 3 | CE/EMBED **msgpack snapshots** | on-disk persistence of #1/#2 | `backend/cache.py:141-198` → `/data/cache/{ce,embed}.snap` | — | every `cache_snapshot_interval_sec` (600s) + on shutdown | discarded on load if ckpt hash ≠ current | — |
| 4 | **Local query-embed cache** | text→vector | `embeddings.py:65` `OrderedDict` | text string | 512 LRU (hardcoded) | process exit only | shares #2's counters |
| 5 | **Remote query-embed cache** | text→vector | `remote_embeddings.py:32` `OrderedDict` | text string | 512 LRU | process exit only | — |
| 6 | **Model singleton** | SentenceTransformer obj | `embeddings.py:59` `_model_cache` | model name | 1/model, unbounded | never (process exit / reload) | — |
| 7 | **Shadow recall counter (NOT a cache)** | would-be epoch per key | `server/tools/_recall_shadow.py:89` `OrderedDict` | (query,dir,branch,type,mode,profile,max_results,min_heat,tags,source) | 4096 LRU | per-dir `bump_epoch(dir)` + global `bump_epoch(None)` | `yadgar_recall_shadow_cache_hits/misses_total{source}` |
| 8 | **Branch detection** | git branch str | `server/tools/project.py:115` `@lru_cache(128)` | (directory, ~30s time-bucket) | 128 LRU | ~30s time-bucket rollover | `yadgar_cache_hit/miss_total{cache="branch_detect"}` |
| 9 | **Default-branch** | git default-branch str | `server/tools/project.py:166` `@lru_cache(128)` | (directory, ~300s time-bucket) | 128 LRU | ~5min bucket rollover | `{cache="default_branch"}` |
| 10 | **Settings** | pydantic Settings | `config.py:964` `@lru_cache(1)` | singleton | 1 | `clear_config_caches()` on config POST / yaml write | — |
| 11 | **Yaml config layer** | dict of YADGAR_* | `config_registry.py:75` `@lru_cache(1)` | singleton | 1 | `clear_config_caches()` | — |
| 12 | **Rules-applicable** | list[rule] per dir | `rules_engine.py:244` dict | directory | unbounded | `.clear()` on add/delete rule | — |
| 13 | **API /api/stats** | stats dict | `server/http.py:129` module global | singleton | unbounded | 60s TTL (v5.51.0) | — |
| 14 | **System metrics** | Prometheus families | `server/_state.py:117` module global | singleton | unbounded | overwritten on update (lock-guarded) | — |
| 15 | **Daemon health** | core/backend metrics | `viz_daemon_health.py:59` module global | singleton | unbounded | background scraper refresh (`YADGAR_VIZ_HEALTH_REFRESH_SEC`) | — |
| 16 | **Graph-layout** | 3D node positions | `storage/ops.py:109` SurrealDB `graph_layout_cache:current` | singleton record | 1 | signature-mismatch vs live graph → recompute | — |
| 17 | **Predictive-coding entity cache** | entity list | `predictive_coding.py:69` instance | singleton | unbounded | `invalidate_entity_cache()` or TTL (`PREDICTIVE_CODING_ENTITY_TTL_SECONDS`) | — |
| 18 | **Stale-wiki-count** | (count, ts) per dir | `server/tools/project.py:2282/2384` module dict | directory | unbounded | caller TTL (~300s) | — |
| 19 | **DBSIZE** | dbsize payload | `backend/embed_service.py:127` module global | singleton | 1 | `DBSIZE_CACHE_TTL_SEC` (60s) | `embed_dbsize_cache_hits/misses_total` |
| 20 | **Token-bucket / rate-limit** | (tokens, refill_ts) | `rate_limit.py:32` `OrderedDict` | arbitrary key (usually dir) | 1000 LRU | time-based refill | — |

**Design patterns worth noting for new candidates:**

- **Checkpoint-hash-in-key** (#1/#2): pure-compute caches with zero invalidation
  logic. The gold standard — copy it when the value is a pure function of the key.
- **Time-bucket TTL trick** (#8/#9): `int(time.time() // X)` as a key fragment
  gives soft TTL with no explicit expiry sweep, and #8 staggers by
  `hash(dir) % 30` to avoid a thundering herd. Reuse this idiom for
  project_brief.
- **Structural epoch + global generation fold** (#7): the shadow counter already
  implements the exact invalidation bus lever (a) / project_brief would use —
  per-dir bump on `memorize`, global bump on consolidation prior-recompute.

---

## 2. Real shadow hit-rate data (live Prometheus, 2026-07-05)

The v5.96 shadow counter is instrumentation only (caches nothing). Live values:

| source | hits | misses | would-be hit-rate |
|---|---|---|---|
| **hook** | 31 | 11 | **~74%** |
| **tool** | 0 | 2 | **~0%** (tiny N) |

**Read this honestly — do NOT collapse to one "70%."** The two lanes measure
different things:

- **hook lane (~74%):** lifecycle-hook recalls (session-start, subagent-start,
  instructions-loaded) fire the *same query* (project name / prompt) for the
  *same directory* across repeated agent spawns. This is the cross-agent /
  session-start repetition lane. **Strong FOR** caching this lane.
- **tool lane (~0%):** real-work `recall()` calls. So far ~0 would-be hits —
  exactly the **write-interleaved anti-correlation trap** the cache-refactor doc
  warned about (recall→memorize→recall busts the epoch each write). N is tiny
  (2), so this is directional, not conclusive — but it says: **do not assume the
  tool-lane win; the shadow counter must run longer before lever (a) is
  justified for real-work recalls.**

Bottom line: the data argues for caching the **session-start / hook /
project_brief lane** and says **nothing good yet** about the write-interleaved
tool lane. That split drives the ranking below.

---

## 3. New caching candidates (ranked)

Value = hit-rate × per-hit cost saved. Risk = invalidation complexity ×
staleness/correctness blast radius.

| # | Candidate | Value | Invalidation | Staleness/correctness risk | Effort |
|---|---|---|---|---|---|
| 1 | **project_brief per (dir,branch,mode)** | **HIGH** — query-agnostic, cross-agent identical key; saves ~6–8 DB round-trips + 479-page wiki-catalog scan + `_render` per hit; every session-start call | per-dir epoch bump on `memorize`/`forget`/consolidation + short TTL (heat/anchor drift). Reuse shadow-counter bus (#7). | **LOW** — a brief is a context nudge; slightly stale hot-list/anchors is acceptable. Not a correctness read. | **Low-Med** |
| 2 | **Recall output cache (lever a)** | **MED, split** — HIGH on hook/session lane (~74%), ~0 on tool lane; per-hit win *shrinks after (c) shipped* (priors N+1 already batched) → residual = spreading + CE HTTP | per-dir structural epoch (memorize/forget/consolidation) + short TTL heat-backstop; heat-boost fire-and-forget on hit, must NOT bump epoch | **MED** — the one lever with a staleness/correctness surface; solved-by-construction via epoch but hit-rate is the real unknown | **Med-High** (already designed) |
| 3 | **Hook hot-context (track-a)** | **MED-HIGH** — removes the ~1.5s hook recall from the event loop entirely; **same car as #1** | background refresh (per consolidation tick + invalidate-on-`memorize`-per-dir) | **LOW** — query-agnostic top-heat context; explicitly a nudge | **Low-Med** (design ready) |
| 4 | **wiki_read/wiki_get per-slug content** | LOW-MED — agents re-read the same slugs; saves one row read + content fetch per hit | bump on `wiki_add`/`wiki_update`/`wiki_delete` for that slug (or per-dir wiki epoch) | LOW — wiki content is edited rarely; short TTL or slug-epoch is safe | Low |
| 5 | **agent_dispatch_prelude per pattern** | LOW-MED — recomputes the contract per call; **HIGH when `include_context=True`** (fires 2 full recall/wiki_query sub-retrievals, `dispatch_helper.py:159-224`) | bump on `agent_prompt_save` for that pattern (+ recall/wiki invalidation if include_context) | LOW — prompt library changes rarely | Low |
| 6 | **wiki_query results per (query,dir,branch,cat,tags)** | LOW-MED — repeated identical wiki searches re-run semantic+FTS (`wiki.py:566-687`, no cache today) | per-dir wiki epoch on wiki_add/update/delete | LOW-MED — a just-added wiki page must appear; epoch handles it | Low |
| 7 | **PPR networkx graph + entity catalog** | LOW — graph rebuilt every PPR call (`retrieval/graph_helpers.py:17`); `get_all_entities` full scan every spreading call (`graph_helpers.py:117`) | session-TTL or graph-structure-change bump | **MED** — this is per-query-seed compute; borders on lever (a) territory. See rejected-list caveat. | Med |
| 8 | **memory_stats / stats panels** | LOW — already partly TTL-cached (#13/#19) | TTL | LOW | — (mostly done) |

### Candidates explicitly REJECTED as "new" (already cached or = lever a)

- **Query embeddings** — already cached (#2/#4/#5, EMBED_CACHE + local
  OrderedDict). Not a new candidate. (One agent flagged a distinct *query-
  expansion* cache — the expand-to-subqueries step at `scoring.py:204`, separate
  from the cached embedding. Dropped as low-value: the expansion is cheap
  string/dedup work and the resulting per-subquery embeds already hit the cache.)
- **CE scores** — already cached (#1, CE_CACHE). Not new.
- **Branch detection** — already cached (#8, lru_cache + 30s bucket). Not new.
- **Default-branch** — already cached (#9). Not new.
- **Config load** — already a singleton (#10/#11 lru_cache). Not new.
- **Per-query PPR / spreading *result*** — seed- and query-dependent; caching the
  *result* of a specific query's graph walk **IS lever (a)** (the output cache),
  not a separable win. Spreading is a per-query N-round-trip BFS
  (`retrieval/core.py:192`, `scoring.py:236`) — **not batchable, not a stored
  scalar** — so it is precisely the residual cost that only an output-cache HIT
  can skip. Do not build a separate "spreading result cache"; it collapses into #2.
  - **BUT NOTE (candidate #7, kept):** the graph-*structure* is query-independent.
    `_build_networkx_graph` (`graph_helpers.py:17`) rebuilds the whole nx.DiGraph
    every PPR call, and `get_all_entities` (`graph_helpers.py:117`) does a full
    entity-table scan every spreading call. Those two are the *inputs* to the
    walk, not the walk itself — cacheable per-session (invalidate on entity-graph
    structure change) independently of lever (a). Medium risk (structure drift),
    medium effort; ranked #7, not rejected — but lower priority than the
    pipeline-independent cars because it touches the retrieval hot path (collision
    with the Train-3 restructure — see Risk 3).
- **Priors (graph/cofire)** — already batched by lever (c) (v5.96); the values
  are precomputed scalars on the row (an effective cache already). Nothing to add.

---

## 4. project_brief deep-dive (the user's lead)

**Why it's the strongest candidate.** Unlike lever (a), project_brief's cache
key has **no query term** — every agent that starts in `/home/max/git/yadgar`
calls `project_brief(directory, mode="catalog")` and computes the **identical**
result. So it sidesteps the per-prompt hit-rate trap entirely: the hit-rate is
"agents-per-directory-per-invalidation-window," which for a multi-agent workflow
is high (every subagent spawn, every session start).

**`signals` mode (stop-hook path, 2–5×/session) does the most redundant work —
but read the cost honestly (main-thread-verified, `project.py:880-1099`).** On
top of the shared 3 presence queries (`_fetch_presence_rows` `project.py:414-426`)
it computes the roadmap signal (`_compute_roadmap_signal` `project.py:1062`):

- **1 wiki query** (`_get_roadmap_wiki_updated_at`) + **3 git subprocesses
  UNCONDITIONALLY** when the roadmap page exists — `_get_master_head_info`
  (`project.py:880-965`): `git log %ct`, `git log %B`, `git show
  default:pyproject.toml`. **Not gated by staleness** (they run to *compute*
  staleness). **NOT cached.**
- **+2 more git subprocesses** (`_get_pyproject_version_at_ts` `project.py:968`)
  only when `lag_hours > 0` (roadmap behind master) — this half IS gated.
- PLUS `_compute_anchor_signals` (`project.py:1102`): an anchor-count query, the
  **O(n²) anchor-redundancy cosine** over deserialized embeddings
  (`_fetch_anchor_redundancy_pairs` `project.py:746`), promote-scan, expired-count,
  cross-project scan — ~5 queries + local cosine.

**⚠️ Cost honesty (the ceiling≠cost trap).** The subagent that surfaced this read
the `timeout=3` as the per-call duration ("6–9s wasted") — **wrong**, same shape
as the "42s spreading cumtime" caveat in the cache-refactor doc. A warm-repo `git
log`/`git show` is **tens of ms**, so the 3 unconditional subprocesses are ~100–
200ms of **spawn overhead**, not seconds. That makes signals-mode caching a
**worthwhile nicety on the hook path**, NOT a "seconds-saved" headline. The real
recurring cost across all modes is the **cross-agent DB round-trips + O(n²) anchor
cosine + 479-page catalog scan**, which is what carries project_brief to #1 —
not the git spawns. Only `_compute_stale_wiki_count` (`project.py:2384`) is
TTL-cached today.

**⚠️ signals-mode has LOWER staleness tolerance than catalog/restore — do NOT
cache the whole payload.** signals drives the stop-hook's `recommended_actions`
(which memorize/anchor/session-end/roadmap actions fire). Caching the whole
signals output risks re-firing or suppressing write actions on stale state.
**Correct design: cache the expensive *query-agnostic sub-pieces* with a TTL** —
the git head-info and the O(n²) anchor cosine — exactly as
`_compute_stale_wiki_count` already does (#18), NOT the whole signals dict. This
matches existing precedent and de-risks the action-signal staleness. The
whole-output cache is safe for catalog/restore (nudge data), not for signals.

**What catalog mode recomputes (`project.py:1983-2037`, all cited):**

| Sub-step | File:line | Cost |
|---|---|---|
| init_memory rows | ctx (query in `project_brief` dispatch) | 1 SELECT |
| active_work rows | ctx | 1 SELECT |
| checkpoint rows | ctx | 1 SELECT |
| anchor rows (global + project) | `_build_anchor_rows_catalog` `project.py:613` | 2 SELECTs |
| recent episodic count (24h) | inline `storage._q(... WHERE created_at >= cutoff)` `project.py:2004` | 1 SELECT (COUNT-ish) |
| hot_memories | `_build_hot_memories` `project.py:589` | 1 SELECT (top-heat) |
| key_wiki_pages | `_build_wiki_pages` | 1 SELECT |
| **wiki_catalog** | `_build_wiki_catalog` `project.py:485` → `storage.list_wiki_catalog` `storage/wiki.py:541` | **1 metadata scan over all wiki pages (479 here)** |
| stale_wiki_count | `_compute_stale_wiki_count` `project.py:2384` | **already TTL-cached (#18)** |
| `_render` string build | `_render_project_brief` `project.py:221` | in-process, small |

So catalog mode ≈ **~8 distinct DB round-trips + a full wiki-catalog metadata
scan**, all recomputed on every call, keyed only on (directory, branch, mode).
Only stale_wiki_count is cached today.

**Cache design (mirrors the shadow-counter bus + the time-bucket idiom):**

- **KEY:** `(resolved_directory, branch_bucket, mode)`. No query. Small keyspace.
- **VALUE:** the full brief dict (store a deep copy; `_render` is derivable or
  stored alongside).
- **INVALIDATION (correctness by construction):** a **per-directory structural
  epoch** in the key — bump on `memorize` / `forget` to that dir, on the
  consolidation pass (anchors/hot-list/heat reorder), and on any `wiki_add/
  update/delete` for the dir (the catalog changes). This is the **exact bus the
  shadow counter (#7) already maintains** (`_recall_shadow.bump_epoch`).
- **STALENESS BOUND (heat/anchor drift):** a **short TTL backstop** (60–300s).
  Heat ticks continuously and would self-invalidate if it bumped the epoch — so,
  identical to lever (a)'s rule: **epoch = structural freshness, TTL =
  heat/anchor-drift freshness.** A brief that is ≤5 min stale on the hot-memory
  ordering is acceptable (it's a context nudge, not a correctness read) — this
  is the key reason project_brief is **safer than lever (a)**: staleness here
  degrades nudge quality, not answer correctness.

**Is it safe?** Yes — highest staleness tolerance of any candidate. The worst
case of a stale brief is "an agent sees a slightly out-of-date hot-list / anchor
set," which self-heals on the next TTL refresh. No correctness surface.

**The connection to track-a (the insight the lead points at).** project_brief's
`hot_memories` + anchors ARE the "query-agnostic hot context" that
`hook-recall-cache-track-a-2026-07-01.md` recommends injecting into the lifecycle
hooks. A **background-refreshed per-directory hot-context blob** would serve BOTH
(a) the cross-agent project_brief and (b) the hook read (sub-ms, off the event
loop, killing the ~1.5s hook recall and its freeze/lag vector). **One build, two
problems solved.** Recommend building project_brief-cache and track-a-hot-context
as a single "per-directory context cache" car with one invalidation bus.

---

## 5. Recommended caching-train order

Ordered highest-value / lowest-risk / **pipeline-independent first**.

1. **Car 1 — per-directory context cache (project_brief + hook hot-context).**
   Pipeline-independent (does not touch the recall dispatch), high value,
   high staleness tolerance, one invalidation bus (reuse the shadow-counter
   epoch machinery). Serves the user's lead AND un-parks track-a. **Build first.**
   - Key: `(dir, branch_bucket, mode)` for the brief; `(dir)` for the hook blob.
   - Invalidate: per-dir epoch bump on memorize/forget/consolidation/wiki-write
     + short TTL (60–300s).
   - Gate: no ranking impact → no LongMemEval gate needed; just a correctness
     test (a memorize to the dir is reflected within one TTL/epoch bump) + a
     staleness test.

2. **Car 2 — wiki_read/agent_dispatch_prelude per-slug/pattern caches.** Small,
   safe, independent. Cheap wins. Slug-epoch or short TTL. Optional; low priority.

3. **Car 3 — recall output cache (lever a) — GATED, LAST.** Do NOT build until:
   (i) the **recall pipeline overhaul settles** (forward-only + Ettin + Train-3
   restructure — see Risks; caching a moving target is wasted work and a
   correctness hazard), AND (ii) the **shadow tool-lane hit-rate** (currently ~0)
   runs long enough to show a hit-rate that beats the invalidation-surface cost.
   The design is already complete (`cache-refactor-2026-07-01.md`); this car is
   *pure sequencing discipline*, not new design. When built: exact-normalized
   string key + per-dir structural epoch + short TTL heat-backstop +
   fire-and-forget heat-boost on hit; place the wrapper at the **new**
   forward-only entry point, not the current dispatch.

**Where this slots vs the other trains.** The recall 3-train overhaul (forward
-only, Ettin, restructure) owns the recall *hot path*. Cars 1 and 2 are
**orthogonal** to it (project_brief, hooks, wiki, prelude are not the recall
dispatch) → they can ship **in parallel / before** the recall trains with no
collision. Car 3 (lever a) is **downstream** of the overhaul and must wait for
it. So the caching train interleaves: **Cars 1–2 now (independent), Car 3 after
the recall overhaul + shadow data.**

---

## 6. Risks — the invalidation footguns

1. **Heat-drift self-destruct (applies to #1 AND #2).** Heat changes on *every*
   recall (+0.1 boost) and decays continuously. If heat drift bumped the epoch,
   the cache would invalidate its own directory on every access → ~0% hit-rate.
   **Rule: epoch bumps on STRUCTURAL writes only (memorize/forget/consolidation);
   heat/decay freshness rides a short TTL backstop, never the epoch.** The
   fire-and-forget heat-boost on a cache hit must NOT bump the epoch or it
   invalidates the entry it just served.

2. **Write-interleaved anti-correlation (lever a's make-or-break).** Real-work
   sessions do recall→memorize→recall; each memorize bumps the epoch → the next
   recall misses. The ~0% tool-lane shadow hit-rate is this trap, observed. **Do
   not assume lever (a)'s win — the shadow counter is the gate.** project_brief
   is *less* exposed: its calls cluster at session start (before the work-phase
   writes), and its staleness tolerance means even a mid-work stale hit is fine.

3. **Caching a moving target (the train-ordering footgun).** The recall pipeline
   is mid-overhaul *today* (`recall-forward-only-2026-07-05.md` — core becomes a
   pure forwarder, stdio dropped, landscape/profile become backend params;
   `recall-3-train-overhaul` — Ettin CE swap + restructure). An output cache
   built against the *current* dispatch would need re-homing after forward-only
   moves the entry point, and Ettin changes the per-hit cost calculus (a faster
   CE shrinks lever (a)'s marginal value further, exactly as (c) did). **Build
   Car 3 against the post-overhaul entry point, not the current one.** Cars 1–2
   are immune (pipeline-independent).

4. **Over-coarse epoch (safe direction).** A per-*directory* epoch over-
   invalidates on a multi-branch dir (a write on any branch busts all branches'
   entries). This is **safe** (over-invalidates, never under) and simpler than
   per-(dir,branch) epochs; accept the coarseness.

5. **Deep-copy discipline.** Callers and the heat-boost mutate returned row dicts
   in place (`m["heat"] = new_heat`). A cache MUST hand out a deep copy, never
   the stored object — else the cached value corrupts. (Same note in the
   cache-refactor doc for lever a; applies to project_brief too.)

6. **Consolidation must bump the global generation.** The consolidation pass
   recomputes graph/cofire priors and reorders heat/anchors — it changes what
   *every* dir's brief and recall would return. The shadow counter already folds
   a global generation bump on consolidation (#7); the real caches must subscribe
   to the same signal, or a post-consolidation hit serves pre-consolidation
   rankings.

---

## 7. Advisor input (both passes)

**Pass 1 (after inventory + shadow-data read, before drafting the ranking):**

- **Fixed a shadow-data overclaim.** Initial read was "31/44 ≈ 70% → build lever
  a." Advisor: decompose by source — **hook ~74%, tool ~0%.** That is NOT
  generic evidence for lever (a); it's evidence for the session-start/hook lane
  and says nothing good yet about write-interleaved tool recalls. Folded into §2.
- **Don't re-derive lever (a).** `cache-refactor-2026-07-01.md` already has the
  full epoch+TTL+heat-boost design and (c) shipped. Reference it, add the
  numbers, spend words where the existing doc is silent (project_brief). Done —
  lever (a) is a row + pointer, not a re-design.
- **project_brief = same car as track-a hot-context.** Its hot_memories+anchors
  ARE the query-agnostic hot context track-a wants; one build solves the
  cross-agent brief AND the hook-lag problem. Folded into §4/§5.
- **Drop already-cached candidates.** Query embeddings (EMBED_CACHE), CE scores
  (CE_CACHE), PPR/spreading-per-query (= lever a, not separable) — mark existing,
  not new. Folded into §3's rejected list.
- **Train-ordering pushback the task wants.** Check whether the recall pipeline
  overhaul makes a caching train premature. It does for lever (a) —
  pipeline-independent cars (project_brief/hot-context, wiki, prelude) first;
  lever (a) gated on both shadow data AND pipeline stability. Folded into §5/§6.

**Pass 2 (before finalizing):**

- **BLOCKER — unverified headline caught + corrected.** The draft's centerpiece
  ("signals-mode fires 3 uncached git subprocesses, the single most concrete
  latency waste") came entirely from a subagent; I had not read the code.
  Main-thread re-verified `_get_master_head_info` (`project.py:880`) +
  `_compute_roadmap_signal` (`project.py:1062`): the 3 git subprocesses DO fire
  unconditionally (when the roadmap page exists), +2 more only when roadmap lags.
  **But** the subagent read `timeout=3` as per-call cost ("6–9s") — the
  ceiling≠cost trap (same shape as the "42s spreading cumtime" caveat). Warm-repo
  git is ~tens of ms → ~100–200ms spawn overhead, a nicety, not a seconds-saved
  headline. **§4 headline downgraded; project_brief stays #1 on the DB
  round-trips + O(n²) cosine + catalog scan, not the git spawns.**
- **signals-mode staleness is LOWER than catalog/restore** — it drives stop-hook
  write actions (`recommended_actions`). Caching the whole signals payload risks
  re-firing/suppressing writes on stale state. **Corrected §4 to cache only the
  query-agnostic sub-pieces (git head-info, O(n²) cosine) with a TTL — matching
  the `_compute_stale_wiki_count` precedent — not the whole signals output.**
- **Query-expansion divergence** between the two agents (one said embeddings
  cached, one flagged the expansion step) — resolved: dropped as low-value with a
  one-line note in the rejected list, rather than left unaddressed.
- **Ranking + train order confirmed sound** by the advisor — no change to the
  sequencing (pipeline-independent cars first, lever (a) gated last).

---

## 8. Open questions for the user

1. **project_brief TTL length** — 60s (fresher nudge) vs 300s (higher hit-rate).
   Your multi-agent spawn cadence sets this.
2. **Build project_brief-cache and track-a hot-context as ONE car?** Recommended
   (shared invalidation bus, shared value shape). Confirm.
3. **Lever (a) gating** — OK to (i) let the shadow tool-lane counter run longer
   and (ii) wait for the recall forward-only + Ettin overhaul to land before
   building the output cache? (Recommended: yes to both.)
4. **wiki/prelude caches (Car 2)** — worth the small effort, or skip as noise?
5. **Is (c)-alone-plus-project_brief-cache enough** for the perceived
   session-start latency, deferring lever (a) indefinitely if the tool-lane
   hit-rate stays low? (Likely yes.)

---

## 9. Key file references

| Concern | File:line |
|---|---|
| project_brief catalog build | `yadgar/server/tools/project.py:1983-2037` |
| project_brief anchors | `project.py:613` (`_build_anchor_rows_catalog`) |
| project_brief hot_memories | `project.py:589` (`_build_hot_memories`) |
| project_brief wiki catalog scan | `project.py:485` → `storage/wiki.py:541` (`list_wiki_catalog`) |
| project_brief stale-count (TTL-cached) | `project.py:2384` (`_compute_stale_wiki_count`) |
| project_brief render | `project.py:221` (`_render_project_brief`) |
| shadow recall counter + epoch bus | `yadgar/server/tools/_recall_shadow.py:89` |
| branch-detect cache (already done) | `project.py:115` / `:166` |
| CE / EMBED cache | `yadgar/backend/cache.py:43`; keys `embed_service.py:573/637` |
| recall entry (lever-a home, current) | `yadgar/server/tools/recall.py:395` |
| recall forward-only (future entry) | `docs/plans/recall-forward-only-2026-07-05.md` |
| lever-a full design | `docs/plans/cache-refactor-2026-07-01.md` |
| track-a hook hot-context design | `docs/plans/hook-recall-cache-track-a-2026-07-01.md` |
| spreading BFS (not batchable, = lever-a residual) | `retrieval/core.py:192`; `scoring.py:236` |
