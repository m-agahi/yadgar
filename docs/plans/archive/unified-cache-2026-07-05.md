> ARCHIVED 2026-07-09 — train shipped #164/#165 (Car 1 project_brief, Car 2 wiki/prelude, Car 3 killed ADR-0071 — 0% tool-path hit-rate). Shadow surface removed.

# Unified Cache Layer — one class for the common shape + total-visibility observability

**Status:** PLAN / INVESTIGATION ONLY. No code changed. **Date:** 2026-07-05.
**Author:** agent (bot). **Scope:** Assess the user's two asks — (a) ONE cache
implementation serving ALL yadgar caching, and (b) TOTAL VISIBILITY (tri-signal
obs on every cache) as non-negotiable — then re-suggest the caching train with the
unified layer as foundation.

**Sibling docs (read first — this plan builds on them, does NOT relitigate):**

- `docs/plans/caching-opportunities-2026-07-05.md` (5ae9693b) — the 20-cache
  inventory + the ranked new-candidate list (project_brief, wiki, prelude,
  recall-output) + the shadow hit-rate split (hook ~74% / tool ~0%).
- `docs/plans/cache-refactor-2026-07-01.md` — the full recall query→output cache
  (lever a) design: exact-string key + per-dir structural epoch + TTL heat-backstop
  + fire-and-forget heat-boost. Lever (c) N+1 batch already shipped (v5.96).
- `docs/plans/hook-recall-cache-track-a-2026-07-01.md` — background-refreshed hook
  hot-context (folds into the project_brief car).
- `docs/plans/recall-forward-only-2026-07-05.md` + `recall-3-train-overhaul-2026-07-04.md`
  — the recall pipeline is mid-overhaul; the load-bearing train-ordering constraint
  for lever (a) only.

---

## TL;DR — the honest verdict (SPLIT the two asks; they have different answers)

The user asked for two things. They are not one decision, and conflating them is the
trap. Answered separately:

1. **Observability unification (ask b) — unqualified YES. This is the real find, and
   it's the user's non-negotiable.** The mess in yadgar caching today is NOT N storage
   impls — it is the **observability surface**. Ground-truth (verified, §3): THREE
   different metric families track "embedding" caching, the CE cache sits on a
   **bespoke** name *off* the generic family, ~14 caches are **totally silent** (zero
   hit/miss metrics), and there is **no shared helper** — every emitting site does a
   bespoke inline `.labels(cache=…).inc()`. Standardizing on the existing
   `yadgar_cache_hit_total{cache="<name>"}` family (`metrics.py:108`) + a shared
   `record_cache_hit/miss` helper + retrofitting the silent caches is a clean, cheap,
   high-value win that delivers "total visibility" **independent of whether unified
   storage pans out**. Ship this FIRST (Car 0).

2. **Storage unification (ask a) — NO as "one cache to rule them all," YES rescoped to
   "one class for the COMMON shape."** The evidence against a single all-swallowing
   class is our own inventory: **10 distinct (eviction × invalidation) combos across 10
   caches** (§2). A class that swallows all 10 grows leaky seams at 6 of them — the
   textbook *fits-all → fits-none*. **But** a single **new** general-purpose class
   (bounded LRU + pluggable invalidation [checkpoint-key | TTL | epoch] + optional
   **deep-copy-on-return** + observability-by-construction) cleanly serves the caches
   that *share a shape*: the new consumers (project_brief, wiki, recall-output) plus the
   existing pure-compute/TTL caches. The deliverable is **"one class for the common
   shape,"** never "migrate all 10."

3. **What stays bespoke (the leaks, named):** branch/default-branch (TTL-in-key via
   `lru_cache` decay), config singletons (`clear()`-hook, not a data cache), the rules
   dict (unbounded whole-clear is *intentionally correct* for small N), the rate limiter
   (mutable rate state — a category error to evict mid-window), and graph-layout
   (out-of-process SurrealDB — an in-process class cannot hold it). These get the
   **obs metric** retrofit (Car 0) but keep their storage mechanics.

4. **Build the class extract-then-generalize, not generalize-then-hope.** Do NOT ship a
   speculative Car 0 class abstracted from the existing 10 — that IS the fits-none trap.
   Design the class from the *new consumers'* shared needs and validate it by building
   **project_brief as its first consumer in the same car** (Car 1).

**Re-suggested train:** Car 0 = observability unification (all caches, independent) →
Car 1 = the general-purpose class + project_brief (co-designed, folds in track-a) →
Car 2 = wiki/prelude instances → Car 3 = recall-output (lever a) instance, gating
unchanged. Optional tail: migrate CE/embed LRU onto the class for obs-consistency only.

---

## 1. Context — what the user wants vs what the codebase is

The user wants (a) ONE cache serving ALL needs and (b) tri-signal obs on every cache
(hit/miss **metrics** labeled by cache name, **spans**, **logs** — the `@observe`
standard, I33-enforced). The investigation below establishes: the codebase already
has a generic per-name metric family and a strong `@observe`/I33 standard, but caching
is fragmented into 10 mechanically-distinct impls with wildly inconsistent obs. The
design question is which of the two asks the fragmentation supports (obs: yes; storage:
partially).

---

## 2. Existing-cache inventory (mechanics + current observability)

Verified against source (main-thread + two verification agents, 2026-07-05). The
prior doc's 20-row inventory stands for the long tail; this table focuses on the 10
load-bearing caches whose **(eviction × invalidation)** shape decides unification, and
adds the **verified current-obs** column (which corrects two prior-doc errors — see
notes below the table).

