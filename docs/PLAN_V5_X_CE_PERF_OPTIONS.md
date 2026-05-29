# PLAN — v5.11+: Cross-Encoder Inference Performance Options (menu, version-assigned)

**Status:** drafted 2026-05-29 for future discussion. Each option is INDIVIDUALLY assigned a version slot per user direction 2026-05-29 evening; selection criteria after 72h soak.

**Master at draft time:** core v5.10.3 (about to ship) + backend v5.4.0 deployed.

## Version slot assignments

| Option | Slot | Trigger |
|---|---|---|
| B (int8 quantization) | **backend-v5.4.2** | If CE cold-path >2s/query sustained AND cache hit-rate <60%. Highest value/effort. |
| E (async ML pool) | **backend-v5.4.3** | If concurrent-session pain observed (multiple agents queueing on /rerank). Currently single-user — defer. |
| G (skip CE on high retrieval_confidence) | **backend-v5.4.4** | If retrieval_confidence ≥0.95 correlates with stable rerank scores (measure first). |
| A (GPU) | **backend-v5.5.0** (major) | Hardware change + nix module rewrite. Last resort. |
| C (batch utilization) | **backend-v5.4.5** | Only if K>32 workflows observed (audit_anchors with N>32 anchors etc.). |
| D (text truncation) | **REJECTED** | Risk of accuracy regression outweighs win. |
| F (smaller CE model) | **REJECTED** | Compounds K=10 accuracy hit. |

**Pick-one-at-a-time order if soak data points to "CE cold-path matters":** B → G → E → C → A. Skip D + F.

---

## Why

Backend v5.4.0 (recall caching) measurably eliminates CE re-inference on cache hit: **326x speedup on warm path** (9446.9 ms cold → 28.92 ms warm on 50-text batch, verified 2026-05-29). Cache is the right lever for repeat queries.

But **cold-path CE inference still ~188ms per pair on 2-CPU backend** (~9.4 s for 50 candidates). That's the dominant cost for first-touch queries OR queries that don't reuse prior text/checkpoint combos. Plan candidates below address cold-path cost itself.

**This plan is NOT a single train.** It's a menu of independent options. Pick + ship one at a time based on observed pain.

---

## Candidates

### A. GPU inference

**Win:** 10-50x faster CE inference (per typical CE benchmarks on small GPU vs 2-CPU).

**Cost:**
- Hardware: GPU on host (NixOS module + driver setup).
- Image: CUDA base + torch+CUDA wheels (~3 GB instead of current 1.65 GB).
- Backend container needs GPU passthrough (`--device` or NVIDIA Container Toolkit).
- Production env-dependent — GPU presence not universal across deployments.

**Sequencing constraint:** would need a separate `yadgar-backend-gpu:VER` image variant + nix module gates which variant to use. Operator chooses at deploy time.

**Risk:** more deps + more failure modes. Worth it only if cold-path is observed bottleneck across daily use. With cache hit-rate ≥30% (target post-soak), GPU's marginal benefit shrinks.

### B. int8 quantized CE model

**Win:** 2-4x faster on CPU. ~50% smaller model footprint.

**Cost:**
- One-shot model conversion (e.g. via `optimum-intel` or `onnxruntime`).
- Slight accuracy hit (usually <1% on CE rerank task).
- Code change: load `.onnx` or quantized weights instead of full safetensors.
- New env knob `YADGAR_CE_MODEL_QUANTIZED=true` for opt-in.

**Sequencing constraint:** can ship alongside existing fp32 model. Operator toggles via env. Backward-compatible.

**Risk:** accuracy regression on edge cases. Mitigate: A/B test on synthetic query set, accept if degradation <2%.

### C. Bigger batch utilization

**Hypothesis to verify first:** does current backend max out CE batch size? Default `batch_size=32` in `sentence-transformers.CrossEncoder.predict()`. If your live recalls run with `K=10` candidates (post-v5.7.2 cut), the model batches all 10 in ONE forward pass — batch utilization already maximal.

