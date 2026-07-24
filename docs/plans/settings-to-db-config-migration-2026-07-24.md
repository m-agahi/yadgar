# Settings → DB runtime_config migration — 2026-07-24

**Task:** #35 (Survey config.yaml/Settings for purely-runtime flags migratable to the DB config store).
**Status:** DRAFT / SKELETON — investigation in flight (the #35 survey). Fill the inventory + finalize batches when it lands.
**Builds on:** [[yadgar-adr-0163]] (DB-backed runtime_config store — dir-scoped, typed, PTC-cached, warmup, invalidation).

## Vision
Yesterday's `runtime_config` store was introduced for one flag (`code_graph.enabled`). But it is a *general* live-config substrate: DB-backed, dir-scoped (per-dir → global → default), typed JSON, core-cache pull-through, warmup at daemon start, invalidation on write, reachable from BOTH core and backend. The `Settings` model has **~335 knobs**; most are behavioral tuning that is restart-required only *incidentally* (pydantic `Settings` loads once at boot), not fundamentally. Migrating the runtime-tunable subset makes them **live, dir-scoped, restart-free** — far more ergonomic to operate + dogfood.

## Goals
- **Primary:** make the runtime-tunable knob subset LIVE (DB-backed, dir-scoped, restart-free).
- **KEY GOAL (user, 2026-07-24):** SEED each migrated knob's current value into `runtime_config` (via the `config_set` MCP tool — NEVER raw SQL/DB inserts, per the standing data-repair-through-MCP rule) and then **REMOVE it from `config.yaml`**. End state: `config.yaml` shrinks to BOOT-ONLY knobs; the migrated knobs live as `Settings`-code-default + DB-override only. Much simpler operational surface — one place (DB) to tune live, `config.yaml` only for what truly needs a restart.

## Non-goals
- NOT migrating structural/boot knobs (ports, URLs, DB/embed connection, secrets, model selection, container identity, pool sizes that fork at boot) — these STAY in `config.yaml`/env.
- NOT removing the `Settings` FIELD (config.py) — it stays as the authoritative CODE DEFAULT (the `default=get_settings().FIELD` arg needs it, and it's the fallback when no DB row exists). Only the `config.yaml` row (and its FIELD_META/registry doc coupling) retires for migrated knobs.
- NOT a big-bang. Phased, highest-value-first, invariant-preserving.

## Phase 0 — Investigation (IN FLIGHT: #35 survey)
Classify every `Settings` field (`yadgar/_shared/config/config.py`, ~335) into 4 buckets:
1. **BOOT-ONLY** — structural, cannot be live DB config (stays restart-required).
2. **STRUCTURAL-REINIT** — runtime-conceptually but needs a controlled re-init (pool respawn / model reload); DB-config only if paired with a re-init trigger.
3. **RUNTIME-CHEAP** — behavioral, read fresh per-call as `self._settings.X` on a non-hot path → swap to `config_get`. Low effort.
4. **RUNTIME-REFACTOR** — behavioral but frozen at `__init__` into an instance attr, OR read on a hot path (per-call resolver perf concern) → needs re-read/invalidation or a perf check.

**Deliverable:** the classified inventory (counts per bucket + the notable migratable knobs grouped by subsystem) + a recommended Batch-1. → paste into this plan's "Inventory" section below.

### Inventory (from #35 survey, 2026-07-24)
**~200 of ~335 migratable.** Buckets:
- **RUNTIME-CHEAP (~145)** — once-per-request/cycle/render reads; swap `self._settings.X` → `config_get`. The high-value set:
  - *Retrieval weights + toggles* (fusion.py:232-270, once-per-request): WRRF_*_WEIGHT (vector/fts/ppr/spreading/graph-prior/cofire), CROSS_ENCODER_WEIGHT/TOP_K, RERANKER_TOP_K, RECALL_*_QUOTA/PRIOR_WEIGHT, GRAPH_MAX_HOPS, GRAPH_SPREADING_DECAY, PPR_DAMPING/ITERATIONS, RETRIEVAL_PROFILE, CANDIDATE_POOL_MULTIPLIER; all `*_ENABLED` reranker/feature toggles (RERANKER/CROSS_ENCODER/GTE/NLI/MULTI_PASSAGE/HEAVY_RERANK/QUERY_ROUTING/QUERY_EXPANSION/TEMPORAL/ADVERSARIAL/COMBMNZ/INDEX_ENRICHMENT + sub-toggles).
  - *Write/wiki/memorize gates* (once-per-request): WRITE_GATE_SHADOW_THRESHOLD, SURPRISE_BOOST, SYNAPTIC_BOOST/WINDOW, MEMORIZE_SIM_THRESHOLD/GATE_ENABLED, HOT_THRESHOLD, WIKI_SIM_CONTENT_THRESHOLD/GATE_ENABLED/MODE/TOP_K.
  - *Consolidation-cycle* (once-per-cycle — cheap even on backend b/c not per-row): CLUSTER_SIMILARITY_THRESHOLD, SIMILARITY_LINK_THRESHOLD, ~20 `*_RETENTION_DAYS`/`*_MAX_AGE_DAYS`, MEMORY_ARCHIVE_* caps, COLD_MEMORY_* gates.
  - *Anchor/signals/project_brief* (~30, once-per-signals-call): all ANCHOR_* audit/TTL, ACTIVE_WORK/CHECKPOINT_*_HOURS, ADR_DUE_WARN_HOURS, PROJECT_BRIEF_MAX_ANCHORS, *_STOP_INTERVAL cadences. (Ties #43 anchor audit.)
  - *Viz (~50)* — ALL VIZ_* (colors/physics/galaxy/caps): pure presentation, ideal live knobs.
- **RUNTIME-REFACTOR (~10)** — the decay/heat BATCH-LOOP knobs (DECAY_FACTOR heat_decay.py:178/thermodynamics.py:193, IMPORTANCE_DECAY_FACTOR :191, COLD_THRESHOLD, RECALL_BOOST, EMOTIONAL_DECAY_RESISTANCE — read per-memory-in-batch) + frozen-at-`__init__` attrs (engram.py:27-28 EXCITABILITY_HALF_LIFE/BOOST; WRITE_GATE_THRESHOLD predictive_coding.py:55). Need resolve-once-per-batch / re-read hooks.
- **STRUCTURAL-REINIT (~10)** — NUM_ASTROCYTE_PROCESSES, TOOL/HOOK_RECALL_POOL_WORKERS, RERANK/RECALL concurrency semaphores, CIRCUIT_BREAKER_*. DB-config only if paired with a re-init trigger.
- **BOOT-ONLY (~95)** — hosts/ports/URLs, paths, secrets/auth, model selection+paths, model warm-up/eviction, cache-init (CE/EMBED_CACHE_*), logging/OTLP, CPU budgets, update strings.
- **DEAD (skip/delete separately):** HOPFIELD_BETA/MAX_PATTERNS, RECALL_QUALITY_FLOOR (no prod reader), some COMET_* (retired ADR-0004).

### ⚠️ Load-bearing caveat (survey-confirmed)
1. **`get_settings()` is `@lru_cache(maxsize=1)`** (config.py:1054) → `self._settings.X` is **NOT live today**. Migration to `config_get` is what MAKES it live. "CHEAP" = cheap to convert, not already-live.
2. **Backend `config_get` = per-call HTTP round-trip** (`runtime_config_client.get()`, urllib, 2s timeout, **NO backend-local cache** — the PTC cache + warmup are CORE-side only, bootstrap.py:124). ⇒ A backend knob read **per-memory in a decay batch of N rows = N HTTP round-trips → fatal.** This is why the decay/heat knobs are REFACTOR (resolve-once-per-batch), not cheap. Cheap-vs-refactor = **loop-depth × process**, not fresh-vs-frozen alone.

## Read-pattern discriminator (the core mechanic)
- **Cheap:** `self._settings.X` read fresh each call → replace with `config_get("X", directory, default=<code default>)` (PTC cache = same speed). e.g. `heat_decay.py:178`, `thermodynamics.py:191`.
- **Refactor:** `self._x = settings.X` frozen at construction (e.g. `engram.py:27-28`) → re-read per cycle, or subscribe to the invalidation hook to refresh the cached attr.
- **Trap:** a knob that looks runtime but is read once into a module/singleton constant → treat as REFACTOR or BOOT.

## Constraints / invariants to respect
- **I25 three-way-sync — read path SOLVED, but the seed+retire goal EVOLVES the ratchet.**
  - Read migration (harmless to I25): `config_get(key, directory, default=get_settings().FIELD)`. The literal `get_settings().FIELD` satisfies the ratchet (`test_no_phantom_knobs.py:119` regex `(get_settings|_settings)\(\)\.FIELD\b`). Settings field STAYS as code default.
  - **Retire-from-config.yaml (the KEY GOAL) DOES change I25:** today the ratchet requires every `Settings` field to have a `config.yaml`/FIELD_META/registry row (three-way sync). Removing migrated knobs from `config.yaml` breaks that unless the invariant evolves. **Required change:** teach the I25 ratchet a THIRD class — a "runtime-migrated" knob is `Settings`-default + DB-override, EXEMPT from the config.yaml-row requirement (perhaps a registry marker `runtime_managed=true`). The ratchet then enforces: boot knobs = full three-way; migrated knobs = Settings field + registry marker, NO config.yaml row. **This is a design sub-task of the train — do it before/with the first retire batch, or the ratchet goes red.**
- **Hot-path perf** — `config_get` is PTC-cached, but confirm the cache read is cheap enough for per-call use on decay/recall hot loops vs a frozen attr. Batch hot-path knobs into REFACTOR with a perf gate.
- **Backend reachability** — confirm brain-dynamics (backend/`_shared`) read the store via the backend surface (`backend/admin_exec/runtime_config.py`), not only core.
- **Default-in-code** — the code default remains authoritative when no DB row exists (already how config_get works).
- **Dir-scoping semantics** — per-dir override is powerful for tuning one project's retrieval without touching global; make sure migrated knobs resolve dir-scope correctly for their consumer (some knobs are inherently global — no dir context).

## Phase 0.7 — I25 ratchet evolution (prerequisite for retire)
Teach the three-way-sync ratchet the "runtime-migrated" class (registry marker `runtime_managed=true`): such a knob = `Settings` code default + DB override, EXEMPT from the config.yaml-row requirement. Update `test_no_phantom_knobs.py` / the I25 checker + `check_config_three_way_sync`. Ship BEFORE the first retire batch, else config.yaml deletions turn the ratchet red.

## Per-knob migration recipe (read → seed → retire)
For each migrated knob:
1. **Read:** swap the consumer `self._settings.X` → `config_get("X", directory, default=get_settings().X)` (or hoist once-per-batch for backend loop knobs — see Phase 0.5 cache).
2. **Seed:** write the current effective value into `runtime_config` via the **`config_set` MCP tool** (scope global unless dir-specific) — NEVER raw SQL/DB insert (standing rule: memory/config data changes go through the MCP tool, not SQL).
3. **Retire:** remove the knob's `config.yaml` row (+ FIELD_META/registry doc coupling), mark it `runtime_managed=true`. Keep the `Settings` field as code default.
4. **Test:** DB override changes behavior live (no restart); ratchet stays green; default (no DB row) still equals the old value.

## Phase 1 — Batch 1 (highest-value RUNTIME-CHEAP)
The knobs a user would actually want to tune live without a restart. Candidates (confirm against survey): decay/heat factors, retrieval weights + thresholds, feature toggles (`*_ENABLED`), retention days, secret-gate heuristic knobs (ties #30). Per knob: swap `self._settings.X` → `config_get`, keep the Settings default, add a test that a DB override changes behavior live. TDD.

## Phase 2 — RUNTIME-REFACTOR
Frozen-at-init + hot-path knobs. Add re-read-per-cycle or an invalidation subscription; perf-gate the hot-path ones.

## Phase 3 — STRUCTURAL-REINIT (optional)
Pool/model knobs that need a controlled re-init trigger (e.g. `NUM_ASTROCYTE_PROCESSES`). Only if worth the re-init machinery.

## Testing
- Per migrated knob: DB override → behavior changes without restart (live-effect test).
- I25 three-way-sync invariant stays green.
- Warmup + invalidation still correct for the new keys.
- No hot-path perf regression (micro-bench the decay/recall loops for any hot-path migration).

## Rollout / risk
- Additive + reversible: a knob with no DB row behaves exactly as today (code default). Low blast radius.
- Migrate in small PRs per subsystem batch; version-bump per PR (runtime code change).
- Ties: #43 (anchor system — some anchor knobs may migrate), #30 (secret-gate knobs), ADR-0163 (store), I25 invariant.

## Decisions (for user)
1. **Backend-local cache on `runtime_config_client.get()`?** Add a short-TTL (e.g. 5-30s) in-process cache + core-push invalidation on the backend client → then the decay/heat BATCH knobs (Batch 3) migrate as CHEAP (no per-batch-hoist refactor needed) AND all backend knobs get free liveness. **Strong rec: YES — do this FIRST, it de-risks the whole migration** (removes the N-round-trip trap) and is a small, self-contained change. Retitle as a Phase 0.5.
2. **STRUCTURAL-REINIT (Batch 4)** — out of scope for now? (rec: yes, defer — needs re-init machinery, low marginal value.)
3. **Dead knobs** (HOPFIELD_*, RECALL_QUALITY_FLOOR, retired COMET_*) — migrate-skip, or delete in a separate cleanup PR? (rec: separate delete PR, not part of this train.)
4. **Batch 1 scope** — start with retrieval weights + toggles + gates + viz (the survey's Batch 1)? Or narrower (retrieval only) for a first proof?
5. **A viz/CLI surface** to browse + set live knobs (config_set exists; a UI makes "dynamic to work with" real) — in this train or a follow-up?

## Phase 0.5 — backend-local config cache (PROPOSED, do first if decision #1 = yes)
Add a short-TTL in-process cache to `runtime_config_client.get()` (backend side) + invalidation-on-write push from core. Converts every backend knob read from an HTTP round-trip to a cached local read → collapses the CHEAP/REFACTOR distinction for backend knobs (the per-row batch trap disappears). Small, isolated, high-leverage. Sequence BEFORE Batch 3 (and ideally before Batch 1's backend knobs).