| Cache | Storage / evict | Invalidation | Key | Thread-safe | Deep-copy? | Persist | Current obs (metric / span / log) |
|---|---|---|---|---|---|---|---|
| **CE score** | `LRUCache` OrderedDict, `max_entries` (100k) | checkpoint-hash-in-key | `qsha:tsha:ckpt` | `threading.Lock` | **no** | msgpack `/data/cache/ce.snap` | metric: `yadgar_embed_ce_cache_{hits,misses}_total` (**no label, bespoke — NOT the generic family**) / span: snapshot-only / log: no |
| **Embed** | `LRUCache` (same class) | checkpoint-hash-in-key | `tsha:mode:ckpt` | Lock | **no** | msgpack `/data/cache/embed.snap` | metric: **THREE families** (see note) / span: snapshot-only / log: no |
| **branch-detect** | `@lru_cache(128)` | TTL via `int((time+hash(dir)%30)//30)` bucket-in-key (~30s, staggered) | `(dir, bucket)` | GIL (lru_cache) | n/a (str) | none | metric: `yadgar_cache_{hit,miss}_total{cache="branch_detect"}` (**generic family** ✓) / span/log: no |
| **default-branch** | `@lru_cache(128)` | TTL `int(time//300)` bucket (~5min) | `(dir, bucket)` | GIL | n/a | none | metric: `{cache="default_branch"}` ✓ / span/log: no |
| **Settings** | `@lru_cache(1)` singleton | `clear_config_caches()` on config write | — | GIL | n/a | none | **SILENT** (`@observe` on fn → span+stage-dur, no cache counter) |
| **yaml config** | `@lru_cache(1)` singleton | `clear_config_caches()` | — | GIL | n/a | none | **SILENT** |
| **rules-applicable** | unbounded `dict` | whole-`.clear()` on rule add/delete | `dir` | none | **no** | none | **SILENT** |
| **shadow recall** | `OrderedDict` 4096 LRU | **epoch-bus** (`bump_epoch(dir|None)`) | 10-tuple | `threading.Lock` | n/a (int) | none | metric: `yadgar_recall_shadow_cache_{hits,misses}_total{source}` / span/log: no. **Instrumentation-only — caches no results.** |
| **stale-wiki-count** | module `dict`, unbounded | per-entry TTL (~300s, since-write) | `dir` | `threading.Lock` | n/a (int) | none | span+stage-dur via `@observe`; **no hit/miss counter** |
| **DBSIZE** | 2 module globals, singleton | TTL 60s (since-write) | — | none (async loop) | shallow dict copy | none | metric: `yadgar_embed_dbsize_cache_{hits,misses}_total` (no label, bespoke) / span/log: no |
| **rate-limit** | `OrderedDict` 1000 LRU | time-refill (token bucket, continuous) | arbitrary (usu. dir) | `threading.Lock` | n/a (float) | none | **SILENT** (also: not a data cache — see leaks) |
| **predictive-coding entity** | single slot, unbounded | **dual**: TTL 300s + explicit `invalidate_entity_cache()` | — | none | **no** | none | **SILENT** |
| **graph-layout** | **SurrealDB record** (out-of-process) | signature-mismatch (caller overwrites) | `graph_layout_cache:current` | DB | n/a | **DB-persisted** | **SILENT** |

**File refs:** `backend/cache.py:43` (LRUCache); `server/tools/project.py:115/166`
(branch); `config.py:964` + `config_registry.py:75,98` (config); `rules_engine.py:244,308,442`;
`server/tools/_recall_shadow.py:76-178` (shadow + epoch bus); `server/tools/project.py:2282`
(stale-count); `backend/embed_service.py:127` (DBSIZE); `rate_limit.py:32`;
`predictive_coding.py:69`; `storage/ops.py:97`.

**⚠️ Prior-doc corrections (both verified against source):**

1. **CE cache does NOT emit `yadgar_cache_hit_total{cache="CE"}`.** It uses a bespoke,
   **unlabeled** `yadgar_embed_ce_cache_hits_total` (`embed_service_metrics.py:199`,
   incremented `embed_service.py:648`). The prior doc's `{cache="CE"}` claim is wrong.
2. **"Embedding" caching is tracked by THREE distinct metric families** — a genuine
   inconsistency, not one counter: (i) `yadgar_embedding_cache_{hits,misses}_total`
   (legacy, no label, core `EmbeddingEngine._query_cache`, `metrics.py:77`); (ii)
   `yadgar_embed_embed_cache_{hits,misses,evictions}_total` + size gauges (backend LRU,
   `embed_service_metrics.py:229`); (iii) the generic `yadgar_cache_hit_total{cache="embedding"}`
   — and the **core side increments BOTH (i) and (iii) simultaneously** (`embeddings.py:308-309`).

### 2a. The (eviction × invalidation) diversity map — the crux

**10 distinct combos across 10 caches** — every cache has a unique pair. This IS the
fits-none evidence:

| Combo | Eviction | Invalidation | Caches | Fits the common class? |
|---|---|---|---|---|
| A | LRU-size (max_entries) | checkpoint-hash-in-key | CE, embed | **YES** (pure-compute, no-invalidation) |
| B | LRU-size (lru_cache 128) | TTL via bucket-in-key | branch, default-branch | **LEAK** — TTL lives in the *key*, not a get/put param; `lru_cache` decay is the invalidation *moment*. A get/put-TTL class changes semantics. |
| C | singleton (lru_cache 1) | explicit `cache_clear()` | Settings, yaml | **LEAK** — config-load-with-clear-hook, not a data cache |
| D | unbounded dict | whole-`.clear()` on mutation | rules | **LEAK** — whole-clear for small N is *intentionally correct*; a sized LRU would serve stale for un-mutated dirs |
| E | LRU-size (OrderedDict 4096) | **epoch-bus** | shadow recall | **YES** (the epoch primitive the class should expose) |
| F | unbounded dict | per-entry TTL | stale-count | **YES** (TTL is the common case) |
| G | singleton | global TTL | DBSIZE | **YES** (degenerate TTL) |
| H | LRU-size (OrderedDict 1000) | time-refill (token bucket) | rate-limit | **LEAK / category error** — mutable rate state, must never evict mid-window; not a data cache |
| I | single slot | **dual** TTL + explicit bust | predictive-coding | **BORDERLINE** — expressible as TTL + manual-bust; marginal value in migrating |
| J | DB record | signature-mismatch | graph-layout | **LEAK** — out-of-process; an in-process class cannot hold it |

**Categorization:** pure-compute (A), epoch-bus (E), TTL-family (F/G/I), singletons that
barely qualify (C/G), a rate-controller misfiled as a cache (H), and one DB-backed cache
(J). **Common shape = A + E + F + G (+ I borderline).** Bespoke stays = B, C, D, H, J.

---

## 3. Observability — the actual find (and the non-negotiable, done right)

### 3a. The standard already exists: `@observe` / I33

- **`@observe`** (`observability/observe.py:228`) emits tri-signal **by tier**:
  `boundary` = span + request counter/duration + INFO/ERROR log; `stage` = span +
  shared stage-duration/error family + ERROR-on-raise log; **`hot` = span-attribute
  only, ZERO per-call metric, ZERO per-call log**; `exempt` = no-op. It labels by
  **function name**, NOT cache name — so `@observe` is the *log/span* mechanism, but
  **cache hit/miss counters are separate** (the generic `{cache=}` family below).
- **I33** (`docs/ARCHITECTURE_INVARIANTS.md:456`): every non-test function under
  `yadgar/` must have a span source or be allowlist-exempt. **Global hard-fail
  (MISSING=0) since v5.105**, enforced by `scripts/check_observe_coverage.py` in
  pre-commit + CI. Anti-cardinality rule: **no per-function histograms** — shared
  families keyed by a bounded label. This is the guardrail the cache-name label must
  respect (bounded set of cache names, not per-key).

### 3b. Observability-by-construction design — TWO orthogonal mechanisms

"Observable by construction" threads three needles at once (I33 pass + the non-negotiable
metric + no v5.105 flood) via **two orthogonal mechanisms**, both automatic:

