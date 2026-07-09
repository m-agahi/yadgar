# Plan: Recall pipeline overhaul — the 3-train program

**Status:** AGREED master plan (from a long user + advisor design session). Encodes *what* and *in what order*; each train's BUILD is separately gated on measurement.
**Date:** 2026-07-04. **REORDERED 2026-07-09 (user decision, ADR-0072):** restructure is now Train 2, Ettin swap is now Train 3 — the 2026-07-09 trace sweep supplied the clean current-model baseline that the old ordering wanted the swap to produce, so the restructure is scoped by TODAY's numbers and the model swap lands last on the restructured pipeline.
**Scope:** The flagship `recall` pipeline — architecture (core→backend), structure (bounded-parallel restructure), and model (CE swap + keep-warm).
**Related tasks / ADRs:** #85 (recall→backend endpoint), #32 (SOTA CE model / Ettin research — doc being written in parallel), #161 (CE top-k decouple/knobs — **CLOSED unmerged**, superseded), ADR-0030 (recall is surreal-IO bound), ADR-0034 (obs standard + traceparent — SHIPPED), ADR-0043 (CE onnx dynamic-int8 REJECTED), ADR-0044 (CE model decision — **forthcoming**, being written in parallel with the #32 research), ADR-0072 (train reorder).
**Reference docs:** `docs/plans/recall-pipeline-to-backend-2026-07-04.md` (Train 1 detail + Statefulness/Fanout audit), `docs/diagrams/specs/recall-proposed-optimized-2026-07-04.yaml` (Train 2 target structure, commit `f9de7cef`; supersedes the earlier `3c16b33b` merged-CE mock), `docs/diagrams/mcp-tool-traces-2026-07-09.md` (current baselines: cold 24.6 s / CE 3-pass 19.0 s = 77% of wall / hot CE-cache-hit 4.1 s).

---

## Overview

Recall is THE core feature. This program improves it along three independent axes, delivered as **three trains**. Original ordering put the model swap before the restructure so the swap's clean numbers would scope the restructure; **reordered 2026-07-09**: the trace sweep on v5.117/5.30 produced a clean, current-model baseline (cold 24,596 ms; CE 3-pass 19.0 s = 77%; warm cache-hit 4,068 ms), so the restructure is scoped by those numbers NOW and the model swap goes last — its LongMemEval A/B then runs against the final pipeline shape instead of a shape that changes underneath it.