For larger batches (50+ candidates in `audit_anchors` or `recall(max_results=50)`), backend may pass 32 then 18 in two batches. Cost ≈ 2× single-batch. If we raise batch_size to 64, fewer model invocations but each takes longer per batch.

**Win:** marginal on current K=10 workflow. Possibly 1.5-2x on K=50+ workflows.

**Cost:** trivial code change. New env knob `YADGAR_CE_BATCH_SIZE` (default 32).

**Risk:** larger batches need more memory. Out-of-memory risk on small CPU containers.

**Decision rule:** ship only if observed P99 recall latency is dominated by K>32 cases.

### D. Truncate long texts before CE

**Win:** CE inference cost scales with token count (attention is O(n²) over sequence). Truncating texts from e.g. 500 tokens → 128 tokens before CE = 4-15x speedup per pair depending on which model.

**Cost:**
- Pre-truncation in `_score_ce_with_cache` before invoking model.
- Configurable max token count: `YADGAR_CE_MAX_TEXT_TOKENS` (default 128).
- Accuracy loss: long-tail content loses relevance signal from truncated section.

**Risk:** the part after token-128 might be the most relevant for some queries. CE was designed to score full text; truncating defeats the purpose. **Lean: skip unless verified that truncation doesn't degrade hit-rate.**

Already partially mitigated: `CROSS_ENCODER_TOP_K=10` (v5.7.2) limits the candidate count, not text length.

### E. Async ML pool (request thread not blocked on inference)

**Win:** doesn't speed CE itself but unblocks the FastAPI request thread. Other concurrent requests don't queue behind one slow CE invocation. Higher throughput under load, same latency per call.

**Cost:**
- Refactor `embed_service.py` /rerank handler to use `asyncio.to_thread` or a dedicated executor for the ML call.
- Care: ML inference holds the GIL inside numpy/torch, so thread pool execution still serializes on Python-side but releases the asyncio loop.
- Better: separate process pool (multiprocessing) → true parallelism, but model loaded per-worker (memory cost N × model_size).

**Decision rule:** ship when observed P99 latency under concurrent load is bad. Currently single-user dev = no contention. Defer until multi-session or production.

**Was already on v5.8+ candidate list per memory anchor 484431 "ProcessPool cancellable inference"** — that anchor describes a richer design including timeout-cancellable workers. Reference + extend rather than re-design.

### F. Smaller CE model

**Win:** halving model params → ~2x faster (compute ∝ params for transformer forward pass at fixed token count).

