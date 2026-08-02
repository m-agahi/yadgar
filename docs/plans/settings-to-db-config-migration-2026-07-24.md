# Settings → DB runtime_config migration — 2026-07-24

**Task:** #35 (Survey config.yaml/Settings for purely-runtime flags migratable to the DB config store).
**Status:** DESIGN — inventory landed (2026-07-24); foundation corrected and phases re-sequenced 2026-08-02.
Three items block Phase 1 and are listed in "Blocking before any seed" below. Not yet buildable.
**Builds on:** [[yadgar-adr-0163]] (DB-backed runtime_config store — dir-scoped, typed, PTC-cached, warmup, invalidation).
**Binding sibling doc:** `docs/plans/split-store-engine-decision-2026-08-02.md` — engine #2 is **MariaDB** (its §4.5,
user-decided 2026-08-02). Its §7 row for this plan is the source of the corrections in §0 below. Where the two
disagree, the decision doc wins; this plan cross-references it rather than restating it.

## Vision
Yesterday's `runtime_config` store was introduced for one flag (`code_graph.enabled`). But it is a *general* live-config substrate: DB-backed, dir-scoped (per-dir → global → default), typed JSON, core-cache pull-through, warmup at daemon start, invalidation on write, reachable from BOTH core and backend. The `Settings` model has **344 annotated fields**; most are behavioral tuning that is restart-required only *incidentally* (pydantic `Settings` loads once at boot), not fundamentally. Migrating the runtime-tunable subset makes them **live, dir-scoped, restart-free** — far more ergonomic to operate + dogfood.

## Goals
- **Primary:** make the runtime-tunable knob subset LIVE (DB-backed, dir-scoped, restart-free).
- **KEY GOAL (user, 2026-07-24):** SEED each migrated knob's current value into `runtime_config` (via the `config_set` MCP tool — NEVER raw SQL/DB inserts, per the standing data-repair-through-MCP rule) and then **REMOVE it from `config.yaml`**. End state: `config.yaml` shrinks to BOOT-ONLY knobs; the migrated knobs live as `Settings`-code-default + DB-override only. Much simpler operational surface — one place (DB) to tune live, `config.yaml` only for what truly needs a restart.

## Non-goals
- NOT migrating structural/boot knobs (ports, URLs, DB/embed connection, secrets, model selection, container identity, pool sizes that fork at boot) — these STAY in `config.yaml`/env.
- NOT removing the `Settings` FIELD (config.py) — it stays as the authoritative CODE DEFAULT. See "End state, walked through one knob" below for exactly what that means at each resolution stage.
- NOT a big-bang. Phased, highest-value-first, invariant-preserving.

---

## §0 — Corrections to this plan's foundation (2026-08-02)

Three things the 2026-07-24 draft got wrong or assumed. Each changes the phase ordering, so they are stated
before the inventory rather than as footnotes.

### 0.1 The store is EMPTY — there is nothing to migrate, and that is a deadline

`config_list()` → `[]`. `db_inspect` over `runtime_config` → **0 rows**. Even `code_graph.enabled`, the flag
the store was built for, has no row — it resolves to its caller-supplied default on every read
(`_runtime_config.py:112-115`).

Three consequences the draft did not draw:

1. **Migrating the table is a schema move with no data to move.** Every "migrate the existing rows" concern
   is void. This is why the split-store decision doc (§1.4, §7) names `runtime_config` the lowest-risk
   possible pilot for engine #2 — see §G below.
2. **Phase ordering that assumed existing rows must be re-derived.** The draft sequenced Phase 0.5
   (backend cache) as a de-risking prerequisite for reads that do not exist yet. Re-sequenced below.
3. **The re-key window is open and this plan closes it.** Task **0095** (project identity key scheme) notes
   the re-key is free *only while `runtime_config` is empty*. The precise gate is **the first `config_set`
   seed**, not the schema — creating a MariaDB `runtime_config` table with zero rows leaves the window open.
   So: **0095 is a blocking prerequisite of the first seed in Phase 1**, and the schema-only pilot in
   Phase 0.9 may proceed while 0095 is still open. Listed in "Blocking before any seed".

### 0.2 The "N HTTP round-trips" framing was wrong — corrected, and the real gap is narrower

The draft's load-bearing caveat #2 read: *"Backend `config_get` = per-call HTTP round-trip …
NO backend-local cache … ⇒ decay batch of N rows = N HTTP round-trips → fatal."* It reads as a description
of a present defect. **It is not one.** Verified 2026-08-02:

- **`heat_decay.py` hoists its config outside its loops.** `_decay_memories` reads `COLD_THRESHOLD`,
  `ACTION_STREAM_COLD_THRESHOLD` and `RECALL_BOOST` at `heat_decay.py:119-122`, *before* the row loop opens
  at `:129`. `_decay_entities` reads `DECAY_FACTOR` and `COLD_THRESHOLD` at `:178-179`, before its loop at
  `:182`. Config reads per decay cycle at this level: **five, not 5·N**.
- **`runtime_config_client` has zero backend importers.** It lives at `yadgar/core/runtime_config_client.py`
  and is explicitly the **host-side** fail-open client for hook scripts and the host CLI (its own module
  docstring, lines 1-17). Every non-test importer is under `yadgar/core/install/` or `yadgar/core/code_graph/`.
  No backend code reads config over HTTP, because no backend code reads the store at all yet.

**What survives the correction, restated as a hazard of the proposed end-state.** The plan proposes swapping
`self._settings.X` for `config_get`. If that swap is applied naively at the *actual* per-row read site, the
hazard is real:

- `heat_decay.py:147` calls `self._thermo.compute_decay(mem, adjusted_hours)` **once per memory**, and
  `compute_decay` reads `IMPORTANCE_DECAY_FACTOR` / `DECAY_FACTOR` / `EMOTIONAL_DECAY_RESISTANCE` from
  settings on every call (`_shared/thermodynamics/thermodynamics.py:191,193,196`). The per-row read is one
  stack frame **below** the file the draft cited.
- N is not hypothetical: measured 2026-08-02, **2,845** `memory` rows and **2,366** `entity` rows. A naive
  swap inside `compute_decay` is ~8,500 uncached config reads per decay cycle.

**And the mechanism is a missing cache, not a missing network hop.** The PTC read-through cache lives at
`yadgar/core/server/tools/_runtime_config.py` — under `yadgar.core`. The import-linter contract
*"backend must not import core"* (`pyproject.toml:303`) makes it **structurally unreachable from backend
code**, and a grep confirms zero backend→core imports exist. A backend consumer today would call
`_get_storage().get_config_row(...)` — the same in-process call the core resolver makes at
`_runtime_config.py:109-113` — with no cache in front of it. Under MariaDB it becomes an `asyncmy` query to
a same-container process, still with no cache in front of it. **The hazard shape is identical in both
worlds; only the per-miss cost moves.** That is why §C below justifies the backend cache on grounds other
than "removes the N-round-trip trap."

**Why this correction matters:** the false framing is what produced the RUNTIME-REFACTOR classification.
§C re-derives that classification from the corrected premise.

### 0.3 The store cannot hold floats — a hard blocker on the highest-value knobs

`_JSON_VALUE_TYPES = (bool, int, str, list, dict)` at `yadgar/core/server/tools/runtime_config.py:52`.
`_apply_config_set` rejects anything else with `invalid_value` (`tools/runtime_config.py:150-151`), and Car G5 routed **both** the
`config_set` MCP tool and the host-side `POST /api/runtime-config/{key}` route through that one validator —
so both write paths reject floats.

**Float is 88 of the 344 annotated `Settings` fields** (int 142, float 88, bool 58, str 55, tuple 1).
It is not an incidental slice: it is the *entire* high-value RUNTIME-CHEAP set — every `WRRF_*_WEIGHT`,
`DECAY_FACTOR`, `IMPORTANCE_DECAY_FACTOR`, `RECALL_BOOST`, `COLD_THRESHOLD`,
`WIKI_SIM_CONTENT_THRESHOLD` (`config.py:332`), `MEMORIZE_SIM_THRESHOLD`, every similarity threshold.

**Phase 1 cannot start on its stated Batch-1 knobs until this widens.** That is a sequencing consequence,
not a bug note — it is the reason Phase 0.3 exists below.

The fix is small: the storage layer already handles floats correctly — `set_config_row` does
`json.dumps(value)` (`_shared/storage/runtime_config.py:101`) and JSON round-trips a float without loss of
the `repr`. Only the tool-level tuple excludes it. **Decision taken:** widen `_JSON_VALUE_TYPES` to include
`float` rather than string-encoding floats at every call site — string-encoding pushes a parse into ~145
consumers and reintroduces exactly the phantom-knob class ADR-0029 killed.

**MariaDB schema question this raises (one line, for the pilot):** the `value` column stays a JSON-encoded
**text** column, matching today's `json.dumps` shape. Do not make it a typed/`DECIMAL` column — a typed
column forces a per-knob type decision at schema time, loses list/dict values, and introduces float-precision
questions the JSON text encoding does not have.

---

## Phase 0 — Investigation (COMPLETE: #35 survey, 2026-07-24)
Classify every `Settings` field (`yadgar/_shared/config/config.py`) into 4 buckets:
1. **BOOT-ONLY** — structural, cannot be live DB config (stays restart-required).
2. **STRUCTURAL-REINIT** — runtime-conceptually but needs a controlled re-init (pool respawn / model reload); DB-config only if paired with a re-init trigger.
3. **RUNTIME-CHEAP** — behavioral, read fresh per-call as `self._settings.X` on a non-hot path → swap to `config_get`. Low effort.
4. **RUNTIME-REFACTOR** — behavioral but frozen at `__init__` into an instance attr, OR read on a hot path (per-call resolver perf concern) → needs re-read/invalidation or a perf check.