1. **I33 compliance by construction (the DECORATOR).** Every class method (`get`/`put`/
   `invalidate`) carries `@observe(tier=obs_tier)`. I33 is a **static AST check for a
   span-source decorator** — `observe` is in `_SPAN_DECORATORS` (`check_observe_coverage.py:38`)
   — so `@observe(tier="hot")` **satisfies the MISSING=0 lint with zero per-call
   overhead** (hot = span-attribute on the enclosing span only, no per-call metric/log —
   verified §3a). This is why `tier="hot"` is the design, NOT "no decorator": a bare
   `get()` under I33 scope would fail the lint.
2. **Total-visibility by construction (the IN-BODY metric).** The hit/miss **metric**
   is emitted in the method body (`record_cache_hit/miss(self.name)`), **orthogonal to
   the decorator**, on **every** get regardless of tier. That is "total visibility" — no
   per-site `@observe` to forget, satisfied for every instance from its `name` alone.

**⚠️ Why the new class MUST decorate where the backend `LRUCache` did not.** The
`LRUCache.get`/`put` carry no `@observe` and are fine — the **backend** has its own
coverage classification (out of core I33 scope). But the new class's common-shape
consumers (project_brief, recall-output, wiki) are **core-side, UNDER I33 scope** →
decorating their hot methods is **not optional**. `tier="hot"` is precisely how the
class stays I33-compliant *and* flood-safe. (This confirms the discriminator: a core-side
cache method without a span source would trip MISSING=0.)

Signal summary (metric always; span/log tier-gated):

| Signal | Rule | Rationale |
|---|---|---|
| **Metric (always, in-body)** | `yadgar_cache_hit_total{cache="<name>"}` / `..._miss_total{cache}` on every get; `yadgar_cache_size_entries{cache}` + `yadgar_cache_evictions_total{cache}` | Generic family exists (`metrics.py:108`); `cache=` is **bounded** (≤ ~20 names) → I33 cardinality-safe. This alone = total visibility. |
| **Span (decorator, tier-gated)** | cold-tier (project_brief/wiki/prelude — few/session): `tier="stage"`-like per-lookup span. hot-tier (CE/embed/recall per-passage): `tier="hot"` — attribute on enclosing span, **no per-lookup span** | `@observe` present either way → I33 satisfied; overhead differs by tier. See §3c. |
| **Log (tier-gated + refined)** | log on **evict/bust ALWAYS** (rare, structural); log on **miss** only cold-tier or **sampled** on hot-tier | The task's "log on every miss" **floods** a hot low-hit cache. Evict/bust is rare → always safe. |

**This resolves a tension the USER raised, not overrides it.** The task explicitly
sanctioned "sampled spans / metric-only on the hottest path if needed … MEASURE the obs
overhead" and flagged the v5.105 span-flood as the thing to avoid. Tier-gating spans/logs
while keeping the metric always-on is the resolution of *their own* tri-signal-everywhere
vs don't-recreate-the-flood caveat — the non-negotiable (hit/miss visibility) is fully
met on every cache.

The class self-registers via the idempotent `_get_or_create` pattern (`observe.py:67`)
against the shared registry; a new instance needs only its `name`.

### 3c. Hot-path cost & the v5.105 span-flood — MEASURE, don't assume

**The v5.105 span-flood is the load-bearing precedent.** What happened (verified,
`tests/test_log_span_amplification.py` + `.observe-allowlist.json`): `@observe` was
applied to the **log-emission path**; under real OTLP each log record opened a span →
`LogSpanProcessor` emitted a `span_end` **log** → that log re-entered the observed path
→ **per-log amplification** → core + backend crash-loop. CI missed it (runs
`OTLP_ENDPOINT=''` → NonRecording spans). Fix: path-glob **exempt** the whole logging
module. **ADR-0037: CI must never `OTEL_SDK_DISABLED`** (it no-ops span *recording*,
hiding exactly this class of bug).

**Implication for cache lookups (hot paths):**

- A per-lookup **span** on the CE/embed `get()` (called per-passage, potentially
  thousands/recall) is the same cardinality/overhead risk. **The current LRUCache
  correctly carries NO `@observe` on `get`/`put`** (only on snapshot I/O) — this is
  right, not an oversight. The class must default hot-tier instances to **metric-only**.
- The metric increment itself is cheap (an atomic `.inc()`), and LRUCache already keeps
  internal `hits/misses/evictions` int counters (`cache.py:59`) — the class can expose
  the same and let a **sampled/periodic** scrape push them to Prometheus (avoids a hot
  `.labels().inc()` per call if profiling shows it matters; metric-family lookups are
  cheap but the periodic-flush option is the escape hatch).
- **Rule: hot-tier = metric-only (or sampled span); cold-tier = full tri-signal.** Per
  instance, set at construction. This satisfies I33 (the function still has a span
  source at the boundary) without recreating the flood.

### 3d. The two-registry seam & the migration cost (must be stated)

- **"One observability surface" ≠ one literal registry.** Core (`metrics.py._registry`)
  and backend (`embed_service_metrics.py._registry`) are **separate processes with
  separate registries**. Unification = the same **metric-name + `cache=` label
  convention** enforced in both, surfaced on one Grafana board — NOT a shared Python
  object. The doc/dashboards treat them as one *namespace*, not one *registry*.
- **Standardizing is a metric rename** → it breaks any existing dashboard/alert on the
  backend CE/embed families and the legacy embedding counter. **Recommended: dual-emit
  during a transition window** (new generic `{cache=}` family alongside the old names)
  OR **adopt-for-new + opportunistic-rename** (new caches use the generic family day 1;
  rename the 3 embedding families + CE in a scheduled follow-up with dashboard updates).
  Do NOT silently drop the old names.

---

## 4. Unified-abstraction design — the general-purpose class (common shape only)

A single **new** module/class (`Cache`), NOT a retrofit of `LRUCache` (which returns
references and is checkpoint-key-only). What makes it genuinely new: **deep-copy-on-return
+ pluggable invalidation + obs-by-construction.**

```
Cache(
    name: str,                       # the bounded metric label; REQUIRED
    max_entries: int,                # bounded LRU (0 = disabled)
    invalidation: TTL(secs) | Epoch(bus, scope_fn) | CheckpointKey | Manual,
    key_fn: Callable | None,         # pluggable key derivation (default: identity)
    deep_copy: bool = False,         # deep-copy on get (row-dict values); off for floats/vectors
    obs_tier: "hot" | "cold" = "cold",  # tri-signal vs metric-only (§3)
    snapshot: SnapshotSpec | None = None,  # optional msgpack persistence (reuse cache.py I/O)
)
  .get(key) -> deep-copied value | None   # emits hit/miss metric always; span/log per tier
  .put(key, value) -> None                # emits size gauge + evict counter/log
  .invalidate(scope) / .clear()           # manual bust; emits bust log
```

