# Yadgar Architectural Invariants

Authoritative source: this file (`docs/ARCHITECTURE_INVARIANTS.md`).
Mirrored in wiki: `yadgar-architectural-invariants`.
Anchored memory: project-scoped, `/home/max/git/yadgar`.

Last updated: 2026-05-20 (post-v5.3.7 soak findings).

---

## Purpose

Any planning for yadgar (vX.Y feature scope, refactor proposal, hotfix) MUST satisfy every invariant below. A plan that violates one is rejected and re-scoped. Override path: edit this file + propose a migration. No silent overrides.

This document was created because v5.1 module decomposition (commit `7c29a33`, 2026-05-17) silently moved drainer-deferred work into the memorize request path, and v5.3.4 (`263bfa3`) added more inline sync I/O. Result: writes feel non-async despite a working queue. The invariants below codify the lessons.

---

## Invariants

### I1. Request path is THIN

MCP tool handlers (`memorize`, `recall`, `wiki_query`, `wiki_add`, etc.) return in O(10ms) p99 from the client's perspective.

Allowed in request path:
- input validation, secrets gate
- `WriteGate` surprisal check (cheap)
- `FileQueue.enqueue` + return ack

NOT allowed in request path:
- `embeddings.encode` or any sync ML call
- vector search (`storage.search_vectors`)
- LLM call of any kind (Ollama, OpenAI, etc.)
- KG entity extraction
- multi-hop graph traversal
- `curator.curate_on_remember` (merge / find-similar)
- `EngramAllocator.allocate`, `AstrocytePool.assign_memory`, `ProspectiveMemory.*`
- `thermo.apply_session_coherence` DB writes beyond inline atomic counters
- `retriever.recall` reinjection

### I2. Drainer is the SINGLE catch-up lane

`QueueDrainer._drain_once` owns the LEAN write fan-out:
- `StorageEngine.insert_memory`
- `EmbeddingEngine.encode_document_enriched`
- `StorageEngine.insert_vector`
- `KnowledgeGraph.extract_entities_typed`
- `FileQueue.archive`

NOT drainer's job:
- `curator.curate_on_remember`
- `EngramAllocator.allocate`
- `AstrocytePool.assign_memory`
- `retriever.recall` (reinjection)
- `LLM conflict resolver`

Those run on a SEPARATE deferred pass (`ConsolidationScheduler` cycle, or a new low-priority background loop). Drainer must not compete for the DB connection pool with request-path read tools.

### I3. Opt-in features short-circuit BEFORE expensive setup

Any env-flag-gated feature (e.g. `YADGAR_CONFLICT_RESOLVER`) must check the flag in an O(1) path BEFORE module import, client construction, or DB query. Off = zero overhead. No "build the client, then check the flag".

### I4. ML compute is `asyncio.to_thread` or drainer-thread ONLY

`SentenceTransformer.encode` is pure sync and blocks the event loop. Every call in an async context must be `await asyncio.to_thread(model.encode, ...)`. Drainer thread (sync) may call directly. Never inline in a coroutine.

### I5. Module decomposition NEVER moves work across boundaries

When splitting a module, preserve the sync / async / queue topology of every call. Decomposition is structural; it must not change WHEN or WHERE work runs.

The v5.1 decomp (`7c29a33`) inlined drainer-only ops into the memorize tool — banned pattern. Future refactors prove they did not regress topology by listing every moved call and its before/after thread/context.

### I6. No double-pay

A write executes heavy ops once. If the inline fallback path runs curator/engram/astrocyte, drainer replay must not re-run them. If drainer runs them, the inline path must not. Use idempotency markers (memory record flags) to detect prior application.

### I7. Queue is the durability boundary

`FileQueue` atomic-rename = durability contract. The sync fallback path is a CRASH RISK unless it enqueues first and then processes. Never process-then-enqueue. Crash mid-process loses the write.

