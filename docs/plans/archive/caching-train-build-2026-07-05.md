> ARCHIVED 2026-07-09 — train shipped #164/#165 (Car 1 project_brief, Car 2 wiki/prelude, Car 3 killed ADR-0071 — 0% tool-path hit-rate). Shadow surface removed.

# Caching Train — buildable per-car spec (Cache class + observability unification)

**Status:** BUILD SPEC. Turns the CONFIRMED, user-approved design into a per-car,
per-file implementation plan. No code in this doc; it is the blueprint the build PRs
follow. **Date:** 2026-07-05. **Author:** agent (bot).

**Confirmed design (do NOT relitigate — the user approved it):** ONE `Cache` class, N
named instances (namespaces), **policy bound at construction** (not a per-call manager
dispatch — rejected; CE `get` stays `self._store[key]`, zero added overhead). Obs is
non-negotiable: the generic `yadgar_cache_hit_total{cache="<name>"}` family everywhere +
a shared `record_cache_hit/miss/evict` helper, `@observe(tier="hot")` on the getters for
I33. branch (TTL-in-key) + rules (whole-flush) FOLD IN as namespaces. Config singleton /
rate-limiter / graph-layout stay bespoke (not caches / different tier) but still get the
obs-metric retrofit.

**Authoritative source docs (this spec builds on them, does not restate):**

- `docs/plans/unified-cache-2026-07-05.md` (d8e6473c) — the design + §R1–R9 re-eval
  (one class / N instances; B+D fold in; only C/H non-caches + J DB-tier stay out).
- `docs/plans/caching-opportunities-2026-07-05.md` (5ae9693b) — the 20-cache inventory +
  ranked new candidates + the shadow hit-rate split (hook ~74% / tool ~0%).
- `docs/plans/cache-refactor-2026-07-01.md` — lever (a) recall-output cache full design.
- `docs/plans/hook-recall-cache-track-a-2026-07-01.md` — hook hot-context (folds into Car 1).
- `docs/plans/recall-forward-only-2026-07-05.md` + `recall-3-train-overhaul-2026-07-04.md`
  — the recall pipeline overhaul that gates Car 3.

---

## 0. TL;DR — the build at a glance

| Car | Scope | Files touched (approx) | PR | Provisional slot |
|---|---|---|---|---|
| **Car 0** | Observability unification — ALL caches on `yadgar_cache_hit_total{cache=}` + shared `record_cache_*` helper; retrofit ~14 silent caches; fold 3 embed families + CE onto the generic family (dual-emit); hot caches via **scrape-time collector**, NOT per-get label-inc. `@observe(tier="hot")` on getters (I33 MISSING=0). **No behavior change.** | ~14–18 | **Own PR** | v5.109.0 |
| **Car 1** | The `Cache` class (generalize `LRUCache` + add deep-copy-on-return + pluggable invalidation + obs-by-construction) **+ project_brief as first consumer** (key `(dir,branch,mode)`, Epoch(dir)+TTL, folds in track-a hook hot-context). | ~6–10 | Own PR (after Car 0) | v5.111.0 |
| **Car 2** | `wiki_read` / `wiki_query` / `agent_dispatch_prelude` as `Cache` instances (small, per-key/pattern). | ~4–6 | Own PR | v5.113.0 |
| **Car 3** | recall-output cache (lever a) as a `Cache` instance. **GATED** on the recall forward-only + Ettin overhaul settling AND the shadow tool-lane hit-rate justifying (~0% today). | ~3–5 | Own PR (downstream) | TBD (post-overhaul) |
| Tail | Migrate CE/embed LRU onto the `Cache` class (obs-consistency only; deep_copy=off). OPTIONAL, non-blocking. | ~3 | Fold into a later car | — |

**PR split:** **Car 0 ships as its own PR** — obs-only, no class, no dependency on the
class panning out; it delivers the user's non-negotiable independently and must not be
coupled to Car 1's blast radius. Cars 1/2/3 each their own PR (one minor per feature per
the versioning convention). **Do NOT bundle Car 0 + Car 1.**

**Version slots (provisional):** core is at **5.107.0**; backend **5.13.0**. Cars take the
next odd minors under the skip-1 convention — **v5.109.0 / v5.111.0 / v5.113.0** — one per
car. Provisional because the recall trains (forward-only → Ettin → restructure) are
in-flight and unversioned; whichever feature merges first claims the next odd slot. Car 0
is core-only (no backend image change) UNLESS the CE/embed dual-emit touches backend
`embed_service_metrics.py` — if it does, bump `BACKEND_VERSION` too (see Car 0 §migration).

**Queue slot (recommended, not just described):** Cars 0/1/2 are **pipeline-independent**
(they do not touch the recall dispatch) → schedule them **in parallel with / ahead of** the
recall forward-only → Ettin → restructure trains, no collision. **Car 3 is downstream** —
gated on the recall overhaul landing + shadow tool-lane data. Concretely: land **Car 0 now**
(cheapest, highest-value, zero dependency), **Car 1 next** (independent of recall), **Car 2**
opportunistically; hold **Car 3** until the forward-only entry point exists.

---

## 1. The `Cache` class — interface + field semantics

**New module:** `yadgar/cache.py` (core-side; NOT `backend/cache.py` — that stays the
lean backend `LRUCache`). The class is built by **generalizing** the existing
`LRUCache` (`backend/cache.py:52`) and adding the three genuinely-new bits:
**deep-copy-on-return**, **pluggable invalidation**, **obs-by-construction**.

