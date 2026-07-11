# T3 — Recall restructure: build-ready plan (adversarial re-audit)

**Status:** BUILD-READY — supersedes the "Train 2 — Restructure" section of
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