Sequence on fallback: `enqueue → mark in-progress → process → archive`. Crash before archive = drainer picks up on next start. Crash before enqueue = data loss.

### I8. Backpressure must be observable

`/metrics` and `memory_stats` MUST surface:
- `yadgar_queue_depth` (gauge)
- `yadgar_drainer_lag_ms` (histogram)
- `yadgar_dlq_size` (gauge)
- `yadgar_drain_cycle_duration_ms` (histogram)

Alert thresholds defined in `docs/configuration.md`. Any new write-path code MUST consider impact on these numbers before merge.

### I9. New write-path code budget ≤5ms p50

Hard latency budget. Code that exceeds it moves to drainer or consolidation. Measured via existing perf tests; new tests added per feature.

### I10. Overrides are explicit

Future plans that want to override an invariant must:
1. Edit this file with the override + reasoning + migration plan.
2. Reference the override commit in the planning PR description.
3. Get explicit user approval — invariant overrides are not a sub-decision.

No silent drift. If an invariant is wrong, prove it wrong here first.

### I11. Heavy stable artifacts live in backend, not core

ML models, datasets, large reference data — anything multi-hundred-MB that changes monthly-or-slower — belongs in the **backend image** (or is fetched into a runtime volume). NEVER bake into the **core image**.

**Why:** core rebuilds on every code change. Stable artifacts in core = every deploy re-ships the model = slow pulls, bloated registry, cache misses for every developer.

**Rule of thumb:** if rebuilding the artifact has a different release cadence than rebuilding code, it does NOT belong in the core image.

**Check:** `docker history docker.io/openfantasy/yadgar:VER` — large layers should only be Python deps (`pip install`). No `COPY models/`, no `RUN download_model` step in the core Dockerfile. Those go in `Dockerfile.backend` or a runtime-mounted volume.

**Violation history:** models were baked into core (~pre-v5.x), bloated it >2GB, had to be manually extracted. Backend currently at 6.78GB (v5.4 F0 scope) — fix it there, not by smuggling into core.

### I12. Measure before optimize

Any perf claim, cache layer, threadpool, batching, async refactor, or "we should be faster here" change MUST be preceded by stage-level profiling data with p50/p95/p99 timing. No "I think this is slow because of X" — instrument first, decide second.

**Required artifacts before a perf-PR ships:**
- Profile output (or `/metrics` histogram) showing which stage is hot
- Before / after numbers in the PR description
- Test that asserts no regression on the measured stage

**Why:** most yadgar "slowness" is sync ML in async contexts (see I4), not DB. Adding caches / threadpools without profiling = guessing. Soak observation 2026-05-20 (read-cache discussion) — cache layer was proposed but profile data was missing, so cache design was deferred until measurement.

**Check:** PR adds a cache, threadpool, or batching → ask for the profile. No profile → not ready. PR claims "X is slow" → ask which stage and how measured.

Paired with I8 (backpressure observable): I8 requires runtime metrics; I12 requires using them before optimizing.

### I13. Bounded file + function complexity

Hard + soft caps so diffs stay reviewable AND decomposition doesn't drift into I5 violations.

**Function caps (per function):**
- Cyclomatic complexity ≤ 15 (hard) / ≤ 10 (soft)
- LOC ≤ 150 (hard) / ≤ 80 (soft)
- Parameter count ≤ 8 (hard) / ≤ 5 (soft)
- Nesting depth ≤ 4 (hard)

**File caps:**
- LOC ≤ 1000 (hard) / ≤ 500 (soft)
- Public symbols ≤ 30 (soft)

**Class caps:**
- Methods ≤ 30 (soft)
- Instance attributes ≤ 15 (soft)
- Inheritance depth ≤ 3 (hard)

**Test files exempt** from LOC + parameter caps. Cyclomatic + nesting still enforced.