### 1a. Source baseline — what `LRUCache` already is (verified)

`yadgar/backend/cache.py`:
- `__init__(max_entries: int, checkpoint_hash: str)` (:52) — OrderedDict + `threading.Lock`.
- `get(key: str) -> Any | None` (:65) — lock-held; **returns a reference** to
  `self._store[key]`; increments `self.hits` (:76) / `self.misses` (:68/:72).
- `put(key: str, value: Any) -> None` (:79) — lock-held OrderedDict insert; increments
  `self.evictions` on LRU overflow (:91).
- Internal int counters `self.hits / self.misses / self.evictions` (:59–61) — **the scrape
  source for the hot path** (§7).
- `save_snapshot` (:109) / `load_snapshot` (:141) — `@observe(tier="stage")`, msgpack +
  header (magic + version + checkpoint hash), atomic temp-file write. **Reuse verbatim**
  for the class's optional `snapshot`.

`LRUCache.get`/`put` carry **no `@observe`** — correct for the backend (own coverage
classification, out of core I33 scope). The new core-side class **must** decorate its
getters (core is under I33 scope) — this is why `tier="hot"` (a span source) is the design,
not "no decorator."

### 1b. The `Cache` class interface

```python
# yadgar/cache.py  — ONE class; N instances; policy bound at CONSTRUCTION.
class Cache:
    def __init__(
        self,
        name: str,                        # bounded metric label; REQUIRED; self-registers
        max_entries: int,                 # bounded LRU; 0 = unbounded (rules whole-flush case)
        invalidation: Invalidation = KeyFn(),  # KeyFn | TTL(secs) | Manual  — the ~3 mechanisms
        key_fn: Callable[..., Hashable] = identity,  # embeds ckpt-hash | time-bucket | epoch
        deep_copy: bool = False,          # copy.deepcopy on get; on=row dicts, off=floats/vectors
        obs_tier: Literal["hot", "cold"] = "cold",  # metric-only vs full tri-signal
        snapshot: SnapshotSpec | None = None,  # optional msgpack persist (reuse cache.py I/O)
    ) -> None: ...

    @observe(tier="hot", name="cache.get")     # span source → I33; hot = span-attr only, no per-call metric/log
    def get(self, key: Hashable) -> Any | None:
        # 1. resolve effective key via key_fn (embeds ckpt/time-bucket/epoch)
        # 2. TTL check if invalidation is TTL(): expire on read
        # 3. hit → self.hits += 1; miss → self.misses += 1   (cheap int, always)
        # 4. IN-BODY metric (cold tier only, inline): record_cache_hit/miss(self.name)
        #    hot tier: NO inline label-inc — the scrape-time collector reads self.hits/misses (§7)
        # 5. return copy.deepcopy(value) if self.deep_copy else value
        ...

    @observe(tier="hot", name="cache.put")
    def put(self, key: Hashable, value: Any) -> None:
        # LRU insert; on evict: self.evictions += 1; cold-tier: record_cache_evict(self.name) + log
        ...

    def invalidate(self, scope: Any = None) -> None: ...   # Manual/WholeFlush bust; logs on bust
    def clear(self) -> None: ...                           # whole-flush (rules case); logs
    def stats(self) -> dict: ...                           # {hits, misses, evictions, size} — collector reads this
```

**Field semantics:**

| Field | Semantics |
|---|---|
| `name` | The bounded `{cache="<name>"}` label + registry key. REQUIRED. `≤ ~20` names total → I33 cardinality-safe. On construct: `_REGISTRY[name] = self` (raises on dup name). |
| `max_entries` | Bounded LRU cap; `0` = unbounded (rules whole-flush namespace). |
| `invalidation` | One of three: **`KeyFn`** (freshness in the key — ckpt / time-bucket / epoch; nothing is ever *invalidated*, stale keys age out via LRU), **`TTL(secs)`** (value stored with write-ts, expired on read — stale-count/DBSIZE/predictive), **`Manual`** (explicit `clear()`/`invalidate()` on a mutation event — rules whole-flush). Default `KeyFn`. |
| `key_fn` | Pluggable key derivation. Combo-1 caches embed freshness here: CE `qsha:tsha:ckpt`; branch `(dir, int((time+hash(dir)%30)//30))`; epoch caches append `_current_epoch(dir)`. Default identity. |
| `deep_copy` | `copy.deepcopy` on `get` return. **ON for row-dict values** (recall-output, project_brief — callers mutate `m["heat"]` in place); **OFF for floats/vectors** (CE/embed — reference is correct, deepcopy would be wrong+slow). |
| `obs_tier` | `"cold"` = inline `record_cache_hit/miss(name)` per get + full span/log (rare caches). `"hot"` = **metric-only via scrape-time collector**, no per-get label-inc, no per-get log (CE/embed/recall-output — thousands of gets/recall). §7. |
| `snapshot` | Optional msgpack persistence — reuse `backend/cache.py` `save_snapshot`/`load_snapshot` I/O verbatim. Only CE/embed use it. |

### 1c. Thin registry + invalidation primitives

```python
_REGISTRY: dict[str, Cache] = {}   # enumeration (/cache-stats) + one config surface; NOT a per-get dispatcher.
                                   # lookup is registry[name].get(key): one dict get, policy already bound.
```

- **Invalidation classes** (`KeyFn` / `TTL(secs)` / `Manual`) are thin policy objects bound
  at construction — NOT branched per `get`. `KeyFn` is the default and the hot-path shape.