**Per-instance policy (how the common-shape caches express themselves):**

| Consumer | max_entries | invalidation | deep_copy | obs_tier | snapshot |
|---|---|---|---|---|---|
| CE / embed (A) | 100k | CheckpointKey | no (floats/vectors) | hot (metric-only) | msgpack |
| recall-output (E) | bounded | Epoch(bus, dir-scope) + TTL backstop | **yes** (row dicts) | hot (metric-only) | no |
| project_brief (E-like) | small | Epoch(dir) + TTL | **yes** | cold (tri-signal) | no |
| wiki_read / prelude (F) | small | TTL or slug/pattern-epoch | yes | cold | no |
| stale-count / DBSIZE (F/G) | small / 1 | TTL | no (int/dict) | cold | no |

**The named-instance registry + central invalidation bus:**

- **Registry:** a module-level `dict[str, Cache]` — every instance registers by `name`.
  Gives one place to enumerate caches (for a `/cache-stats` panel), one obs surface, and
  one place a config knob can size/disable a named cache.
- **Invalidation bus = the EXISTING epoch bus** (`_recall_shadow.bump_epoch(dir|None)`,
  `_recall_shadow.py`). It already has the right API and **2 production callers**:
  `_memorize_phases/_phase_post_write.py:58` (per-dir on memorize) and
  `consolidation/cls.py:25` (global on prior recompute). An `Epoch`-invalidation instance
  subscribes to this bus (embeds `_current_epoch(dir)` in its key). **Do NOT build a new
  bus** — promote the shadow module's bus to the shared primitive. This is why E-shape
  caches (recall-output, project_brief) unify cleanly: they already share one bus.

### 4a. Where it leaks (what resists the common shape — stays bespoke)

Honest enumeration (from §2a), each with the retrofit it *does* get (obs metric) vs what
it keeps (mechanics):

- **B branch/default-branch** — TTL is encoded in the **key** (`lru_cache` decay is the
  invalidation moment). A get/put-TTL class changes *when* invalidation fires. Low value,
  works today. **Stays bespoke; keeps its generic `{cache=}` metric (already has it).**
- **C config singletons** — a config loader with a `clear()` hook, not a data cache.
  Wrapping it adds nothing. **Stays bespoke; add a metric only if a hit-rate question
  ever arises (unlikely).**
- **D rules dict** — unbounded whole-clear is **intentionally correct** for small N;
  a sized LRU would silently serve stale for un-mutated dirs. **Stays bespoke; obs metric
  optional.**
- **H rate-limiter** — mutable rate state that must never be evicted mid-window.
  **Category error to call it a cache. Excluded entirely.**
- **J graph-layout** — lives in SurrealDB, survives restarts, signature-mismatch
  semantics. An in-process class cannot hold it without a cache-over-cache layer (a
  second invalidation point). **Stays bespoke.**
- **I predictive-coding** — borderline; expressible as TTL + manual-bust. Migrate only
  if convenient; not a priority.

**This is the honest answer to "where does it leak": 5 caches (B/C/D/H/J) resist the
common shape and stay bespoke; they still get the obs metric in Car 0. The class serves
A/E/F/G (+ new consumers) — the shapes that genuinely share a shape.**

---

## 5. Recommended caching-train order (Car 0 unified obs first)

Extract-then-generalize. Obs unification is independent and delivers the non-negotiable
regardless of whether the storage class pans out — so it leads.

- **Car 0 — Observability unification (all caches; pipeline-independent; delivers the
  non-negotiable FIRST).** Shared `record_cache_hit/miss(name)` + size/evict helpers on
  the generic `yadgar_cache_{hit,miss}_total{cache}` family. Retrofit the ~14 silent
  caches. **Collapse the 3 embedding families** (dual-emit or scheduled rename +
  dashboard update) and **move CE onto the generic family** (or dual-emit). One Grafana
  board across both registries (namespace, not registry). **No storage change — pure
  obs.** Independent of everything.
- **Car 1 — the general-purpose `Cache` class + project_brief as its first consumer**
  (co-designed — do NOT ship the class speculatively; validate it by building
  project_brief on it). Folds in track-a hook hot-context (shared value shape + the
  same epoch bus). Key `(dir, branch_bucket, mode)`; Epoch(dir) + short TTL; deep-copy;
  cold-tier tri-signal. Gate: correctness test (a memorize reflects within one
  epoch/TTL) + staleness test. No ranking gate needed (nudge data).
- **Car 2 — wiki_read / dispatch_prelude as `Cache` instances.** Small, safe, cheap.
  TTL or slug/pattern-epoch. Optional / low priority.
- **Car 3 — recall-output cache (lever a) as a `Cache` instance. GATED, LAST.** Gating
  UNCHANGED from the prior docs: build only after (i) the recall forward-only + Ettin
  overhaul settles (place the wrapper at the *new* entry point) AND (ii) the shadow
  tool-lane hit-rate justifies it (~0 so far). Epoch(dir) + TTL backstop + fire-and-forget
  heat-boost that must NOT bump the epoch.
- **Optional tail — migrate CE/embed LRU onto the `Cache` class** for obs-consistency
  only (they work mechanically; deep_copy=off). Do NOT block anything on this.

**Slotting vs the recall trains (unchanged):** Car 0 and Car 1/2 are **pipeline-independent**
(obs, project_brief, wiki, prelude are not the recall dispatch) → ship in parallel with /
before the recall forward-only / reorg / Ettin / restructure trains, no collision. Car 3
is **downstream** of the recall overhaul and waits for it.

---

## 6. Risks

1. **Fits-none via over-generalization (THE trap).** If Car 0 ships a class abstracted
   from all 10 existing caches, it grows the B/C/D/H/J seams and fits none. **Mitigation:
   Car 0 is obs-only (no class); the class (Car 1) is designed from the NEW consumers and
   validated by building project_brief on it — extract, don't speculate.**
2. **Metric rename breaks dashboards/alerts.** Standardizing the CE/embed/legacy families
   onto the generic `{cache=}` family orphans existing Grafana/alert queries. **Mitigation:
   dual-emit during a transition window OR adopt-for-new + scheduled rename with dashboard
   PR; never silent-drop.**
3. **v5.105 span-flood recurrence.** A per-lookup span/log on a hot cache `get` can
   re-trigger the amplification (esp. if it logs on miss). **Mitigation: hot-tier =
   metric-only (or sampled span), log on evict/bust only; CI must keep OTLP live per
   ADR-0037 so a flood is caught, not masked.**
4. **Deep-copy discipline / cost.** Row-dict caches (recall-output, project_brief) MUST
   deep-copy on return (callers mutate `m["heat"]` in place) — but deep-copy on a hot
   path has a cost. **Mitigation: deep_copy is per-instance opt-in; off for
   float/vector values (CE/embed), on for row dicts; measure the copy cost on the
   recall-output path before Car 3.**