**Justified-cohesion override** for soft caps. ALL three must hold:
1. Every branch is part of a single cohesive flow
2. Decomposition would force shared mutable state across helpers, OR move work across thread/async boundaries (forbidden by I5), OR lose error-handling context
3. Override documented inline: `# noqa: C901 – cohesive: <reason>` + one-line comment

Hard caps allow NO override. If hit, the decomposition design must prove I5 preservation explicitly.

**Critical anti-pattern (per v5.1 incident):** decomposition that creates implicit shared state across helpers OR moves work across thread/async boundaries is WORSE than the mega-function. The v5.1 module decomp (commit `7c29a33`) violated I5 while "fixing" complexity. **Decomposing without preserving topology = banned.**

**Enforcement:**
- pre-commit `ruff check --select=C901 --max-complexity=15` (cyclomatic + max-args)
- Add custom `check-complexity` pre-commit hook (sibling to existing `sync-version` + `check-backend-bump`) for LOC + nesting + file-size
- Soft caps warn; hard caps block commit
- Existing violations catalogued in `docs/complexity-audit.md` (P12)

**Why:** unreviewable diffs hide bugs. But over-decomposition (v5.1) moves work around without simplifying. Caps + I5 together = readable AND correct.

---

## Patterns Library

Validated mechanisms shipped in yadgar. Patterns differ from invariants: invariants describe WHAT must hold; patterns describe HOW a shipped mechanism works. Any plan adding similar capability MUST follow these patterns OR explicitly justify deviation in the PR description (per I10).

### CB-1. Circuit breaker on external dependencies

Shipped: v5.3.10 (N4) — `RemoteMLClient` `/rerank` endpoints.

