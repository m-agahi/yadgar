> ARCHIVED 2026-07-13 — CONCLUDED — Ettin-32m selected and shipped in recall Train 4.

# CE Rerank Alternatives — Research Synthesis (2026-07-04)

**Status (updated 2026-07-09):** CONCLUDED — research input to recall Train 3 (Ettin model swap). Ettin-32M/68M winner; benchmark against LongMemEval recall@k required before swap. ADR-0044 forthcoming on swap decision.

**Sources synthesized:**
- Report A: SOTA reranker model replacements (model survey, CPU benchmarks, same-harness table)
- Report B: Accelerating the current model on CPU (threading, quantization, graph fusion)
- Report C: Architectural alternatives (cascade, late-interaction, early-exit, sparse, LLM listwise)

---

## VERDICT

**Winner (convergent across A and B): Ettin reranker family** (Tom Aarsen / Johns Hopkins, Apache-2.0).

ModernBERT-lineage cross-encoders trained by pointwise MSE distillation from mxbai-rerank-large-v2 (1.54B teacher). Same architectural lineage as the incumbent GTE-reranker-ModernBERT-base — minimal integration risk, native ONNX + OpenVINO export, loads as drop-in `sentence-transformers` `CrossEncoder`. 8K context matches GTE's 8192 max seq length. No ONNX export required for initial benchmarking.

**Primary recommendation: benchmark Ettin-32M and Ettin-68M on `--cpus 2`, LongMemEval-gated.**

The MTEB delta (−0.006 for 32M, +0.007 for 68M vs GTE) is general-domain MTEB, not conversational memory. The real quality risk must be measured against LongMemEval recall@k on the yadgar backend. The speed delta (2–6×) is large enough to justify the benchmark even under moderate quality risk.

---

## Incumbent Baseline

| Model | Params | MTEB NDCG@10 | NanoBEIR NDCG@10 | CPU pairs/s | Max seq |
|---|---|---|---|---|---|
| GTE-reranker-ModernBERT-base | 150M | 0.5843 | 0.7017 | 14.7 | 8192 |

Measured runtime: **~11.3s/rerank pass**, 3 passes per `recall` call. CE accounts for 88–91% of subagent_start latency (Tempo trace: total 10.77s, CE span 9.57s). The 11.3s figure is a cold-load number; warm micro-bench shows ~3.9s/pass — the delta is model cold-load (~7s one-time cost per idle timeout, not per call). The cascade already runs: `CROSS_ENCODER_TOP_K=10` means CE sees only the top 10 fusion candidates, not 50.

---

## Ettin Candidate Table

All numbers from the Ettin blog benchmark (single harness, includes GTE incumbent at 14.7 pairs/s — apples-to-apples).

| Model | HF ID | Params | MTEB NDCG@10 | vs GTE | NanoBEIR NDCG@10 | CPU pairs/s | vs GTE | Recommendation |
|---|---|---|---|---|---|---|---|---|
| **Ettin-17M** | `cross-encoder/ettin-reranker-17m-v1` | 17.6M | 0.5576 | −0.027 | 0.6746 | 267.4 | **18×** | Aggressive-latency stretch; quality dip real |
| **Ettin-32M** | `cross-encoder/ettin-reranker-32m-v1` | 32.8M | 0.5779 | −0.006 | 0.6825 | 92.5 | **6.3×** | **Primary benchmark target** |
| **Ettin-68M** | `cross-encoder/ettin-reranker-68m-v1` | 68.6M | 0.5915 | +0.007 | 0.6915 | 31.2 | **2.1×** | **Safety fallback; quality above GTE** |
| Ettin-150M | `cross-encoder/ettin-reranker-150m-v1` | 150.9M | 0.5994 | +0.015 | 0.7086 | 14.0 | ≈1× | Quality-ceiling control in bench; no latency win |
| GTE-ModernBERT-base (incumbent) | `Alibaba-NLP/gte-reranker-modernbert-base` | 150M | 0.5843 | — | 0.7017 | 14.7 | — | Current production model |

**Integration path:** `CrossEncoder("cross-encoder/ettin-reranker-32m-v1")` loads without code change. ONNX exports exist natively; confirm FlashRank loadability before porting from torch. For the initial LongMemEval bench, torch is fine.