5. **Heat-drift self-destruct (E-shape caches).** Epoch must bump on STRUCTURAL writes
   only (memorize/forget/consolidation), never on heat/decay — else the cache
   invalidates its own dir every access (~0% hit-rate). Heat freshness rides the TTL
   backstop. (Same rule as the prior docs; the shared bus already does exactly this.)
6. **Two-registry drift.** Core and backend enforcing the naming convention separately
   can drift. **Mitigation: a lint/test asserting the `cache=` label + metric-name shape
   in both modules; the convention is the contract, not a shared object.**

---

## 7. Advisor input (both passes)

**Pass 1 (after inventory + obs ground-truth, before drafting — the unified-vs-bespoke
pressure-test):**

- **SPLIT the two asks — they have different answers.** Obs unification (b) = unqualified
  YES and *the real find*; storage unification (a) = NO as "one to rule all," YES rescoped
  to "one class for the common shape." Conflating them is the trap. Folded into the TL;DR
  + the whole doc structure.
- **The 10-combo diversity map IS the fits-none proof** — lead the leak analysis with it;
  name B/C/D/H/J as bespoke exemptions explicitly. Folded into §2a/§4a.
- **Two-registry seam** — "one obs surface" is a naming *convention* across two processes,
  not one registry. Say it. Folded into §3d.
- **Metric-rename cost** — standardizing breaks backend dashboards; dual-emit or scheduled
  rename. Folded into §3d/Risk 2.
- **Restate the non-negotiable precisely** — metric always (that's total visibility);
  spans/logs tier-gated to survive v5.105; refine "log on miss" (floods) to "log on
  evict/bust always, miss cold/sampled." Folded into §3b.
- **Deep-copy is why the class is genuinely NEW** (LRUCache returns references — wrong for
  row dicts). Folded into §4 opening + Risk 4.
- **Extract-then-generalize, not generalize-then-hope** — build the class *from* the new
  consumers, validate via project_brief in the same car; don't ship a speculative Car 0
  class. Folded into §5 Car 1 + Risk 1.

**Pass 2 (before finalizing):**

- **Split framing confirmed sound** — obs=YES, storage=rescoped-YES; leak enumeration
  (B/C/D/H/J), inventory corrections (CE off the generic family, 3 embedding families),
  and extract-then-generalize train order all validated. No re-plan.
- **BLOCKER caught in the central "observable by construction" claim.** The draft's §3b
  framed hot-tier as "NO per-lookup span," which reads as "`get()` has no span source →
  fails I33 MISSING=0." Fixed: **`@observe(tier="hot")` IS a span source** (verified —
  `observe` ∈ `_SPAN_DECORATORS`, `check_observe_coverage.py:38`; I33 is a static AST
  decorator-presence check), so it passes I33 with zero flood risk. The precise design is
  **two orthogonal mechanisms**: the decorator (tier-gated) gives I33 compliance; the
  in-body metric (every get, all tiers) gives total visibility. Rewrote §3b around this.
- **Core-vs-backend I33-scope discriminator surfaced + verified.** The backend `LRUCache`
  gets away with no `@observe` on `get`/`put` (own coverage classification); the new
  class's consumers are **core-side, under I33 scope** → decorating is mandatory, which
  is *why* `tier="hot"` (not "no decorator") is the design. Stated in §3b.
- **Framed the tier-gating as resolving the user's OWN caveat**, not watering down the
  non-negotiable — the task sanctioned "sampled/metric-only on the hottest path … MEASURE"
  and named v5.105 as the hazard. Folded into §3b.
- **Completion order confirmed:** fill this pass-2 section → commit to master (bot
  identity, docs workflow sanctioned) → then the user report with the resulting sha.

---

## 8. Open questions for the user

1. **Metric-rename strategy for the 3 embedding families + CE:** dual-emit transition
   window, or adopt-for-new + scheduled rename with a dashboard PR? (Recommended:
   dual-emit — zero dashboard breakage, delete the old names on a later tick.)
2. **Migrate CE/embed LRU onto the new `Cache` class** (obs-consistency, they work
   mechanically) or leave them on `LRUCache`? (Recommended: optional tail, not blocking.)
3. **project_brief TTL length** — 60s (fresher) vs 300s (higher hit-rate). Your
   multi-agent spawn cadence sets it. (Carried from the prior doc.)
4. **Is `hot`-tier metric-only acceptable for CE/embed/recall-output** (no per-lookup
   span), or do you want sampled spans there? (Recommended: metric-only; sampled span as
   a later opt-in if a trace-gap shows up.)
5. **Lever (a) gating unchanged** — OK to keep Car 3 gated on the recall overhaul +
   shadow tool-lane data? (Recommended: yes.)

---

## 9. Key file references

| Concern | File:line |
|---|---|
| `@observe` decorator (tiers, exempt) | `yadgar/observability/observe.py:228` |
| I33 invariant (MISSING=0 hard-fail) | `docs/ARCHITECTURE_INVARIANTS.md:456` |
| I33 lint enforcement | `scripts/check_observe_coverage.py` |
| generic `yadgar_cache_{hit,miss}_total{cache}` | `yadgar/metrics.py:108` |
| CE cache bespoke metric (NOT generic) | `yadgar/backend/embed_service_metrics.py:199`; incr `embed_service.py:648` |
| 3 embedding metric families | `metrics.py:77`; `embed_service_metrics.py:229`; `metrics.py:108` (`embeddings.py:308-309`) |
| shadow recall + epoch bus | `yadgar/server/tools/_recall_shadow.py:76-178` |
| epoch-bus callers | `_memorize_phases/_phase_post_write.py:58`; `consolidation/cls.py:25` |
| LRUCache (class, no name attr, snapshot-only obs) | `yadgar/backend/cache.py:43` |
| v5.105 span-flood test + fix | `yadgar/tests/test_log_span_amplification.py`; `.observe-allowlist.json`; ADR-0037 |
| branch / default-branch (bespoke, generic metric) | `server/tools/project.py:115/166` |
| config singletons + clear | `config.py:964`; `config_registry.py:75,98` |
| rules dict | `rules_engine.py:244,308,442` |
| graph-layout (DB-backed leak) | `storage/ops.py:97` |
| lever-a full design | `docs/plans/cache-refactor-2026-07-01.md` |
| caching opportunities (20-cache inventory) | `docs/plans/caching-opportunities-2026-07-05.md` |

---

## Re-evaluation: single namespaced cache (2026-07-05, user-challenged)

**Status:** RE-EVALUATION of §2–§4's "one storage cache = NO" conclusion. The user
challenged it: *"one cache with NAMESPACES (per-namespace policy) to prevent leaks,
serving everything — fewer moving parts. I doubt the 'no'."* Re-examined honestly,
code-verified (2026-07-05, verification agent + main-thread reasoning). **The honest
delta is a WIDENING + partial vindication of the prior scoping, not a reversal** (see R1
for why the "flip" framing would be confirmation bias). This section supersedes §2a's
"fits-none" framing and §4a's "5 leaks" list; §3 (observability) and §5 (train order)
stand unchanged.

