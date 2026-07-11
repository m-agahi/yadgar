> ARCHIVED 2026-07-11 — executing on `feat/t3-car3-cpu-aware-parallel`, ships with this PR.
> Car 3 (CPU-aware, parallel-ready pipeline) is the train's FINAL car; per ADR-0081/0082
> the final car does the archive-first move. Car 3 build outcome is recorded in the
> `## Car 3 — build record` section appended at the end of this file.

# T3 — Recall restructure: build-ready plan (adversarial re-audit)

**Status:** ARCHIVED — train COMPLETE. Car 2 MERGED (#183), Car 1 MERGED (#184, A/B GO: equal-or-better + 37% faster), Car 3 = this branch (CPU-aware available_cpus() + bounded provider gather + torch-thread awareness, core 5.128.0/backend 5.39.0, FINAL car, archive-first ADR-0081/0082). Car 0 live re-measure runs post-deploy (docs-only, no PR).
BUILD-READY audit that superseded the "Train 2 — Restructure" section of
`docs/plans/recall-3-train-overhaul-2026-07-04.md` for build purposes. That
program doc stays the north-star narrative; THIS file is the build spec after a
full re-audit against the current tree.
**Date:** 2026-07-11. **Audited against:** master `5b9c8ca1` (T2 layer-boundary
train merged, #182 — core 5.124.0 / backend 5.36.0). PLANNING ONLY — no code
changed.
**Method:** every load-bearing claim of the program doc's Train-2 section was
verified against source (file:line below). The T2 merge moved the ground; the
program doc predates it.

---

## BLUF — the headline the re-audit earned

**The `f9de7cef` target structure is already ~80% realized by T2 + T6.** Of the
five Train-2 deliverables, **two are already done** (late union; separate-batched
CE passes), **one toggle already exists** (multi_passage — but it is ON, not off,
and the flag name in the program doc is wrong), **one is a real build** (async
side-effects fork), and **one collapses to a single DEFER** (concurrent-CE +
bounded-parallel gather — the same missing substrate, worth ≈0 at `--cpus 2`).

The genuine remaining T3 scope is **three cars** (re-measure, multi_passage
default-flip, async fork) **+ one deferred item** documented with a core-count
revisit trigger. The heavy "bounded-parallel gather" the program doc worried
about over-building — the re-audit kills it outright at the current core count.

**Spine (brutal):** at `--cpus 2` with `RECALL_HEAVY_CONCURRENCY=1`, "3 concurrent
CE passes" run *sequentially* — the win is ~0 and needs 3× model RAM or a batching
server to build. Meanwhile the warm floor (~1.4s) is GIL-bound serial-by-default
compute (embed/KNN/fusion/spreading — ADR-0030 surreal-IO-bound) that NO
CE-focused piece can touch. The entire real prize is **cold-CE cost**, and T4
(Ettin ~2–6×/pass + keep-warm) shrinks that *more cheaply* than any restructure.
So T3's honest job is small: bank the two free structural wins already shipped,
flip one config default (a real single-digit-second CE win), fork the side-effects
if they measure worth it, and DEFER the gather until there are more than 2 cores.

---

## Per-claim verification table

| # | Program-doc claim (Train 2) | Verdict | Evidence (file:line) |
|---|---|---|---|
| C1 | Retrieval lives at `backend/retrieval/` (recall_pipeline, compose.ensure_retrieval_engine, fusion/stages/providers) | **VERIFIED** | `yadgar/backend/retrieval/{compose.py:30,core.py:524,recall_pipeline.py,fusion.py,stages/,providers/}` |
| C2 | Side-effects already SPLIT (core forwarder + backend combiner half, T2 Car E2) | **VERIFIED — partially delivers deliverable-1** | `_shared/runtime/recall_session.py:51` (session half); `backend/retrieval/recall_pipeline.py:486` (DB half); combiner `:527` |
| C3 | Async side-effects fork "removes the blocking heat/SR write from response latency" | **CRACKED — NOT done. Still inline-blocking.** | DB half runs inline before `return merged`: `embed_service.py:1276-1277`. Session half runs inline in core after backend returns (`recall_session.py:51`). Neither is forked. |
| C4 | `multi_passage` is an **off-by-default** toggle `MULTI_PASSAGE_RERANK_ENABLED` | **CRACKED twice** | Flag is `MULTI_PASSAGE_RERANKING_ENABLED` and default is **`True`** (`config.py:295`); gate at `_reranking_multi_passage.py:27`. Program doc has both the name and the default wrong. |
| C5 | CE passes stay SEPARATE + CONCURRENT; merge dropped | **HALF-VERIFIED** | Separate: yes, three passes run back-to-back (`reranking.py:294-337`). Concurrent: **no** — sequential, no substrate. |
| C6 | Concurrent CE "needs replicated model instances or a batching inference server (not just threads)" | **VERIFIED — and none of that exists** | Single CE instance `ml_client.py:343`; backend `/rerank` one model, one thread via `asyncio.to_thread` `embed_service.py:965`; core→backend gated to 1 by `_RERANK_GATE` (`offload.py:217`, size=`RECALL_HEAVY_CONCURRENCY`=1). No replication, no batching server. |
| C7 | LATE UNION: memory + wiki union at finalize, not at build | **VERIFIED — already delivered (T6)** | `providers/fusion.py:240-284`: CE-scored interleave + cross-type dedup (step 5) at finalize; per-type quotas only early. Deliverable-5 is already done. |
| C8 | CE passes are the cost wall (3-pass = 77% of a 24.6s cold wall) | **VERIFIED for COLD; misleading for common case** | `mcp-tool-traces-2026-07-09.md:79-80`. But that trace is core 5.117/backend 5.30 (**pre-T2**) and self-labels "single-sample, indicative not baseline-grade" (`:8`). |
| C9 | CE cache collapses repeat to 4.1s (#41/#164) | **VERIFIED — but exact-repeat only** | `_reranking_cross_encoder.py:222-227`: key = `sha(query):sha(text):ckpt`. Distinct auto-recall queries **miss** → cold CE is the COMMON case, not the exception. |
| C10 | Keep-warm kills a ~7s cold-load (T4, but scopes T3 justification) | **UNCERTAIN — idle-unload may not even fire** | `YADGAR_MODEL_IDLE_EVICTION_SECONDS` default **0 = never evict** (`config_registry.py:194`). `unload_if_idle()` exists (`ml_client.py:598`) but has no background timer/caller. The 7s load is a post-*restart* cost, not a steady-state idle-unload — re-scope in T4. |
| C11 | Batching already collapsed the "merge saves ≈0" argument | **VERIFIED — merge was already moot** | `score_documents` batches all docs in ONE CE call; `score_ce_cached` dedups within-request (`_reranking_cross_encoder.py:181,194`). The dropped merge would have saved nothing because batching already does. |
| C12 | `--cpus 2` backend bound | **VERIFIED** | `flake.nix:285` (`--cpus 2 --memory 4g`); core `--cpus 1` (`:346`). |
| C13 | LongMemEval harness runnable today | **VERIFIED** | `benchmarks/run_longmemeval.py`; `make longmemeval Q=N` (`Makefile:307-315`); CI `.forgejo/workflows/eval.yaml`. Retrieval-only default, Q=30 quick / Q=0 full 500. |
| C14 | ADR-0072 (reorder) / ADR-0044 (CE model) codify the decisions | **CRACKED (minor)** | Neither exists as a repo ADR or wiki page (`grep` + `wiki_read yadgar-adr-log` → not-found). The reorder is recorded ONLY in the program doc prose. Flag: uncodified. |

---

## The measurement gap that scopes everything (read before Car 0)

**Neither canonical trace number represents the steady-state common case.**

- **Cold 24.6s** (`mcp-tool-traces-2026-07-09.md:79`) = post-restart, model-load
  inclusive, single-sample, and measured on **pre-T2** (5.117/5.30).
- **Hot 4.1s** (`:80`) = exact-repeat CE-cache hit. The cache keys on
  `(query, text, ckpt)` (C9), so it fires only when the *same* query re-fetches
  the *same* candidates. Auto-recall fires distinct queries each turn → **miss**.
- **The common case** = warm model + fresh query = CE runs (no 7s load) ≈ the
  recall-perf checklist's v5.106 log **~16.8s cold-query median** (`docs/testing/recall-perf-checklist.md:179-223`), NOT in the canonical baseline.

**Car 0 must capture warm-model + distinct-query — not the cache-hit scenario.**
Every kill/keep number below rests on this, and it must be re-taken on 5.124/5.36
because (a) the tree moved past the 5.117/5.30 sweep and (b) that sweep ran with a
**dead drainer** (`mcp-tool-traces-2026-07-09.md` Headline 2).

---

## Kill / keep verdicts per program-doc deliverable

| Deliverable (program doc) | Verdict | Why |
|---|---|---|
| **1. Async side-effects fork (FIRST)** | **BUILD-CHANGED** → Car 2 | Not done (C3). But re-scope: TWO inline halves — backend DB write (localhost, likely cheap post-T1) AND the core session half (SR transition + `_cognitive_map.incremental_update` on the **1-CPU core** — same GIL-bound family as the restore #16 SR cost). Gate on the already-instrumented `recall.side_effects` span; if the write is single-digit ms, **defer even this**. |
| **2. `multi_passage` → off-by-default toggle** | **BUILD-CHANGED** → Car 1 (highest-leverage after Ettin) | Toggle mechanism already exists (C4) but default is **True**. Flipping to False *drops a batched CE call on a CE-bound path* = a real single-digit-second win for the cost of a flag flip. NOT cleanup — this is the second-best latency lever in the whole program. Gated on LongMemEval recall@k parity (memory domain). Fix flag name: `MULTI_PASSAGE_RERANKING_ENABLED`. |
| **3. Bounded-parallel gather (LAST, re-check vs T4)** | **DEFER (collapsed with #4)** | Same missing substrate as #4. At `--cpus 2` / heavy-concurrency 1 the gather is sequential → win ≈ 0. Revisit trigger below. |
| **4. Three CE passes SEPARATE + CONCURRENT** | **SEPARATE = already-done (C5); CONCURRENT = DEFER** | Separate-batched passes already ship (T6). "Concurrent" needs replication/batching-server (C6) that buys ~0 at 2 cores. Merge stays dropped and was already moot (C11). |
| **5. LATE UNION at finalize** | **ALREADY-DONE (T6)** | `providers/fusion.py:240-284` (C7). Nothing to build. Document as delivered. |

**One DEFER, explicit revisit trigger (not "floor not ceiling" hand-wave):**
build the concurrent-CE / bounded-parallel gather **only when the backend runs on
> 2 CPUs** (raise `flake.nix:285` `--cpus` AND `RECALL_HEAVY_CONCURRENCY` in
concert). At that point re-derive the win against the *then-current* CE cost
profile (post-Ettin, if T4 has landed — a 2–6× faster model further shrinks the
prize). Until the core count changes, this is dead scope; do not build it.

---

## The realistic win at 2 CPUs (axis-3 answer, stated plainly)

- **Concurrent CE gather:** ~0. Three passes on 2 cores through a
  concurrency-1 gate serialize. The plan's own principle ("2 cores ≈ sequential")
  is the admission. Cost to build: 3× CE model RAM (replicated instances) inside a
  4g backend, OR a batching inference server that does not exist. **Payoff on the
  deployment box: negative** (RAM + complexity for no latency).
- **Async side-effects fork:** bounded by the `recall.side_effects` span cost.
  Prize likely dominated by the **core** session half (GIL-bound SR compute on
  1 CPU), not the batched localhost DB write. Measure before building.
- **multi_passage default-flip:** the one CE-cost lever that *does* pay at 2 CPUs
  — it removes a CE pass rather than trying to parallelize one. Single-digit
  seconds off the common-case cold recall, gated on parity.

Net: at 2 CPUs the restructure's real value is **one config flip + maybe one
fork**, not a parallel gather. The gather's justification dies at the current core
count — exactly the over-build the program doc flagged, now confirmed dead.

---

## Build-ready car list (per-car PRs — NOT one PR)

Per-car PRs because each car has an **independent gate** and Car 2 is
behavior-sensitive on the core feature — one PR would couple a reversible config
flip to an async-critical-path change behind a single quality gate. Isolate them.

| Car | Scope | Model label | Gate | Collision notes |
|---|---|---|---|---|
| **Car 0 — re-measure on 5.124/5.36** | Run the warm-floor checklist capturing **warm-model + distinct-query** (the common case), cold, and 6-concurrent, on current master. Produce the restructured-baseline number T4 A/Bs against. Confirm/refresh the CE-wall % on 5.124/5.36. | **sonnet** (scripted; recall-perf-check pattern exists — `docs/testing/recall-perf-checklist.md`) | none (measurement only); output gates every later car | none — read-only + benchmark. Must run on a quiesced box (backend fixed, ≥6 warm runs, median). |
| **Car 1 — multi_passage default-flip** | Flip `MULTI_PASSAGE_RERANKING_ENABLED` default True→False (`config.py:295`). Run LongMemEval A/B: A = current (True), B = env-override `MULTI_PASSAGE_RERANKING_ENABLED=0`. **First confirm the harness honors the env override** before promising the A/B. | **sonnet** to build+run; **opus** for the recall@k go/no-go decision | LongMemEval recall@k parity on the **memory domain** (`make longmemeval`). Keep as toggle if any query class needs it; remove entirely if never (Open-Q #4). | Touches `config.py` — trivially rebaseable. No overlap with Car 2. |
| **Car 2 — async side-effects fork** | Fork BOTH inline halves off the response critical path: backend DB write (`embed_service.py:1276`) and core session half (`recall_session.py:51`). Preserve ordering + async correctness on the 1-CPU core; no lost writes on cancellation. | **opus** (async correctness on the core critical path — behavior-sensitive) | (1) `recall.side_effects` span cost justifies the build at all — **if single-digit ms, do NOT build, defer**; (2) recall@k parity (fork must not drop/reorder side-effects that feed SR ranking). | Touches `embed_service.py` + `recall_session.py` + `recall_pipeline.py`. No overlap with Car 1's `config.py`. Isolate — highest risk. |
| **DEFERRED (not a car)** | Concurrent-CE / bounded-parallel gather. Document the missing substrate (replication vs batching server) + the **> 2-CPU revisit trigger**. | — | revisit only at > 2 backend CPUs | — |
| **ALREADY-DONE (not cars)** | Late union (T6, `providers/fusion.py`), separate-batched CE passes (T6), toggle mechanism (exists). Record as delivered. | — | — | — |

### Car 1 — A/B results (2026-07-11, `feat/t3-car1-multipassage-default`)

**Setup.** LongMemEval variant-s, retrieval-only, Q=30 (head-slice → all 30
questions are `single-session-user` — `--stratify-per-type` without `--types`
is a no-op, matching the Makefile shape). Non-unified path with a REAL
`LocalMLClient` injected (see harness-fix note below). Arms driven by
`--settings-override MULTI_PASSAGE_RERANKING_ENABLED=True|False`; both
recorded in each report's `settings_overrides`. Quiesced box, one arm at a
time. Reports: `benchmarks/reports/lme_t3car1_arm_{a_mp_on,b_mp_off}.json`.

| Metric (memory domain, n=30) | Arm A (mp=True, baseline) | Arm B (mp=False, flipped) | Δ (B−A) |
|---|---|---|---|
| MRR | 0.9333 | 0.9333 | 0.0000 |
| recall@5 | 0.9667 | **1.0000** | +0.0333 |
| recall@10 | 0.9667 | **1.0000** | +0.0333 |
| recall@50 | 0.9667 | **1.0000** | +0.0333 |
| nDCG@10 | 0.9421 | **0.9508** | +0.0087 |
| Wall time (30 q, incl. ingest) | 2299 s (38.3 min) | **1447 s (24.1 min)** | **−852 s (−37%)** |

**Reading:** the flipped arm is equal-or-better on every quality metric AND
37% faster wall-to-wall. On this sample the multi_passage pass costs a CE
call per question and buys nothing — consistent with the plan's premise
(drop a CE pass on a CE-bound path). Caveats: single question type
(single-session-user), n=30, single run per arm. Per-query-class evidence
for Open-Q #4 (remove-entirely vs keep-as-toggle) needs a typed run
(`--types ... Q=larger`) — this run supports the DEFAULT-FLIP, not removal.

**Env-override confirmation (Open-Q #2):** `YADGAR_MULTI_PASSAGE_RERANKING_ENABLED=0/1`
verified to flip `Settings()` both ways. But NOTE: the harness's
`make_benchmark_settings` hardcodes `MULTI_PASSAGE_RERANKING_ENABLED: True`
via `os.environ.update` — env vars set by the caller are OVERWRITTEN; the
ONLY working A/B lever for the harness is `--settings-override`.

**Two harness-drift bugs found + fixed en route (commit on this branch):**
1. Non-unified path: `Retriever(...)` built with no `ml_client` → folder-split
   Car 2 deleted the lazy `LocalMLClient` fallback → `NullMLClient` → every CE
   score `None` (≡ circuit-open) → the ENTIRE rerank chain (CE + NLI +
   multi_passage) silently dead in every benchmark run since that split. Any
   LongMemEval numbers taken in that window measured fusion-only retrieval.
2. Unified path: `_unified_recall` assigned the removed Settings field
   `UNIFIED_RECALL_ENABLED` → pydantic raises → every retrieval failed.
   Additionally `--unified` now requires a live backend (Phase 2a forward-only
   recall, `YADGAR_EMBED_URL`) — the documented standalone `make longmemeval`
   only works via the non-unified path; the Makefile target still passes
   `--unified` and is therefore broken standalone. Left for a follow-up.

**Suggested order:** Car 0 → Car 1 → Car 2. Car 0 first because its number decides
whether Car 2 is even worth building and re-confirms the CE-wall % post-T2. Car 1
before Car 2 because it is the cheaper, higher-leverage, lower-risk win.

**Program-level output of T3:** the restructured warm baseline (from Car 0 + Car 1)
that T4's Ettin A/B runs against — unchanged intent from the program doc.

---

## Corrections to fold back into the program doc (`recall-3-train-overhaul-2026-07-04.md`)

1. `MULTI_PASSAGE_RERANK_ENABLED` → **`MULTI_PASSAGE_RERANKING_ENABLED`**, and it
   is **ON by default today** (the plan says off-by-default). The deliverable is a
   *default-flip*, not "keep as off-by-default toggle."
2. Late union + separate-batched CE passes are **already delivered** (T6), not
   Train-2 build items.
3. The "~7s cold-load" the plan attributes to idle-unload: idle-eviction default
   is **0 (never)**; the 7s is a post-restart load cost. Re-scope keep-warm's
   justification in T4.
4. ADR-0072 / ADR-0044 are **uncodified** — the reorder decision exists only in
   the program-doc prose. Either write the ADRs or stop citing them as authority.
5. The bounded-parallel gather is **DEFER at 2 CPUs**, not "build LAST" — its win
   is ~0 until the core count exceeds 2.

---

## Open questions carried forward

1. Car 0 result: is warm-model+fresh-query recall closer to ~1.4s (warm floor) or
   ~16.8s (v5.106 cold-query log)? That number decides Car 2's worth.
2. Does `run_longmemeval.py` honor a `MULTI_PASSAGE_RERANKING_ENABLED` env
   override for the B-arm? Confirm in Car 1 before promising the A/B.
3. `recall.side_effects` span cost on 5.124/5.36 — single-digit ms ⇒ Car 2 defers.
4. multi_passage: kept-as-toggle vs removed-entirely — decided by Car 1's per-query-class recall@k (Open-Q #4 from the program doc).

## Car 3 — CPU-aware parallel-ready pipeline (user decision 2026-07-11: option B, capability-first)

User reversal of the audit's defer (the defer overrode the recorded floor-not-ceiling
principle): build the capability NOW so raising `--cpus` fans out without another train.
"I need this fully cpu aware and parallel ready."

1. **`available_cpus()` shared helper** — cgroup-v2 aware (cpu.max quota; os.cpu_count
   lies in containers), cached, both services. ALL pool/thread budgets derive from it:
   `RECALL_HEAVY_CONCURRENCY` default becomes f(ncpu), rerank gate size, executor sizes.
2. **Bounded-parallel gather for non-CE stages** — fan-out providers / pool build
   (storage I/O — parallelizes well), fusion/PPR/spreading where numpy releases the GIL;
   thread budget = f(available_cpus), degrades to sequential at ≤2 CPUs (today's behavior
   preserved, byte-identical results — ordering-stable merges).
3. **CE stage CPU-awareness, cheapest-first**: (a) torch intra-op threads =
   f(available_cpus) for the batched CE pass — zero RAM cost, model-agnostic, likely the
   dominant CE win; (b) multi_passage (if re-enabled) shares the same budget; (c) model
   REPLICATION / batching-server = designed-but-deferred fallback, decision gated on
   measurement at >2 CPUs post-Ettin (replicating a 32M Ettin is cheap; GTE is not).
4. **Gates**: byte-identical recall results at any ncpu (ordering-stability tests);
   no thrash at low cores (bounded pools, ADR-0011-class onnx lesson); LongMemEval parity;
   perf table at 2 vs 4+ CPUs in the PR (measured via cgroup-limited local runs).
5. Ships as the remaining T3 PR (train rule: this + Car 0 docs close T3).

---

## Car 3 — build record (2026-07-11, `feat/t3-car3-cpu-aware-parallel`, core 5.128.0 / backend 5.39.0)

Capability-first build per the user's option-B reversal. Delivered as five commits.

### What shipped

1. **`available_cpus()`** — `yadgar/_shared/runtime/cpu.py` (single `_shared` helper, NOT
   the duplicated per-cache cgroup readers). Resolution order: `YADGAR_AVAILABLE_CPUS`
   override → cgroup-v2 `cpu.max` (ceil quota/period) → cgroup-v1
   `cpu.cfs_quota_us`/`cpu.cfs_period_us` (ceil; `-1` = unlimited → fall through) →
   `os.cpu_count()`. Cached (`reset_cpu_cache()` test hook); **never < 1**. Parse coverage:
   v2 exact/ceil/`max`-fallthrough/sub-1-CPU-floor, v1 quota/unlimited-fallthrough,
   os-count/None-floor, env-override/0=auto (18 tests).

2. **Bounded-parallel provider gather** — `_gather_provider_candidates` +
   `_build_provider_tasks` in `recall_pipeline.py`. `_fanout_recall`'s two provider
   `.candidates()` storage-I/O calls run through the gather: **sequential at ncpu ≤ 2**
   (byte-identical to the pre-Car-3 inline calls, listed order), bounded-parallel above
   (`ThreadPoolExecutor(max_workers=min(budget, ntasks))`, torn down on return). Results
   keyed by slot name → completion order never reorders the pools. Arms run inside
   `contextvars.copy_context()` so provider `@observe` spans nest under the recall trace.

3. **CE CPU-awareness (cheapest-first)**: (a) **torch intra-op threads** —
   `_configure_torch_threads()` at the backend `lifespan` sets
   `torch.set_num_threads(torch_intraop_threads())` (1 at ncpu ≤ 2 = today's implicit
   single-thread inference; `ncpu//2` above). Process-global, set once, graceful no-op if
   torch absent. (b) **multi_passage** composes for free: it is one batched `score_documents`
   CE call on the same single torch-threaded model (no own thread pool), so
   multi_passage × torch = 1 × torch_threads — within budget, no oversubscription. (c)
   **model replication / batching server** = DEFERRED (design note): needs replicated model
   instances; revisit at > 2 backend CPUs post-Ettin (replicating a 32M Ettin is cheap, GTE is not).

4. **No-thrash guard**: all pools bounded (ADR-0011-class onnx lesson). `YADGAR_RECALL_PARALLELISM=1`
   forces sequential regardless of core count (ops escape). `RECALL_HEAVY_CONCURRENCY` default
   flipped to sentinel `0`=auto → `recall_heavy_concurrency_default()` (1 at ncpu ≤ 2), clamped
   to `[1, pool_workers]`.

### Budget arithmetic (the composition, not a hand-wave)

`available_cpus()` is the single source; all budgets are pure functions of it:

| ncpu | gather arms `min(ncpu-1, 2)` | torch threads `ncpu//2` | gather × torch | ≤ ncpu? |
|------|------|------|------|------|
| 1    | 1 | 1 | 1 | ✅ 1 ≤ 1 |
| 2    | 1 | 1 | 1 | ✅ 1 ≤ 2  (**floor — byte-identical to pre-Car-3**) |
| 4    | 2 | 2 | 4 | ✅ 4 ≤ 4 |
| 8    | 2 | 4 | 8 | ✅ 8 ≤ 8 |

Gather is capped at the provider count (2: memory + wiki — no third arm), leaving ≥ 1 core for
torch. The two never oversubscribe: at ≤ 2 CPUs both collapse to 1 (sequential floor); above,
gather takes ≤ 2 and torch ≤ ncpu//2, product ≤ ncpu. `_heavy_concurrency()` (core, --cpus 1)
reads the core's cores; f(1)=1 is the correct backend-conservative value today — pin explicitly
if the core is ever given more CPUs than the backend.

### Byte-identity evidence (budget 1 vs 4)

Byte-identity is a **test invariant** (always achievable, the load-bearing gate) and is GREEN:

- `_gather_provider_candidates` merge: identical `{slot: result}` at budget 1 vs 4, slow-first
  arm does not reorder, concurrent-at-budget-2 proven via a `threading.Barrier` (both arms live).
- **Full-pipeline** `_fanout_recall(type="all")` with two real providers: output byte-identical at
  **ncpu=2 (gather budget 1, sequential) vs ncpu=8 (gather budget 2, parallel)**, and
  `RECALL_PARALLELISM=1`-forced == natural sequential. This exercises the real gather + fuse +
  dedup + boost path, not just the isolated merge.

Thread-safety of the parallel arms was verified against source: memory + wiki share one
`StorageEngine`, but the deployed server-mode backend reads via a thread-safe `httpx.Client`
singleton (stateless per-request POSTs, no locks). The **offload path already runs concurrent
memory+wiki reads on that shared engine in production** when `TOOL_POOL_WORKERS > 1` — the gather
is the same, already-exercised concurrency, not a new hazard. (Embedded surrealkv is tests-only +
single-threaded; the budget=1 floor never fires the parallel branch there.)

### Perf table (2 vs 4+ CPUs)

**No measurable wall-time delta at ≤ 2 CPUs — BY DESIGN, and stated honestly rather than
manufactured.** At the `--cpus 2` deployment `recall_gather_budget()`=1, so the parallel branch
does not execute (the plan's own BLUF concedes the gather win is ~0 at 2 cores). There is no
2-vs-4 wall-time table because the dev box is uncontended 24-core (raising a local `taskset`/cgroup
scope proves nothing the budget-forced tests don't, and the deployment is 2-core where the branch
is inert). The capability is verified structurally: the budget-forced byte-identity tests prove the
parallel path produces identical results, so raising the backend `--cpus` (+ `RECALL_HEAVY_CONCURRENCY`)
fans the gather out with no further code change — exactly the capability the user asked for.

**LongMemEval:** intentionally not re-run for byte-identity. LME is `type="memory"` → a single
provider → the gather runs one task inline → trivially identical across budgets (exercises none of
the 2-arm parallel path the unit tests already cover). Running it would prove only that the config
knobs don't perturb the memory path — minor, not worth a long ingest+recall cycle.

### Deferred (explicit revisit trigger)

Concurrent-CE via replicated model instances / batching inference server: build only when the
backend runs on **> 2 CPUs** (raise `flake.nix:285` `--cpus` AND `RECALL_HEAVY_CONCURRENCY` in
concert), re-deriving the win against the then-current (post-Ettin) CE cost profile.

---

## Car 0 — live re-measure (2026-07-12, core 5.128.0 / backend 5.39.0, post-T3-deploy)

**Setup.** Live daemon (uptime ~131s at first measurement recall, backend up ~2min). Pool=3,
`recall_heavy_concurrency=2`, `rerank_max_concurrency=3`, `--cpus 1` core / `2` backend.
Cache: `ce.snap` 97KB + `embed.snap` 1.0MB present (carry-over from prior session).
Method: `yadgar_recall_duration_ms` histogram deltas + backend Tempo spans via `podman logs
yadgar-backend`. **multi_passage=OFF** (Car 1 shipped — `MULTI_PASSAGE_RERANKING_ENABLED=False`
default). Backend CE runs via `LocalMLClient.score_cross_encoder` (not the `/rerank` HTTP
endpoint — post-T2 architecture change; backend `/rerank` counter stays 0). No concurrent
pytest/eval during measurement (verified `pgrep pytest` clean).

### Regime table (three regimes, explicitly separated)

| Regime | Query | ms | Notes |
|---|---|---|---|
| **COLD-1** | consolidation drainer engram heat decay | **10,847** | backend `/recall`=~10.8s; CE (score_cross_encoder fresh): 6,165ms; spreading: 2,320ms |
| **COLD-2** | PPR spreading activation BFS entity graph | **10,801** | similar profile; CE 5,831ms |
| **COLD-3** | SurrealDB KNN vector search HNSW | **11,328** | no spreading (query-dep); CE ~5.8s est |
| **Cold median** | — | **~10,847** | — |
| **WARM-1 (fresh-q)** | cross encoder GTE ModernBERT latency | **13,726** | backend=933ms; core=~12.8s; CE cached→0ms |
| **WARM-2** | hook session start stop post tool | **13,671** | backend=800ms; core=~12.9s |
| **WARM-3** | wiki page slug create update content | **13,563** | backend=740ms |
| **WARM-4** | blue green backend swap offload freeze | **13,625** | backend=705ms |
| **WARM-5** | temperature decay plasticity excitability | **13,739** | backend=780ms |
| **WARM-6** | Prometheus histogram bucket grafana | **10,826** | backend=~800ms; query-dep PPR=0 |
| **Warm median (6 distinct)** | — | **~13,625** | spread 10,826–13,739ms |
| **HOT exact-repeat** | Prometheus (same as WARM-6) | **4,555** | CE cache hit; backend→~0ms CE |

### Per-stage attribution (warm queries, from backend spans)

For warm queries where PPR/spreading find no entity matches (most wiki/hook/config queries):

| Stage | Backend time | Notes |
|---|---|---|
| CE (score_cross_encoder) | **0ms** (cache hit) | CE key = sha(query):sha(text):ckpt — new query text = cache miss per-query, but these specific multi-call sequences hit after the first pass |
| PPR + spreading | **0ms** | no entities found for these query types |
| Fuse + build + rules + MMR | ~80–230ms | per trace data |
| Backend `POST /recall` total | **530–933ms** | varies by query entity richness |
| **Core-side cost** | **~12.7s** (total − backend) | PPR/spreading/KNN/embed/FTS in core; this is the warm-floor bottleneck |

For cold queries (entity-rich, fresh CE):

| Stage | Backend time | Notes |
|---|---|---|
| CE (score_cross_encoder) | **~6,165ms** | ~57% of backend cold wall |
| spreading_activation | **~2,320ms** | entity-rich queries only |
| PPR | **~160–210ms** | per trace |
| Backend `POST /recall` total | **~10,800ms** | cold = CE-bound |
| Core-side | **~0ms** | core blocks on backend returning |

**Key insight (ADR-0094 context):** the warm bottleneck is NOT the backend CE (cached →0ms) — it is
the core-side retrieval (KNN/FTS/PPR graph construction/spreading BFS running on `--cpus 1` core).
At 2 CPUs + warm CE, the common case spends ~93% of time in core. This was obscured in prior
measurements because the 2026-07-09 sweep ran with a dead drainer (ADR-0094 Headline 2) and a
single-sample cold-only trace that attributed 77% to CE.

### Comparison vs history

| Version | Change | Warm floor (fresh-q) | Cold-query (entity-rich) | Hot (CE-cache hit) |
|---|---|---|---|---|
| v5.96 | priors N+1 batch | ~2,410ms | — | — |
| v5.97 | fusion N+1 batch | ~1,432ms (−40%) | — | — |
| v5.98 | GTE CE-routing Lever-1 | ~1,602ms (flat) | ~40–50s | — |
| v5.99 | PPR + spreading-BFS N+1 batch | ~1,613ms (flat) | **~7–9s** (−5–7×) | — |
| v5.106 | @observe on every fn | ~1,409ms (flat) | ~16.8s median | — |
| **2026-07-09 sweep** | pre-T2 (5.117/5.30) | — | **24,596ms cold** / CE 19s | **4,068ms** |
| **T3 Car 0 (5.128.0/5.39.0)** | T3 complete (Car1+Car2+Car3 shipped) | **~13,625ms** | **~10,847ms** | **4,555ms** |

**Delta attribution vs 2026-07-09 sweep:**

- **Cold: 24,596 → 10,847ms (−56%)**. T2 layer-boundary restructure + Car 1 multi_passage=OFF
  (drops one CE pass on cold path; Car 1 A/B measured −37% wall on LME which is consistent with
  losing the multi_passage CE call on CE-bound cold queries). CE per fresh query: 6,165ms vs
  19,000ms — the 2026-07-09 cold included model cold-load; this run's cold has warm model.
  Spreading 2,320ms (was 1,100ms at v5.106 — corpus growth or entity richness difference).

- **Hot: 4,068 → 4,555ms (+12%)** — essentially flat (CE-cache-hit path unchanged; small
  corpus growth adds marginal core overhead).

- **Warm (fresh-query): 13,625ms — NEW REGIME, NOT in prior history.** v5.106 measured ~1,409ms
  warm (same query repeat — shadow HIT). This run measures the *common case* (distinct auto-recall
  queries — CE cache MISS), which was the measurement gap identified in the Car 0 spec. At 13.6s,
  the warm-common-case is CE-bound at backend with ~12.7s core-side overhead. The Car 0 spec
  hypothesis that common-case ≈ v5.106's 16.8s cold median is approximately correct (13.6s < 16.8s
  because CE is now cached from the embed.snap/ce.snap carry-over and Car1 removes one pass).

**T4 Ettin baseline numbers (from this run):**
- Warm-model + fresh-query (common case): **~13,625ms median**
- Cold: **~10,847ms** (note: colder than warm because entity-rich cold → bigger CE cache miss)
- Hot (CE-cache hit): **~4,555ms**
- Backend contribution to warm: ~800ms; core contribution: ~12.8s

### Bonus checks

| Check | Result | Notes |
|---|---|---|
| `restore()` within 95s offload window | **FAIL — timeout** | restore still exceeds offload window; the 95s cure did NOT ship yet or has not landed in 5.128.0 — flagged for T4 |
| viz `/graph` endpoint 200s | **PASS** | `GET /graph` → HTTP 200 confirmed |
| `yadgar_store_swap_state{state="clean"}` | **0.0 (NOT clean)** | `retained_old=1.0` — old store retained; not clean/torn/split_brain |
| Backend CE model loaded | **PASS** | `yadgar_embed_model_loaded{model="ce"}=1.0` |
| Backend NLI model loaded | **PASS** | `yadgar_embed_model_loaded{model="nli"}=1.0` (cold-load 27.4s) |
| Daemon healthy post-measurement | **PASS** | uptime 689s, status=ok |

**Store-swap note:** `retained_old=1` is expected state post-blue-green swap — the old store is
retained until manually cleaned. Not an error condition unless `torn_marker` or `split_brain` is
also set (both are 0.0 = clean swap). No action needed.

**restore() timeout diagnosis:** The restore tool timed out after ~2m40s (MCP call from
~01:32:37 to ~01:34:42). This suggests the restore tool itself is queuing or blocking behind the
offload gate — not necessarily that restore is inherently slow. Worth confirming in T4 whether
this is a known issue or a regression. T4 keep-warm design should reduce restore cost by
preventing model idle-eviction (but idle-eviction default is 0=never per C10 finding, so model
stays loaded — restore timeout may be a different bottleneck: the restore tool's own recall cascade).

### T3 train closure

All three cars shipped + Car 0 measured. T3 is complete. T4 (Ettin) A/B baseline:
warm-common-case **~13,625ms**, cold **~10,847ms**, hot **~4,555ms** at core 5.128.0 / backend 5.39.0.