Sources: [Ettin blog](https://huggingface.co/blog/ettin-reranker) · [Ettin-68M card](https://huggingface.co/cross-encoder/ettin-reranker-68m-v1) · [Ettin-32M card](https://huggingface.co/cross-encoder/ettin-reranker-32m-v1) · [Ettin collection](https://huggingface.co/collections/cross-encoder/ettin-rerankers)

---

## Ranked Lever Table

Levers ranked by expected impact on the CE-stage wall (currently ~11.3s cold / ~3.9s warm per pass).

| # | Lever | Expected speedup | Quality risk | Effort | Train |
|---|---|---|---|---|---|
| 1 | **Ettin-32M swap** | **6.3× per pass** | Low-med (LongMemEval gate needed; MTEB −0.006 general-domain only) | Very low (model swap, no export) | **Train 2** |
| 2 | **Ettin-68M swap** | **2.1× per pass** | Very low (MTEB +0.007 above GTE; safety fallback if 32M regresses) | Very low | **Train 2** |
| 3 | **Keep-model-warm** | **~7s cold-load eliminated** (warm: ~3.9s/pass vs cold: ~11.3s) | None | Low (extend idle timeout / eager preload on startup) | **Train 2** |
| 4 | **ORT graph-fusion fp32** (ENABLE_ALL + offline transformer optimizer) | 2–4× vs torch eager | Near-zero (fusions exact on CPU EP) | Medium (ONNX export with `attn_implementation="eager"`, Optimum ≥ post-Jun-2025) | Train 3 |
| 5 | **Thread-cap fix** (`intra_op_num_threads=2`, `allow_spinning="0"`) | Removes 2× quota-thrash penalty; isolates precision comparisons | None | Very low (config-only; read cgroup `cpu.max`) | Train 3 |
| 6 | **Seq-len 512→256** | ~2× if passages short | Low-med (safe if P95 < 200 tokens; harmful if P95 > 350 — profile first) | Very low (one parameter) | Train 3 |
| 7 | **Static int8, attention-excluded** (Optimum `avx512_vnni`, calibrated) | 2–3× over fp32 (stacked on graph-fusion) | Low if attention nodes excluded; collapse if full-int8 | High (new export + calibration set, ~100–500 pairs) | Train 3 |
| 8 | **Layer early-exit** (SIGIR 2025 SEE) | ~2.5×/pair at equal MTEB | Low (verify on LongMemEval — SIGIR numbers are MS MARCO) | Medium (code-level CE forward pass change) | Train 3 |
| 9 | **Confidence-skip** (bypass CE when fusion margin large) | Variable; skips CE entirely on easy queries | Low if threshold tuned on LongMemEval | Low | Train 3 |
| 10 | **ColBERT/late-interaction** (answerai-colbert-small, 33M) | Large at query time (MaxSim vs transformer passes) | Med (A/B vs full CE needed on LongMemEval) | High (doc-side token-embedding precompute + storage) | Post-train-3 |

Notes:
- Speedups **do not multiply cleanly** (Amdahl + shared I/O overhead). Combined target (threads-capped + Ettin-68M + ORT fusion) is plausible 3–4× improvement, not arithmetic product.
- The cascade (50→10 candidates) is **already implemented** via `CROSS_ENCODER_TOP_K=10`. CE already sees only top-10. The fusion-reuse "free 5×" is not an available lever — it was already taken in v5.7.2.

---

## Dead Ends (Confirmed)

| Dead end | Reason |
|---|---|
| ONNX dynamic-int8 (prior ADR-0043 result) | Real overhead beyond thread bug: unconstrained 0.83× loss = dynamic quant overhead (per-op scale nodes at small batch). Thread fix is prerequisite for clean comparison, but doesn't make dynamic-int8 a guaranteed win. |
| OpenVINO EP / IPEX | Intel-only. AMD Ryzen AI 9 HX PRO 375 (Zen5). Zero validated benefit; potential errors. Skip entirely. |
| fp16 on CPU | 2–7× slower (emulated as fp32 pairs). Never. |
| LLM listwise rerankers (RankGPT, RankZephyr-7B) | 7B on 2 CPU cores is far heavier than the 11.3s wall. Off-table. |
| bge-reranker-v2-m3 (568M) | ~12s/1000 pairs — slower **and** lower quality (0.5526 MTEB) than GTE. No reason to use. |
| bge-reranker-large (560M) | 0.5098 MTEB — slower and weaker than GTE. |
| Qwen3-Reranker-0.6B | 0.5940 MTEB but LLM-style causal scoring, 0.6B; heavy on 2 cores. |
| Qwen3-Reranker-4B | CPU-dead. |
| mxbai-rerank-large-v2 (1.54B) | GPU-class (387 pairs/s on H100 only). |
| jina-reranker-v3 | CC-BY-NC-4.0 (non-commercial self-host restricted). |
| jina-reranker-v2-base-multilingual | CC-BY-NC-4.0 (same). |
| Cohere Rerank 3.5 | API-only ($2/1k). No open weights. Not self-hostable. |
| Voyage rerank-2.5 / 2.5-lite | API-only. No downloadable weights. |
| FlashAttention on CPU | CUDA-only. Does not exist on CPU. |
| BetterTransformer on CPU (unpadding) | ModernBERT's unpadding requires `flash_attention_2` (GPU-only); CPU falls back to padded standard attention. Minimal gain. |
| MiniLM-L6 as CE replacement | 0.5082 MTEB (−0.076 vs GTE), 512-token cap. Quality and seq-len regression. Only useful as cascade filter if fusion leaks gold — but cascade already runs at K=10. |

---

## Architectural Findings (Report C Summary)

The cascade **is already done**: `CROSS_ENCODER_TOP_K=10` limits CE input to the top-10 fusion candidates, not the raw 50. The "free 5× cascade" is not available. The fusion-reuse lever was taken in v5.7.2.

Remaining architectural levers worth pursuing:

- **Layer early-exit** (~2.5×/pair, SIGIR 2025): exit transformer layers early per-candidate when confidence is sufficient. Code-level change to CE forward pass. Multiplicative with model swap. Verify on LongMemEval (SIGIR MS MARCO numbers only). Source: [SEE-SIGIR25](https://github.com/veneres/SEE-SIGIR25), [ACM 10.1145/3726302.3729962](https://dl.acm.org/doi/10.1145/3726302.3729962).
- **Confidence-skip**: bypass CE entirely when fusion top-1 margin is unambiguous. Tune threshold on LongMemEval. Low effort.
- **ColBERT late-interaction** (answerai-colbert-small-v1, 33M, BEIR 53.79): strongest architectural quality-preserving replacement. Requires doc-side precompute of token embedding matrices + storage. Use reranking-mode MaxSim (no PLAID index needed) for initial A/B. Source: [HF card](https://huggingface.co/answerdotai/answerai-colbert-small-v1), [ColBERTv2 arXiv:2112.01488](https://arxiv.org/abs/2112.01488).
- **Quality-load-bearing note**: the fusion CE call is the primary quality gate. Do not cut it without LongMemEval validation.

---

## Acceleration Findings (Report B Summary)

### Thread-cap bug (prerequisite for all precision comparisons)

ORT pip build has **no OpenMP**; `OMP_NUM_THREADS` is ignored. `intra_op_num_threads` is the only switch. Default = 0 → reads host `sysconf(_SC_NPROCESSORS_ONLN)` = 24 cores; CFS `--cpus 2` quota is invisible. Spawns 24 threads against 2 schedulable cores → thrash + spin-wait burns quota idle.

Fix (config-only, zero quality risk):
```python
# ORT
opts.intra_op_num_threads = 2
opts.inter_op_num_threads = 1
opts.execution_mode = ORT_SEQUENTIAL
opts.add_session_config_entry("session.intra_op.allow_spinning", "0")

# PyTorch
torch.set_num_threads(2)
torch.set_num_interop_threads(1)  # call once at import
```

Auto-detect budget from cgroup: `/sys/fs/cgroup/cpu.max` (`200000 100000` → `ceil = 2`). `os.cpu_count()`, `sched_getaffinity`, and `os.process_cpu_count()` all return 24 under `--cpus` quota (quota ≠ affinity).

Sources: [ORT threading docs](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) · ORT issues #3233 #21252 #16048 · [openpilot #22736](https://github.com/commaai/openpilot/issues/22736)

### ORT graph fusion (Tier 1 — quality-safe, no model swap)

Export with `attn_implementation="eager"` → Optimum ≥ post-Jun-2025 (PR #2208) → `ORT_ENABLE_ALL` + offline transformer optimizer. Expected 2–4× vs torch eager, near-zero accuracy risk (fusions exact on CPU EP). ModernBERT's alternating local/global attention may only partially fuse → profile actual win before committing.

Sources: [opensource.microsoft ORT 2.9× BERT](https://opensource.microsoft.com/projects/onnxruntime) · [Optimum PR #2208](https://github.com/huggingface/optimum/pull/2208) · [HF transformers #35545](https://github.com/huggingface/transformers/issues/35545)

### Static int8 (Tier 2 — VNNI confirmed, attention-excluded)

AMD Ryzen AI 9 HX PRO 375 has `avx512_vnni` (Zen5). ORT oneDNN dispatches VNNI kernels automatically. Use `AutoQuantizationConfig.avx512_vnni(is_static=True)` with attention nodes excluded (ModernBERT masked-fill collapses under full int8). Expected 2–3× over fp32, stacked on graph-fusion. Requires new export + calibration set (~100–500 pairs). Do not use `reduce_range=True` (VNNI has full 8-bit; `reduce_range` is a non-VNNI workaround).

The prior ADR-0043 dynamic-int8 result (0.83× unconstrained, 2× slower under quota) had two confounds: (1) the quota-2 thrash penalty — fixable with thread cap; (2) genuine dynamic-quant per-op overhead at small batch — not fixable by threading alone. Static int8 removes the per-op overhead. Test as a head-to-head at `intra_op=2` after T0 thread fix.

---

## 3-Train Plan Mapping

### Train 2 (current sprint)
- **Ettin-32M benchmark**: run LongMemEval recall@k on `--cpus 2` backend vs GTE incumbent. Use `CrossEncoder("cross-encoder/ettin-reranker-32m-v1")` torch path. No ONNX work yet.
- **Ettin-68M benchmark**: parallel bench as safety fallback. Run Ettin-17M as aggressive-latency stretch; Ettin-150M as quality-ceiling control.
- **Keep-model-warm**: eliminate the ~7s cold-load from idle timeout. Extend timeout or eager-preload on service start. This alone likely halves the cold-path wall (11.3s → ~4s).
- Decision gate: if Ettin-32M holds LongMemEval recall@k within acceptable bound → ship as replacement. If it regresses → fall back to Ettin-68M.

### Train 3 (post-Ettin, gated on post-Ettin latency numbers)
All train-3 candidates are evaluated against the new Ettin baseline, not GTE:
- Thread-cap fix (prerequisite; do before any precision work)
- ORT graph-fusion fp32 export (primary quality-safe win on whichever Ettin model wins)
- Seq-len 512→256 (profile passage token distribution first — safe if P95 < 200 tokens)
- Static int8 attn-excluded (stacked on graph-fusion; needs calibration export)
- Layer early-exit SIGIR 2025 (stack on model win; verify on LongMemEval)
- Confidence-skip CE on easy queries

### Post-train-3 (if ceiling still insufficient)
- ColBERT/late-interaction (answerai-colbert-small-v1): requires doc-side precompute + storage pipeline. Only if train-3 stack still insufficient after LongMemEval validation.

---

## Source Index

| Report | Key URLs |
|---|---|
| A — Model survey | [Ettin blog](https://huggingface.co/blog/ettin-reranker) · [Ettin-68M card](https://huggingface.co/cross-encoder/ettin-reranker-68m-v1) · [Ettin-32M card](https://huggingface.co/cross-encoder/ettin-reranker-32m-v1) · [Ettin collection](https://huggingface.co/collections/cross-encoder/ettin-rerankers) · [GTE-reranker-ModernBERT](https://huggingface.co/Alibaba-NLP/gte-reranker-modernbert-base) · [bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) · [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) · [jina-reranker-v3](https://huggingface.co/jinaai/jina-reranker-v3) · [Cohere Rerank](https://cohere.com/rerank) · [Voyage rerank](https://huggingface.co/voyageai/rerank-2.5) · [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank) |
| B — Acceleration | [ORT threading docs](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) · [ORT quantization docs](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html) · [Optimum PR #2208](https://github.com/huggingface/optimum/pull/2208) · [HF transformers #35545](https://github.com/huggingface/transformers/issues/35545) · ORT issues #3233 #5628 #6695 #21252 · [openpilot #22736](https://github.com/commaai/openpilot/issues/22736) · [pytorch cpu threading](https://pytorch.org/docs/stable/notes/cpu_threading_torchscript_inference.html) · [sbert.net cross-encoder efficiency](https://www.sbert.net/docs/usage/cross_encoder.html) |
| C — Architecture | [Ettin blog](https://huggingface.co/blog/ettin-reranker) · [answerai-colbert-small](https://huggingface.co/answerdotai/answerai-colbert-small-v1) · [ColBERTv2 arXiv:2112.01488](https://arxiv.org/abs/2112.01488) · [PLAID / ColBERTv2 topic](https://www.emergentmind.com/topics/colbertv2-retriever) · [SEE-SIGIR25 repo](https://github.com/veneres/SEE-SIGIR25) · [ACM 10.1145/3726302.3729962](https://dl.acm.org/doi/10.1145/3726302.3729962) · [SPLADE-v3 2403.06789](https://www.emergentmind.com/papers/2403.06789) · [RankZephyr 2312.02724](https://arxiv.org/pdf/2312.02724) · [MICE arXiv:2602.16299](https://arxiv.org/pdf/2602.16299) · [BigDataBoutique CE guide](https://bigdataboutique.com/blog/rag-reranking-improving-retrieval-quality-with-cross-encoders) |