**Cost:**
- Model swap. Currently using BGE/MiniLM class (~80MB params). Could try `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~17MB params).
- Accuracy hit: typically 3-8% worse on CE rerank benchmarks.

**Risk:** the v5.7.2 `CROSS_ENCODER_TOP_K=10` cut already trades latency for slightly worse recall. Compounding with smaller model risks meaningful accuracy degradation.

**Lean: skip.** Quantization (Option B) gives similar speedup with less accuracy loss.

### G. Skip CE entirely when retrieval confidence is high

**Win:** for queries where BM25 + HNSW scores already separate top-K candidates clearly, the CE rerank adds little signal. Skip CE → saves 9.4s.

**Cost:**
- Implement `_retrieval_confidence` threshold check (this field already exists in recall response, e.g. `_retrieval_confidence: 0.9605` from observed recall).
- New env knob `YADGAR_CE_SKIP_THRESHOLD` (default 0.95): if `retrieval_confidence >= threshold`, skip CE.
- Returns BM25/HNSW-ranked results without CE rerank.

**Risk:** semantic edge cases where BM25 score is high but CE would re-rank differently. Acceptable cost for the 9.4s saving when retrieval is unambiguous.

**Decision rule:** measure CE rerank-vs-pre-rerank score correlation on real workload. If high correlation (>0.9) at high retrieval_confidence, ship the skip.

---

## Recommended ordering

Given current state (single-user dev, cache hit-rate target ≥30% post-soak, no GPU on host):

1. **First: measure 24h-72h cache hit-rate baseline.** If hit-rate ≥60%, cold-path cost matters less; defer all CE perf work.
2. **Next (low-risk, high-value): B (int8 quantization).** 2-4x cold-path speedup, opt-in via env, accuracy loss small + measurable.
3. **Then (if multi-session ever): E (async ML pool).** Throughput win under contention.
4. **Then (if cold-path still dominates): G (skip CE on high-confidence).** Saves 9.4s for 30-50% of queries possibly.
5. **Last resort: A (GPU).** Big infra change, only if all software levers exhausted.
6. **Skip: D (truncation), F (smaller model)** — risk of accuracy regression outweighs benefit given existing K=10 cut.
7. **Verify first: C (batch size).** Inspect current backend batch behavior before changing.

---

## What does NOT ship in any of these

- Custom CE training (way out of yadgar scope).
- ANN-only retrieval (HNSW without CE rerank for all queries) — CE rerank is real value for recall accuracy, not pure overhead.
- Distributed inference (multi-host CE workers) — premature optimization for single-user.

---

## Measurement protocol (before picking option)

Before committing to ANY of the above, gather data:

1. **24-72h soak:** observe `yadgar_embed_ce_cache_hits_total` vs `yadgar_embed_ce_cache_misses_total`. Hit-rate = hits / (hits + misses). Target ≥30% per backend v5.4.0 plan.
2. **Cold path P50 / P95:** add Tempo span timing for cache-miss CE calls specifically. Distribution shape informs whether 9.4s observed (50-text batch) is typical or pathological.
3. **CE inference budget:** `yadgar_recall_stage_ms{stage="rerank"}` histogram. P99 / median ratio shows whether cold tail is the problem (long tail → cache helps a lot; flat distribution → cache helps everyone proportionally).
4. **Concurrent load:** any queue depth on backend `/rerank` endpoint? `yadgar_embed_inflight_requests` gauge if exists.

Without these numbers, picking an option is gut-feeling. With them, decision is data-driven.

---

## Sequencing relative to other planned work

| Plan | State | Order |
|---|---|---|
| v5.10.2 secret-gate + memorize parity + nightly bugs | shipped | done |
| v5.10.3 scan script fix | in-flight | imminent |
| `PLAN_SESSION_END_CAPTURE.md` | drafted | next functional |
| v5.11 anchor cross-project + Jira + migration_grace | drafted | 4-week wait |
| Backend v5.4.1 N+1 hydration batching | drafted in v5.4.0 plan §v5.4.1 | independent track, 1s/recall win |
| **CE perf options (this menu)** | drafted, not scheduled | after 72h soak data |

---

## Open questions

- **Cache hit-rate threshold for cold-path-doesn't-matter decision:** lean ≥60% (recall feels instant in practice). Verify with real soak.
- **Quantization toolchain choice:** `optimum-intel` (Intel-optimized) vs `onnxruntime` (cross-platform) vs PyTorch eager mode quantization. Decide at implementation time based on what installs cleanly in the backend image.
- **GPU NixOS module:** does the existing `~/git/nix` setup have GPU support templates? If not, that's a separate prerequisite plan.
- **Smaller CE model A/B test methodology:** use the recall test fixtures + measure rerank score correlation? Or end-to-end "did the right memory surface first?" — latter is what users care about. Need test set with ground-truth answers.

---

## v5.X+ follow-up (post-implementation of any option)

- Update Grafana dashboard with cache hit-rate panel + cold-path P95 panel.
- Document tuning knobs in MIGRATION_NOTES for whichever option ships.
- Operational runbook: "if cache hit-rate drops below X%, investigate Y."