**State machine:** per-endpoint `CLOSED → OPEN → HALF_OPEN`. Open after `YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive failures (default 3). Stay open `YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC` (default 60s). Single probe on cooldown end → success closes, failure re-opens.

**Applies to:** HTTP/RPC to a slower-than-request-path service whose failure shouldn't propagate as latency to the caller. Current users: `/rerank/ce`, `/rerank/nli`, `/rerank/pair`. Future targets: LLM clients (Ollama), external embedding services, any backend endpoint added that isn't correctness-critical.

**Does NOT apply to:** SurrealDB queries (fast + correctness-critical — failure must propagate). Health probes (those ARE the probe).

**Caller contract:** when `score_X()` returns `None`, caller MUST degrade gracefully (skip stage, return pre-rerank order, never crash). See `yadgar/retrieval/_reranking_cross_encoder.py` + `_reranking_nli.py` for the canonical None-guard pattern.

**Code:** `yadgar/ml_client.py::_CircuitBreaker` + per-endpoint instances on `RemoteMLClient`.
**Tests:** `yadgar/tests/test_circuit_breaker.py` (7 tests).
**Env:** `YADGAR_CIRCUIT_BREAKER_ENABLED` (default 1), `_FAILURE_THRESHOLD` (3), `_OPEN_DURATION_SEC` (60).

**Why this matters (don't break):** v5.3.9 `BindsTo → Wants` decoupled core from backend lifecycle. Without CB-1, core busy-loops retrying against a struggling backend (the v5.3.10 CPU incident). CB-1 is the architectural pair to the decouple — removing it re-introduces the CPU regression.

**Banned regressions:**
- Removing the breaker without equivalent fault-isolation (rate limiter, bulkhead, exponential backoff).
- Disabling per-endpoint isolation (one breaker for all endpoints — a slow CE would block NLI/pair).
- Bypassing the breaker in "retry harder" patches.

### Pattern slots (planned, not yet shipped)

- **CB-2** — bulkhead / connection-pool isolation. Trigger: if backend connection-pool exhaustion surfaces in v5.4 P11 metrics.
- **CB-3** — rate limiter on hook firing. Trigger: v5.3.10 root cause was hook volume driving rerank load; if hook traffic keeps stressing backend even with CB-1, add upstream throttle.
- **DOC-1** — branch-routing canonical-NULL pattern. Trigger: once W1 ships (`wiki_add` `branch_hint` arg), document the symmetric "branch=None means canonical" rule for `wiki_read` callers.

---

## Current violations (as of v5.3.7, snapshot 2026-05-20)

| Site | Invariant | Notes |
|---|---|---|
| `yadgar/server/tools/memorize.py:154` `embeddings.encode` | I1, I4 | sync ML in fallback request path |
| `yadgar/server/tools/memorize.py:229` `curator.curate_on_remember` | I1, I2 | curator inlined v5.1 (commit 7c29a33) |
| `yadgar/server/tools/memorize.py:310` `pool.assign_memory` | I1, I2 | astrocyte inlined v5.1 |
| `yadgar/server/tools/memorize.py:321/331` `prospective.*` | I1, I2 | prospective inlined v5.1 |
| `yadgar/server/tools/memorize.py:337` `engram.allocate` | I1, I2 | engram inlined v5.1 |
| `yadgar/server/tools/memorize.py:366` `thermo.apply_session_coherence` | I1 | DB write per request |
| `yadgar/server/tools/memorize.py:392` `retriever.recall` reinjection | I1, I2 | recall inside memorize |
| `yadgar/conflict_resolver.py:149` `httpx.post` | I3, I4 | sync 30s timeout if YADGAR_CONFLICT_RESOLVER=on |
| Drainer `_apply()` semantics | I2, I6 | drainer replays the full memorize tool, not lean inserts → drainer cycle pays full cost; if it falls behind, fallback hits sync path |

---

## Candidate plans to resolve current violations (v5.3.8 hotfix or v5.4 perf scope)

Ordered by leverage. Pick a subset per release.

### P1. Split memorize into thin-enqueue + heavy-drain helpers

Refactor `yadgar/server/tools/memorize.py` into:
- `_memorize_enqueue(payload)` — request path, ~50 lines: validation + secrets + WriteGate + enqueue + ack.
- `_memorize_apply_lean(payload)` — drainer path, ~150 lines: insert_memory + encode + insert_vector + extract_entities + archive. THIS is what `FileQueue._apply` calls.
- `_memorize_apply_consolidation(memory_id)` — deferred pass: curator merge, engram allocate, astrocyte assign, reinjection, postmortem boost.

Drainer cycle now finishes in O(20ms) per item. Consolidation runs every N seconds or at queue-idle.

### P2. Move deferred ops to ConsolidationScheduler

The existing `ConsolidationScheduler` (30min cycle) already does periodic work. Add a fast-tier sub-cycle (5–15s) that picks up memories where `last_consolidated IS NONE` and runs curator/engram/astrocyte/reinjection.

Pro: re-uses existing scheduler. Con: longer staleness window for "recently written" memories (mitigated by tier-1 fast cycle).

### P3. Wrap sync ML in `asyncio.to_thread`

For any path that runs in an async context (FastAPI handlers, hooks endpoint, etc.), every `model.encode(...)` becomes `await asyncio.to_thread(model.encode, ...)`. Drainer stays sync.

Quick win; 0 architectural change. Survey: `grep -n "\.encode(" yadgar/ -r`.

### P4. C4 conflict resolver gate hoist

`yadgar/conflict_resolver.py`: check `YADGAR_CONFLICT_RESOLVER` env at module import time. If off, the class is a no-op stub. No httpx.Client built, no Ollama URL resolved.

### P5. Backpressure metrics surfacing

Add `yadgar_queue_depth`, `yadgar_drainer_lag_ms`, `yadgar_dlq_size`, `yadgar_drain_cycle_duration_ms` to `/metrics`. Expose in `memory_stats` MCP tool output. Wire alert thresholds in `docs/configuration.md`.

Required by I8 — currently we're flying blind on drainer health.

### P6. Drainer concurrency

If SurrealDB connection pool allows, multiple drainer workers (`YADGAR_DRAINER_WORKERS`, default 1). Each worker pulls from queue; SurrealDB sequencing handled at the DB layer.

Risk: write-order semantics, contention. Bench before shipping.

### P7. Reinjection becomes opt-in

`YADGAR_REINJECT_ON_WRITE=on` (default OFF). Most users likely never asked for write-time reinjection. Cheap to drop from hot path.

### P8. Idempotency markers for I6

Add `consolidation_state` field on memory record (NULL / drainer-done / consolidation-done). Drainer sets `drainer-done`; consolidation pass sets `consolidation-done` and skips already-done. Required if both inline-fallback and drainer-replay paths exist.

### P9. Image partitioning audit for I11

During v5.4 F0 (backend image bloat fix), enforce I11: every layer over ~100MB justified or moved. Add `docker history` check to release-readiness CI. Confirm no model weights / large data in core image.

### P10. Read-path stage timing for I12 (folded into P11)

Originally proposed standalone; now subsumed by P11 below.

### P12. Complexity audit — one-time catalog (PRE-P1)

NOT auto-decompose. Catalog only.

**Pass output:** `docs/complexity-audit.md` table with columns:
- `file:line` function/file
- Current cyclomatic, LOC, params, nesting (vs I13 caps)
- Hard-cap or soft-cap violation
- Decomposition risk per I5 (HIGH = crosses thread/async boundary or shares mutable state; MEDIUM = parameter-passing rewrite; LOW = mechanical split)
- Proposed action: `decompose-low-risk` / `decompose-with-topology-proof` / `justify-cohesion (noqa)` / `defer`

**Risk-tiered scheduling:**
- LOW risk → decompose in v5.4/v5.5 bundles, ~5 functions per PR, each with before/after test parity
- MEDIUM risk → per-PR explicit topology proof (every moved call's before/after thread/context per I5)
- HIGH risk → don't touch without metrics evidence it matters (P11-gated)
- `justify-cohesion` → one-line PR adds `# noqa: C901 – cohesive: <reason>` annotation