### R1. TL;DR — the honest re-verdict (a widening, NOT a reversal)

**First, an honest read of what the prior doc actually said** — because the naive
telling ("the NO is reversed") would be exactly the confirmation-biased answer the user
wants to hear, and it does not survive opening the prior doc:

- The prior **§4 is titled "the general-purpose *class*"** and **§5 Car 1 builds it**. The
  prior doc **already endorsed one class** with a policy *union* (`TTL | Epoch |
  CheckpointKey | Manual`) bound at construction. Its "NO" was **scoping** — *which* caches
  the class covers — never "one class is wrong."
- Prior §4a **already excluded C** (config, "not a data cache") **and H** (rate-limiter,
  "category error… excluded entirely") — the same two this re-eval calls non-caches.
- J (graph-layout) stays out in **both** docs.

**So the real delta is narrow and precise:** **B (branch) and D (rules) move from
bespoke-leak into the class** — the prior doc **over-classified** them (R4), and the user
was **right to doubt** those two leaks. Plus the class-vs-manager sharpening (R2). That is
a **widening of the class's scope + a partial vindication of the user**, not a "flip."

**The re-verdict, stated honestly:** **YES — one cache *class* with N named instances
serves every in-memory cache.** The prior doc's qualified-YES-to-one-class was right; its
"5 leaks" over-counted by 2. Corrected:

- The "10 (eviction × invalidation) combos" that drove the "fits-none" framing **collapse
  to ~3 invalidation mechanisms** on honest inspection (R3) — the code-grounded reason
  B/D were mis-scored as leaks.
- The 5 "bespoke leaks" (§4a) re-bucket to **2 non-caches (C, H — already excluded) + 2
  expressible (B, D — the actual correction) + 1 different-tier (J)** (R4). Only J stays
  out, because it lives in a **different storage tier** (SurrealDB, out-of-process), not
  because a policy can't express it.
- The strongest potential pushback — **hot-path overhead** on CE/embed — is **immaterial**
  under the correct design (R5). It would only bite the *wrong* design (a per-call
  `manager.get(namespace, key)` dispatch), which we do NOT recommend.

**This delivers the user's explicit goal — fewer moving parts — for real:** one
implementation to test/maintain/harden, N thin policy configs, isolated instances.

### R2. The distinction the prior doc blurred: one CLASS vs one MANAGER

"One namespaced cache serving everything" is **two different designs**, and the prior
doc's pushback silently answered the worse one:

1. **One CLASS, N instances** — `Cache(name="ce", policy=…)`, `Cache(name="project_brief",
   policy=…)`. Policy bound at **construction**. `get(key)` on an instance is
   `self._store[key]` behind one method — *identical hot path to today's `LRUCache.get`*.
   This is the prior doc's own §4 recommendation for the common shape.
2. **One MANAGER, single `get(namespace, key)` entry** — namespaces live inside one object,
   policy **dispatched per call**. A per-`get` namespace lookup + policy branch.

**The hot-path-overhead and shared-blast-radius objections belong to design (2), not (1).**
The user's stated goal (fewer moving parts, one well-tested impl, isolated policies) is
delivered by **(1)**. Design (2) adds a dispatch layer that buys nothing functional and
concentrates exactly those two costs. **Recommendation: one CLASS, N instances.** If a
registry is wanted (for `/cache-stats` enumeration + one config surface), it is a *thin
registration map* — `registry[name] = instance` at construction — so a lookup is still
`registry[name].get(key)` (one dict get, policy already bound), never a per-`get` policy
branch. That registry is the prior doc's §4 "named-instance registry" and is compatible.

### R3. The "10 combos" collapse on the invalidation axis (code-verified)

The prior §2a listed 10 unique (eviction × invalidation) pairs and read that as
fits-none. But the pairs are not 10 *policies* — on the invalidation axis they collapse
to **~3 mechanisms**, verified against the cited mechanics:

| Real invalidation mechanism | Caches | How it works | The "policy" is really… |
|---|---|---|---|
| **1. None — freshness in `key_fn`** | CE, embed (ckpt-in-key); branch, default-branch (time-bucket-in-key); shadow, recall-output, project_brief (epoch-in-key) | New key ⇒ miss ⇒ recompute; stale keys age out via LRU. Nothing is ever *invalidated* — the key simply changes. | **one policy + a pluggable `key_fn`** that embeds ckpt-hash / time-bucket / epoch-counter. A/B/E are the SAME mechanism. **Caveat:** E's `key_fn` reads `_current_epoch(dir)`, which requires the epoch bus to be bumped externally (`_phase_post_write.py:58`, `cls.py:25`) — external plumbing A/B don't need. The *cache-side* mechanism is identical; the epoch bus stays a shared moving part. |
| **2. TTL-since-write** | stale-count, DBSIZE, predictive-coding | value stored with a write-timestamp; expired on read | one policy: `TTL(secs)` |
| **3. Manual / whole-flush** | config\*, rules | explicit `clear()` on a mutation event | one policy: `Manual`/`WholeFlush` |

\* config is memoization, not a cache — see R4.

This is the **code-grounded refutation** of "10 combos → fits-none": there are ~3
invalidation mechanisms and one eviction primitive (bounded LRU, `max_entries=0` ⇒
unbounded). Verified:

- **A/B/E are one mechanism.** CE key = `qsha:tsha:ckpt` (`embed_service.py:637`); branch
  key = `(dir, int((time+hash(dir)%30)//30))` (`project.py:150`); epoch key embeds
  `_current_epoch(dir)` (`_recall_shadow.py:111`). All three: *freshness lives in the key,
  invalidation is "the key moved."* The prior doc scored B a "LEAK" because it imagined a
  **get/put-TTL** class (which *would* change semantics) — but the correct expression is a
  **`key_fn` that appends the time bucket**, which is *exactly what the code already does*.
  B is not a leak; it is combo-A with a different `key_fn`. (Verified `project.py:115-150`.)
- **D (rules) is whole-flush.** `self._applicable_rules_cache: dict[str,list] = {}`
  (`rules_engine.py:244`), `.clear()` on add/delete (`:308,:442`). A `WholeFlush`/`Manual`
  namespace with `max_entries=0` (unbounded) + `.clear()`-on-bust expresses this **exactly**.
  The prior doc's "sized-LRU would serve stale for un-mutated dirs" objection is a
  strawman — the namespace is unbounded + manual-clear, identical semantics.

### R4. The 5 "leaks" re-classified honestly (3 buckets)

