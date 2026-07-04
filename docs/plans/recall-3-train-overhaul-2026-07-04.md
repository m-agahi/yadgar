# Plan: Recall pipeline overhaul — the 3-train program

**Status:** AGREED master plan (from a long user + advisor design session). Encodes *what* and *in what order*; each train's BUILD is separately gated on the prior train's measurement.
**Date:** 2026-07-04
**Scope:** The flagship `recall` pipeline — architecture (core→backend), model (CE swap + keep-warm), and structure (bounded-parallel restructure).
**Related tasks / ADRs:** #85 (recall→backend endpoint), #32 (SOTA CE model / Ettin research — doc being written in parallel), #161 (CE top-k decouple/knobs — **CLOSED unmerged**, superseded), ADR-0030 (recall is surreal-IO bound), ADR-0034 (obs standard + traceparent — SHIPPED), ADR-0043 (CE onnx dynamic-int8 REJECTED), ADR-0044 (CE model decision — **forthcoming**, being written in parallel with the #32 research).
**Reference docs:** `docs/plans/recall-pipeline-to-backend-2026-07-04.md` (Train 1 detail + Statefulness/Fanout audit), `docs/diagrams/specs/recall-proposed-optimized-2026-07-04.yaml` (Train 3 target structure, commit `f9de7cef`; supersedes the earlier `3c16b33b` merged-CE mock), `docs/diagrams/specs/recall-cold-trace-2026-07-04.yaml` + `recall-warm-cache-*.yaml` (current baselines).

---

## Overview