- **Epoch primitive = the EXISTING bus** (`_recall_shadow.py`, §6). An `Epoch`-style cache is
  just a `KeyFn` whose `key_fn` appends `_current_epoch(dir)`. Do NOT build a new bus.

---

## 2. Car 0 — Observability unification (own PR, v5.109.0)

**Goal:** total visibility — every cache emits `yadgar_cache_hit_total{cache="<name>"}` /
`_miss_total` (+ size/eviction) through ONE shared helper. **No behavior change; no storage
change; no class.** This is the user's non-negotiable and ships FIRST, independent of Car 1.

### 2a. The shared helper (new — none exists today)

Add to `yadgar/metrics.py` (which already owns `yadgar_cache_hit_total` :108,
`yadgar_cache_miss_total` :115, both `["cache"]`-labeled, and a module `_registry` :47):

```python
# yadgar/metrics.py — NEW helpers (replace scattered inline .labels(cache=…).inc())
def record_cache_hit(cache: str) -> None:  yadgar_cache_hit_total.labels(cache=cache).inc()
def record_cache_miss(cache: str) -> None: yadgar_cache_miss_total.labels(cache=cache).inc()
def record_cache_evict(cache: str, n: int = 1) -> None: ...   # add yadgar_cache_evictions_total{cache}
# NEW families (Car 0 adds them — only hit/miss exist today):
yadgar_cache_evictions_total = Counter("yadgar_cache_evictions_total", "...", ["cache"], registry=_registry)
yadgar_cache_size_entries    = Gauge("yadgar_cache_size_entries", "...", ["cache"], registry=_registry)
```

### 2b. The scrape-time collector (THE hot-path escape hatch — load-bearing)

