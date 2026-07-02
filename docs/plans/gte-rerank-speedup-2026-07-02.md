# GTE-ModernBERT Rerank Speedup — Warm ~1.43s → ~1.0s (task #92) — 2026-07-02

> Measure-first, quality-gated design. Target: post-v5.97 warm recall ~1.43s → ~1.0s
> on THIS box (backend `--cpus 2`, RAM-constrained; core `--cpus 1`). All timings below
> are from a standalone off-daemon micro-benchmark of the REAL model on 2 pinned cores
> (`taskset -c 0,1`, `.venv` torch 2.11 / sentence_transformers 5.4.1), NOT the live
> daemon (freeze-prone). Source facts cite `file:line` at commit on `docs/gte-rerank-speedup`.

## TL;DR — the headline finding inverts the naive plan

The ~720 ms warm-**HIT** CE is **NOT** the main cross-encoder call. On a warm-HIT repeat
the main `mode=ce` call is served from the backend LRU cache (~0 ms model time). The 720 ms
is the **uncached multi-passage `mode=pair` single-pair RPCs** — the one CE path that bypasses
the cache (`embed_service.py:716-719` vs the cached `:720-721`). So:

- **#1 lever (zero quality risk): route multi-passage cluster scoring through the cached,
  batched `mode=ce` path** instead of per-cluster `mode=pair` RPCs. Score-identical
  (`score_pair(q,t) == score_cross_encoder(q,[t])[0]`, `ml_client.py:538-541`), and on a
  warm-HIT repeat the combined-cluster texts then cache-hit → CE ≈ 0 ms. **This alone plausibly
  takes warm-HIT CE 720 → ~150-250 ms and hits the ~1.0 s target for the same-query warm case.**
- Naive levers that measurement KILLED: (c) collapsing multi-passage into one batch on *raw
  compute* is **negative** (batching does not beat splitting on 2 cores — see §2), and
  (e) torch thread-count tuning is **flat/negative** (the cgroup quota already confines threads —
  see §2). Their value, if any, is via the **cache** axis, not compute/threads.
- (b) fewer candidate pairs and (a) int8/ONNX quantization are the levers for the **cache-MISS /
  varied-query** warm path (CE ~2.9 s cold, `recall-warm-profile` miss column), which is the
  *true production* warm cost. They carry the quality risk and are gated behind LongMemEval.

## 0. Honest scope of "1.43 s"