Recall is THE core feature. This program improves it along three independent axes, delivered as **three trains** whose ordering is deliberate (advisor-reordered). The trains are *sequenced* — but only ONE hard dependency exists (Train 3 is scoped by Train 2's numbers). The other two are independent and could ship in any order; the chosen order reflects priority and risk, not a rigid chain.

- **Train 1 — Move recall to the backend AS-IS (#85).** Architectural: relocate the *existing* pipeline into a backend `POST /recall` endpoint; core becomes a thin forwarder. **Latency-neutral by design — sold as no-scatter/soundness, NOT as speed.**
- **Train 2 — Ettin model swap + keep-warm.** Swap the CE reranker to Ettin (the #32 winner), kill the idle-unload (removes the ~7 s cold-load). This is also the **clean warm baseline** that scopes Train 3.
- **Train 3 — Restructure, SCOPED BY POST-ETTIN NUMBERS.** Build only the parallel structure the post-Ettin measurement justifies. The design mock exists (`f9de7cef`); BUILD is gated.

**Governing discipline (two standing user directives):**
1. **Quality gates everything** — every latency change is A/B'd on **LongMemEval recall@k on the memory domain**. "A speedy system that spits out garbage is not useful." Speed and quality are weighted **equally**.
2. **DESIGN vs BUILD** — the design mock documents the full parallel structure for free; each train's BUILD is gated on the PRIOR train's measurement. Do NOT pre-commit all three.

---

## The 3 trains

### Train 1 — Move recall to the backend AS-IS (#85)

**Scope.** Backend gains `POST /recall`, which runs the **EXISTING** `Retriever` (+ fanout orchestration) locally — via `LocalMLClient` (models already loaded backend-side) and a `StorageEngine` pointed at **localhost** SurrealDB (`:8000`). Core's `recall` MCP handler becomes a **thin forwarder**: parse args → assemble `RecallRequest` → `httpx` POST → return results. No code port: the whole `yadgar` package (incl. `yadgar.retrieval.*`) is **already** installed in the backend image (`Dockerfile.backend:18`, `pip install "/app[ml]"`), so this is "add a route + gut core to forward," not a relocation of modules.

**Deliverables.**
- Backend `/recall` route (Bearer-authed via the existing `_require_admin_token`, same as `/embed` and `/rerank`); stateless per request, knobs carried explicitly in `RecallRequest.knobs`.
- Core thin forwarder behind a top-level **dual-path flag** (`RECALL_BACKEND_ENABLED`): OFF (default during migration + permanent for stdio/no-backend mode) runs the pipeline locally exactly as today; ON forwards to the backend.
- **Split of `_apply_recall_side_effects`** (`recall.py:379`) at the boundary — the one required refactor (see Risk):
  - **Backend half** (in `/recall`, becomes localhost writes): DB writes — `boost_memories_access` (heat + `last_accessed`) and `thermo.record_access` / `update_memory_metamemory`.
  - **Core half** (stays core-side, runs on the returned results): SR successor-representation transition (`_cognitive_map` + `_last_recalled_ids`), action-buffer capture, replay/checkpoint counter — these need core-process singletons a stateless backend does not have.
- `mode="landscape"` stays **core-only** in the first cut (it bypasses the fanout via `AstrocytePool.consensus_retrieve`).
- `BACKEND_VERSION` bump (the recall contract moves service tracks; asserted by the canonical-version drift-guard).

**Gate.** Byte-identical dual-path contract test (same corpus + queries through OFF and ON paths → equal ranked ids + scores) **and** recall e2e green **and** the LongMemEval memory-domain quality gate non-regressing. Because ON runs the same code as OFF, parity is the *expectation*; any drift is a wiring bug that aborts cutover.

**Risk.** Blast radius is high (THE feature), so mitigation is heavy: dual-path flag → atomic, reversible cutover (rollback = flip the flag, no data/schema migration); byte-identical contract test; quality gate. The single non-obvious hazard: `_apply_recall_side_effects` is a **mixed DB/core-local unit** — moving it *wholesale* to the backend would break the three core-local writes (no `_st` in a stateless handler). The **split above is the entire delta between "breaks" and "clean."** The Statefulness+Fanout audit (in the pipeline-to-backend doc) verdict is **GO-WITH-CAVEATS: no category-(c) hard-blocker; nothing in the relocating compute path is core-bound state.**

**Why this order (Train 1 first — priority, NOT dependency).** Train 1 is **independent** — it neither blocks nor is blocked by the other two. It goes first because it is the *architectural* cleanup (kill the core/backend scatter) that every later change lands on top of, and because it is cheap to build and fully reversible. It is explicitly **latency-neutral**: the dominant costs (CE ~compute, spreading ~CPU) already live backend-side or are compute, so relocation saves neither. **Do not sell Train 1 as speed.** Its value is soundness/no-scatter and a clean single-container path for Train 3 to restructure.

---

### Train 2 — Ettin model swap + keep-warm

**Scope.** Swap the CE reranker from **GTE-reranker-ModernBERT → Ettin-reranker** (32M primary / 68M safety fallback — same ModernBERT architecture, Apache-2.0, no ONNX export needed). Ettin is the **convergent #32 winner** (~2–6× faster per CE pass). **PLUS keep-model-warm**: kill the idle-unload so the model stays resident — this removes the ~7 s cold-load. The change is small: a model swap in `ml_client` + the gate. It does **not even require Train 1** (it is a model swap, orthogonal to where the pipeline runs).

**Deliverables.**
- Ettin-32M as the primary CE model; 68M wired as a safety fallback if the gate on 32M is marginal.
- Keep-warm: disable the idle-unload path so the CE model is not evicted between recalls.
- The **clean warm baseline measurement** (this is a first-class deliverable, not a side effect — it scopes Train 3): warm CE ≈ 3.9 s/pass (micro-bench), post-swap and post-keep-warm, on the same box, per the recall-perf warm-floor checklist.

**Gate (the real risk lives here).** **LongMemEval recall@k on the MEMORY domain.** Ettin's published −0.006 delta is **general MTEB, NOT conversational memory** — the memory domain is the actual risk surface, so the gate must measure *that* domain, not trust the general number. If 32M regresses recall@k on the memory domain beyond tolerance, fall back to 68M; if 68M also regresses, the swap aborts (same discipline that killed onnx-int8, ADR-0043).

**Risk.** Quality regression on the memory domain (mitigated by the domain-specific gate + 68M fallback). Latency risk is low — Ettin is same-arch, strictly cheaper. Keep-warm trades idle RAM for the ~7 s cold-load; acceptable given recall is the flagship path.

**Why this order (Train 2 second — it SCOPES Train 3).** Train 2 produces the **clean, warm, post-Ettin numbers** that determine *how much* Train 3 needs to build. After Ettin (CE ~0.6–3.9 s/pass warm) the residual CPU-bound time may be small — so the restructure must be **scoped by measurement**, not designed blind. Train 2 is the measurement that makes Train 3's scope honest. It is placed after Train 1 by priority only (it does not depend on Train 1).

---

### Train 3 — Restructure, SCOPED BY POST-ETTIN NUMBERS

**Scope.** Implement the proposed structure (`docs/diagrams/specs/recall-proposed-optimized-2026-07-04.yaml`, `f9de7cef`) — **but only the parts the post-Ettin numbers justify.** The design is a mock; the build is gated per-piece.

**Deliverables (each gated on the Train 2 numbers + LongMemEval where it touches ranking).**
- **Async side-effects fork (cheap + safe — the low-risk keeper).** Finalize (node 19) fans to BOTH the return path (node 21, critical) AND the side-effect path (node 20, async, off the critical path). Removes the blocking heat-boost/SR write from response latency. Low risk, high certainty — build this.
- **`multi_passage` → optional, off-by-default toggle** (`MULTI_PASSAGE_RERANK_ENABLED`, gate on LongMemEval). It is a quality knob, not a default cost. **Kept, not dropped.**
- **Bounded-parallelism structure** per the Design Principles below — parallel STRUCTURE + bounded thread budget, GIL-aware (ML stages parallelize freely; Python/graph stages serial-by-default).
- **CE passes stay SEPARATE + CONCURRENT (three passes: memory cross_encoder ∥ optional memory multi_passage ∥ wiki crossfuse-CE).** The risky **CE-pool-MERGE is DROPPED.** Merging saves ≈0 compute (the pairs sum regardless of whether they run in one pass or three) and adds quality risk — so keep the three passes separate-concurrent instead. Concurrent CE requires **replicated model instances or a batching inference server** (not just a thread divide) — scope that as part of the build.
- **LATE UNION**: because the CE passes are separate, the memory pool and wiki pool stay separate through their own CE and only union at finalize + cross-type dedup (node 19), not at build (node 10).

**Gate.** The **post-Ettin measurement scopes what gets built.** After Ettin the residual CPU-bound time may be small → build only what the numbers justify. Every ranking-affecting piece (the `multi_passage` toggle, any K-trim, any structural change that reorders candidates) is A/B'd on **LongMemEval recall@k on the memory domain**. Speed AND quality equally weighted.

**Risk.** Complexity cost is real and lands on THE core feature — accept it *deliberately* as the price of scalability, but bound it: the worst case is **worst-case-sequential in MILLISECONDS, not in BUGS** (see Principle 5). Uncapped concurrency on few cores THRASHES (the onnx-bug failure mode) → bounded pools are mandatory (Principle 2).

**Why this order (Train 3 last — it DEPENDS on Train 2).** This is the one hard dependency in the program: Train 3's scope is a function of Train 2's clean numbers. Building the restructure before measuring the post-Ettin baseline would over-build (or mis-build) against a cost profile that Ettin already changed. Gate → measure → scope → build.

---

## Design principles

The core of the plan — the outcome of the user + advisor debate. These govern *how* Train 3 (and any future recall parallelism) is built.

1. **`--cpus 2` is the accessibility FLOOR, not a ceiling.** Design for the **scaling curve**: parallel STRUCTURE + a **bounded thread budget** (total threads ≈ ncpu, split across the concurrent tasks). On 2 cores each concurrent task gets ~1 thread and runs ~sequentially — graceful, no thrash. On more cores the *same* structure fans out and speeds up. Serial-by-design bakes a ceiling that no amount of extra cores can beat; parallel-structure-with-bounded-budget degrades gracefully at the floor AND scales past it.

2. **Uncapped concurrency on few cores THRASHES** — worse than serial (this was the onnx bug). So **bounded pools are mandatory.** The bound is what makes the floor case graceful instead of catastrophic.

3. **GIL-aware split (the crux).** The two stage classes must be treated differently:
   - **ML stages** (CE, embed, torch) are **native and release the GIL** → threads parallelize freely AND **degrade gracefully to serial** on few cores → **green light** to parallelize by default.
   - **Python / graph stages** (convex fusion, PPR, networkx spreading BFS compute) are **GIL-bound** → the only real parallelism is **multiprocessing**, which is a **FLOOR-CASE LOSS on 2 cores** (IPC + pickle overhead) that **does NOT degrade to sequential**. This asymmetry is the crux: unlike ML threads, a GIL-bound multiprocess split *cannot* fall back gracefully. So these stages are **serial-by-DEFAULT**; parallelize them ONLY if the numbers prove a win.
   - (Spreading is **mixed**: per-depth DB IO can overlap across cores (v5.99/v5.104 batching), the BFS compute between batches cannot — annotate honestly, do not overclaim a parallel-gather win.)

4. **"3 concurrent CE passes" is not free threading.** It needs **replicated model instances or a batching inference server**, not just a thread divide — scope the mechanism as part of Train 3's build.

5. **Complexity cost is real and lands on THE core feature.** Accept it **deliberately** as the price of scalability. The bound on the downside: worst case is **worst-case-sequential in MILLISECONDS, not worst-case in BUGS** — the bounded, GIL-aware structure guarantees the floor case is a graceful serial-ish run, never a thrash or a correctness regression.

6. **QUALITY gates everything.** Ettin, the `multi_passage` toggle, any K-trim, any merge, any reorder: all A/B'd on **LongMemEval recall@k on the memory domain.** Speed AND quality are weighted **equally** (standing user directive).

7. **DESIGN vs BUILD.** The mock documents the full parallel structure for free (design is cheap). BUILD each train gated on the PRIOR train's measurement — **do not pre-commit all three.**

---

## Measurement plan

Each train produces the measurement that scopes the next. This is the spine of the DESIGN-vs-BUILD discipline (Principle 7).

| Train | What it measures | Method | What the number decides |
|---|---|---|---|
| **1 (backend move)** | Latency neutrality (no regression) + correctness parity | Byte-identical dual-path A/B (OFF vs ON), recall e2e, Tempo waterfall | Confirms the move is safe/neutral. Train 1 is NOT a speed play — the measurement is a **no-regression check**, not a win to chase. |
| **2 (Ettin + keep-warm)** | Clean **warm** CE cost/pass after the swap + keep-warm; recall@k on the memory domain | Recall-perf warm-floor checklist (≥6 warm runs, median, same box, backend fixed) + LongMemEval memory-domain A/B | The **post-Ettin warm baseline that scopes Train 3.** If residual CPU-bound time is small, Train 3 shrinks. Also the go/no-go on the model swap itself (recall@k). |
| **3 (restructure)** | Per-piece latency win vs complexity, on the post-Ettin baseline; recall@k on every ranking-affecting piece | Same warm-floor checklist + 6-concurrent scenario + LongMemEval per toggle | Which structural pieces get built at all. Build only what the numbers justify. |

**On the "11.3 s CE wall".** The 11.3 s figure is almost certainly a **COLD-load + first-pass artifact**, not the real warm cost — the warm micro-bench is ≈ 3.9 s/pass. A clean bench (Train 2, post-keep-warm) **resolves** this. Do NOT design Train 3 against 11.3 s; design against the Train 2 warm number.

**Baseline hygiene.** Use the corrected baseline (new query ~23 s; exact repeat / CE-cache hit ~5 s), NOT the older "warm ~1409 ms" figure, which undercounts by ~6 s of MCP-wrapper overhead. ADR-0030's standing finding (cold ≈ warm, recall is surreal-IO bound) frames the DB-boundary reasoning but must be confirmed by measurement, not assumed.

---

## Open questions

1. **Post-Ettin residual (blocks Train 3 scope).** After Ettin + keep-warm, how much CPU-bound time actually remains? If small, Train 3 collapses to just the async-side-effects fork + the `multi_passage` toggle, and the heavier bounded-parallel gather may not be worth building. Train 2's measurement answers this.
2. **CE replication mechanism.** Replicated model instances vs a batching inference server for the three concurrent CE passes — which, and at what memory cost? Scope in Train 3.
3. **Side-effect write timing (partially answered by Train 1's split).** The async-side-effects fork (Train 3) removes the blocking heat/SR write from the critical path — but Train 1 already makes the DB half a localhost write. Measure whether the async fork is still worth building after Train 1, or whether the localhost write is already cheap enough.
4. **`multi_passage` value.** Does the off-by-default `multi_passage` pass earn its keep on any query class per LongMemEval? If never, it can be removed rather than toggled.
5. **Ettin 32M vs 68M.** Is 32M sufficient on the memory domain, or is 68M needed for the quality floor? Train 2's gate answers this.
6. **ADR-0044 / #32 doc convergence.** The CE-model decision ADR (0044) and the #32 research doc are being written in parallel; Train 2 should cite the final Ettin decision from those once landed.

---

## Dead ends (do not revisit)

- **onnx dynamic-int8** — REJECTED (ADR-0043). Torch stays the CE backend; no onnx dependency in the `/recall` path.
- **OpenVINO / IPEX** — Intel-only. This is an **AMD box** → not applicable.
- **CE top-k decouple (#161)** — **CLOSED unmerged.** Superseded by this refactor and diminished by the Ettin swap (a faster model shrinks the per-pass cost that the top-k knobs were trying to trim). (Note: the pipeline-to-backend doc refers to a "#28" CE-topk item; #161 is the authoritative closed issue for this program.)
- **Single merged CE pass / `multi_passage` dropped** — the earlier `3c16b33b` mock's design. **Reversed.** The CE-pool-MERGE is dropped (saves ≈0 compute); the three CE passes stay separate-concurrent, and `multi_passage` is kept as an off-by-default toggle.

---

## References

- **Train 1 detail + audit:** `docs/plans/recall-pipeline-to-backend-2026-07-04.md` (BLUF, target architecture, migration strategy, measure-first gate, and the Statefulness + Fanout audit whose verdict is GO-WITH-CAVEATS).
- **Train 3 target structure:** `docs/diagrams/specs/recall-proposed-optimized-2026-07-04.yaml` (commit `f9de7cef` — the current design-for-scaling mock; supersedes the earlier `3c16b33b` merged-CE mock).
- **Baselines:** `docs/diagrams/specs/recall-cold-trace-2026-07-04.yaml`, `recall-warm-cache-hit.yaml`, `recall-warm-cache-miss.yaml`.
- **#32 research doc** (Ettin / SOTA CE model) — being written in parallel.
- **ADR-0044** (CE model decision) — forthcoming, parallel with #32.
- **ADR-0030** (recall is surreal-IO bound), **ADR-0034** (obs standard + traceparent, SHIPPED), **ADR-0043** (onnx dynamic-int8 REJECTED).
- **Perf method:** `docs/testing/recall-perf-checklist.md` (warm-floor checklist).
