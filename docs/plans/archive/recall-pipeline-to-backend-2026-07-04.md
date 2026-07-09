# Plan: Move the recall pipeline from core into a stateless backend `/recall` endpoint

**Status:** DRAFT — **CONDITIONAL / measure-first gated.** Do NOT build until the gate in §7 passes.
**Date:** 2026-07-04
**Task:** #85 (nimble-core: PPR/spreading → stateless backend endpoint), taken to full scope (the whole recall pipeline, not just PPR/spreading).
**Related:** ADR-0030 (recall is surreal-IO bound), ADR-0033 (contested baseline, OPEN), ADR-0034 (obs standard + core→backend traceparent — SHIPPED P0), ADR-0038/0043 (CE onnx-int8 REJECTED, torch stays), #28 (CE top-k decouple, in-flight), #88 (recall output cache).

---

## Headline recommendation (BLUF)

**CONDITIONAL GO — measure first.** The migration is *cheap to build* (the whole `yadgar` package is already installed in the backend image; this is "add a route + gut core to forward", not a code port) but the *value is unproven and plausibly small*. The two dominant recall costs — cross-encoder (~15-16s, 56-78%) and spreading (~3-4s) — are **compute**, and CE **already runs backend-side**. Moving the pipeline saves neither. What the migration actually removes is **cross-container round-trip + serialization of intermediate candidate data between core and the backend host**.

From the measured v5.101 waterfall, the *confirmed* boundary-attributable win is only ~1s (build_results + get_memories ~720ms + wiki.query ~310ms, both cross-container DB reads that become localhost). The whole go/no-go swings on **one un-attributed number: the ~6.2s un-spanned MCP-wrapper region** (offload boundary + heat-boost writes + result formatting), which is CE-correlated.