- **Train 1 — Move recall to the backend AS-IS (#85). SHIPPED** (`8ae9e52c` Train 1, `219dd61f` Train 1.5 forward-only, #163). Core is a thin forwarder; dual-path flag removed; stdio path dropped.
- **Train 2 — Restructure, SCOPED BY THE 2026-07-09 BASELINE.** Build the parallel structure the current-model numbers justify, per-piece, cheapest-first. Design mock exists (`f9de7cef`); BUILD is gated per piece.
- **Train 3 — Ettin model swap + keep-warm.** Swap the CE reranker to Ettin (the #32 winner) on the restructured pipeline, kill the idle-unload (~7 s cold-load). Gated by LongMemEval memory-domain A/B.

**Governing discipline (two standing user directives):**
1. **Quality gates everything** — every latency change is A/B'd on **LongMemEval recall@k on the memory domain**. "A speedy system that spits out garbage is not useful." Speed and quality are weighted **equally**.
2. **DESIGN vs BUILD** — the design mock documents the full parallel structure for free; each train's BUILD is gated on measurement. Do NOT pre-commit all trains.

**Accepted trade-off of the reorder (eyes open):** sizing the restructure against the GTE cost profile can over-build relative to post-Ettin residuals — a faster model may shrink what bounded-parallelism was worth building. Mitigation: Train 2 builds per-piece, cheapest-first (async side-effects fork → `multi_passage` toggle → bounded-parallel gather LAST), and the heavier pieces re-check their justification against the Train 3 swap plan before build.

---

## The 3 trains

### Train 1 — Move recall to the backend AS-IS (#85) — SHIPPED

Shipped as `8ae9e52c` (Train 1) + `219dd61f` (Train 1.5 forward-only, #163). Backend `POST /recall` runs the existing `Retriever`; core `recall` MCP handler is a thin forwarder; the dual-path flag and stdio/no-backend path were removed in 1.5. `_apply_recall_side_effects` split landed per the boundary design (backend half = DB writes; core half = SR transition/action-buffer/replay counters). `mode="landscape"` stays core-only. Byte-identical parity + e2e + quality gates passed at cutover. Historical detail: `docs/plans/recall-pipeline-to-backend-2026-07-04.md`.

---

### Train 2 — Restructure, SCOPED BY THE 2026-07-09 BASELINE

**Scope.** Implement the proposed structure (`docs/diagrams/specs/recall-proposed-optimized-2026-07-04.yaml`, `f9de7cef`) — **but only the parts the current-model numbers justify.** Baseline that scopes this train: 2026-07-09 sweep — cold 24,596 ms with CE 3-pass = 19.0 s (77% of wall); warm CE-cache-hit 4,068 ms. The design is a mock; the build is gated per-piece, cheapest-first.

**Deliverables (each gated on the 2026-07-09 numbers + LongMemEval where it touches ranking; build in this order).**
- **Async side-effects fork (cheap + safe — the low-risk keeper, FIRST).** Finalize (node 19) fans to BOTH the return path (node 21, critical) AND the side-effect path (node 20, async, off the critical path). Removes the blocking heat-boost/SR write from response latency. Low risk, high certainty — build this.
- **`multi_passage` → optional, off-by-default toggle** (`MULTI_PASSAGE_RERANK_ENABLED`, gate on LongMemEval). It is a quality knob, not a default cost. **Kept, not dropped.**
- **Bounded-parallelism structure (LAST, and re-check against Train 3 before building)** per the Design Principles below — parallel STRUCTURE + bounded thread budget, GIL-aware (ML stages parallelize freely; Python/graph stages serial-by-default). Before building this piece, sanity-check its projected win against the post-Ettin cost profile Train 3 will create (a ~2–6× faster CE shrinks the concurrency prize) — do not build a gather whose justification the next train deletes.
- **CE passes stay SEPARATE + CONCURRENT (three passes: memory cross_encoder ∥ optional memory multi_passage ∥ wiki crossfuse-CE).** The risky **CE-pool-MERGE is DROPPED.** Merging saves ≈0 compute (the pairs sum regardless of whether they run in one pass or three) and adds quality risk — so keep the three passes separate-concurrent instead. Concurrent CE requires **replicated model instances or a batching inference server** (not just a thread divide) — scope that as part of the build, and note the replication mechanism must survive the Train 3 model swap (design it model-agnostic).
- **LATE UNION**: because the CE passes are separate, the memory pool and wiki pool stay separate through their own CE and only union at finalize + cross-type dedup (node 19), not at build (node 10).

**Gate.** The **2026-07-09 baseline scopes what gets built**; every ranking-affecting piece (the `multi_passage` toggle, any K-trim, any structural change that reorders candidates) is A/B'd on **LongMemEval recall@k on the memory domain**. The restructure itself must hold recall@k parity — it reorders execution, not ranking. Speed AND quality equally weighted. Output of this train: a **restructured-pipeline warm baseline** (same warm-floor checklist) that Train 3's swap A/B runs against.

**Risk.** Complexity cost is real and lands on THE core feature — accept it *deliberately* as the price of scalability, but bound it: the worst case is **worst-case-sequential in MILLISECONDS, not in BUGS** (see Principle 5). Uncapped concurrency on few cores THRASHES (the onnx-bug failure mode) → bounded pools are mandatory (Principle 2). Reorder-specific risk: over-building against GTE costs — bounded by the cheapest-first order and the pre-build re-check of the heavy pieces.

**Why this order (Train 2 second — user decision 2026-07-09, ADR-0072).** The old plan wanted the swap's clean numbers first; the 2026-07-09 sweep already delivered clean current-model numbers, which removes most of that argument. Restructure-first means: the structural levers (side-effect fork, pass toggling, late union) are model-agnostic and keep their value through any future model change, and Train 3's quality A/B runs once, against the final pipeline shape.

---

### Train 3 — Ettin model swap + keep-warm

**Scope.** Swap the CE reranker from **GTE-reranker-ModernBERT → Ettin-reranker** (32M primary / 68M safety fallback — same ModernBERT architecture, Apache-2.0, no ONNX export needed). Ettin is the **convergent #32 winner** (~2–6× faster per CE pass). **PLUS keep-model-warm**: kill the idle-unload so the model stays resident — this removes the ~7 s cold-load. The change is small: a model swap in `ml_client` + the gate. Runs on the Train 2 restructured pipeline (concurrent CE passes / replicated instances must load Ettin identically — the Train 2 replication mechanism is model-agnostic by design).

**Deliverables.**
- Ettin-32M as the primary CE model; 68M wired as a safety fallback if the gate on 32M is marginal.
- Keep-warm: disable the idle-unload path so the CE model is not evicted between recalls.
- The **final warm baseline measurement** post-swap + post-keep-warm, same box, per the recall-perf warm-floor checklist — the program's exit number.

**Gate (the real risk lives here).** **LongMemEval recall@k on the MEMORY domain**, A/B against the Train 2 restructured baseline. Ettin's published −0.006 delta is **general MTEB, NOT conversational memory** — the memory domain is the actual risk surface, so the gate must measure *that* domain, not trust the general number. If 32M regresses recall@k beyond tolerance, fall back to 68M; if 68M also regresses, the swap aborts (same discipline that killed onnx-int8, ADR-0043) — and the program still keeps every Train 2 structural win.

**Risk.** Quality regression on the memory domain (mitigated by the domain-specific gate + 68M fallback + abort path that preserves Train 2's gains). Latency risk is low — Ettin is same-arch, strictly cheaper. Keep-warm trades idle RAM for the ~7 s cold-load; acceptable given recall is the flagship path.

**Why this order (Train 3 last — user decision 2026-07-09, ADR-0072).** The swap is the highest quality-risk item; running it last means its A/B measures exactly one variable (the model) on a stable, final pipeline shape. An aborted swap loses nothing structural.

---

## Design principles

The core of the plan — the outcome of the user + advisor debate. These govern *how* Train 2 (and any future recall parallelism) is built.

1. **`--cpus 2` is the accessibility FLOOR, not a ceiling.** Design for the **scaling curve**: parallel STRUCTURE + a **bounded thread budget** (total threads ≈ ncpu, split across the concurrent tasks). On 2 cores each concurrent task gets ~1 thread and runs ~sequentially — graceful, no thrash. On more cores the *same* structure fans out and speeds up. Serial-by-design bakes a ceiling that no amount of extra cores can beat; parallel-structure-with-bounded-budget degrades gracefully at the floor AND scales past it.

2. **Uncapped concurrency on few cores THRASHES** — worse than serial (this was the onnx bug). So **bounded pools are mandatory.** The bound is what makes the floor case graceful instead of catastrophic.

3. **GIL-aware split (the crux).** The two stage classes must be treated differently:
   - **ML stages** (CE, embed, torch) are **native and release the GIL** → threads parallelize freely AND **degrade gracefully to serial** on few cores → **green light** to parallelize by default.
   - **Python / graph stages** (convex fusion, PPR, networkx spreading BFS compute) are **GIL-bound** → the only real parallelism is **multiprocessing**, which is a **FLOOR-CASE LOSS on 2 cores** (IPC + pickle overhead) that **does NOT degrade to sequential**. This asymmetry is the crux: unlike ML threads, a GIL-bound multiprocess split *cannot* fall back gracefully. So these stages are **serial-by-DEFAULT**; parallelize them ONLY if the numbers prove a win.
   - (Spreading is **mixed**: per-depth DB IO can overlap across cores (v5.99/v5.104 batching), the BFS compute between batches cannot — annotate honestly, do not overclaim a parallel-gather win.)

4. **"3 concurrent CE passes" is not free threading.** It needs **replicated model instances or a batching inference server**, not just a thread divide — scope the mechanism as part of Train 2's build, and keep it model-agnostic (it must survive Train 3's swap).

5. **Complexity cost is real and lands on THE core feature.** Accept it **deliberately** as the price of scalability. The bound on the downside: worst case is **worst-case-sequential in MILLISECONDS, not worst-case in BUGS** — the bounded, GIL-aware structure guarantees the floor case is a graceful serial-ish run, never a thrash or a correctness regression.

6. **QUALITY gates everything.** Ettin, the `multi_passage` toggle, any K-trim, any merge, any reorder: all A/B'd on **LongMemEval recall@k on the memory domain.** Speed AND quality are weighted **equally** (standing user directive).

7. **DESIGN vs BUILD.** The mock documents the full parallel structure for free (design is cheap). BUILD each train gated on measurement — **do not pre-commit all trains.**

---

## Measurement plan

Each train produces the measurement that scopes the next. This is the spine of the DESIGN-vs-BUILD discipline (Principle 7).

| Train | What it measures | Method | What the number decides |
|---|---|---|---|
| **1 (backend move) — SHIPPED** | Latency neutrality (no regression) + correctness parity | Byte-identical dual-path A/B (OFF vs ON), recall e2e, Tempo waterfall | Confirmed safe/neutral; shipped `8ae9e52c`/`219dd61f`. |
| **2 (restructure)** | Per-piece latency win vs complexity, on the **2026-07-09 current-model baseline** (cold 24.6 s / CE 3-pass 19.0 s / warm hit 4.1 s); recall@k parity on every ranking-affecting piece | Warm-floor checklist (≥6 warm runs, median, same box, backend fixed) + 6-concurrent scenario + LongMemEval per toggle | Which structural pieces get built at all (cheapest-first; heavy pieces re-checked against the Train 3 swap). Output: the **restructured warm baseline** Train 3 A/Bs against. |
| **3 (Ettin + keep-warm)** | Warm CE cost/pass after the swap + keep-warm; recall@k on the memory domain vs the Train 2 baseline | Same warm-floor checklist + LongMemEval memory-domain A/B | Go/no-go on the model swap (32M → 68M fallback → abort). The program's exit number. |

**On the CE wall.** The 2026-07-09 sweep resolves the old "11.3 s artifact" question: real composition is **3 GTE passes ≈ 19.0 s on a cold model = 77% of a 24.6 s cold wall**, and a CE-cache hit collapses the repeat call to 4.1 s. Design Train 2 against these numbers; Train 3's keep-warm addresses the ~7 s cold-load component.

**Baseline hygiene.** Canonical baseline = `docs/diagrams/mcp-tool-traces-2026-07-09.md` (cold 24,596 ms; hot cache-hit 4,068 ms). The older "warm ~1409 ms" figure undercounts by ~6 s of MCP-wrapper overhead — do not use. ADR-0030's standing finding (cold ≈ warm, recall is surreal-IO bound) frames the DB-boundary reasoning but must be confirmed by measurement, not assumed.

---

## Open questions

1. **Which Train 2 pieces survive the pre-build re-check?** The async fork and `multi_passage` toggle are safe builds; the bounded-parallel gather must justify itself against a cost profile that Train 3's ~2–6× CE speedup will shrink. Answered per-piece at Train 2 build time.
2. **CE replication mechanism.** Replicated model instances vs a batching inference server for the three concurrent CE passes — which, at what memory cost, and confirmed model-agnostic (survives the swap)? Scope in Train 2.
3. **Side-effect write timing (partially answered by Train 1's split).** The async-side-effects fork removes the blocking heat/SR write from the critical path — but Train 1 already made the DB half a localhost write. Measure whether the async fork is still worth building, or whether the localhost write is already cheap enough.
4. **`multi_passage` value.** Does the off-by-default `multi_passage` pass earn its keep on any query class per LongMemEval? If never, it can be removed rather than toggled.
5. **Ettin 32M vs 68M.** Is 32M sufficient on the memory domain, or is 68M needed for the quality floor? Train 3's gate answers this.
6. **ADR-0044 / #32 doc convergence.** The CE-model decision ADR (0044) and the #32 research doc are being written in parallel; Train 3 should cite the final Ettin decision from those once landed.

---

## Dead ends (do not revisit)

- **onnx dynamic-int8** — REJECTED (ADR-0043); backend fully removed (ADR-0067). Torch stays the CE backend; no onnx dependency in the `/recall` path.
- **OpenVINO / IPEX** — Intel-only. This is an **AMD box** → not applicable.
- **CE top-k decouple (#161)** — **CLOSED unmerged.** Superseded by this refactor and diminished by the Ettin swap (a faster model shrinks the per-pass cost that the top-k knobs were trying to trim). (Note: the pipeline-to-backend doc refers to a "#28" CE-topk item; #161 is the authoritative closed issue for this program.)
- **Single merged CE pass / `multi_passage` dropped** — the earlier `3c16b33b` mock's design. **Reversed.** The CE-pool-MERGE is dropped (saves ≈0 compute); the three CE passes stay separate-concurrent, and `multi_passage` is kept as an off-by-default toggle.
- **Recall-output cache (#88 / caching-train Car 3)** — killed indefinitely (ADR-0071, supersedes ADR-0052). Not a lever for this program.

---

## References

- **Train 1 detail + audit:** `docs/plans/recall-pipeline-to-backend-2026-07-04.md` (BLUF, target architecture, migration strategy, measure-first gate, and the Statefulness + Fanout audit whose verdict was GO-WITH-CAVEATS; shipped).
- **Train 2 target structure:** `docs/diagrams/specs/recall-proposed-optimized-2026-07-04.yaml` (commit `f9de7cef` — the current design-for-scaling mock; supersedes the earlier `3c16b33b` merged-CE mock).
- **Baselines:** `docs/diagrams/mcp-tool-traces-2026-07-09.md` (canonical, post-R3); historical: `docs/diagrams/specs/recall-cold-trace-2026-07-04.yaml`, `recall-warm-cache-hit.yaml`, `recall-warm-cache-miss.yaml`.
- **#32 research doc** (Ettin / SOTA CE model) — being written in parallel.
- **ADR-0044** (CE model decision) — forthcoming, parallel with #32. **ADR-0072** (train reorder, 2026-07-09).
- **ADR-0030** (recall is surreal-IO bound), **ADR-0034** (obs standard + traceparent, SHIPPED), **ADR-0043** (onnx dynamic-int8 REJECTED), **ADR-0067** (onnx backend removed), **ADR-0071** (recall-output cache killed).
- **Perf method:** `docs/testing/recall-perf-checklist.md` (warm-floor checklist).