| Prior "leak" | Bucket | Verdict |
|---|---|---|
| **C config singletons** (`config.py:964`, `config_registry.py:75`) | **NOT-A-CACHE** | `@lru_cache(maxsize=1)` memoized loader + `clear_config_caches()` hook. No key→value, no eviction, no hit/miss. It is a **singleton reload**, not a cache. Correctly excluded — **not a strike against unifying the real caches.** |
| **H rate-limiter** (`rate_limit.py:32`) | **NOT-A-CACHE** | Stored tuple `(tokens, last_time)` is **mutated in place** every access (`rate_limit.py:51,55` — refill + decrement written back). Mutable rate state, not a key→value cache. Category error to evict mid-window. Correctly excluded. |
| **B branch / default-branch** (`project.py:115/166`) | **EXPRESSIBLE** | Combo-1 (freshness-in-`key_fn`). A `key_fn` embedding the `int(time//bucket)` term expresses it exactly (R3). Prior doc **over-classified** it as a leak by assuming a get/put-TTL class. |
| **D rules dict** (`rules_engine.py:244`) | **EXPRESSIBLE** | Whole-flush namespace (unbounded + `.clear()`-on-bust). Prior doc **over-classified**. |
| **J graph-layout** (`storage/ops.py:102-130`) | **GENUINELY RESISTS (different tier)** | `UPSERT graph_layout_cache:current …` — a **SurrealDB record**, out-of-process, restart-surviving. Not a policy the in-memory class can't express; a **different storage tier**. |

**Net: prior doc's "5 bespoke leaks" → really 2 non-caches (C, H) + 2 expressible
(B, D) + 1 different-tier (J).** The 2 non-caches don't count against unification (they
aren't caches). B and D fold into the class. Only J stays out.

**On J — exclude it, do NOT add a pluggable DB backend.** A DB-backed storage tier on the
cache class is **scope creep that ADDS moving parts** — the exact opposite of the user's
goal. graph-layout is a persisted, signature-validated singleton record with its own
recompute semantics; wrapping it in an in-memory cache class buys nothing and adds a
second invalidation point. Clean boundary: **the class serves in-memory caches; graph-layout
is a DB record and stays a DB record.** That is a scoping decision, not a unification failure.

### R5. Hot-path overhead — the key pushback, bounded and dismissed

**Ground truth (verified `embed_service.py:618-671`):** CE `get` is called **per-passage
in a loop** — `_score_ce_with_cache`: `for i, key in enumerate(keys): hit = _ce_cache.get(key)`
(`:644-645`) — thousands of gets per recall. This IS the latency-critical hot path, and it
was the strongest candidate for a SURE "no."

**It is not a blocker, because the overhead lives in the design we don't recommend:**

- Today: `_ce_cache.get(key)` on a **bare instance** → `self._store[key]` under a lock
  (`cache.py:65-77`). Under **one-class-N-instances**, CE is a `Cache` instance; its `get`
  is the *same* `self._store[key]` behind one method. **Zero added indirection vs today** —
  policy (checkpoint-in-key, deep_copy=off, obs_tier=hot) is bound at construction, not
  branched per call.
- The overhead the pushback imagines (namespace dict lookup + per-`get` policy branch +
  `.labels(cache=).inc()` per call) is the **manager** design (R2, design 2). We reject it.