**Ordering: PRE-P1.** Reason — informs how big the memorize-split (P1) needs to be + which functions can move safely. Audit is cheap, informative, doesn't need observability data (P12 input = static analysis, not runtime).

**Tools:** existing `ruff` + custom AST script for LOC/nesting/file-size. Output committed to repo + referenced from `docs/ARCHITECTURE_INVARIANTS.md` current-violations table.

### P11. Observability v1 — UNIFIED metrics framework (PRE-REQUISITE for all perf PRs)

Subsumes P5 + P10. SINGLE bundle, FIRST v5.4 PR. Without it, I12 (measure before optimize) cannot be enforced — all subsequent perf work is blind.

Every metric uses `prometheus_client.Histogram` or `Gauge`, surfaced at `/metrics`, also via `memory_stats`.

**Write path:**
- `yadgar_queue_depth`, `yadgar_dlq_size` (gauge)
- `yadgar_drainer_lag_ms`, `yadgar_drain_cycle_duration_ms` (histogram)
- `yadgar_drain_stage_ms{stage}` (histogram, labels: `insert`, `encode`, `vector`, `entities`, `archive`)
- `yadgar_writegate_outcome{outcome}` (counter)

**Read path:**
- `yadgar_recall_duration_ms`, `yadgar_recall_result_count` (histogram)
- `yadgar_recall_stage_ms{stage}` (histogram, labels: `embed_query`, `bm25`, `hnsw`, `ppr`, `spreading_activation`, `cross_encoder`, `nli`, `contextual_prefix`, `rerank_final`)
- `yadgar_wiki_query_duration_ms` + `_stage_ms{stage}` (same shape)

**Embedding:**
- `yadgar_encode_duration_ms{model}` (histogram)
- `yadgar_encode_queue_depth` (gauge, if to_thread queue added)
- `yadgar_encode_cache_hit_rate` (gauge, if query-embedding cache shipped)