### Inventory (from #35 survey, 2026-07-24)
**~200 of 344 migratable.** Buckets:
- **RUNTIME-CHEAP (~145)** — once-per-request/cycle/render reads; swap `self._settings.X` → `config_get`. The high-value set:
  - *Retrieval weights + toggles* (`backend/retrieval/fusion.py:232-235,259,270`): WRRF_*_WEIGHT (vector/fts/ppr/spreading/graph-prior/cofire), CROSS_ENCODER_WEIGHT/TOP_K, RERANKER_TOP_K, RECALL_*_QUOTA/PRIOR_WEIGHT, GRAPH_MAX_HOPS, GRAPH_SPREADING_DECAY, PPR_DAMPING/ITERATIONS, RETRIEVAL_PROFILE, CANDIDATE_POOL_MULTIPLIER; all `*_ENABLED` reranker/feature toggles (RERANKER/CROSS_ENCODER/GTE/NLI/MULTI_PASSAGE/HEAVY_RERANK/QUERY_ROUTING/QUERY_EXPANSION/TEMPORAL/ADVERSARIAL/COMBMNZ/INDEX_ENRICHMENT + sub-toggles).
    *Read-frequency verified 2026-08-02:* the `signal_weights` dict is built inside `_fuse_scores`, which runs **once per fuse call** (once per request), not once per candidate. That one site is genuinely CHEAP. The other ~144 are **not** individually verified — see §F.
  - *Write/wiki/memorize gates* (once-per-request): WRITE_GATE_SHADOW_THRESHOLD, SURPRISE_BOOST, SYNAPTIC_BOOST/WINDOW, MEMORIZE_SIM_THRESHOLD/GATE_ENABLED, HOT_THRESHOLD, WIKI_SIM_CONTENT_THRESHOLD/GATE_ENABLED/MODE/TOP_K.
  - *Consolidation-cycle* (once-per-cycle — cheap even on backend b/c not per-row): CLUSTER_SIMILARITY_THRESHOLD, SIMILARITY_LINK_THRESHOLD, ~20 `*_RETENTION_DAYS`/`*_MAX_AGE_DAYS`, MEMORY_ARCHIVE_* caps, COLD_MEMORY_* gates.
  - *Anchor/signals/project_brief* (~30, once-per-signals-call): all ANCHOR_* audit/TTL, ACTIVE_WORK/CHECKPOINT_*_HOURS, ADR_DUE_WARN_HOURS, PROJECT_BRIEF_MAX_ANCHORS, *_STOP_INTERVAL cadences. (Ties #43 anchor audit.)
  - *Viz (~50)* — ALL VIZ_* (colors/physics/galaxy/caps): pure presentation, ideal live knobs.
- **RUNTIME-REFACTOR (~10)** — split into two causes by §C.3; the draft conflated them.
  - *Loop-depth:* DECAY_FACTOR / IMPORTANCE_DECAY_FACTOR / EMOTIONAL_DECAY_RESISTANCE, read per-memory inside
    `thermodynamics.py:191,193,196` (called from `heat_decay.py:147`); COLD_THRESHOLD and RECALL_BOOST, already
    hoisted at `heat_decay.py:119-122,178-179`.
  - *Frozen-at-`__init__`:* `_shared/contracts/engram.py:27-28` (EXCITABILITY_HALF_LIFE_HOURS,
    EXCITABILITY_BOOST); `backend/predictive_coding/predictive_coding.py:55`
    (WRITE_GATE_THRESHOLD — note the same knob is *also* read live at `:216`, so it is a mixed knob).
- **STRUCTURAL-REINIT (~10)** — NUM_ASTROCYTE_PROCESSES, TOOL/HOOK_RECALL_POOL_WORKERS, RERANK/RECALL concurrency semaphores, CIRCUIT_BREAKER_*. DB-config only if paired with a re-init trigger.
- **BOOT-ONLY (~95)** — hosts/ports/URLs, paths, secrets/auth, model selection+paths, model warm-up/eviction, cache-init (CE/EMBED_CACHE_*), logging/OTLP, CPU budgets, update strings.
- **DEAD (skip/delete separately):** HOPFIELD_BETA, RECALL_QUALITY_FLOOR (no prod reader), some COMET_* (retired ADR-0004).
  **Correction 2026-08-02:** the draft also listed HOPFIELD_MAX_PATTERNS as dead. It is **not** — it is read
  at `_shared/contracts/engram.py:26` (`self._num_slots = settings.HOPFIELD_MAX_PATTERNS`). It is a
  frozen-at-`__init__` knob, and a STRUCTURAL one at that (slot count), so it belongs in Phase 3 rather than
  in either the dead list or the RUNTIME-REFACTOR list. Verify the rest of the dead list the same way before
  any delete PR — one wrong entry in four is a bad hit rate.

### ⚠️ Load-bearing caveat (survey-confirmed, amended 2026-08-02)
1. **`get_settings()` is `@lru_cache(maxsize=1)`** (`config.py:1068-1069` — the draft said 1054; the file has
   drifted) → `self._settings.X` is **NOT live today**. Migration to `config_get` is what MAKES it live.
   "CHEAP" = cheap to convert, not already-live.
2. ~~Backend `config_get` = per-call HTTP round-trip.~~ **Withdrawn — see §0.2.** The correct statement:
   there is no backend-reachable cache in front of `get_config_row`, and cheap-vs-refactor is
   **loop-depth × cache-reachability**, not fresh-vs-frozen alone.
3. **Floats are rejected by the write path** — §0.3. Blocks Batch 1.

---

## §D — End state, walked through one knob

The draft's non-goal line ("the `Settings` field stays as the authoritative code default") was correct but
under-specified, and the user reported not being able to tell what had actually been designed. This section
is the answer, concrete.

**Worked example: `WIKI_SIM_CONTENT_THRESHOLD`.** Picked because it exercises every seam — it is a float
(§0.3 blocker), it has a core consumer (`core/server/tools/wiki.py:986`) *and* two backend consumers
(`backend/queue_drainer/__init__.py:559`, `backend/queue_drainer/dlq.py:316`), and it is plausibly something
a user would want set differently per repo.

### D.1 Resolution order, after migration

| Stage | Where it lives | Value | Who reads it |
|---|---|---|---|
| 1. Code default | `Settings.WIKI_SIM_CONTENT_THRESHOLD: float = 0.80` (`config.py:332`) | `0.80` | passed as the `default=` argument on every `config_get` call; never read directly by consumers after migration |
| 2. `config.yaml` row | **deleted** by the retire step | — | — |
| 3. Global DB row | `runtime_config` where `directory IS NONE` | e.g. `0.85` | overrides stage 1 |
| 4. Per-directory DB row | `runtime_config` where `directory = '/home/max/git/yadgar'` | e.g. `0.75` | overrides stage 3 **for that directory only** |
| 5. Core PTC cache | `_runtime_config.py` `Cache(name="runtime_config")` | caches the **resolved** value under the *requested* `(key, directory)` pair | core-side callers |
| 6. Backend cache | new — §C | caches the same resolved value, backend-side | backend-side callers |

`config_get("WIKI_SIM_CONTENT_THRESHOLD", directory, default=get_settings().WIKI_SIM_CONTENT_THRESHOLD)`
returns, in order: cache hit → per-dir row → global row → the passed default
(`_runtime_config.py:108-118`, `:132-144`).

### D.2 What `config_get` returns at each stage

- **No rows at all** (today's state for every knob): returns `0.80`. Identical to pre-migration behaviour.
  This is why the migration is reversible per-knob — deleting the row restores the old value exactly.
- **Global row only, `0.85`:** every directory gets `0.85`.
- **Global `0.85` + per-dir `0.75` for this repo:** this repo gets `0.75`; every other directory gets `0.85`.
- **Caller passes `directory=None`** (a genuinely global consumer — e.g. the DLQ drainer, which has no
  project context): per-dir resolution is skipped entirely (`_runtime_config.py:108`), so it sees `0.85`.
  **This is a real semantic hazard, not a detail:** a knob whose consumers are split between dir-aware and
  dir-blind call sites will behave inconsistently under a per-dir override. `WIKI_SIM_CONTENT_THRESHOLD`
  is exactly such a knob — `wiki.py` has a directory, `dlq.py` does not. Per-knob, the migration must
  decide *global-only* or *dir-scoped*, and a dir-scoped knob with a dir-blind consumer is a defect.

### D.3 What happens when the DB is unreachable

Three distinct paths, and they are deliberately not uniform:

- **In-daemon (`config_get` resolver):** any storage exception → return the caller's `default`, log at
  warning, and **do not cache the failure** (`_runtime_config.py:116-118`, and the comment at `:139-143`).
  A transient blip therefore cannot pin a wrong value. Behaviour degrades to the code default — i.e. to
  exactly today's behaviour.
- **Host-side (`runtime_config_client.get`):** fail-open to the caller's `default` on daemon-down, timeout,
  non-200 or malformed JSON — never raises (`runtime_config_client.py:39-79`). Stop-hooks depend on this.
- **Writes are NOT fail-open:** `runtime_config_client.set`/`delete` return `False` on any failure
  (`:124-159`, `:163-192`) so a caller can report "couldn't set" rather than assume the write landed.

**The end-state property this buys:** a knob's DB row is always *additive*. Losing the DB, the row, or the
whole engine #2 returns the system to its `Settings` defaults — never to an undefined state. That is the
whole reason the `Settings` field stays.

---

## §C — Chained cache design (core PTC → backend cache → DB)

New section. The user's direction is caches on **both** sides. Today the PTC read-through cache is
core-side only, and per §0.2 it is *structurally* unreachable from backend (`pyproject.toml:303`), not
merely absent.

### C.1 What each layer holds

| Layer | Holds | Key | Lifetime / eviction | Invalidation |
|---|---|---|---|---|
| **Core PTC** (exists) | resolved value for a *requested* `(key, directory)` | `f"{key}\x00{directory or ''}"` (`_runtime_config.py:93-95`) | byte-bounded LRU on the core RAM-% budget (`:64-72`) | `Manual` — whole-flush via `invalidate_config_cache()` on every write (`:147-158`) |
| **Backend cache** (new) | same resolved value, backend-side | same `(key, directory)` pair, **plus a version** — see C.2 | in-process dict, bounded by row count (the whole table is <1k rows; no LRU needed) | version-in-key bump, not TTL — see C.2 |
| **DB** | `{key, directory, value(JSON text), created_at, updated_at}` | `(key, directory)`, `directory IS NONE` = global | durable | n/a |
| **Core warmup** | pre-fills the core PTC with every stored row at daemon start | — | `bootstrap/bootstrap.py:124-126` → `warmup_runtime_config_cache` | best-effort; failure never blocks start |

The backend layer gets an equivalent warmup at backend start, for the same reason: with a bounded row count
the whole table fits, so a cold backend never pays a miss.

### C.2 Invalidation across the chain — decision: **version-in-key, not TTL**

**Reuse ADR-0053's mechanism.** `yadgar/backend/cache/scope_versions.py` already implements exactly this:
a process-global, thread-safe `(scope_kind, scope_id) → int` map where a reader embeds the current version
in its cache key, so a bump makes every prior key unreachable with **no explicit invalidate call and no
cross-service round-trip** (`scope_versions.py:22-62`). Use `scope_kind="config"` with `scope_id=None`
(one scope for the whole table — writes are rare enough that per-key scoping buys nothing).

**Why not a short TTL,** which the 2026-07-24 draft proposed (5-30s):
- A TTL makes staleness *unbounded in the wrong direction* — it is bounded in time but **guaranteed**: after
  a `config_set`, the backend serves the stale value for up to the TTL, every time, deterministically. The
  user's stated hazard ("a stale backend cache serves stale values to the decay loop after a `config_set`")
  is precisely what a TTL fails to prevent.
- ADR-0053 already rejected the naive alternatives for backend caches and gave a reason a TTL does not
  answer: a global epoch churns to ~0% hit rate. Version-in-key does not, because the version only moves on
  an actual write.
- The mechanism costs one dict read per cache lookup and one dict write per config write. Cheaper than a
  monotonic-clock TTL check.

**Where the bump goes.** The backend admin ops `runtime_config_set` / `runtime_config_delete`
(`backend/admin_exec/runtime_config.py:28-58`) are the only code that writes the table. They call
`ScopeVersions.bump("config", None)` after a successful `set_config_row` / `delete_config_row`. Every
config write in the system funnels through those two functions, so the bump cannot be missed.

**Propagation to the core PTC.** Unchanged from today, and it already works: core's `config_set` tool
forwards to the backend admin op and *then* calls `invalidate_config_cache()`
(`tools/runtime_config.py:157-158` for set, `:176-177` for delete). Core busts its own cache on its own
write path.

**The gap this leaves, stated honestly:** a write that does **not** pass through a core tool would bust the
backend cache (via the bump) but leave the core PTC stale until the next core-side write or restart. Today
no such path exists — the `_forward_admin` route is the only writer. Under MariaDB it becomes *possible*
(anything with the DSN can write).

The asymmetry is worth naming: the backend layer is version-keyed, so it *self-heals* on a foreign write.
The core PTC uses `Manual` whole-flush and holds **no version to check** — it has no mechanism to learn
about a write it did not itself perform. The writer-invariant is therefore not a convenience; it is
load-bearing for the core layer's correctness specifically.

**Mitigation, and it is the cheap one:** keep the invariant that all writes go through `_apply_config_set`,
and enforce it as a test (Phase 0.9) rather than building a core-invalidation channel for a path that does
not exist. Do not build a pub/sub for a hypothetical writer.

### C.3 Which knobs become cheap once a backend cache exists — re-derived, not asserted

The draft claimed the backend cache "collapses the CHEAP/REFACTOR distinction for backend knobs." That is
half right, and the half that is wrong matters. The ~10 REFACTOR knobs have **two different causes**:

| Cause | Knobs | Does a backend cache fix it? |
|---|---|---|
| **Loop-depth** — read per-row inside a batch | DECAY_FACTOR, IMPORTANCE_DECAY_FACTOR, EMOTIONAL_DECAY_RESISTANCE (`thermodynamics.py:191,193,196`, called per-memory from `heat_decay.py:147`) | **Yes** — a cached read is a dict lookup, so per-row is acceptable. A hoist would also fix it, with no cache. |
| **Frozen at `__init__`** — value captured at construction time | `engram.py:26-28`, `predictive_coding.py:55` | **No.** Nothing about read *cost* is the problem; the value is captured once and the object outlives every config change. Needs a re-read hook or an invalidation subscription regardless of engine or cache. |

**So: roughly half the REFACTOR bucket collapses; half survives any cache and any engine.** That is a more
useful statement than "the category collapses," and it means Phase 2 does not disappear — it shrinks to the
frozen-at-init knobs plus the mixed knob (`WRITE_GATE_THRESHOLD`, frozen at `predictive_coding.py:55` and
live at `:216`).

**The backend cache's real justification, since it is no longer "removes the N-round-trip trap":**
1. It removes the need to hoist at ~145 call sites. A hoist is a per-site refactor that must be re-verified
   every time the surrounding code changes; a cache is one mechanism.
2. It gives per-read liveness granularity. A hoisted value is stale for the duration of the cycle; a cached
   value is stale only until the next write bumps the version.
3. It makes the CHEAP classification *robust to being wrong*. Per §F the ~145 CHEAP knobs' read frequency is
   estimated, not measured. With a cache in front, a misclassified per-candidate read costs a dict lookup
   instead of a query. **This is the strongest argument and it should be the stated one.**

### C.4 Interaction with engine #2 (MariaDB) — the write topology inverts

MariaDB is MySQL-wire, so `mysql+asyncmy://` is a first-class SQLAlchemy 2.0 **async** dialect (decision doc
§4.4, §4.5). Concretely, for this chain:

- **The per-miss cost changes, the shape does not.** A backend miss today is an in-process
  `get_config_row` against Surreal. Under MariaDB it is an `async` query to a separate process in the same
  container. Both are "one uncached DB read per miss." Neither is an HTTP round-trip. The cache is
  justified by C.3's three reasons in both worlds.
- **Ownership inverts, and this is the new design point.** Today the table is written by a backend admin op
  that core forwards to, and *core* owns the only cache. Under MariaDB the backend owns the table outright,
  so backend-local invalidation on its own write is free and synchronous — the `ScopeVersions.bump` in C.2
  happens in the same function as the `INSERT`. The residual problem becomes *core's PTC learning about a
  backend write* — the reverse of today's problem. As stated in C.2, today's `_forward_admin`-only writer
  invariant covers it; the inversion is the reason that invariant must become a tested rule rather than an
  accident.
- **Migrations become Alembic.** Per decision doc §4.5, task 0051 (surrealmigrate fork) is **mooted** and
  most of 0048 collapses into "adopt Alembic." Any phase in this plan that would have added a hand-rolled
  append-only migration for a knob-table change (e.g. the float widening, if it needed one — it does not,
  since the value column is JSON text) targets **Alembic** instead.

---

## Read-pattern discriminator (the core mechanic)
- **Cheap:** `self._settings.X` read fresh each call on a once-per-request/cycle path → replace with
  `config_get("X", directory, default=<code default>)`. Verified example: `fusion.py:232-235`
  (once per fuse call).
- **Refactor (loop-depth):** read per-row inside a batch — `thermodynamics.py:191,193,196`. Either hoist to
  the batch boundary (as `heat_decay.py:119-122,178-179` already does for its own knobs) or rely on the
  backend cache (§C.3).
- **Refactor (frozen):** `self._x = settings.X` at construction (`engram.py:26-28`,
  `predictive_coding.py:55`) → re-read per cycle, or subscribe to invalidation. **No cache helps here.**
- **Trap:** a knob that looks runtime but is read once into a module/singleton constant → treat as REFACTOR or BOOT.
- **Trap (new):** a *mixed* knob — read both frozen and live (`WRITE_GATE_THRESHOLD`). Migrating only one
  site produces two different effective values for one key.

## Constraints / invariants to respect
- **I25 three-way-sync — read path SOLVED, but the seed+retire goal EVOLVES the ratchet.** See Phase 0.7,
  which now also corrects *which* ratchet breaks and how.
- **Hot-path perf** — see §C.3 and §F.
- **Backend reachability** — resolved: backend reads go via `_get_storage().get_config_row(...)`, never
  over HTTP; the missing piece was a backend-reachable cache (§0.2), which §C designs.
- **Default-in-code** — the code default remains authoritative when no DB row exists (§D.2).
- **Dir-scoping semantics** — per-knob, decide global-only vs dir-scoped, and reject dir-scoped knobs that
  have dir-blind consumers (§D.2's `WIKI_SIM_CONTENT_THRESHOLD` case).
- **Float rejection** — §0.3. Blocks Batch 1 until Phase 0.3 lands.

---

## Blocking before any seed
Ordered. Nothing in Phase 1 may run until all three are settled.

1. **Task 0095 — project identity key scheme.** Free to re-key only while `runtime_config` is empty. The gate
   is the **first `config_set`**, not the schema; the Phase 0.9 pilot may proceed with 0095 open.
2. **Phase 0.3 — float widening.** Without it, Batch 1's stated knobs cannot be stored at all.
3. **Phase 0.7 — I25 ratchet evolution.** Without it, the first `config.yaml` deletion turns
   `test_config_three_way_sync` red *and* silently drops the knob from `test_no_phantom_knobs`' scope.

## Phase 0.3 — widen the accepted value types to include float
`_JSON_VALUE_TYPES` at `tools/runtime_config.py:52` gains `float`. Storage needs no change
(`json.dumps` at `_shared/storage/runtime_config.py:101` already handles it). Tests: a float round-trips
through `config_set` → `config_get` with its value preserved; the host-side `POST` route accepts it (same
validator, Car G5); a non-JSON type is still rejected. Small, isolated, blocking. **Do first.**

## Phase 0.5 — backend-reachable config cache
Build §C's backend layer: a small in-process resolved-value cache in `yadgar/backend/`, keyed by
`(key, directory, version)` with the version from `ScopeVersions("config", None)`, bumped by the two admin
ops in `backend/admin_exec/runtime_config.py`. Warmup at backend start, mirroring
`warmup_runtime_config_cache`.

*Changed decision:* the 2026-07-24 draft proposed a **short-TTL** cache and sequenced this as the de-risking
prerequisite for the whole migration. Both change. TTL → version-in-key (§C.2, reusing ADR-0053's
`ScopeVersions`), because a TTL guarantees post-write staleness rather than preventing it. And it is no
longer the top-priority de-risker, because §0.2 showed the trap it was de-risking is not present — it is
now justified by §C.3's three reasons, chiefly making a wrong CHEAP classification cheap.

*Also changed:* this section was at the bottom of the 2026-07-24 draft, after the phases that depended on
it. Moved into sequence.

## Phase 0.7 — I25 ratchet evolution (prerequisite for retire)

The draft was one sentence. It also named the wrong failure. Both ratchets are static — pure imports plus
source greps over the `yadgar/` tree, with **no DB access** — so neither can see a `runtime_config` row.
What breaks is not one thing:

**Failure 1 — `test_config_three_way_sync` goes RED (loud, expected).** Every `Settings` field must be in
`FIELD_META` **and** `_REGISTRY`, or on the allowlist; `test_all_settings_fields_covered`
(`test_config_three_way_sync.py:158`) fails on the gap. Deleting a migrated knob's `config.yaml`/FIELD_META
row trips it immediately. This is the failure the draft anticipated.

**Failure 2 — `test_no_phantom_knobs` goes SILENTLY GREEN (quiet, worse).** Its scope is computed as
*Settings field* **∧** *has a FIELD_META entry* (`test_no_phantom_knobs.py:73-78`). Removing the FIELD_META
entry does not fail the ratchet — it removes the field from the ratchet's scope entirely. The knob simply
stops being checked. **Retiring knobs from `config.yaml` silently shrinks phantom-knob coverage by ~145
fields**, which is a coverage hole disguised as a passing test.

### The replacement — a positive marker the ratchets iterate over

A knob with no `config.yaml` row still has a literal default in source: the `Settings` field itself
(`WIKI_SIM_CONTENT_THRESHOLD: float = 0.80`, `config.py:332`), which the non-goals keep deliberately. So a
static ratchet *can* still verify it — the question is what it verifies against.

**Design: `runtime_managed=true` as a positive registry class, not an exemption.**

1. **The marker lives in `config_registry.py`,** on the existing `ConfigEntry` (today
   `ConfigEntry("YADGAR_WIKI_SIM_CONTENT_THRESHOLD", "0.80", "float")`,
   `config_registry.py:393`). A `runtime_managed` entry keeps its registry row — it loses only the
   `config.yaml`/FIELD_META row. This is deliberate: the registry stays the single enumerable list of every
   knob, so nothing can fall out of *both* surfaces and become invisible.
2. **`test_config_three_way_sync` learns a third class.** Today: yaml ∧ registry, or allowlist. New:
   yaml ∧ registry, **or** registry ∧ `runtime_managed=true`, or allowlist. Note this is *not* the existing
   allowlist — the allowlist means "env-only by design," which is the opposite claim.
3. **`test_no_phantom_knobs` widens its scope** from *Settings ∧ FIELD_META* to
   *Settings ∧ (FIELD_META ∨ runtime_managed)*, and gains a second assertion for the runtime-managed class:
   a `runtime_managed` knob must have **at least one `config_get("<KEY>", …)` call site** in the source
   corpus. That is the replacement for the "has a `get_settings().FIELD` consumer" check at
   `test_no_phantom_knobs.py:120-121` — same grep shape, different target. It catches the new phantom
   class: a knob marked runtime-managed, deleted from yaml, and read by nobody.
4. **A fourth, new assertion — the default is still reachable.** A `runtime_managed` knob's `config_get`
   call site must pass `default=get_settings().<FIELD>` (or the literal `Settings` default). Without this,
   a migrated knob whose DB row is deleted silently resolves to `None`. Grep-checkable, same corpus scan.

**What this does NOT verify, stated plainly:** no static ratchet can check that the DB row's *value* is
sane, or that a seeded value matches the code default. That is a runtime concern and belongs in the
per-knob live-effect test (Testing, below), not the ratchet.

Ship BEFORE the first retire batch.

## Phase 0.9 — engine-#2 pilot: `runtime_config` onto MariaDB, schema only, zero rows
See §G. Schema move only, no seed — so task 0095's free-re-key window stays open through this phase.
Carries the engine-#2 operational bootstrap (decision doc §2's four arms).

## Per-knob migration recipe (read → seed → retire)
For each migrated knob:
1. **Read:** swap the consumer `self._settings.X` → `config_get("X", directory, default=get_settings().X)`.
   Decide global-only vs dir-scoped first (§D.2), and check for mixed/frozen sites.
2. **Seed:** write the current effective value into `runtime_config` via the **`config_set` MCP tool** —
   NEVER raw SQL/DB insert.
3. **Retire:** remove the knob's `config.yaml`/FIELD_META row, mark the registry entry
   `runtime_managed=true`. Keep the `Settings` field as code default.
4. **Test:** DB override changes behavior live (no restart); both ratchets stay green; deleting the row
   restores the old value exactly.

## Phase 1 — Batch 1 (highest-value RUNTIME-CHEAP)
Gated on all three "Blocking before any seed" items. Candidates: retrieval weights + thresholds, feature
toggles (`*_ENABLED`), gates, retention days. Per knob: the recipe above. TDD.

## Phase 2 — the surviving RUNTIME-REFACTOR
Shrunk by §C.3 to the **frozen-at-init** knobs (`engram.py:26-28`, `predictive_coding.py:55`) plus the mixed
knob. Needs a re-read-per-cycle hook or an invalidation subscription — a cache does not help. The
loop-depth knobs move into Batch 1 once the backend cache lands.

## Phase 3 — STRUCTURAL-REINIT (optional)
Pool/model knobs needing a controlled re-init trigger. Only if worth the machinery. (rec: defer.)

---

## §F — Measurement vs estimate

The cheap-vs-refactor split was reasoned from loop depth, never measured. Partially fixed, partially not —
and the residual risk is stated rather than papered over.

**Measured (2026-08-02), so no measurement step is needed for the decay path:**
- N for `_decay_memories` = `len(get_all_memories_for_decay_scalar())`, upper-bounded by the `memory` row
  count = **2,845**. Strictly less: the loop `continue`s on `is_protected` rows (`heat_decay.py:130-131`).
- N for `_decay_entities` = `len(get_all_entities_for_decay())`, upper-bounded by the `entity` row
  count = **2,366**.
  (Both counts measured 2026-08-02 via `db_inspect`. They are row-count proxies, not the loop lengths — good
  enough to fix the order of magnitude, which is all the classification needs.)
- The decay loop's own knob reads are already hoisted (`heat_decay.py:119-122,178-179`); the per-row reads
  are one frame down in `thermodynamics.compute_decay`.
- `fusion.py`'s `signal_weights` is built once per fuse call, not per candidate.

**Not measured — the named residual risk:** whether each of the other ~145 RUNTIME-CHEAP knobs is read
once-per-request or once-per-candidate. That is an **estimate from reading call sites**, not data.

**Consequence if wrong:** a knob classified CHEAP that is actually read per-candidate lands an uncached DB
query inside the retrieval hot path — the exact failure the (withdrawn) N-round-trips framing feared, in
the one place it would actually bite.

**Mitigation, and it is why §C.3 orders the backend cache before Batch 1:** with a cache in front, a
misclassified read costs a dict lookup rather than a query. Ordering the cache first converts this from a
correctness risk into a performance footnote. **Do not attempt to measure all ~145 read rates** — the
measurement is more expensive than the mitigation.

**One measurement that IS worth taking, before the pilot:** MariaDB idle RSS in the 4 GB-capped backend
container. Decision doc §4.4/§4.5 names it as the recommendation's disqualifying condition and records it
as still unverified.

---

## §G — Reconciliation with the split-store engine decision

Read against `docs/plans/split-store-engine-decision-2026-08-02.md` §7's row for this plan. **Agreed on all
four points**, and the phases above are structured around them:

1. *"Phase ordering rests on a false premise — the store is empty."* Correct. → §0.1, and the phases
   re-sequenced.
2. *"The REFACTOR classification exists only because the backend has no PTC."* Correct **for the loop-depth
   half**; the frozen-at-init half has a different cause and survives. → §C.3.
3. *"Correct the N-HTTP-round-trips framing."* Correct, and the correction goes further than the decision
   doc states: `runtime_config_client` has no backend importers at all, and `pyproject.toml:303` makes the
   core PTC structurally unreachable from backend. → §0.2.
4. *"0095 is blocking and time-boxed."* Correct, with one refinement: the gate is the **first seed**, not
   the schema. → §0.1.3.

### `runtime_config` as the first mover onto MariaDB — agreed, and here is the argument

Of the four relational kinds in the decision doc's §1.1 table, `runtime_config` is the **only one that is
already a real SurrealDB table** (`migrations.py:340`). Tasks, ADRs and agent-prompts are all markdown wiki
pages today — moving any of them to engine #2 is simultaneously a *schema design* and a *data migration*
of hundreds of rows parsed out of prose. `runtime_config` is neither: the schema exists, and there are
**zero rows to migrate**.

So the pilot reduces to: create the table in MariaDB, repoint `_RuntimeConfigMixin`'s four methods
(`set_config_row` / `get_config_row` / `list_config_rows` / `delete_config_row`,
`_shared/storage/runtime_config.py:93-191`) at the new engine, and verify. Nothing above those four methods
changes — the resolver, the cache, the tools, the route and the host client all sit behind them.

**The cost this plan therefore inherits, stated up front.** Decision doc §2 is explicit that the four
operational arms — backup, restore-verification enumeration, migrations, `check_invariants` — land in the
**same commit** as the first engine-#2 row, because of the 2026-06-16 incident where a partial restore
passed a `>=` check and destroyed 3,622 memories. Making `runtime_config` the pilot means **this plan's
Phase 0.9 carries the engine-#2 operational bootstrap.** That is a real cost and it is larger than the
schema move itself.

It is nonetheless an argument *for* going first, not against: those four arms have to be built for the
first engine-#2 table whichever kind it is, and building them against a table with **zero rows and zero
readers in production** is the cheapest possible rehearsal. A restore-verification bug found here costs
nothing; the same bug found while migrating 195 ADRs costs the ADR corpus.

**One thing the pilot must NOT do:** seed a row. Per §0.1.3 the first seed closes task 0095's free-re-key
window. Phase 0.9 is schema + operational arms + tests against an empty table; Phase 1 seeds.

---

## Testing
- Per migrated knob: DB override → behavior changes without restart (live-effect test).
- Per migrated knob: **delete the row → the value returns to the `Settings` default exactly** (the
  reversibility property §D.3 depends on).
- Both I25 ratchets green, including the new `runtime_managed` assertions (Phase 0.7 items 3 and 4).
- Float round-trip through `config_set` → `config_get` (Phase 0.3).
- Warmup + version-bump invalidation correct on both cache layers; a `config_set` is visible to a backend
  reader on its very next read (no TTL window).
- **Writer-invariant test:** the only paths that write `runtime_config` are `_apply_config_set` /
  `_apply_config_delete` → the two backend admin ops. This is what makes §C.2's core-invalidation gap
  a non-issue; it must be enforced, not assumed.
- No hot-path perf regression (micro-bench the decay/recall loops for any hot-path migration).

## Rollout / risk
- Additive + reversible: a knob with no DB row behaves exactly as today (code default). Low blast radius.
- Migrate in small PRs per subsystem batch; version-bump per PR (runtime code change).
- **The largest risk is not in this plan's knobs** — it is that Phase 0.9 carries the engine-#2 operational
  bootstrap (§G). Budget it as such.
- Ties: #43 (anchor system), #30 (secret-gate knobs), #95 (project identity key — **blocking**),
  ADR-0163 (store), ADR-0053 (cache invalidation mechanism), ADR-0183 (interface seam), I25 invariant.

## Decisions (for user)

**Resolved since the draft** (recorded so the change is visible):
- ~~*Backend-local cache — yes/no, and short-TTL?*~~ **Yes, but version-in-key, not TTL** (§C.2), and no
  longer the top-priority de-risker (§0.2, Phase 0.5).
- ~~*Which engine?*~~ **MariaDB**, decided by the user 2026-08-02 (decision doc §4.5). Task 0051 mooted;
  0048 collapses into "adopt Alembic."
- *Float values* — **widen `_JSON_VALUE_TYPES`** rather than string-encode (§0.3). Flagged for confirmation
  below because it blocks Batch 1.

**Still open:**
1. **Confirm the float widening** (§0.3, Phase 0.3). It is a one-line change but it gates Batch 1 entirely,
   and the alternative (string-encoding at ~145 call sites) is bad enough that it should be rejected
   explicitly rather than by omission.
2. **Task 0095 — settle the project identity key before the first seed** (§0.1.3). The refinement to
   confirm: schema-only Phase 0.9 may proceed with 0095 open; only Phase 1's first `config_set` is gated.
3. **Scope: does this plan own the engine-#2 operational bootstrap?** §G's first-mover argument means
   Phase 0.9 carries backup, restore-verification enumeration, Alembic migrations and cross-engine
   `check_invariants` — decision doc §2 requires all four in the same commit as the first engine-#2 row.
   That is a materially bigger scope than "knob migration" implies, and it should be an explicit choice.
   Alternatives: (a) accept it here, on the argument that a zero-row table is the cheapest possible
   rehearsal; (b) split Phase 0.9 into its own plan/train that this one depends on; (c) let a different
   kind (ADRs) go first and pay the bootstrap there instead — worse, since ADRs also carry a real
   data migration.
4. **Batch 1 scope** — retrieval weights + toggles + gates + viz together, or retrieval only for a first
   proof?
5. **STRUCTURAL-REINIT (Phase 3)** — defer? (rec: yes.)
6. **Dead knobs** (HOPFIELD_BETA, RECALL_QUALITY_FLOOR, retired COMET_*) — separate delete PR, not this
   train? (rec: yes, and re-verify each has no reader first — the draft's list already had one false
   entry, HOPFIELD_MAX_PATTERNS.)
7. **A viz/CLI surface** to browse + set live knobs — this train or a follow-up?