- Even the per-`get` metric label-inc is avoidable: `LRUCache` already keeps internal
  `hits/misses/evictions` ints (`cache.py:59-61`); the class exposes the same and a
  **periodic scrape** flushes them to Prometheus (§3c escape hatch) — no hot `.labels().inc()`.
  **Reconciles with R8's "metric fires on every get":** the internal int counter *is*
  incremented per-get (cheap, always current) — visibility is total; only the Prometheus
  **label-inc** is deferred to the periodic scrape on hot-tier instances. Cold-tier
  instances label-inc inline (they're rare). Total visibility, hot-path label-inc avoided.
- deep_copy is **per-instance opt-in, OFF for CE/embed** (floats/vectors) — the verification
  agent's "get returns a reference, deep-copy breaks it" is a **config**, not a barrier: CE
  sets `deep_copy=False` and gets today's reference semantics; row-dict caches set
  `deep_copy=True`.

**Bound:** a CE hit avoids a cross-encoder forward pass (**milliseconds**). The class's
marginal cost over today's bare `LRUCache` is **~zero** (same method shape). The overhead
is immaterial. **Hot-path overhead is OFF the blocker list.** (This was the one reason that
could have been SURE; verified it isn't, under the recommended design.)

### R6. Shared blast-radius & god-object — honest weighing

- **Shared blast-radius** (one impl serving CE+embed+recall+project_brief → a core bug
  breaks all): real, but **weighed against the benefit it's smaller.** One well-tested impl
  = fewer *total* bugs than N bespoke impls each with its own edge cases (the current state:
  3 embedding metric families, CE off the generic family, ~14 silent caches — that
  fragmentation is itself the bug surface). **Mitigation is already the build order:** CE/embed
  stay on their lean isolated `LRUCache` path until the class is proven on the cold-path new
  consumers (extract-then-generalize, §5) — so a class bug **cannot** touch the recall hot
  path during rollout. Migrating CE/embed onto the class stays the **optional tail** (obs
  consistency only), never a blocker. Net: blast-radius is real but bounded by build order,
  and the "N isolated impls" alternative has a *larger* aggregate bug surface today.
- **God-object config complexity** ("one thing supporting the UNION of all policies"): the
  honest test is whether the complexity is *reduced* or merely *relocated*. Here it is
  **reduced**, because the union is small (R3: ~3 invalidation mechanisms + 1 eviction
  primitive + 2 booleans deep_copy/obs_tier + a `key_fn`), and each namespace picks a point
  in it declaratively at construction. That is genuinely **"N simple configs on 1 tested
  impl" < "N bespoke impls."** The misconfiguration risk (a namespace picking the wrong
  policy) is real but *localized and testable* (one config line per cache, unit-testable),
  versus today's risk which is *distributed* across N hand-rolled impls. **Verdict: fewer
  moving parts, delivered — not faked** — *provided* the design is one-class-N-instances
  (R2). The god-object risk materializes only if it were one manager with per-call dispatch.

### R7. Verdict + namespace-policy interface sketch

**Does ONE namespaced cache serve everything? YES** — one `Cache` class, N instances,
policy bound at construction — for all **8 real in-memory caches**. **2 non-caches**
(config, rate-limit) are correctly excluded (they aren't caches). **1 cache**
(graph-layout) stays out as a **different storage tier** (SurrealDB), by scoping choice,
not policy failure. No SURE reason blocks in-memory unification.

```
class Cache:                              # ONE class; N instances; policy at construction
    name: str                             # bounded metric label; REQUIRED
    max_entries: int                      # bounded LRU (0 = unbounded)
    invalidation: KeyFn | TTL(secs) | Manual   # the ~3 mechanisms (R3); default KeyFn
    key_fn: Callable = identity           # embeds ckpt-hash | time-bucket | epoch (combo-1)
    deep_copy: bool = False               # off for floats/vectors; on for row dicts
    obs_tier: "hot" | "cold" = "cold"     # metric-only vs tri-signal (§3)
    snapshot: SnapshotSpec | None = None  # optional msgpack (reuse cache.py I/O)

  get(key)  -> value | None   # self._store[key] behind one method; metric always; span/log per tier
  put(key, value)            # size gauge + evict counter
  clear() / invalidate(scope)  # Manual/WholeFlush bust

# thin registry (enumeration + config surface only) — NOT a per-get dispatcher:
_REGISTRY: dict[str, Cache] = {}          # registry[name].get(key) — one dict lookup, policy pre-bound
```

**How each real cache maps to an instance:**

| Cache | max_entries | invalidation / key_fn | deep_copy | obs_tier | snapshot |
|---|---|---|---|---|---|
| CE (A) | 100k | `key_fn = qsha:tsha:ckpt` | no | hot | msgpack |
| embed (A) | 100k | `key_fn = tsha:mode:ckpt` | no | hot | msgpack |
| branch / default-branch (B) | 128 | `key_fn = (dir, time//bucket)` | no | cold | no |
| shadow / recall-output / project_brief (E) | bounded | `key_fn` embeds `_current_epoch(dir)` + TTL backstop | yes (row dicts) | hot / cold | no |
| stale-count / DBSIZE / predictive (F/G/I) | small / 1 | `TTL(secs)` | no | cold | no |
| rules (D) | 0 (unbounded) | `Manual` + `.clear()`-on-mutation | no | cold | no |

**Handled outside the class:** config singleton + rate-limiter (**not caches** — stay as-is,
get an obs metric in Car 0 only if a hit-rate question ever arises); graph-layout
(**SurrealDB tier** — stays a DB record, obs metric in Car 0).

### R8. Observability + train order — re-confirmed under one-class-N-instances

**Observability (§3) is unchanged and, if anything, cleaner.** Obs-by-construction still
holds: `@observe(tier=obs_tier)` on the class methods satisfies I33 (span source present);
the in-body `record_cache_hit/miss(self.name)` metric fires on every get (total visibility);
hot-tier = metric-only to avoid the v5.105 span-flood. One class ⇒ **one** place the tri-signal
is wired ⇒ every instance is observable from its `name` alone. (No change to §3.)

**Train order (§5) holds exactly:**

- **Car 0 — observability unification** (all caches; pipeline-independent). Unchanged — it is
  obs-only and leads regardless.
- **Car 1 — the `Cache` class + project_brief as first instance** (extract-then-generalize:
  build the class *from* the cold-path new consumer, validate, don't speculate). This is
  where "one class" is born; project_brief is instance #1.
- **Car 2 — wiki_read / dispatch_prelude as `Cache` instances.** Adding instances, not
  rebuilding.
- **Car 3 — recall-output (lever a) as a `Cache` instance. GATED, LAST** (recall overhaul +
  shadow tool-lane data). Unchanged.
- **Optional tail — migrate CE/embed onto the class** for obs-consistency only. Doubles as the
  blast-radius mitigation: CE/embed stay isolated on `LRUCache` until the class is proven.

"One class, N instances" builds the class once (Car 1) then adds instances (Cars 2–3) — the
order is **identical** to the prior doc. The re-verdict changes the *framing* (it's a genuine
YES, not a rescoped one) and removes the "5 leaks" caveat down to one scoped exclusion; it
does **not** change the build sequence.

### R9. Advisor input (re-evaluation)

**Pass 1 (after steelmanning + drafting the reframe, before finalizing):**

- **Lead with the invalidation-axis COLLAPSE, not config enumeration.** The sharp argument is
  that A/B/E are the *same* mechanism (invalidation=none, freshness in `key_fn`) — ~3
  mechanisms, not 10 combos. More convincing and code-verifiable. Folded into R3.
- **Draw the one-CLASS-N-instances vs one-MANAGER distinction explicitly** — the hot-path and
  blast-radius objections belong to the manager design; the user's fewer-moving-parts goal is
  delivered by one-class-N-instances. The prior doc's pushback silently answered the worse
  design. Folded into R2 + R5.
- **Hot-path overhead is not a SURE reason** under one-class-N-instances (CE `get` stays
  `self._store[key]`, same as today); verify item 8 doesn't force a per-call branch (it
  doesn't). Move it off the blocker list. Folded into R5.
- **J = different storage tier, EXCLUDE (don't add a pluggable DB backend — scope creep that
  adds moving parts).** 5 leaks → 2 non-caches + 1 different-tier. Folded into R4.
- **Confirmation-bias check (don't over-rotate the other way):** keep extract-then-generalize;
  CE/embed migration stays the optional tail, which doubles as blast-radius mitigation. Folded
  into R6/R8.

**Pass 2 (before finalizing — bias check both directions):**

- **Under-rotation direction: clean, don't flip back.** Verdict (YES, one class / N
  instances) is right and code-verified. B-via-`key_fn` and D-via-whole-flush hold; the
  hot-path dismissal is sound (`LRUCache.get` already bumps internal hit/miss ints
  `cache.py:59-61`, so a hot-tier `Cache.get` doing the same + periodic scrape equals
  today); J-as-different-tier is the right exclusion. Kept all of it.
- **Over-rotation direction — the real bias catch: R1's "the NO does not survive, verdict
  flips to YES" OVERSTATED the delta in exactly the direction the user wanted to hear.**
  The prior doc's §4/§5 **already endorsed one class**; its "NO" was scoping (which caches
  the class covers), and it **already excluded C and H**. The true delta is only that **B
  and D fold in** (prior over-classified them) + the class-vs-manager sharpening — a
  **widening + partial vindication**, not a reversal. A skeptical reader who opens the
  prior doc sees it already recommended one class and the "flip" evaporates. **Fixed: R1
  rewritten to lead with the honest read of the prior doc and frame the delta as a
  widening**; the user still gets their win (they were right to doubt the B/D leaks) and it
  is defensible under scrutiny.
- **R3 clause added:** E's `key_fn` reads `_current_epoch(dir)`, needing the epoch bus
  bumped externally — external plumbing A/B don't need. Cache-side mechanism identical; the
  epoch bus stays a shared moving part. Don't let "invalidation=none" read as "zero extra
  plumbing for E."
- **R5/R8 tension resolved:** internal int counter incremented per-get (cheap, always
  current) + periodic Prometheus scrape on hot-tier → total visibility, hot-path label-inc
  avoided. One reconciling clause added to R5.
- **Completion order:** fill this pass-2 section → commit to master (bot identity, docs
  workflow sanctioned) → user report leading with the honest delta ("B and D fold in; prior
  over-classified them; only 2 non-caches + 1 DB-tier stay out"), not "the NO is reversed."