- If that 6.2s is mostly **DB writes / candidate serialization crossing the container boundary** → migration wins ~3-7s (15-30%) → **GO**.
- If it is **CE-adjacent compute** → migration saves ~1s (~4%) on THE core feature with high blast radius → **NO-GO / defer** behind the CE and spreading compute levers (#28, #32, #88).

**Recall is THE core feature; a regression is severe. Do not let "easy to build" become "worth building." Attribute the 6.2s first (§7), then decide.**

---

## 1. Context

`recall` is the flagship MCP tool. Today the entire recall *compute* pipeline runs in the **core** process; the **backend** container is a thin ML service (embed + rerank) that also co-hosts the SurrealDB server. The user directive (task #85 at full scope): move the whole pipeline — fusion, spreading/PPR, CE rerank, NLI, MMR, heat/offload, profile/belief, formatting — into a stateless backend `/recall` endpoint, leaving core a thin async router that forwards the MCP call.

The hypothesis behind #85 is that co-locating compute with data kills per-stage RPC latency and the N+1 round-trips. This plan tests that hypothesis against the real code and the measured trace numbers, and finds the hypothesis **partially true but likely smaller than assumed** — because the single biggest cost (CE) already lives backend-side, and DB "co-location" is really localhost-HTTP, not in-process.

The #85 charter is explicitly **measure-first**. This plan honours that: the recommendation is gated on a specific measurement, not on architectural elegance.

### Deployment topology (verified from code)

- **Backend container** (`Dockerfile.backend`): runs the SurrealDB server (`:8000`) **and** the FastAPI embed/rerank service (`:8001`). Installs the full `yadgar` package **with** `[ml]` (`pip install "/app[ml]" uvicorn`, `Dockerfile.backend:18`) + torch CPU.
- **Core container** (`Dockerfile`): runs the MCP server + viz. Installs `yadgar` **without** `[ml]` (`pip install /app`, `Dockerfile:10`).
- Core reaches the DB **cross-container** over HTTP: `YADGAR_DB_URL=http://yadgar-backend:8000` (`docker-compose.yml:83`, `README.md:197`). Core's `StorageEngine` runs in **server mode** — a shared `httpx.Client` to `:8000` (`yadgar/storage/__init__.py:221-268`).
- Core reaches models over HTTP: `RemoteMLClient` → backend `/rerank` (`:8001`).
- **Precise DB statement (load-bearing for the whole latency argument):** the SurrealDB *server process is inside the backend container*. Core reaches it cross-container. A recall pipeline *running in the backend* reaches the **same** DB over **localhost** (still HTTP, not in-process — surrealkv holds an exclusive file lock and does not support concurrent access, `storage/__init__.py:302`, so the backend pipeline cannot embed-open the file the SurrealDB server owns). The lever is *localhost vs cross-container HTTP + keeping fat intermediate data inside one container*, **not** "DB becomes in-process."

---

## 2. Current pipeline map

Prod path is `_fanout_recall` (`yadgar/server/tools/recall.py:651`, gated by `UNIFIED_RECALL_ENABLED=True`, default True). Note: the fanout `MemoryProvider.candidates()` still invokes the **legacy `Retriever.recall()`** internally (`yadgar/retrieval/core.py:484`) for the memory signal — so the full FTS+vector+PPR+spreading+WRRF+CE(+NLI) pipeline **is** exercised in prod, wrapped by the fanout cross-type fusion.

### Stage table (prod fanout path + the legacy memory sub-pipeline it calls)

| # | Stage | File:line | Runs in | Data touched | Cross-container hop today |
|---|---|---|---|---|---|
| 1 | Query embed (`encode_query`) | `retrieval/core.py:575` | CORE | in-process EmbeddingEngine (MiniLM, small, no `[ml]`) | none (in-process) |
| 2a | Memory candidate fetch (`MemoryProvider.candidates` → `Retriever.recall`) | `server/tools/recall.py:307`, `retrieval/providers/memory.py:62`, `retrieval/core.py:484` | CORE | SurrealDB (FTS + HNSW KNN) | **DB reads → :8000 (cross-container)** |
| 2b | Wiki candidate fetch (`WikiProvider.candidates`) | `server/tools/recall.py:317` | CORE | SurrealDB (BM25 + vector) | **DB reads → :8000** |
| 3 | PPR (legacy sub-pipeline) | `retrieval/core.py:586`, `scoring.py:244` | CORE | builds ephemeral `networkx.DiGraph` from DB entity/rel rows (`graph_helpers.py:18`) | **DB reads → :8000** (batched v5.99) |
| 4 | Spreading activation | `retrieval/core.py:589`, `scoring.py:265` | CORE | same ephemeral graph; BFS over DB rows | **DB reads → :8000** (batched v5.99/v5.104) |
| 5 | Temporal scoring | `retrieval/core.py:592` | CORE | SurrealDB temporal query | **DB read → :8000** |
| 6 | WRRF / convex fusion (`_fuse_scores`) | `retrieval/core.py:597` | CORE | pure python | none |
| 7 | Build initial results (`_build_initial_results`) | `retrieval/core.py:600` | CORE | SurrealDB gets full memory dicts | **DB reads → :8000 (~720ms measured)** |
| 8 | Heuristic rerank | `retrieval/reranking.py:94` | CORE | pure python | none |
| 9 | Cross-encoder rerank (`_rerank_cross_encoder`) | `retrieval/reranking.py:137`, `_reranking_cross_encoder.py:129` | CORE calls → **BACKEND compute** | GTE-ModernBERT model | **`/rerank ce` → :8001 (×3 batches, ~15-16s COMPUTE)** |
| 10 | NLI rerank (legacy; default OFF) | `retrieval/reranking.py:147`, `_reranking_nli.py:26` | CORE calls → **BACKEND compute** | NLI model | `/rerank nli → :8001` (when enabled) |
| 11 | Multi-passage rerank | `retrieval/reranking.py:168` | CORE | pure python | none |
| 12 | Profile/belief merge | `retrieval/reranking.py:179` | CORE | SurrealDB (`_search_profiles_and_beliefs`) | **DB reads → :8000** |
| 13 | MMR diversity | `retrieval/reranking.py:203`, `_reranking_mmr.py:102` | CORE | reuses in-memory query embedding (no re-fetch) | none |
| 14 | Adversarial / rules / engram / metacognition | `retrieval/reranking.py:215-266` | CORE | mostly pure python (rules may hit DB) | mostly none |
| 15 | Cross-type fusion (fanout: `fuse_candidates`) | `retrieval/providers/fusion.py:175`; CE at `:56` | CORE calls → **BACKEND** | in-memory candidates + CE | **`/rerank ce → :8001`** (fanout's own CE pass) |
| 16 | Content dedup + trim `[:max_results]` | `server/tools/recall.py:347-348` | CORE | pure python | none |
| 17 | Heat boost + offload side-effects (`_apply_recall_side_effects`) | `server/tools/recall.py:379,564,664,800` | CORE | **synchronous batched** `storage.boost_memories_access(ids, now)` | **DB WRITE → :8000 (blocking, cross-container)** |

### Round-trip count per recall (RemoteMLClient / Docker prod)

- **Model RPCs to :8001:** 1 CE (fanout) or 2 (legacy CE+NLI when enabled). CE is dominant (~15-16s) but is **compute**, unaffected by relocation.
- **DB round-trips to :8000 (cross-container):** many — candidate FTS/vector fetch, PPR/spreading graph rows, temporal, build_results (~720ms), profile/belief, and the blocking heat/offload write. **These are the round-trips the migration actually removes** (they become localhost).

### Stateful bits (what would have to move / rebuild)

- **networkx graph:** ephemeral, rebuilt per-request from DB rows (`graph_helpers.py:18`), discarded after. **No persistent in-process state → nothing to migrate.** De-risks the "rebuild caches in backend" concern entirely.
- **CE / embed caches + shadow counters:** already live backend-side (the backend loads the models and caches — `embed_cache_*`, `ce_cache_*` metrics in `embed_service.py`). The core-side `RemoteMLClient` is a thin RPC shim. Moving the pipeline keeps these where they already are.
- **DB connection:** core's `StorageEngine` httpx client to :8000 would be re-pointed to localhost in the backend process.

---

## 3. Target architecture

### 3.1 Backend `/recall` endpoint contract

**`POST /recall`** on the backend service (`:8001`), Bearer-authed via the existing `_require_admin_token` dependency (`embed_service.py:324`) — same auth as `/embed` and `/rerank`. Stateless per request; the backend reads/writes its own co-located SurrealDB (localhost `:8000`) and runs models in-process via `LocalMLClient` (no `/rerank` self-hop).

Request (Pydantic, all knobs carried explicitly so the endpoint is config-stateless):

```
RecallRequest {
  query: str
  directory: str
  current_branch: str | None
  default_branch: str | None
  max_results: int
  min_heat: float
  type: "all" | "memory" | "wiki"
  profile: str | None
  mode: str | None            # landscape etc.
  stage_overrides: dict | None
  knobs: {                    # explicit — no reliance on a shared Settings singleton for request-varying values
    TOP_K, RERANKER_TOP_K, CROSS_ENCODER_TOP_K, CROSS_ENCODER_ENABLED,
    NLI_RERANKING_ENABLED, PPR_DAMPING, GRAPH_SPREADING_DECAY, GRAPH_MAX_HOPS,
    MMR_LAMBDA, UNIFIED_RECALL_ENABLED, ...
  }
  traceparent: (via HTTP header, injected by core's HTTPXClientInstrumentor)
}
```

Response:

```
RecallResponse {
  results: [ <formatted memory/wiki dicts, exactly the current recall output shape> ]
  trace: { stage_ms: {...}, candidate_counts: {...}, ce_batches: n, cache: {...} }  # optional, debug
}
```

**Side-effects (heat/offload):** run **inside** the backend `/recall` handler, against localhost SurrealDB — the batched `boost_memories_access` write no longer crosses the container boundary. Contract decision: whether the write blocks the response (as today) or is fire-and-forget stays as-is initially (today it is synchronous/blocking, `recall.py:379-46`); revisit as a separate optimization.

### 3.2 Core's new (thin) role

Core keeps: MCP protocol handling, auth/session, hooks, `memorize`/`wiki`/`block`/`checkpoint`/`anchor` writes, consolidation, drainer, viz — everything that is not recall *compute*. Core's `recall` MCP handler becomes:

1. Parse + validate MCP args (unchanged).
2. Assemble `RecallRequest` (query + directory + branch + max_results + resolved knobs from its Settings).
3. `httpx` POST → backend `/recall` (traceparent auto-injected).
4. Return the response results to the MCP caller.

All of `yadgar/retrieval/*`, fusion, spreading/PPR, reranking, MMR, NLI, profile/belief, heat/offload compute **leaves** core's request path (code can stay in the package — see §4 — but is only *invoked* backend-side in Docker mode).

**stdio / daemon (no-backend) mode:** must keep working. `LocalMLClient` already lets core run the whole pipeline in-process today. The dual-path flag (§5) preserves the local path: when there is no backend URL, core runs the pipeline locally exactly as now.

### 3.3 Observability continuity (ADR-0034)

**Task premise refuted — observed state wins.** ADR-0034's P0 (core→backend W3C traceparent propagation) is **SHIPPED (v5.101)** and **tested**: `HTTPXClientInstrumentor().instrument()` is hoisted into `setup_tracing()` (`tracing.py:569-572`, also `server/_app.py:41-45`), the backend runs `FastAPIInstrumentor.instrument_app()` (`embed_service.py:536`), and `tests/test_backend_traceparent_e2e.py:40-98` asserts a shared trace_id across the boundary. The obs section is therefore a **topology note, not a gap to fix**:

- **Today:** core emits per-stage recall spans (`retrieval.pipeline.run` boundary + per-stage `@observe(tier="stage")` + `yadgar_recall_stage_ms` histograms, `pipeline.py`, `reranking.py:27`), and a `backend.rerank.{mode}` child span for each CE/NLI hop.
- **After migration:** core emits **one passthrough span** wrapping the `/recall` POST; **all per-stage spans + `yadgar_recall_stage_ms` metrics move backend-side** (the `@observe` decorators travel with the code — already applied, unchanged). The existing `HTTPXClientInstrumentor` on core's new `/recall` client stitches core's passthrough span to the backend pipeline spans into **one connected trace tree** — the same mechanism that already stitches `/rerank`.
- **ADR-0041/0042 caution:** the backend already runs the obs standard; adding a data-returning `/recall` route must respect the log-subsystem `@observe` exemptions (`log_config.py` glob-exempt) and the `_SpanEndFilter` on the log ring — no new function in the recall path may re-open the span→log feedback loop. The recall code is already `@observe`-annotated and shipped, so this is a "don't regress" note.
- **Metric cardinality:** `yadgar_recall_stage_ms` label set is unchanged; it just emits from the backend process. No new series.

### 3.4 Config / knob flow

Today many recall knobs are read **core-side, pre-RPC** (fanout pool `max_results*3` at `recall.py:280`; `RERANKER_TOP_K`, `CROSS_ENCODER_TOP_K` at `reranking.py`; `PPR_DAMPING`, `GRAPH_SPREADING_DECAY` in the stage modules). Model-selection knobs (`GTE_RERANKER_MODEL`, `NLI_MODEL`) are already read backend-side at reranker init.

**Design decision: carry request-varying knobs in the `RecallRequest.knobs` block** (explicit), so the endpoint is stateless w.r.t. per-call retrieval shape and core keeps authority over profile/tier selection. Deployment-fixed knobs (model names, cache sizes) stay read from the backend's Settings singleton (yaml/env) — they are already backend-side.

**Three-way-sync test** (`test_config_three_way_sync.py`): every Settings field must be in `FIELD_META` **and** `_REGISTRY`, or allow-listed. Moving where a knob is *read* does not change its registration; but any **new** env var the backend needs (e.g. to select recall-in-backend) must satisfy the sync test. `#28` (CROSS_ENCODER_TOP_K/RERANKER_TOP_K registry rows) must land first (see §6) so those knobs are cleanly registered before they become part of the `/recall` contract.

---

## 4. Code-sharing boundary (the effort linchpin — DECISIVE)

**The whole `yadgar` package, including `yadgar.retrieval.*`, is ALREADY installed in the backend image** (`Dockerfile.backend:18` `pip install "/app[ml]"`). The backend today imports `yadgar.config.Settings` and `yadgar.backend.ml_client.LocalMLClient` (`embed_service.py:371-374`). It does **not** yet *invoke* `yadgar.retrieval.reranking`/`fusion`/`scoring`, but those modules are importable in-image right now.

**Therefore this is NOT a code port and NOT a duplicate-vs-shared-lib problem.** The migration is:

1. Add a `/recall` route to `embed_service.py` that instantiates the **existing** `Retriever` (+ fanout orchestration) with a **`LocalMLClient`** (backend already loads torch/models) and a **`StorageEngine` pointed at localhost `:8000`**.
2. Wire the existing `@observe`-annotated pipeline unchanged.
3. Gut core's recall handler to forward (§3.2), behind the dual-path flag (§5).

No module needs to be relocated or duplicated. The shared code stays one copy in the package; only *where it runs* changes. This collapses the largest anticipated risk.

**Caveat:** confirm no core-only import creeps into the recall path that is absent in the backend image (both install the same package, so this should hold, but the pipeline currently assumes a core-configured `Retriever`; the backend must construct an equivalent). This is a construction/wiring task, not a dependency task.

---

## 5. Migration strategy

### Recommended: **big-bang implementation, dual-path flag, atomic cutover** — NOT stage-by-stage.

**Why not incremental (stage-by-stage, e.g. spreading first per #85's literal framing):** the stages feed each other (fetch → fusion → rerank → nli → mmr → side-effects). Relocating one stage while its neighbours stay in core creates **core→backend→core ping-pong** — *more* boundary crossings, net latency-negative until the migration is complete. The code-sharing finding (§4) means there is no effort saving from slicing it; incremental only adds transient regressions on THE core feature. **Push back on "spreading first"** — build the whole `/recall` at once.

**Dual-path flag:** a top-level setting (e.g. `RECALL_BACKEND_ENABLED`) selects, in core's `recall` handler:

- **OFF (default during migration):** core runs the pipeline locally exactly as today (also the permanent path for stdio/no-backend mode).
- **ON:** core forwards to backend `/recall`.

Both paths must return byte-identical results for the same input (contract test: run the same corpus + queries through both, assert equal ranked ids + scores). This gives an **atomic, reversible cutover** and a built-in A/B harness for the latency measurement.

### Deploy / versioning

- Major backend change → **`BACKEND_VERSION` bump** (`yadgar/__init__.py`, asserted against `server.json` by `test_v5_46_12_backend_version_canonical.py` + `scripts/check_backend_bump.py`). The recall *contract* moves service tracks.
- Core version bump for the handler change + flag.
- **Rollout:** ship backend with `/recall` + core with the flag defaulted OFF. Flip `RECALL_BACKEND_ENABLED=true` in one deployment, watch traces + the quality gate, then default it on.
- **Rollback:** flip the flag OFF — core resumes the local path with zero backend dependency. No data migration, no schema change → rollback is a config flip.

### Test / e2e migration

- Existing recall unit/integration tests run against the local path (flag OFF) unchanged.
- Add a `/recall` contract test (dual-path equality) and a backend e2e that drives `/recall` against a real local SurrealDB (mirrors the existing `make e2e` behavior-contract suite).
- LongMemEval recall@k quality gate on the ON path (see §8) — a regression aborts, same discipline as ADR-0038/0043.

---

## 6. Interplay with in-flight work

- **#28 (CE top-k decouple + configurable) — MUST land first.** It touches `reranking.py:136-144` (the CE seam) and registers `CROSS_ENCODER_TOP_K`/`RERANKER_TOP_K` in the config registry. Building `/recall` on top of the un-decoupled seam would fight the same code and re-open the dual-knob bug. Land #28, then the CE knobs are clean inputs to the `/recall` contract.
- **#13 onnx-int8 — REJECTED (ADR-0043).** Torch stays the CE backend; the `/recall` endpoint uses `LocalMLClient` with torch, no onnx dependency. onnx wiring stays dormant/opt-in.
- **v5.99/v5.104 spreading + PPR N+1 batch — already done.** The DB reads feeding spreading/PPR are already batched, which *reduces* the migration's DB-round-trip win (fewer, fatter reads to relocate) — factor this into the §7 measurement.
- **#88 (recall output cache)** and **#32 (SOTA CE model)** are the *compute* levers that likely beat this migration on latency — the honest alternatives if §7 says NO-GO.

---

## 7. Measure-first gate (blocks the BUILD, not this plan)

Per #85's charter and the corrected v5.101 baseline. **Do not build until this passes.**

### Use the corrected numbers (NOT the task's "warm 1409ms")

The task prompt cites "warm ~1409ms". The v5.101 corrected-baseline anchor states that figure **undercounts by ~6s of MCP-tool-wrapper overhead** (it was the pipeline histogram measured on CE-cache hits). Real user-POV MCP-tool latency: **new query ~23s; exact repeat (CE-cache hit) ~5s.** Per-stage (Tempo waterfall):

- CE (GTE-ModernBERT) = **56-78%, ~15-16s** — three `backend.rerank.ce` calls/recall. **Already backend-side.**
- spreading = **3.4-4.8s** (networkx CPU) — binding constraint once CE is cached.
- build_results + get_memories ~**720ms**; wiki.query ~**310ms** — cross-container DB reads.
- **un-spanned ~6.2s** = `tool.recall − retrieval.recall` on CE-firing recalls (only ~0.3s on cache-hit → CE-correlated, not constant serialization). This region = offload boundary + `_apply_recall_side_effects` blocking DB writes + result formatting.

### The discriminating measurement

**Attribute the ~6.2s un-spanned region** (this is already a tracked task: "close the recall trace gap" — instrument the MCP-wrapper: offload boundary, heat/offload write, formatting). This is the number the migration targets and the only one that decides go/no-go. Code-reading cannot answer it — that is the point of the gate.

| Confirmed boundary-attributable (become localhost / stay in-container) | ~1s (build_results 720ms + wiki 310ms + the batched heat write) |
| CE compute (already backend-side) | ~0 win from migration |
| spreading compute | ~0 win (only its feeding DB reads, already batched) |
| **The swing: the 6.2s** | **GO if mostly DB writes/serialization crossing the boundary; NO-GO if CE-adjacent compute** |

### Gate criteria

- **GO** if the 6.2s attribution shows ≥~3s is cross-container DB write/serialization that the migration eliminates → projected ~15-30% warm-recall win → proceed to build.
- **NO-GO / defer** if the 6.2s is CE-adjacent compute (the migration would save only ~1s / ~4% on THE core feature) → do not build; pursue #88 (output cache), #28 (CE pair count), #32 (SOTA CE) instead.

Measurement method: the recall-perf warm-floor checklist (`docs/testing/recall-perf-checklist.md`), ≥6 warm runs, median, same box, backend fixed, Tempo waterfall. **Also note ADR-0030's standing finding:** cold ≈ warm because recall is surreal-IO bound — which *supports* the DB-boundary hypothesis but must be confirmed by the 6.2s attribution, not assumed.

---

## 8. Quality gate (non-negotiable, standing user directive)

"A speedy system that spits out garbage is not useful." Every latency change is gated on recall quality: **LongMemEval recall@k / ndcg / mrr, controlled A/B (same questions, only the path differs)** — the dual-path flag (§5) is the harness. The ON path must be **byte-identical or non-regressing** vs the local path (it runs the same code, so parity is the expectation; any drift is a wiring bug). A regression aborts the cutover — same discipline that killed onnx-int8 (ADR-0043).

---

## 9. Risk audit (brutal)

1. **The win may not exist (top risk).** CE and spreading are compute, CE already backend-side. Confirmed boundary win ~1s (~4%). If the 6.2s is compute-adjacent, this is effort + blast radius for ~nothing. **Mitigation: §7 gate is blocking.**
2. **Blast radius.** Recall is THE feature; a regression is severe and user-visible. **Mitigation: dual-path flag + atomic reversible cutover (config flip) + byte-identical contract test + quality gate.**
3. **Latency could get *worse* if done incrementally** (core↔backend ping-pong mid-migration). **Mitigation: big-bang implementation, not stage-by-stage.**
4. **"Co-location" over-claim.** DB stays localhost-HTTP (surrealkv file-lock precludes in-process embed alongside the SurrealDB server). The win is localhost-vs-cross-container + keeping fat intermediate data in one container — real but bounded. **Mitigation: framed correctly in §1/§7; measured, not assumed.**
5. **Config authority split.** Backend becomes recall-config-autonomous unless knobs are carried per-request. **Mitigation: explicit `RecallRequest.knobs` block; deployment-fixed knobs stay Settings-side; three-way-sync respected; #28 lands first.**
6. **Auth exposure.** `/recall` returns memory *content*. **Mitigation: reuse `_require_admin_token` (same Bearer auth as `/embed`/`/rerank`) — the backend is already an authed data surface, so this is not a new exposure class, but it must be wired, not defaulted-open.**
7. **stdio / no-backend mode must not break.** **Mitigation: flag OFF = local path, which is the current in-process pipeline via `LocalMLClient` — unchanged and permanent for daemon mode.**
8. **Observability regression (ADR-0041/0042).** The backend runs the obs standard; a new `/recall` route must not re-open the span→log feedback loop. **Mitigation: recall code is already `@observe`-annotated + shipped; honor the log-subsystem exemptions + `_SpanEndFilter`; add a recording-provider test for the new route.**
9. **Backend contract/versioning drift.** **Mitigation: `BACKEND_VERSION` bump + the canonical-version drift-guard test + `check_backend_bump.py`; core version-gates on backend_version.**
10. **Stateful bits.** None persistent (graph is ephemeral, caches already backend-side). **Low risk — de-risked by investigation.**

**What could make this NOT worth it:** the §7 gate returns NO-GO (the 6.2s is compute) → a ~4% win on the highest-blast-radius feature is not worth the change; ship #88/#28/#32 instead.

---

## 10. Open questions

1. **The 6.2s attribution (blocking).** Resolve via the "close recall trace gap" instrumentation before any build. Everything downstream depends on it.
2. **Side-effect write timing.** Today `_apply_recall_side_effects` is synchronous/blocking on the response. Should the migrated `/recall` keep it blocking (simpler, and it becomes a cheap localhost write) or make it fire-and-forget (removes it from the response critical path entirely — possibly a bigger win than the migration itself, and doable *without* the migration)? Measure its share of the 6.2s first.
3. **Fanout vs legacy consolidation.** The fanout path wraps the legacy `Retriever.recall`; the `/recall` endpoint should host the fanout orchestrator and the legacy sub-pipeline together. Confirm no core-only glue (auth/session/scope resolution) is entangled in `_fanout_recall` that must stay core-side.
4. **Knob surface.** Exact `RecallRequest.knobs` field list — enumerate against the config registry after #28 lands.
5. **Concurrency.** The backend already serializes CE on `--cpus 2` (heavy-rerank gate). Moving the full pipeline in-container concentrates more CPU work on the backend — does the recall pool/semaphore need re-tuning? Measure under the 6-concurrent scenario from the perf checklist.
6. **Is the ~1s DB-read win even worth a major-version backend change on its own** if the 6.2s turns out compute-bound? (Likely no → NO-GO.)

---

## 11. Advisor input

The advisor (stronger reviewer, saw the full transcript) was consulted twice, as the user directed.

**Pass 1 (after mapping current pipeline, before committing to strategy) flagged:**
- The value proposition is weaker than the task assumes — CE and spreading are compute; CE already runs backend-side; center the plan on the honest, likely-single-digit-% boundary win. → **Adopted as the BLUF and §7.**
- Sharpen measure-first to "attribute the un-spanned ~6.2s," not "measure latency." → **Adopted as the §7 discriminating measurement.**
- Don't over-claim co-location: DB stays localhost-HTTP, not in-process (surrealkv file lock). → **Corrected throughout (§1, §7, risk #4).**
- Big-bang behind a top-level dual-path flag, not stage-by-stage (avoids ping-pong). → **Adopted as §5, with pushback on #85's "spreading first" framing.**
- Wait for the code-sharing/topology facts before finalizing effort. → **Done; §4 is decisive (package already in backend image → not a port).**
- Snags: `/recall` auth exposure; "stateless" ≠ no cache. → **Adopted (§3.1, risk #6, §3.2).**

**Pass 2 (before finalizing) reconciled and confirmed:**
- Reconciled the two agents' DB claims: SurrealDB server is *inside* the backend container; core reaches it cross-container; backend pipeline reaches it over localhost (still HTTP). → **Stated precisely in §1.**
- The go/no-go pivots on one number (the 6.2s); ~1s (~4%) is the confirmed floor; write the conditional recommendation front-and-center; the gate blocks the *build*, not the *plan*. → **This plan's structure.**
- Don't let "easy to build" (§4) become "worth building" — blast radius + possibly-4% = conditional headline. → **Adopted as the BLUF.**
- Traceparent already wired+tested → obs is a topology note, not a gap. → **§3.3.**
- Nail heat/offload write timing (blocking vs async — the 95s offload timeout hints async elsewhere) and `/recall` auth as an exposure change; define "stateless" as no cross-request session state but keep warm models/caches. → **§3.1, §3.2, open Q2, risk #6.**

**Net advisor verdict:** rigorous plan; the honest framing leans "measure boundary-attributable latency first; likely defer behind CE/spreading compute unless the 6.2s proves to be serialization." This plan encodes exactly that as a blocking gate.

---

## Statefulness + Fanout Audit (2026-07-04)

**Status:** COMPLETE.

Read-only audit gating the go/no-go on relocating the recall pipeline into a stateless
per-request backend `/recall` endpoint. The go/no-go here is **not speed** — it is
**"does moving recall to a stateless backend break anything?"** Independent (adversarial)
verification of the plan's §2 "Stateful bits" / risk #10 claims, with fresh file:line
evidence rather than restating the plan. Three arm's-length investigators traced the path;
every load-bearing claim below was re-verified by hand against source.

Categories used per finding:
- **(a) rebuild-per-request** — reconstructed from request args + DB/models each call → moves cleanly, non-issue.
- **(b) moves-to-backend / split-at-boundary** — real state, but lives (or trivially relocates) inside the backend container, OR is a post-compute side-effect that stays core-side → extra work, quantified.
- **(c) hard-blocker** — genuinely core-process-bound AND read *inside* the compute pipeline that must move → not reconstructable backend-side from request args + DB.

**The load-bearing structural fact (decides the whole audit):** the recall *compute* pipeline
that would move to the backend is `_fanout_recall` → `Retriever.recall` + fusion/PPR/spreading/
rerank/MMR — i.e. everything under `yadgar/retrieval/*`. All the process-local session state
the investigators flagged (`_last_recalled_ids`, `_cognitive_map`, action buffer, checkpoint
counter, shadow-cache counters) is touched **only in the MCP-handler layer of `recall.py`,
above that boundary — never inside `yadgar/retrieval/*`.** Verified: `grep -rn` for
`_last_recalled_ids|_cognitive_map|record_transition|consensus_retrieve|side_effect|
record_recall_sr` across `yadgar/retrieval/` returns **zero hits**. Every consumer lives in
`server/tools/recall.py` / `server/_state.py`. So none of it feeds the ranking that relocates;
it is post-compute bookkeeping wrapped around the pipeline's output.

### Audit 1 — Statefulness

Full path traced: `recall()` MCP handler → `_fanout_recall` (`recall.py:651`, dispatched at
`:650-665`) → `MemoryProvider.candidates` → `Retriever.recall` (`retrieval/core.py:484`) →
PPR/spreading (`scoring.py`, `graph_helpers.py`) → fusion (`providers/fusion.py`) → rerank
(`reranking.py`, `_reranking_*`) → `_apply_recall_side_effects` (`recall.py:379`).

| Thing | File:line | Category | Why |
|---|---|---|---|
| `networkx.DiGraph` (PPR/spreading) | `retrieval/graph_helpers.py:18` (`nx.DiGraph()` fresh) | **(a)** | Rebuilt per call from DB rows, discarded. No warm graph held in core. Plan's claim **confirmed** from source. |
| Branch/scope resolution `_detect_branch(directory)` | `recall.py:592-593` → `server/tools/project.py:139` (`git rev-parse -C <dir>`) | **(a)** | Resolved **in the MCP handler** from the `directory` request arg, then passed *into* the pipeline. Already has a `branch_hint` fallback (`recall.py:601-602`) written explicitly for "container scenario / no `.git` in `_cwd`". Backend gets `current_branch`/`default_branch` in `RecallRequest`. **Non-issue — already container-ready.** |
| `EmbeddingEngine._query_cache` (query-embed LRU, 512) | `embeddings.py:65`; singleton at `server/_state.py:46` | **(a/b)** | Module-level singleton → cross-request cache in core today. Warm-only, bounded, **no correctness dependency** — a cold backend re-embeds identical queries. Moves with the code (backend gets its own engine + cache). |
| Warm CE / NLI / FlashRank model handles | `backend/ml_client.py:355-360` (`LocalMLClient`) | **(b)** | Warm model state. **Already backend-side today** (core's `RemoteMLClient` is a thin RPC shim to `:8001`). Migration keeps them where they are. |
| `#88` shadow-cache counters `_SHADOW_KEYS`/`_DIR_EPOCH`/`_GLOBAL_GEN` | `server/tools/_recall_shadow.py:75-90`; call at `recall.py:619` | **(a/b)** | Module-level cross-request dicts, but **instrumentation only** — `observe_recall` bumps Prometheus "would-hit/would-miss" counters and **never reads or mutates recall output** (docstring + `observe_recall` body confirm). Call sits at **handler level, pre-dispatch** (`recall.py:604-634`) → stays core-side naturally. Not wired as a result cache. Losing it in a cold backend only resets a decision-gate metric. |
| `_last_recalled_ids` (per-session last top-id) | `server/_state.py:120`; read `recall.py:368`, write `:375` | **(b) split-at-boundary** | Cross-request `OrderedDict`, `session_key="default"` (process-global). Read/written **only inside `_apply_recall_side_effects`** (post-compute). **Not read anywhere in `yadgar/retrieval/*`.** → stays core-side; not a pipeline input. |
| `_cognitive_map` (SR successor-representation graph) | `server/_state.py:62`; write `recall.py:371-372` | **(b) split-at-boundary** | Module-level singleton, mutated by `_record_recall_sr_transition` (`recall.py:352,427`). Also **post-compute only**; **zero references in `yadgar/retrieval/*`.** SR transitions are recorded *from* the recall result, they do not *rank* it. → stays core-side. |

**Suspect verdicts (the four the audit was told to force):**
1. **Branch/scope resolution → NOT A BLOCKER.** Resolved handler-side from the `directory` arg with an existing container `branch_hint` fallback. Closes plan Open-Q3.
2. **`#88` shadow cache → NOT A BLOCKER.** Wired but instrumentation-only; changes no output; handler-level. Stays core-side.
3. **`Retriever`/`EmbeddingEngine` lifecycle → (a/b), not a blocker.** Warm caches move with the code; no correctness dependency.
4. **networkx graph → (a), confirmed rebuilt per call.** De-risks the "rebuild caches in backend" concern entirely.

**No category-(c) hard-blocker exists.** The two "hard-blocker" calls the investigators raised
(`_last_recalled_ids`, `_cognitive_map`) were **downgraded on evidence**: they are write-only
post-compute side-effects with zero reads inside the pipeline, so they do not gate relocating
the compute. They become a boundary-split caveat (Audit 3), not a wall.

### Audit 2 — Fanout completeness

`_fanout_recall` (`recall.py:216-348`) fans out to **exactly two arms**, type-filter dispatched.
Enumerated from the code (not the plan); the provider dir `yadgar/retrieval/providers/` holds
only `memory.py`, `wiki.py`, `fusion.py`, `base.py` — no hidden profile/belief/engram/block arm
in the fanout (those live in the *legacy sub-pipeline's* rerank stages, inside `Retriever.recall`,
and move with it).

| Arm | File:line | Data source | Backend-accessible |
|---|---|---|---|
| **Memory** | `recall.py:305-307` → `MemoryProvider.candidates` (`providers/memory.py`) → `Retriever.recall` (`retrieval/core.py:484`) | SurrealDB `memory` table (FTS `search_memories_fts_scored` + HNSW `search_vectors`), all via `StorageEngine._q` | **Y** — SurrealDB on `:8000` |
| **Wiki** | `recall.py:315-317` → `WikiProvider.candidates` (`providers/wiki.py`) → `WikiStore.query` (`wiki.py:914`) | SurrealDB `wiki_page` table (`search_wiki_fts_scored` `wiki.py:803`, `search_wiki_vectors` `:828`, `search_wiki_vectors_tagged` `:854`), all via `self._storage` (StorageEngine) | **Y** — SurrealDB on `:8000` |

**Wiki data-source pinned (was the load-bearing unknown):** the wiki store is **NOT files and NOT
in-memory** — `WikiStore` reads and writes exclusively through `self._storage` (StorageEngine); its
content lives in the SurrealDB `wiki_page` table (versions in `wiki_page_version`). Verified by hand:
every read in `wiki.py` is a `self._storage.*` call. **Fully backend-servable.**

**All fanout arms are cleanly backend-servable.** No arm reads core-local files, host filesystem, or
in-process state populated by a different process. The one investigator caveat — *embedded* SurrealDB
mode (`surrealkv`, exclusive file lock, not remotely servable) — **does not apply to this migration**:
per §1, the backend runs the SurrealDB **server** and the `/recall` pipeline reaches it over
**localhost HTTP** (server mode), exactly as core reaches it cross-container today. Embedded mode is a
no-backend/stdio concern, and the dual-path flag (§5) keeps that on the local in-process path anyway.

> **Note — landscape mode is out of the fanout.** `mode="landscape"` (`recall.py:557-565`) is an
> **exclusive** dispatch that bypasses `_fanout_recall` entirely and calls `_st._pool.consensus_retrieve`
> (`AstrocytePool`, `recall.py:202`) — a core-local, experimental (#67) path. It is **not** part of the
> default recall path this migration targets and should **not** be relocated in the first cut; core keeps
> serving `mode="landscape"` locally. Flag it explicitly in the `/recall` contract as core-only.

### Audit 3 — Side-effect writes

`_apply_recall_side_effects` (`recall.py:379`) is a **mixed unit** and the single real caveat of this
audit. It runs **after** `merged` is produced, called only from the three MCP-handler dispatch points
(`recall.py:564` landscape, `:664` fanout, `:800` legacy) — **never from inside `yadgar/retrieval/*`.**

| Write op | File:line | Target | Backend-servable | Timing |
|---|---|---|---|---|
| `storage.boost_memories_access(ids, now)` (heat + `last_accessed`, batched v5.102) | `recall.py:424` → `storage/memory.py:915` | SurrealDB `memory` table via StorageEngine → `:8000` | **Y** (DB write) | **blocking** |
| `thermo.record_access` → `update_memory_metamemory` (access/useful/confidence) | `recall.py:420` → thermo → `storage` | SurrealDB `memory` table via StorageEngine → `:8000` | **Y** (DB write) | **blocking** |
| `_record_recall_sr_transition` → `_cognitive_map.record_transition/incremental_update` | `recall.py:427` → `:352,371-372` | Core-local `_cognitive_map` singleton + `_last_recalled_ids` (`_state.py:62,120`) | **N** (core-local) | **blocking** |
| `buffer.capture_action("recall", …)` | `recall.py:433` | Core-local `_st._buffer` action deque | **N** (core-local) | **blocking** |
| `_replay.record_tool_call()` (auto-checkpoint counter) | `recall.py:442` | Core-local `_st._replay` counter | **N** (core-local) | **blocking** |

All writes are **synchronous/blocking** on the response — no fire-and-forget queue (the ~95s figure
elsewhere is an offload/consolidation timeout, not a recall-write mechanism). No cross-request batch
buffer is held in core for these writes (the v5.102 batch is a single in-request call).

**Core-side coordination dependencies:** the three core-local writes need the running-process singletons
`_st._cognitive_map`, `_st._buffer`, `_st._replay`, `_st._last_recalled_ids` — none exist in a stateless
backend handler. The two DB writes need only a StorageEngine, which the backend owns.

**⚠️ Plan internal inconsistency to correct.** §3.1 says the heat/offload writes *"run **inside** the
backend `/recall` handler"*, but §3.2 says core keeps side-effects. Moving `_apply_recall_side_effects`
**wholesale** into the backend (as §3.1 reads) **would break** the three core-local writes (the backend
has no `_st`). The correct resolution is a **split at the boundary** (below).

### Verdict

## GO — WITH CAVEATS. No hard-blocker; no core-bound state feeds the compute that relocates.

**Direct answer to the user's question — "does moving recall to a stateless backend break anything?"**

- **The compute pipeline (`_fanout_recall` + `Retriever.recall` + fusion/PPR/spreading/rerank/MMR, all of
  `yadgar/retrieval/*`): NO, nothing breaks.** All state it touches is either rebuilt per request from
  DB/models (graph, branch, embeddings) or already backend-side (models). Both fanout arms read SurrealDB
  via StorageEngine and are fully backend-servable. There is **no category-(c) state** in the relocating path.
- **`_apply_recall_side_effects`: YES, it breaks IF moved wholesale to the backend as §3.1 currently reads**
  — the SR/cognitive-map, action-buffer, and checkpoint-counter writes have no `_st` in a stateless handler.
  **NO breakage if the function is split at the boundary** (the one required change).

**Implementation checklist (the caveats — all category (a/b), quantified):**

1. **[SPLIT — required] `_apply_recall_side_effects` becomes two halves at the boundary** (`recall.py:379`).
   - **Backend half (in `/recall` handler):** the two **DB writes** — `boost_memories_access` (heat +
     `last_accessed`) and `thermo.record_access`/`update_memory_metamemory`. These become **localhost**
     writes, preserving §3.1's win and removing the batched heat write from the cross-container path.
   - **Core half (in core's thin handler, on the returned `merged`):** `_record_recall_sr_transition`
     (`_last_recalled_ids` + `_cognitive_map`), `buffer.capture_action`, `_replay.record_tool_call`.
   - **Effort:** refactor one ~64-line function into two; the core half runs on the results the backend
     returns. Small, mechanical, testable (heat-boost parity is already covered by
     `test_server::test_recall_boosts_heat`). Add a test asserting the split keeps both halves firing.
2. **[DOC — required] Correct §3.1 ↔ §3.2.** State explicitly: **DB side-effect writes move to the backend
   handler; SR/action/checkpoint side-effects stay core-side** on the returned results. As written, §3.1 and
   §3.2 contradict each other on this exact point.
3. **[CONTRACT] `mode="landscape"` stays core-only** (`recall.py:557`, `AstrocytePool.consensus_retrieve`).
   Do not relocate it in the first cut; core keeps serving it locally. Mark it core-only in the `/recall` contract.
4. **[CONFIG] Carry `current_branch`/`default_branch` in `RecallRequest`** (already the plan's design, §3.1).
   Branch is resolved handler-side from the `directory` arg; the backend must not attempt `_detect_branch`
   itself (no host `.git`). This is already how `branch_hint` works — just ensure the request carries it.
5. **[NON-ISSUE, note only]** `#88` shadow-observe (`recall.py:619`) and `_consolidation.record_activity`
   (`recall.py:552`) sit at handler level, **above** the compute boundary, and stay core-side naturally — no
   action, listed so they are not re-litigated. Warm embed/model caches (`embeddings.py:65`,
   `ml_client.py:355`) move with the code; a cold backend re-embeds/re-loads with no correctness change.

**Bottom line:** the plan's §2/risk-#10 claim ("no persistent state → low risk") is **substantially correct
for the compute pipeline** but **incomplete** — it under-states `_apply_recall_side_effects`, which is a mixed
DB/core-local unit that must be **split**, not moved wholesale. That split is the entire delta between "breaks"
and "clean." It is bounded implementation work (one function, one doc fix), not a blocker. **GO-WITH-CAVEATS;
the caveats above are the implementation checklist.**