**⚠️ Process placement is load-bearing.** Every per-passage-hot cache is **backend-side** —
CE (`embed_service.py:648`), embed-vector (`:576`), and (post-forward-only) recall-output.
Core `metrics.py._registry` (:47) and backend `embed_service_metrics.py._registry` are
**separate processes** (the exact seam §2g's both-registry lint guards). **A core collector
CANNOT read backend cache ints.** So:

- **Hot collector lives in the BACKEND — `embed_service_metrics.py`** — reads the CE +
  embed-vector internal ints (`LRUCache.hits/misses/evictions`, `backend/cache.py:59–61`) at
  scrape time and emits BOTH the old bespoke names AND the new `{cache="ce"}` / `{cache="embed"}`
  series into the **backend** registry. The generic `yadgar_cache_hit_total{cache=}` family
  therefore exists in BOTH registries under the same name — exactly the "one namespace, two
  registries" model the design §3d mandates and §2g checks.

```python
# yadgar/backend/embed_service_metrics.py — backend scrape collector; ZERO per-get label-inc.
class CacheStatsCollector:
    def collect(self):   # called only at /metrics scrape time
        for name, c in backend_cache_instances():        # CE, embed LRUCache instances
            yield CounterMetricFamily("yadgar_cache_hit_total",  "...", value=[(name, c.hits)])   # NEW generic only
            yield CounterMetricFamily("yadgar_cache_miss_total", "...", value=[(name, c.misses)])
            # evictions, size_entries — NEW generic {cache=} series only
_registry.register(CacheStatsCollector())   # backend _registry
```

**⚠️ The collector emits ONLY the NEW generic `{cache=}` series (dual-emit resolution A).**
The old bespoke counters (`yadgar_embed_ce_cache_*` `:199`, `yadgar_embed_embed_cache_*` `:229`)
stay STATICALLY declared and inline-incremented EXACTLY as today (`embed_service.py:648/652/
576/580/597`) — **do NOT re-emit them from the collector.** A collector `yield` of an
already-statically-declared name in the SAME process = duplicate `# TYPE` at scrape →
Prometheus rejects the scrape → breaks the CE metric path (violates "no behavior change").
True dual-emit = the OLD series untouched (static+inline) + the NEW `{cache=}` series added
(collector) off the same `LRUCache` ints. Collision-free: `yadgar_cache_hit_total` is declared
statically only CORE-side (`metrics.py:108`), never backend-side, so the backend collector is
its sole backend emitter. The old names are deleted on the scheduled-rename tick (§2d), at
which point their static decls + inline incs are removed together.

- **Hot caches (CE, embed) do NOT call `record_cache_hit` per get.** They bump the cheap
  internal ints (`self.hits += 1`, already present in `LRUCache` :59–61); the backend
  collector reads them at scrape time. **No added latency on the per-passage CE loop
  (`embed_service.py:644`).** This is the §R5 / §3c answer and keeps Car 0 "no behavior change"
  for the hot path.
- **Core caches are at most once-per-recall** (query-embed `embeddings.py:300` fires ~once per
  recall, not per passage) → **all core caches use inline `record_cache_hit(name)`**; core
  likely needs NO collector at all.
- **CE/embed dual-emit** (§2d) = TWO series off the SAME scrape-time ints (old bespoke name +
  new generic `{cache=}`), NOT two hot per-get incs. Re-introducing a per-get `.labels().inc()`
  for the second series would recreate the exact latency regression the collector exists to
  avoid — the failure mode to guard against in review.

### 2c. Retrofit the silent caches (verified sites)

| Cache | File:line (get/lookup site) | Retrofit | Tier |
|---|---|---|---|
| config singleton | `config.py:964` (`get_settings` `@lru_cache(1)`) | inline hit/miss on the lru_cache wrapper (caveat §2e) | cold |
| yaml config | `config_registry.py:74` (`_yaml_layer`, already `@observe`) | inline hit/miss (caveat §2e) | cold |
| rules dict | lookup `rules_engine.py:327`; put `:351`; clear `:308,:442` | inline hit/miss at :327; evict at clear | cold |
| stale-wiki-count | `project.py:2384` (`_compute_stale_wiki_count`, TTL) | inline hit/miss | cold |
| rate-limit | `rate_limit.py:32/36` (`_buckets`) | inline hit/miss (caveat §2e — mutable state, semantically odd) | cold |
| predictive-coding | `predictive_coding.py:75–88` (`_get_cached_entities`) | inline hit/miss | cold |
| graph-layout | read `storage/ops.py:109`; write `:127` (DB record) | inline hit/miss on the SELECT | cold |
| local query-embed | `embeddings.py:300–309` (already double-emits to `{cache="embedding"}`) | keep the generic; drop the legacy after dual-emit window (§2d) | hot (has ints? add) |
| remote query-embed | `remote_embeddings.py:81–91` (**fully silent**) | inline hit/miss `{cache="remote_embedding"}` | cold |
| branch-detect | `project.py:149–157` | **already emits `{cache="branch_detect"}`** — migrate to `record_cache_hit` helper only | cold |
| default-branch | `project.py:165` | no metric today — add `{cache="default_branch"}` via helper | cold |

### 2d. Fold the 3 embedding families + CE onto the generic family (dual-emit)

Verified current mess:
- Core legacy `yadgar_embedding_cache_hits/misses_total` (no label, `metrics.py:77/83`);
  core ALSO emits `yadgar_cache_hit_total{cache="embedding"}` — **double-emit today**
  (`embeddings.py:308–309`).
- CE bespoke UNLABELED family `yadgar_embed_ce_cache_{hits,misses,evictions,size_entries,size_bytes}`
  (`embed_service_metrics.py:199–227`; incr `embed_service.py:648/652`).
- Embed backend LRU family `yadgar_embed_embed_cache_*` (`embed_service_metrics.py:229–254`;
  incr `embed_service.py:576/580/597`).

**Strategy = DUAL-EMIT (default, §10-Q1).** New generic `{cache="ce"}` / `{cache="embed"}`
series emitted **via the backend collector** (§2b) alongside the old bespoke names, which stay
**untouched** (static decl + inline incs) for the transition window; old names + their static
decls + inline incs deleted TOGETHER on a later scheduled tick with a dashboard PR. **Never
silent-drop** (orphans backend Grafana/alerts). Both series read the SAME internal ints, but
the OLD series keep their existing inline incs and the NEW series come only from the collector —
**the collector must NOT re-yield the old names** (duplicate `# TYPE` → scrape rejection; §2b).
Zero extra hot incs either way.

### 2e. Caveats (task requires all ~14; note the odd ones)

- **config singleton + rate-limiter are not data caches** (singleton reload / mutable rate
  state). A hit/miss counter is semantically odd but harmless; add per the task's "retrofit
  ALL" directive. They already carry `@observe` → **I33 holds regardless** of the counter.
- **graph-layout is a DB record** (out-of-process) — the counter tracks the in-process
  SELECT/UPSERT hit, not a real in-memory hit-rate. Add per task; note the semantic.

### 2f. I33 / span coverage

Every retrofit site is either already `@observe`-decorated (config, yaml, branch, default-branch,
stale-count via existing decorators) or is a metric-only edit inside an already-covered function.
**Car 0 adds no new undecorated functions** → I33 MISSING=0 stays green. Verify with
`scripts/check_observe_coverage.py` in pre-commit.

### 2g. Tests (tri-signal obs assertions)

- **Metric-emitted-per-get:** for each retrofitted cache, a test asserting
  `yadgar_cache_hit_total{cache="<name>"}` increments on a hit and `_miss_total` on a miss
  (scrape the `_registry`). Hot caches: assert the collector yields the series with the
  right value from the internal ints (drive N hits, scrape, assert count).
- **Eviction metric:** drive an over-cap put, assert `yadgar_cache_evictions_total` moves.
- **I33 lint clean:** `check_observe_coverage.py` exit 0 (MISSING=0).
- **No span-flood:** a test in the shape of `tests/test_log_span_amplification.py` asserting a
  hot-cache get under live-ish OTLP emits **no per-get span/log** (metric-only) — guards §R5.
- **Both-registry convention lint (Risk-6 → concrete test):** a test asserting the
  `cache=`-label + `yadgar_cache_*` metric-name shape holds in BOTH `metrics.py` (core) and
  `embed_service_metrics.py` (backend) — the convention is the contract across two processes.
- **Dual-emit collision + parity:** assert the backend `/metrics` scrape PARSES cleanly (no
  duplicate `# TYPE` — guards the resolution-A rule that the collector must not re-yield the
  old names). Then assert the new `{cache="ce"}` value == `LRUCache.hits` (the collector reads
  the ints correctly). The old bespoke series stay untouched, so no old-vs-new equality needed.

### 2h. Version bump

Core `5.107.0 → 5.109.0` (`pyproject.toml`). **Backend bump is MANDATORY** — the hot
collector + CE/embed dual-emit live in `embed_service_metrics.py` (backend, §2b), which is
per-passage hot and cannot be done core-side. Bump `BACKEND_VERSION` `5.13.0 → 5.14.0`
(`yadgar/__init__.py:21` + `server.json` + `docker-compose.yml`) and rebuild the backend
image (amd64-only per the build-cost anchor). Core-side retrofits (config/rules/branch/…) ride
the core bump only.

---

## 3. Car 1 — `Cache` class + project_brief first consumer (own PR, v5.111.0)

**Extract-then-generalize:** build the class (`§1`) AND its first real consumer
(project_brief) in the SAME PR, so the class is validated against a live need, not shipped
speculatively.

### 3a. Files touched

- **NEW `yadgar/cache.py`** — the `Cache` class + `_REGISTRY` + `KeyFn/TTL/Manual` +
  `SnapshotSpec` (reuse `backend/cache.py` I/O). Plus `record_cache_*` already added in Car 0.
- **`yadgar/server/tools/project.py`** — wrap `project_brief` (`@_tool()` :2040,
  `project_brief(directory, mode="catalog", branch_hint=None)`) with a `Cache` instance.
- Wire `record_cache_*` collector for the new instance (Car 0 machinery).
- **Track-a hook hot-context** (`docs/plans/hook-recall-cache-track-a-2026-07-01.md`) folds in
  here — same per-directory value shape + same epoch bus.

### 3b. The project_brief cache instance

```python
project_brief_cache = Cache(
    name="project_brief",
    max_entries=256,                          # small keyspace (dirs × modes)
    invalidation=KeyFn(),                     # freshness embedded in the key
    key_fn=lambda dir, branch, mode: (resolved_dir(dir), branch, mode, _current_epoch(resolved_dir(dir))),
    deep_copy=True,                           # brief dict is mutated by callers/_render
    obs_tier="cold",                          # few calls/session → full tri-signal fine
)
```

- **KEY:** `(resolved_directory, branch, mode, epoch)`. No query term → cross-agent identical
  key (the whole reason project_brief beats lever (a)).
- **Invalidation:** epoch (structural) + short **TTL backstop** (heat/anchor drift). The
  design's `TTL` and `KeyFn(epoch)` compose — either encode TTL as a time-bucket term in
  `key_fn`, OR use `TTL(300)` invalidation with the epoch in the key. **Recommend: epoch in
  key + `TTL(300)`** so structural writes bust immediately and heat-drift rides the 300s
  backstop (§10-Q2).
- **Heat-drift rule (Risk):** the epoch bumps on STRUCTURAL writes only (memorize/forget/
  consolidation, §6) — NEVER on heat/decay, else the dir self-invalidates every access.
- **⚠️ Epoch-key normalization must match (build-time check).** `key_fn` calls
  `_current_epoch(resolved_dir)` where `resolved_dir` = `_resolve_project_root(dir)`
  (git-root walk, `project.py:2069`), but memorize bumps via `bump_epoch(ctx.context)` (the
  RAW working dir) and `_current_epoch` keys on the raw string (`_DIR_EPOCH.get(directory, 0)`).
  If `ctx.context` (e.g. a subdir) ≠ project_brief's resolved git-root string, the bump lands
  on a DIFFERENT `_DIR_EPOCH` key → the cached brief is NEVER structurally invalidated (TTL
  still backstops, so nudge-correctness holds, but the epoch is decorative). **Build MUST
  verify both sides normalize the dir identically** — resolve on BOTH the bump and the read,
  or key on the raw dir on both. This matters far more for Car 3 (recall-output IS a
  correctness surface — a decorative epoch there serves stale ranked results).

### 3c. Mode discrimination (verified — critical correctness point)

`project_brief` dispatches by mode (`project.py:2090/2102/2110`):
- **`catalog` / `restore` / `full`** = nudge data (anchors, hot_memories, wiki catalog scan,
  `_render`). **Safe to cache the whole payload.**
- **`signals`** (`_project_brief_signals` :2090) drives the stop-hook's `recommended_actions`
  (which memorize/anchor/session-end/roadmap write-actions fire) → **LOWER staleness
  tolerance.** **Do NOT cache the whole signals payload.** Cache only the query-agnostic
  expensive sub-pieces with a TTL (the git head-info + O(n²) anchor cosine) — mirror the
  existing `_compute_stale_wiki_count` TTL precedent (`project.py:2384`). The `mode` is in the
  key, so the instance simply is NOT consulted (or only for sub-pieces) when `mode="signals"`.

### 3d. Tests

- **hit/miss:** two identical `project_brief(dir, mode="catalog")` calls → 1 miss + 1 hit
  (assert metric + returned-equality).
- **epoch-bump busts:** `project_brief` → `memorize(context=dir)` (bumps epoch via
  `_phase_post_write.py:58`) → `project_brief` MISSES (fresh compute).
- **deep-copy isolation:** get a cached brief, mutate a returned row (`m["heat"] = 9`), get
  again → the cached row is UNCHANGED (deep-copy on return works).
- **staleness bound (signals):** assert `mode="signals"` is NOT served from a stale whole-payload
  cache — the `recommended_actions` reflect a just-written memorize within the TTL/epoch.
- **obs:** `{cache="project_brief"}` hit/miss series move; I33 clean; no per-get span flood.
- **No LongMemEval gate needed** (nudge data, no ranking impact) — just correctness + staleness.

### 3e. Version bump

Core `5.109.0 → 5.111.0`. Backend untouched (project_brief is core-side).

---

## 4. Car 2 — wiki_read / wiki_query / agent_dispatch_prelude instances (own PR, v5.113.0)

Small, safe, per-key/pattern. Adds instances; does not rebuild the class.

| Consumer | Key | Invalidation | deep_copy | obs_tier |
|---|---|---|---|---|
| `wiki_read` (per slug) | `(slug, dir, branch)` | bump on `wiki_add/update/delete` for slug (or per-dir wiki epoch) + short TTL | yes (content dict) | cold |
| `wiki_query` (per query) | `(query, dir, branch, cat, tags)` | per-dir wiki epoch on wiki write | yes | cold |
| `agent_dispatch_prelude` (per pattern) | `pattern` (+ `include_context` flag) | bump on `agent_prompt_save` for pattern (+ recall/wiki epoch if `include_context=True`) | yes | cold |

**Wiki epoch:** extend the same bus (§6) with a per-dir wiki generation bumped on
`wiki_add/wiki_update/wiki_delete`, OR reuse `bump_epoch(dir)` if wiki writes already route
through it (verify in the build — if not, add a `bump_epoch` call in the wiki-write path).

**Tests:** hit/miss per consumer; a `wiki_update(slug)` busts that slug's entry; an
`agent_prompt_save(pattern)` busts that pattern; obs series move. Optional / low priority —
can be deferred without blocking Cars 0/1/3.

**Version:** core `5.111.0 → 5.113.0`. Backend untouched.

---

## 5. Car 3 — recall-output cache (lever a), GATED, downstream

**Design is COMPLETE** (`docs/plans/cache-refactor-2026-07-01.md`) — this car is pure
sequencing discipline, not new design. Build as a `Cache` instance:

```python
recall_output_cache = Cache(
    name="recall_output",
    max_entries=...,                          # bounded
    invalidation=KeyFn(),                     # epoch in key + TTL backstop
    key_fn=<exact-normalized query string + (dir, branch, type, mode, profile, max_results, min_heat, tags) + _current_epoch(dir)>,
    deep_copy=True,                           # row dicts — callers mutate m["heat"]
    obs_tier="hot",                           # per-recall; metric-only via collector
)
```

- **Key:** the shadow-counter's exact 10-tuple (`_recall_shadow.py:126–137`) + epoch. The
  shadow cache is instrumentation-only (caches no results, `_SHADOW_KEYS` :89) — Car 3 makes
  the real cache with the same key shape.
- **fire-and-forget heat-boost on hit** must NOT bump the epoch (else it busts the entry it
  just served — Risk 1 / §R8).
- **Place the wrapper at the POST-overhaul entry point**, not the current `recall.py:395`
  dispatch — after forward-only moves the entry to the backend `POST /recall`.

**GATE (both must hold — unchanged from the design):**
1. The recall **forward-only + Ettin** overhaul settles (place the wrapper at the *new*
   entry; a faster Ettin CE also shrinks lever (a)'s marginal value → re-measure).
2. The shadow **tool-lane** hit-rate justifies it (**~0% today** vs hook-lane ~74% — the
   write-interleaved anti-correlation trap). Let the shadow counter run longer.

**Tests (when built):** epoch-bump busts; deep-copy isolation; heat-boost does NOT self-bust;
**LongMemEval recall@k gate** (Car 3 touches the ranked output — unlike Cars 1/2 it has a
correctness surface); obs metric-only (no flood).

**Version:** TBD, post-overhaul (next free odd minor at build time).

---

## 6. Invalidation-bus wiring (verified)

**The bus already exists** — `yadgar/server/tools/_recall_shadow.py`:

```python
_DIR_EPOCH: dict[str, int] = {}          # :79  per-directory epoch
_GLOBAL_GEN: list[int] = [0]             # :85  global generation

@observe(tier="hot", name="tools.recall_shadow.bump_epoch")   # :92
def bump_epoch(directory: str | None) -> None:                # concrete dir → that dir; None → global gen
    with _LOCK:
        if directory: _DIR_EPOCH[directory] = _DIR_EPOCH.get(directory, 0) + 1
        else:         _GLOBAL_GEN[0] += 1

@observe(tier="hot", name="tools.recall_shadow._current_epoch")  # :111
def _current_epoch(directory: str | None) -> int:
    return _DIR_EPOCH.get(directory or "global", 0) + _GLOBAL_GEN[0]  # effective = per-dir + global
```

**The 2 (only) production callers — verified, no others:**
- `_memorize_phases/_phase_post_write.py:58` — `bump_epoch(ctx.context)` (per-dir on memorize).
- `consolidation/cls.py:25` — `bump_epoch(None)` (global on consolidation prior recompute).

**Wiring per car:**
- **Car 1 (project_brief):** `key_fn` appends `_current_epoch(resolved_dir)`. Already bumped by
  the 2 callers on memorize + consolidation. **`forget` does NOT bump today** (investigator
  confirmed only 2 callers) — add a `bump_epoch(dir)` in the forget path. Note: this is small
  scope creep beyond `project.py` (touches the forget path) and is more load-bearing for Car 3
  (recall correctness) than Car 1 (nudge, TTL-backstopped). Normalize the dir identically to
  the read (see §3b epoch-normalization check).
- **Car 2 (wiki):** add a per-dir wiki bump in the `wiki_add/update/delete` path (reuse
  `bump_epoch(dir)` or a parallel wiki-epoch — decide in build; reusing keeps one bus).
- **Car 3 (recall-output):** same `_current_epoch(dir)` in the key; heat-boost must NOT call
  `bump_epoch`.

**Promote the bus to a shared primitive:** it currently lives in `_recall_shadow.py`. Cars
1–3 import `bump_epoch`/`_current_epoch` from there (or move them to `yadgar/cache.py` and
re-export — decide in Car 1; moving is cleaner but touches the 2 callers + shadow module).

---

## 7. Observability design + I33 / flood-safety

**Two orthogonal mechanisms, both automatic (verified against `observe.py` + I33 lint):**

1. **I33 by construction (the DECORATOR).** `Cache.get`/`put` carry `@observe(tier="hot")`.
   `observe` ∈ `_SPAN_DECORATORS` (`scripts/check_observe_coverage.py:38` =
   `{"trace_span", "observe", "_tool"}`), and `tier="hot"` emits **span-attribute only — no
   per-call metric, no per-call log** (`observe.py`, Tier `Literal["boundary","stage","hot"]`
   :52). So a core-side `get()` passes MISSING=0 with **zero per-call overhead**.
2. **Total-visibility by construction (the IN-BODY / scrape metric).** Hit/miss counts are
   ALWAYS captured (cheap internal `self.hits/misses` ints). Surfaced two ways by tier:
   - **cold-tier:** inline `record_cache_hit/miss(self.name)` per get (rare caches).
   - **hot-tier:** the **`CacheStatsCollector`** (§2b) reads `self.hits/misses` at scrape
     time — **no per-get `.labels().inc()`**. This is the v5.105-flood-safe hot path.

| Signal | Rule | Why |
|---|---|---|
| **Metric** | always (cold=inline, hot=scrape collector) `yadgar_cache_{hit,miss,evictions}_total{cache}` + `_size_entries{cache}` | generic family exists (`metrics.py:108`); `cache=` bounded (≤~20) → I33 cardinality-safe. THIS is total visibility. |
| **Span** | cold=per-lookup span; hot=`tier="hot"` attribute on enclosing span, no per-lookup span | `@observe` present either way → I33 satisfied; overhead differs by tier. |
| **Log** | evict/bust ALWAYS (rare, structural); miss = cold-tier only or sampled | "log on every miss" FLOODS a hot low-hit cache (v5.105). Evict/bust is rare → safe. |

**v5.105 flood precedent (the guardrail):** `@observe` on the log path caused per-log span
amplification → crash-loop; CI missed it under `OTLP_ENDPOINT=''`. **ADR-0037: CI must never
`OTEL_SDK_DISABLED`.** Cache design consequence: hot getters = metric-only, log on evict/bust
only, keep CI OTLP live so a regression is caught.

---

## 8. Sequencing / PR-split / queue slot

- **PR split:** Car 0 = its OWN PR (obs-only, zero dependency on the class — delivers the
  non-negotiable regardless). Car 1, Car 2, Car 3 = one PR each (one minor per feature).
  **Do NOT bundle Car 0 + Car 1** — coupling the fast independent win to the class's blast
  radius defeats the point.
- **Provisional slots:** v5.109.0 (Car 0) → v5.111.0 (Car 1) → v5.113.0 (Car 2) → TBD (Car 3).
  Odd-only skip-1 convention. Provisional: recall trains are in-flight/unversioned; first
  feature to merge claims the next odd slot.
- **Queue slot vs recall trains:** Cars 0/1/2 are **pipeline-independent** (obs, project_brief,
  wiki, prelude are not the recall dispatch) → schedule **parallel with / ahead of** the
  recall forward-only → Ettin → restructure trains, no collision. **Car 3 is downstream** —
  after forward-only creates the new entry point + shadow tool-lane data justifies.
  **Recommended order: Car 0 now → Car 1 next → Car 2 opportunistic → Car 3 gated.**

---

## 9. Risks + migration safety

1. **Hot-path latency regression (THE Car 0 trap).** Moving CE onto the generic family must
   NOT become a per-get `.labels(cache="ce").inc()` (CE get is per-passage, thousands/recall).
   **Mitigation: scrape-time collector reads internal ints (§2b/§7); dual-emit = two series
   off one counter, not two hot incs.** Guarded by the no-span-flood test (§2g).
2. **Metric-rename breaks backend dashboards/alerts.** **Mitigation: dual-emit default (§2d) —
   new generic `{cache=}` alongside old CE/embedding names; scheduled rename + dashboard PR
   later; never silent-drop.** Alt: adopt-for-new + scheduled rename.
3. **Two-registry drift** (core `metrics.py` + backend `embed_service_metrics.py` are separate
   processes). **Mitigation: the both-registry convention lint test (§2g Risk-6) — assert the
   `cache=` label + metric-name shape in both. The convention is the contract, not a shared
   object.**
4. **v5.105 span-flood recurrence.** **Mitigation: hot-tier metric-only, log on evict/bust
   only, CI keeps OTLP live per ADR-0037 (§7).**
5. **Deep-copy discipline/cost.** Row-dict caches MUST deep-copy on return (callers mutate
   `m["heat"]`); deep-copy on a hot path has cost. **Mitigation: `deep_copy` per-instance
   opt-in — OFF for CE/embed floats/vectors, ON for row dicts; measure the copy cost on the
   recall-output path before Car 3.**
6. **Heat-drift self-destruct (E-shape caches).** Epoch must bump on STRUCTURAL writes only
   (memorize/forget/consolidation), never heat/decay. **Mitigation: the bus already does
   exactly this (§6); heat freshness rides the TTL backstop; the heat-boost must not bump.**
7. **Blast-radius (one impl serves many).** **Mitigation: build order — CE/embed stay on
   isolated `LRUCache` until the class is proven on the cold-path new consumers; CE/embed
   migration is the optional tail, never a blocker.**
8. **Bus location coupling.** Moving `bump_epoch`/`_current_epoch` out of `_recall_shadow.py`
   touches the 2 callers + shadow module. **Mitigation: Car 1 can import-in-place (no move) to
   de-risk; move to `yadgar/cache.py` later if wanted.**

---

## 10. Open parameters (carry from design §8 — recommended defaults, confirm before build)

1. **Metric-rename strategy** for the 3 embedding families + CE — **default: dual-emit**
   (new generic `{cache=}` family alongside the old names; never silent-drop; delete old
   on a later tick). Alt: adopt-for-new + scheduled rename with a dashboard PR. **Confirm.**
2. **project_brief TTL** — **default: 300s** (higher hit-rate; nudge data tolerates it).
   Alt: 60s (fresher). Multi-agent spawn cadence sets it. **Confirm.**
3. **Migrate CE/embed onto the class** — **default: optional tail, non-blocking**; they
   work mechanically on `LRUCache`. **Confirm.**
4. **hot-tier metric-only** (no per-lookup span on CE/embed/recall-output) — **default: yes**
   (v5.105 flood-safe); sampled spans a later opt-in. **Confirm.**
5. **Lever (a) gating unchanged** — hold Car 3 on the recall overhaul + shadow tool-lane
   data. **default: yes. Confirm.**

---

## 11. Advisor input

**Pass 1 (before drafting — sequencing + the hot-path focus):**

- **Leave interface tables until source facts land** — "buildable" = exact signatures +
  file:line + concrete wiring, not "wire the epoch bus." Drafted structure first, filled
  interfaces after the two investigators returned. Done.
- **The one hot-path trap:** moving CE onto the generic family must NOT become a per-get
  `.labels(cache="ce").inc()` — added latency on the hottest path, breaks "no behavior
  change." Answer = scrape-time collector off the internal ints. Folded into §2b/§7 + Risk 1.
- **Decisive deliverable answers:** Car 0 = own PR; provisional odd slots 5.109/5.111/5.113;
  queue slot recommended (Cars 0/1/2 parallel/ahead, Car 3 downstream). Folded into §0/§8.
- **Migration safety:** dual-emit default (never silent-drop); Risk-6 → a concrete
  both-registry convention lint test. Folded into §2d/§2g/Risk 2–3.
- **Carry §8 open-Qs as gates with recommended defaults, not silent decisions.** Folded into §10.
- **Config/rate-limit counters are semantically odd** (singleton reload / mutable state) — add
  per the task but note the caveat; they already carry `@observe` so I33 holds. Folded into §2e.

**Pass 2 (after Car 0/1 drafted — migration + hot-path correctness verify):**

- **BLOCKER (fixed): the scrape collector was in the wrong process.** The draft put
  `CacheStatsCollector` in core `metrics.py`, but the per-passage-hot caches (CE, embed,
  recall-output) are **backend-side** — core and backend are separate processes with separate
  registries, so a core collector cannot read backend ints. As written it would silently emit
  nothing, or get "fixed" with a backend per-get `.inc()` — the exact latency regression the
  collector avoids. **Fixed §2b: hot collector moves to `embed_service_metrics.py` (backend),
  reads CE/embed ints, dual-emits old+new series into the backend registry (one namespace, two
  registries per §3d). Core caches are ≤once-per-recall → all inline `record_cache_hit`, no
  core collector needed.**
- **Consequence (fixed §2h): backend version bump is MANDATORY, not conditional** — the hot
  collector is backend-side and cannot be core-only. `BACKEND_VERSION` 5.13→5.14 + image
  rebuild required for Car 0.
- **VERIFY (added §3b/§6): epoch-key normalization must match** — project_brief's `key_fn`
  reads `_current_epoch(resolved_git_root)` but memorize bumps `bump_epoch(ctx.context)` (raw
  dir), and `_current_epoch` keys on the raw string. A subdir mismatch makes the epoch
  decorative (TTL still backstops project_brief; but load-bearing for Car 3's ranked output).
  Added a build-time normalization check.
- **NOTE (added §6): `forget` does not bump today** (only 2 callers verified) — Car 1 adds a
  `bump_epoch` in the forget path; small scope creep, more load-bearing for Car 3.
- **Interfaces confirmed faithful to source** — LRUCache sig, `@observe` tiers,
  `_SPAN_DECORATORS`, epoch bus verbatim, `project_brief` `@_tool()` :2040, shadow 10-tuple all
  match the investigators' findings. PR split / slots / queue / dual-emit / §10 gates sound and
  decisive — not re-opened.
**Pass 3 (final — dual-emit collision catch):**

- **BLOCKER (fixed): the pass-2 collector re-emitted the OLD bespoke names, colliding with
  their still-present static declarations.** Re-`yield`ing an already-statically-declared
  counter name in the SAME process = duplicate `# TYPE` at scrape → Prometheus rejects the
  whole scrape → breaks the CE metric path (violates "no behavior change"). **Fixed
  (resolution A, §2b/§2d): the collector emits ONLY the NEW generic `{cache=}` series; the old
  bespoke counters stay untouched (static decl + inline incs) until the scheduled-rename tick,
  when decls + incs are removed together.** The new `yadgar_cache_hit_total` is declared
  statically only core-side (`metrics.py:108`), so the backend collector is its sole backend
  emitter — collision-free.
- **§2g parity test repurposed:** was "old == new" (presumed a clean scrape, wouldn't catch the
  collision); now "scrape parses cleanly (no dup `# TYPE`)" + "new `{cache=ce}` == `LRUCache.hits`".
- Everything else confirmed sound; not reopened.

- **Completion order:** filled → committed to master (bot identity, docs-workflow sanctioned)
  → user report leading with the Cache interface + per-car scope + collector-process placement
  and the dual-emit collision as the top-two migration risks.