The commit `172194d5` figure "warm 2.4s→1.43s" is **scenario #2 = WARM repeat (shadow HIT)** —
the *same query* re-issued. That is a best case: the 10-pair main CE call is a full LRU hit.
The **varied-query** warm path (distinct query each time) pays the cache-MISS CE (~2.9 s per the
profile miss column, ~2050 ms of which is the 10-pair main call — see §2 reconciliation). This
plan targets the ~1.43 s HIT scenario named in the task (#1 lever), and separately addresses the
varied-query path (levers a/b) so the win is not purely same-query.

---

## 1. Where the 720 ms goes — source trace

Model: **`Alibaba-NLP/gte-reranker-modernbert-base`** (`config.py:274`, `GTE_RERANKER_MODEL`).
Loaded as **FP32 PyTorch** `sentence_transformers.CrossEncoder` (`ml_client.py:364-369`), NOT
ONNX. `max_length=512` (`config.py:275`), texts truncated `[:512]`. Runs on the BACKEND container
via `RemoteMLClient._rerank_rpc` → backend `POST /rerank` (`ml_client.py:702-704`,
`embed_service.py:673`). GTE preempts the FlashRank/ST-CrossEncoder fallback chain whenever
`GTE_RERANKER_ENABLED=true` (default, `config.py:273`; `ml_client.py:477-485`). `CROSS_ENCODER_BACKEND=onnx-int8`
only gates the ST fallback that GTE preempts — confirmed dead-end (PR #143 Fix-3).

**Two CE code paths, only one cached:**

| path | fires when | backend handler | cached? | `file:line` |
|---|---|---|---|---|
| main `mode=ce` | every recall — scores `memories[:CROSS_ENCODER_TOP_K]` | `_score_ce_with_cache` (LRU) | **YES** | `embed_service.py:720-721`, `618-670` |
| multi-passage `mode=pair` | per cluster of ≥2 (query-dependent) | `ml.score_pair` direct | **NO** | `embed_service.py:716-719`; `_reranking_multi_passage.py:28-35` |

The LRU key is `sha256(query)[:16]:sha256(text)[:16]:ckpt` (`embed_service.py:621,631-637`) —
stable across identical repeats since fusion output is deterministic → main call cache-hits on
warm repeat.

**Why "~2.7 calls" (correcting the profile's ceil framing):** it is **1 batched main `ce` call**
(all `TOP_K` memories × 1 variant, or ×2 in open-domain mode — `_build_expanded_pairs`,
`_reranking_cross_encoder.py:18-40,119-126`) **+ ~1.7 single-pair `mode=pair` calls**, one per
qualifying cluster (`multi_passage_rerank` loops `score_single_pair` per cluster,
`_reranking_multi_passage.py:28-35`). It is NOT batch-splitting of the main call — the main call
is one HTTP request with all pairs (`ml_client.py:702-704`, no chunking).

### Reconciliation with the profile (the number that proves the cache story)

Measured **~205 ms per query-passage pair**, dead-linear (§2). `recall-warm-profile` reports
`ce_rerank` **miss=2906 ms, hit=903 ms** (720 CE + ~183 MMR marginal):

- **Cold main call:** `TOP_K=10` × ~205 ms ≈ **2050 ms** ≈ the miss−hit delta (2906−903 ≈ 2000 ms). ✓
- **Warm-HIT:** main 10-pair call cache-hits (~0 ms) + ~3 uncached multi-passage pairs × 205 ≈
  **~615 ms** + MMR ~183 ≈ **~900 ms** ≈ the 903 ms stage; the CE-only slice ≈ 720 ms. ✓

The 720 ms warm-HIT CE is therefore **uncached multi-passage pairs**, full stop. The gap between
"720 ms" and "10 pairs × 205 = 2050 ms" is **the cache**, not fewer pairs and not shorter text.

---

## 2. Micro-benchmark — the levers, measured on 2 pinned cores

Standalone, off-daemon, `taskset -c 0,1` (simulating the `--cpus 2` cgroup), model warm, median
of n runs. Scripts: `/tmp/gte_thread_bench.py`, `/tmp/gte_scaling_bench.py`. Host has 24 cores
(`nproc`); `.venv` torch/ST versions above.

**Per-pair cost is dead-linear and text-length-insensitive (at ~150-token inputs):**

| max_length | 5 pairs | 10 pairs | 20 pairs | ms/pair |
|---|---|---|---|---|
| 512 | 1027 | 2076 | 4102 | ~205 |
| 256 | 1023 | 2018 | 4131 | ~205 |
| 128 | 1019 | 2015 | 4133 | ~205 |

→ **CE cost ≈ (pairs) × ~205 ms.** Candidate/pair count is the only proportional compute lever.
→ **`max_length` reduction buys nothing** at these input lengths (the tested passages are
~150 tokens; the cap sits above the input so lowering it does not truncate → no FLOP change).
Do NOT propose truncation as a lever without first measuring genuinely long (>512-token) inputs;
memory contents are typically short, so this is a non-lever here.

**Threading (lever e) — FLAT/negative when pinned to 2 cores:**

| torch threads | batched 20-pair |
|---|---|
| 1 | 7053 ms |
| 2 | 4074 ms |
| 4 | 3900 ms |
| 12 (current default `os.cpu_count()//2` = 24//2, `embeddings.py:147`) | 3841 ms |

→ The kernel confines threads to the 2-core cgroup; oversubscription (12 threads / 2 cores) costs
**~0**, and forcing threads to 2 is ~6% *slower*. **Reject lever (e).** (The `os.cpu_count()//2=12`
"oversubscription bug" hypothesis is real in code but empirically inert under the cpu quota.)

**Batch-vs-split (lever c on the compute axis) — negative:** 20 pairs in one call = 4074 ms;
20 pairs as 20 separate calls = 3375 ms. On 2 cores the large batched matmul is slightly *worse*
than splitting. Collapsing multi-passage's ~1.7 pairs into the main batch saved **−109 ms**
(i.e. cost more). **Batching is NOT a raw-compute win here** — its ONLY value is enabling the
cache (see #1 lever). This directly contradicts the profile's "batching beats parallelizing"
intuition for *this* model on *these* cores.

**RAM:** backend holds 1 embedding model (all-MiniLM-L6-v2) + 1 reranker (GTE, lazy-loaded, never
both a reranker AND fallback simultaneously) — `embed_service.py:343-370`, `ml_client.py:363-370`.
GTE-ModernBERT-base ≈ 150 M params → ~600 MB FP32 RSS; int8 ≈ ~150-200 MB. Model load ~600 ms
(one-time). No concurrent second reranker. int8 REDUCES RAM (favorable on this box).

---

## 3. Levers — ranked by (ms saved on target scenario ÷ quality risk ÷ feasibility)

### Lever 1 — Cache/route multi-passage pair scoring through the cached `ce` path  ★ headline
- **Mechanism:** replace per-cluster `score_single_pair` (`mode=pair`, uncached) with a single
  `score_cross_encoder(query, [combined_1, combined_2, …])` (`mode=ce`, LRU-cached) call, then
  map scores back to clusters. `multi_passage_rerank` (`_reranking_multi_passage.py:16-47`) already
  computes all `combined` cluster strings up-front before scoring — trivially batchable.
- **Expected:** warm-HIT CE **720 → ~150-250 ms** (main call already cached; combined-cluster texts
  now also cache on repeat). Warm recall **~1.43 → ~0.95-1.1 s** — **hits target** on the same-query
  scenario. Cache-MISS/varied path: one batched RPC instead of ~1.7 serial gated RPCs → saves the
  per-call HTTP + heavy-gate-acquire overhead (small), same raw compute.
- **Quality risk: ZERO.** Score-identical: `score_pair(q,t)` IS `score_cross_encoder(q,[t])[0]`
  (`ml_client.py:538-541`, `465-485`); same GTE model for both modes (`_get_reranker()`,
  `embed_service.py:711`; both route to `_try_gte_reranker`). No ranking change — only *where/whether*
  identical scores are cached.
- **RAM:** none.
- **Feasibility: HIGH.** Pure core-side change in `_reranking_multi_passage.py` + a `score_clusters`
  helper on the CE mixin. No backend change strictly required (route the combined texts through the
  existing `score_cross_encoder`). Optional backend nicety: give `mode=pair` its own cache — but
  routing through `mode=ce` is simpler and needs no backend edit.
- **Go/no-go gate:** parity unit test — batched-cluster scores == per-cluster `score_single_pair`
  scores (exact float match, they call the same function). LongMemEval retrieval-only ndcg@5 must
  be **byte-identical** (this changes nothing observable). If ndcg moves at all → bug, not tradeoff.

### Lever 2 — Reduce candidate pairs fed to CE (varied-query / cache-MISS path)  ★ secondary
- **Mechanism:** `CROSS_ENCODER_TOP_K` 10 → 5 (`config.py:177`; sliced at
  `_reranking_cross_encoder.py:119`), and/or gate the open-domain 2×-variant expansion
  (`_build_expanded_pairs:35-39`) which can double pairs on open-domain queries.
- **Expected:** linear — each removed pair saves ~205 ms on cache-MISS. TOP_K 10→5 ≈ **−1025 ms**
  on the cold/varied main call (2050 → 1025 ms). On warm-HIT: near-zero (main call already cached).
- **Quality risk: MEDIUM — this is a real recall/precision tradeoff.** Fewer memories reranked →
  fewer chances to promote a correct-but-low-BM25/HNSW memory into the top-k.
- **RAM:** none.
- **Feasibility: HIGH** (config knob) but **must be quality-gated**.
- **Go/no-go gate:** LongMemEval retrieval-only, before/after, per §4 bar. Adopt only if ndcg@5 and
  recall@5 deltas are within the bar across ALL question types (multi-session is the sensitive one —
  baseline recall@5=0.825). Capture a k=5/8/10 curve; pick the smallest k that stays within bar.

### Lever 3 — int8 / ONNX-quantize GTE-ModernBERT (both paths)  ★ high payoff, high feasibility risk
- **Mechanism:** export GTE-ModernBERT to ONNX + dynamic int8, load via `CrossEncoder(..., backend="onnx")`
  (ST 5.x supports it; the existing ST-fallback ONNX path is `ml_client.py:438-443`). Halves per-pair
  compute → ~205 → ~100 ms/pair.
- **Expected:** ~2× on EVERY pair, both cached-miss and multi-passage. Cold main call 2050 → ~1025 ms;
  helps the varied-query warm path most. Stacks with Lever 1.
- **Quality risk: MEDIUM** (int8 ≈ ~1% score drift; must verify ranking stability).
- **Feasibility: LOW-MEDIUM / RISKY on this box:**
  - **No GTE ONNX artifact exists** in-repo (only the ST-MiniLM `model_qint8_avx512.onnx` fallback).
  - **ModernBERT ONNX export is non-trivial** — alternating local/global attention + unpadding are
    known-frictional to export; not a one-command optimum export.
  - **onnxruntime↔numpy is BROKEN in the venv** (per `recall-warm-profile` #3) → runtime repair is
    prerequisite. onnxruntime IS transitively available via `sentence_transformers[onnx]` but the
    import currently fails.
  - int8 REDUCES RAM (~600 → ~200 MB) — favorable.
- **Do NOT make the ~1.0 s target contingent on this.** It is a follow-up that deepens the win on the
  varied-query path once the export + runtime are proven. Sequence it AFTER Levers 1-2 land.
- **Go/no-go gate:** (a) `import onnxruntime` succeeds in the backend image; (b) exported model
  produces scores within ~1% of FP32 on a fixed pair set; (c) LongMemEval retrieval-only within §4 bar.

### Rejected levers (measurement-killed — do not pursue on this box)
- **(c) batch-collapse for compute:** negative on 2 cores (§2). Only useful via the cache (Lever 1).
- **(e) torch thread tuning:** flat/negative under the cpu quota (§2).
- **`max_length` truncation:** no FLOP change at typical (short) memory lengths (§2).

---

## 4. Quality-parity method (mandatory before ANY model/quant/candidate change)

**Harness:** `make longmemeval Q=N` (`Makefile:312-315`) — routes through the **unified MCP recall
path** (prod recall, `--unified`), **`--retrieval-only`** (measures rerank ordering directly via
ndcg/recall/mrr, skips the LLM QA step → cheap + deterministic-enough for parity). Reports carry
per-question-type `recall@5/10/50, ndcg@5/10/50, mrr` (see `benchmarks/reports/lme_full_v5.80_qa_*.json`
aggregated block; retrieval-only variants in `benchmarks/results/longmemeval_s_retrieval_*.json`).

**Procedure per lever:**
1. Baseline: `make longmemeval Q=100` on `origin/master`, save the aggregated block.
2. Apply lever behind a config flag; re-run same Q, same seed/stratify.
3. Compare per-type `ndcg@5`, `recall@5`, `mrr`.
4. Plus a **rerank-score parity check** (unit test): on a fixed (query, candidates) set, dump raw CE
   scores before/after. Lever 1 must be exact-equal; Levers 2/3 quantify the drift.

**Acceptable-quality-delta bar (retrieval-only, Q≥100, all question types):**
- Lever 1: **exact parity** — ndcg@5/recall@5/mrr byte-identical. Any movement = bug.
- Levers 2/3: **ndcg@5 ≥ −0.005 (−0.5 pt) AND recall@5 ≥ −0.01 (−1 pt) per type**, no single type
  regressing >1 pt. multi-session (baseline recall@5=0.825, the weakest) is the binding constraint —
  gate on it explicitly. Latency win must exceed ~150 ms to justify any quality cost.

---

## 5. Recommended ordered plan + expected floor

1. **Lever 1 (cache/route multi-passage → `ce`).** Zero-risk, targets the named scenario.
   Warm-HIT CE 720 → ~150-250 ms; **warm ~1.43 → ~0.95-1.1 s. ← TARGET MET (same-query warm).**
   Gate: exact ndcg parity.
2. **Lever 2 (TOP_K 10→5, gate open-domain expansion).** For the varied-query warm path
   (the real production cost). Cold main call 2050 → ~1025 ms. Gate: §4 bar; pick smallest k in-bar.
3. **Lever 3 (int8/ONNX GTE), sequenced last, non-blocking for target.** Repair onnxruntime →
   export → ~2× per pair. Deepens the varied-query win. Gate: runtime + ~1% drift + §4 bar.

**Expected floor after 1-2 (this hardware):**
- **Same-query warm ≈ 0.95-1.1 s** (CE mostly cached; residual = PPR 0-620 ms query-dep + priors/fts/knn ~240 ms + overhead).
- **Varied-query warm ≈ 1.4-1.6 s** without Lever 3, → ~1.1-1.3 s with Lever 3.

**Caveat — PPR dominates the residual and is CPU-bound on `--cpus 1`.** PPR swings **0-620 ms** by
query entity-richness (`recall-warm-profile §1`). Even with CE → 0, an **entity-rich** query stays
**~0.86 s+** (620 PPR + 240 other). The ~1.0 s target is a **median / entity-poor** figure; state it
as such. CE reduction is necessary but not sufficient on entity-rich queries — that residual needs
more CPU (or an output cache, task #88), not more CE tuning.

**When is recall un-improvable on this box?** After Levers 1-2 the warm cost is CE-cache-miss pairs
(int8-bounded by Lever 3) + networkx PPR (CPU-bound, `--cpus 1`) + minimal Surreal round-trips.
Below ~0.9-1.1 s (entity-poor) requires more CPU/RAM or the output cache (#88, skips work rather than
speeding it). GTE tuning cannot go further without a smaller/distilled reranker (parity-risky) or HW.

---

## 6. Implementation findings (v5.98.0 — 2026-07-02)

Levers built and gated. Corrections to the plan's assumptions, verified in code + measurement.

### Lever 1 — SHIPPED (core-only, zero quality risk)
- `multi_passage_rerank` now batches all qualifying-cluster combined texts through ONE
  cached `score_documents` → `score_cross_encoder` (backend `mode=ce`, LRU) call instead of
  per-cluster `score_single_pair` (`mode=pair`, uncached). Parity confirmed in code:
  `score_pair(q,t) == score_cross_encoder(q,[t])[0]` (same GTE forward pass).
- New `score_documents` on `_CrossEncoderMixin` maps circuit-breaker-open whole-list `None`
  → per-document `0.0`, matching `score_single_pair`'s per-pair `None→0.0`.
- Gate = exact-parity unit test (`test_reranking_multi_passage_parity.py`): byte-identical
  `_retrieval_score` vs the pre-v5.98 per-pair loop. PASS.

### Lever 2 — candidate-count (`CROSS_ENCODER_TOP_K` 10→5), flag-gated, LongMemEval-gated
- **Truncation caveat (load-bearing):** in the unified path `cross_encoder_rerank` returns
  `memories_to_score[:CROSS_ENCODER_TOP_K]` — it TRUNCATES the result set to `top_k`
  (`reranking.py:138` calls it with `top_k=None` → the config default; benchmark forces 75 so it
  does not truncate below `max_results=50`). So at `TOP_K=5` **recall@10 / recall@50 collapse to a
  ≤5 ceiling — those columns are truncation artifacts, not quality**. Only **recall@5 / ndcg@5 /
  mrr** are interpretable at reduced `TOP_K`. Gate on recall@5/ndcg@5 (multi-session binding).
- Benchmark harness gained `--settings-override KEY=VALUE` (repeatable) to A/B any Settings field
  against the hardcoded benchmark defaults without editing the runner. Baseline and lever are both
  run at production-realistic `TOP_K` (10 vs 5), NOT vs the benchmark's default 75.

### Lever 3 — int8 ONNX GTE — FEASIBLE (materially easier than the plan feared)
The plan's three blockers all dissolved:
1. **onnxruntime↔numpy "conflict" was a missing `libz.so.1`** on this NixOS box's `.venv`
   (`ImportError: libz.so.1`), NOT a numpy version conflict. Fixed locally with
   `LD_LIBRARY_PATH=<nix zlib>/lib`. **The backend image (`python:3.14-slim-trixie`, Debian) ships
   libz natively — no image change needed for the runtime.** onnxruntime 1.27.0 imports and exposes
   `CPUExecutionProvider` once libz resolves.
2. **ModernBERT ONNX export is a non-issue: the HF repo ships pre-built ONNX variants** —
   `onnx/model.onnx`, `onnx/model_int8.onnx`, `onnx/model_quantized.onnx`, fp16, q4, etc. No custom
   export, no artifact to build into the image: ST loads it directly via
   `CrossEncoder(..., backend="onnx", model_kwargs={"file_name": "onnx/model_int8.onnx"})`.
   (A local optimum export + AVX512-VNNI dynamic int8 quantize also succeeded — 571 MB fp32 → 144 MB
   int8 — but the repo file is the shipping path.)
3. **Speed/parity measured on 2 pinned cores (`taskset -c 0,1`, same query/passages):**

   | backend | ms/pair (n=10) | vs torch | top-5 set | full ranking |
   |---|---|---|---|---|
   | torch fp32 (ST) | ~66 | 1.0× | {0,2,4,6,8} | reference |
   | ONNX fp32 (raw ort) | ~66 | ~1.0× (**no win**) | identical | identical to torch |
   | ONNX int8 (raw ort) | ~20 | ~3.3× | identical | intra-top swap only |
   | ONNX int8 (ST, prod path) | ~37 | ~1.8× | identical | intra-top swap only |

   - **fp32 ONNX buys nothing** (same per-pair as torch) — int8 is the only Lever-3 value. Dropped fp32-ONNX.
   - **int8 preserves the top-5 SET** (recall@5 unaffected on the probe) — only reorders *within* the
     top set. This is the pattern the recall@5 gate tolerates; ndcg@5 takes a mild hit at worst.
   - **Per-pair absolute is input-length-dependent** (short probe passages ≈ 66ms/pair torch; the
     plan's ~150-token passages ≈ 205ms/pair — reconciled). The **int8 speedup RATIO (~1.8× via ST,
     ~3.3× raw) is length-stable** — anchor latency claims to the ratio, not the absolute.

- **Backend change → `backend_version` 5.9.0 → 5.10.0 + image rebuild** IF the default is flipped to
  onnx-int8 (touches `embed_service`/`ml_client` model load). Shipped default stays `torch`; the flag
  makes int8 opt-in until the LongMemEval gate clears, at which point flip + bump backend_version.

### Gate matrix (Q=100 retrieval-only, stratified, production-realistic values)
- **A** = `TOP_K=10, torch` (shared baseline)
- **B** = `TOP_K=5, torch` (Lever 2) — gate vs A on recall@5/ndcg@5
- **C** = `TOP_K=10, onnx-int8` (Lever 3) — gate vs A on recall@5/ndcg@5 (TOP_K held fixed)
- Lever 1 needs no run (unit-test-proven, baked into all three).
- Bar: ndcg@5 ≥ −0.5pt AND recall@5 ≥ −1pt per type; multi-session recall@5 binding.

## 7. Ship reconciliation (v5.98.0 finalization — 2026-07-02) + Lever-3 follow-up

Verified at finalization; corrects §6.3's "FEASIBLE" to an honest DORMANT ship.

- **Lever 1 — ACTIVE.** Parity test `test_reranking_multi_passage_parity.py` present
  (5 tests). Parity invariant confirmed against real code, not just the mock:
  `LocalMLClient.score_pair(q,t)` literally returns `score_cross_encoder(q,[t])[0]`.
- **Lever 2 — DORMANT.** `CROSS_ENCODER_TOP_K` default unchanged at `10`.
- **Lever 3 — DORMANT (code-present, NOT yet functional in a deployed image).**
  §6.3's blocker-dissolution was verified only in a local venv (hand-repaired
  `LD_LIBRARY_PATH`). Re-assessed at ship:
  - The artifact IS real and HF-shipped (`onnx/model_int8.onnx`, 151 MB, verified in
    the model repo) — downloaded on demand, NOT a missing local file. onnxruntime IS
    locked (`uv.lock`) via `sentence-transformers[onnx]` in the `[ml]` extra.
  - BUT image-level `import onnxruntime` + onnx `CrossEncoder` load is **UNVERIFIED in a
    built backend image**. The classic slim-image failure is `libgomp.so.1` (NOT libz).
    Building the image + 151 MB download to prove it was out of scope for this pass.
  - **Guardrail shipped:** flipping `GTE_RERANKER_BACKEND=onnx-int8` when the load fails
    raises a loud `OnnxRerankerUnavailableError` (distinct from the generic transient
    GTE-failure path, which still falls back). Tests:
    `test_gte_onnx_int8_load_failure_raises_loud_not_silent` + a torch-path contrast.

**FOLLOW-UP TASK — Lever 3 artifact/runtime verification pending. Do NOT flip
`GTE_RERANKER_BACKEND=onnx-int8` until it lands.** Concretely:
1. Build the backend image at `backend_version 5.10.0` and prove, inside the built image,
   that `import onnxruntime` succeeds and the onnx `CrossEncoder` loads + scores (add
   `libgomp1` to `Dockerfile.backend` apt install if the import fails).
2. Add a fp32-vs-int8 score-parity smoke test (real model) once the runtime is proven.
3. Run gate matrix cell **C** (Q=100 retrieval-only); adopt only if within the §4 bar
   (multi-session recall@5 binding).
Only after 1-3 clear: flip the default (or set the env), keeping `backend_version`
bumped (already at `5.10.0` this release).