**KG / curator / engram:**
- `yadgar_entity_extract_duration_ms`, `yadgar_curator_duration_ms`, `yadgar_engram_allocate_duration_ms`, `yadgar_astrocyte_assign_duration_ms` (histogram)
- `yadgar_curator_merge_outcome{outcome}` (counter: `merged`, `linked`, `noop`)

**LLM (C4 conflict resolver):**
- `yadgar_llm_call_duration_ms{provider,model,purpose}` (histogram)
- `yadgar_llm_decision{outcome}` (counter: `add`, `update`, `delete`, `noop`, `error`)

**MCP transport + auth:**
- `yadgar_mcp_request_duration_ms{tool}` (histogram)
- `yadgar_mcp_auth_check_duration_ms` (histogram)
- `yadgar_mcp_request_count{tool,status}` (counter)

**Database:**
- `yadgar_surrealdb_query_duration_ms{op}` (histogram)
- `yadgar_surrealdb_connection_pool_wait_ms` (histogram)
- `yadgar_surrealdb_pool_active` (gauge)

**Process:**
- `yadgar_process_rss_bytes`, `yadgar_process_cpu_percent`, `yadgar_process_open_fds` (gauge)
- `yadgar_python_gc_duration_ms{generation}` (histogram)

**Subagents:**
- `yadgar_subagent_dispatch_count{agent_type}` (counter)
- `yadgar_subagent_capture_rate` (gauge — findings parse success / dispatched)

**Viz:**
- `yadgar_viz_api_graph_duration_ms` (histogram)
- `yadgar_viz_sse_clients` (gauge)
- `yadgar_viz_dbsize_sample_duration_ms` (histogram)

**Ships with:** Grafana dashboard JSON + alert rules YAML committed to `docs/observability/`. Decorator helper in `yadgar/observability/timing.py` for per-stage histograms. Backward-compatible: missing metrics return 0, no client breakage.

**Cost:** ~20 instrumentation sites, <1µs/observe overhead, ~30 lines per stage via decorator. Negligible runtime cost.

**Unblocks:**
- I12 enforceable (no perf PR without profile data)
- P3 asyncio.to_thread can prove before/after
- P1+P2 memorize split can prove drainer cycle ≤ 20ms target
- v5.5 cache decisions (Q2 B vs A) data-driven
- regression detection via alert thresholds

**Ordering:** ships FIRST in v5.4, before P1/P2/P3 etc. Reason: I12 says measure first.

---

## Decision log

- 2026-05-20: file created. Triggered by user-reported write-speed regression during v5.3.7 soak. No invariants overridden yet.
- 2026-05-20: I11 added (image partitioning) after recall of past model-in-core bloat incident.
- 2026-05-20: I12 added (measure before optimize) after read-cache proposal lacked profile data.
- 2026-05-20: P11 added (Observability v1) unifying P5 + P10; ordered FIRST in v5.4 per I12.
- 2026-05-20: I13 added (bounded complexity, hard+soft caps) + P12 (complexity audit). P12 ordered PRE-P1 — informs memorize-split scope. Advisor unavailable at decision time; numbers (15 cyclomatic / 150 LOC hard / 80 LOC soft) refined as audit data lands.
- 2026-05-21: Patterns Library section added (introductory CB-1 for circuit breaker shipped v5.3.10). Patterns ≠ invariants; both must be checked by future planning.

---

## Cross-references

- Wiki: `yadgar-architectural-invariants` (mirror of this file).
- Wiki: `yadgar-write-pipeline-surprise-gated` (original write-flow spec).
- Wiki: `yadgar-roadmap-future-improvements` (current roadmap, v5.4/v5.5 trajectory).
- Anchor: project-scoped memory at `/home/max/git/yadgar` tagged `_architectural-invariants`.
- Source memories: v5.1 commit `7c29a33` (regression), v5.3.4 commit `263bfa3` (C4 inline), soak observation 2026-05-20 (mem id 493702).
