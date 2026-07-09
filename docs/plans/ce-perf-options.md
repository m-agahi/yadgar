# PLAN — v5.11+: Cross-Encoder Inference Performance Options (menu, version-assigned)

**Status (corrected 2026-07-09):** option B (int8-onnx) REJECTED — controlled A/B proved 2× slower than torch (ADR-0043); onnx backend removed (ADR-0067). ROADMAP notes "onnx-int8 REJECTED — needs new approach." Next lever: recall Train 3 Ettin model swap (see ce-rerank-alternatives-research-2026-07-04.md). Options E/G/A/C remain as candidates if Ettin underperforms.

**Master at draft time:** core v5.10.3 + backend v5.4.0 deployed.

> **AUDIT 2026-06-25 (improvement-train #29).** The menu is a 2026-05-29 decision
> record and reads as stale in places (version slots like "backend-v5.4.2" never
> materialized; the project is now at v5.81). Re-verified the CE load path against
> current code so option B is buildable — **and the plan's old assumption that the CE
> lives in `embed_service.py` is WRONG now**:
> - The cross-encoder is loaded in **`yadgar/backend/ml_client.py`**, not
>   `embed_service.py`. Load chain: `ml_client.py` tries **GTE-reranker**
>   (`Alibaba-NLP/gte-reranker-modernbert-base`, ~line 352) → **FlashRank** (ONNX,
>   already low-latency) → **sentence-transformers `CrossEncoder`**
>   (`_try_st_cross_encoder`, load at **line 434** `CrossEncoder(ce_model)`,
>   `predict()` at line 446). The fp32 MiniLM `CrossEncoder` is the **fallback**, not
>   the primary, on current master.
> - Config: `CROSS_ENCODER_MODEL` (`config.py:162`, default
>   `cross-encoder/ms-marco-MiniLM-L-6-v2`), `CROSS_ENCODER_ENABLED` (163),
>   `CROSS_ENCODER_TOP_K=10` (164), `CROSS_ENCODER_WEIGHT=0.6` (165). Env knobs
>   `YADGAR_CROSS_ENCODER_*`. **These ARE I25 three-way Settings fields** (config.py +
>   config_yaml FIELD_META + config_registry) — the backend reads them via `settings`
>   when present (`ml_client._try_st_cross_encoder`), falling back to a literal default
>   only when `settings is None`. So a new model knob added as a Settings field obeys
>   I25; a backend-only `os.getenv` knob would NOT — pick deliberately (see option B).
> - NO existing int8/ONNX CE-weight quantization. (`embeddings.py` has
>   `quantize()`/`dequantize()` but that is float32→int8 for STORAGE of embedding
>   vectors, unrelated to CE model weights. `benchmarks/run_locomo_jscore.py` uses
>   BitsAndBytes 4-bit for an LLM, not the CE.) FlashRank already gives an ONNX path —
>   relevant prior art for option B.

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

---

## Option B — concrete ship plan (chosen 2026-06-25, improvement-train #29 car #4)

> **AUDIT 2026-06-26 (v5.85 train, car #4) — verified-current; one FLAG resolved,
> one claim corrected.**
> - **FLAG at §"Config knob" lines ~257-258 (`settings` threaded to
>   `_try_st_cross_encoder`?) → RESOLVED YES.** `_try_st_cross_encoder` reads
>   `settings.CROSS_ENCODER_MODEL` at `yadgar/backend/ml_client.py:428`; `settings`
>   is stored on the client at `:339` (`self._settings`) and re-read at `:415`. The
>   bare-`os.getenv` fallback path is therefore NOT needed; use the I25 three-way
>   Settings field. (`CROSS_ENCODER_MODEL` defined `config.py:162`,
>   `CROSS_ENCODER_ENABLED` `:163`.)
> - **CORRECTION — "consistent with the existing knobs" (line ~250) is MISLEADING.**
>   The existing `CROSS_ENCODER_*` fields are **NOT in `config_registry.py`**
>   (`grep -c CROSS_ENCODER config_registry.py` → 0). They pass the I25 three-way
>   sync test only via the **grandfathered env-only allowlist**
>   (`tests/config_env_only_allowlist.txt`). A *new* `CROSS_ENCODER_BACKEND` knob
>   will **NOT** inherit that grandfathering — it must be added to
>   `config_registry.py` explicitly (a `ConfigEntry(...)` row) or
>   `test_config_three_way_sync.py` will FAIL. So the I25 step is "config.py +
>   FIELD_META + **a real registry row**", not "follow the existing CE knobs"
>   (which dodge the registry). This is the one place the plan understates the work.
> - **Everything else verified accurate:** load path at `ml_client.py:434`,
>   `predict()` at `:446`, GTE primary + FlashRank fallback unchanged, the <2%
>   offline A/B gate is the right gate. **Effort M, Risk M — unchanged.**
> - **How this goes wrong like C1/C2:** the trap here is the registry omission — if
>   the implementer copies the grandfathered CE knobs' pattern (no registry row),
>   the three-way-sync test breaks and looks like an unrelated failure. The corrected
>   note above pre-empts it.

User direction: ship int8 CE quantization. This section is the buildable spec; the
menu above is the rationale.

### What ships
An **opt-in quantized cross-encoder load path** in the sentence-transformers
fallback branch of `yadgar/backend/ml_client.py`, gated so the current fp32 default
is unchanged unless explicitly enabled. The GTE-reranker primary + FlashRank (ONNX)
fallback stay as-is; option B targets the `_try_st_cross_encoder` branch (load at
**line 434**, `predict()` at **446**) which is the fp32 MiniLM path.

### Load-path change
Two viable mechanisms — pick at impl time by what installs cleanly in the backend
image:
1. **sentence-transformers ONNX backend** (modern ST): `CrossEncoder(model_name,
   backend="onnx", model_kwargs={"file_name": "model_qint8_avx512.onnx"})`. The
   quantized ONNX export of `cross-encoder/ms-marco-MiniLM-L-6-v2` is published on
   HF. Lowest-friction if the pinned ST version supports `backend=`.
2. **optimum.onnxruntime** (`ORTModelForSequenceClassification` + tokenizer) wrapped
   to expose `.predict(pairs)`. More code, no ST-version dependency.
FlashRank already proves ONNX runs in this image — prior art for (1)/(2).

### Config knob — DECISION REQUIRED (the I25 wrinkle)
Add ONE knob. Two placements, mutually exclusive:
- **(preferred) I25 three-way Settings field** `CROSS_ENCODER_BACKEND` (values
  `st` | `onnx-int8`, default `st`) added in lockstep to `config.py` +
  `config_yaml.py` FIELD_META + `config_registry.py` (`YADGAR_CROSS_ENCODER_BACKEND`),
  enforced by `test_config_three_way_sync.py`. `ml_client` already reads
  `settings.CROSS_ENCODER_*` when `settings` is present, so this is consistent with
  the existing knobs. **Do this unless there's a reason the backend can't see Settings
  at load time.**
- (only if Settings is genuinely unavailable in the backend load path) a bare
  `os.getenv("YADGAR_CROSS_ENCODER_BACKEND")` in `ml_client` — but then it must NOT
  be added to the I25 three-way (it would fail the sync test as an orphan). The audit
  found `ml_client` DOES receive `settings`, so the I25 field is the right call.
  **[FLAG RESOLVED 2026-06-26: `settings` IS threaded —
  `ml_client.py:428` reads `settings.CROSS_ENCODER_MODEL`, stored at `:339`. Use the
  I25 Settings field; the bare-getenv fallback is unnecessary. NOTE: the new
  `CROSS_ENCODER_BACKEND` knob needs an explicit `config_registry.py` row — the
  existing CE knobs are grandfathered via the env-only allowlist and a new knob will
  not inherit that, see the AUDIT note at the top of this section.]**

### TDD outline (failing first)
- `test_ce_backend_default_is_st` — assert `Settings().CROSS_ENCODER_BACKEND == "st"`
  (red until the field exists).
- `test_ce_onnx_backend_loads` — with `CROSS_ENCODER_BACKEND="onnx-int8"`,
  `_try_st_cross_encoder` returns a predictor whose `.predict([[q,d]])` yields a
  float score (mock/skip-guard the actual ONNX download in CI, mirroring the
  COMET-test hermeticity rule — do NOT pull weights in CI).
- `test_config_three_way_sync` stays green (the new field added to all three).
- **Accuracy gate (offline, not CI):** A/B the int8 vs fp32 CE on the LongMemEval
  recall set; accept if recall@k degradation < 2% (menu §B risk). Record the number
  in the PR. Reuses the `make longmemeval` harness (benchmark-runbook wiki).

### Contracts / config
- I25 three-way sync (the new `CROSS_ENCODER_BACKEND` field).
- No BEHAVIOR_CONTRACT row changes (rerank is a scoring component; recall BCs are
  outcome-level and must stay green — run the recall e2e suite).
- `CAPABILITY_REGISTRY.md`: note the optional int8 CE backend under the rerank
  capability.

### Risks
- Accuracy regression on edge cases — gated by the <2% offline A/B above.
- ONNX export availability / image install friction — mitigated by the two-mechanism
  choice + FlashRank prior art.
- **Open toolchain sub-choice (optimum-intel vs onnxruntime vs ST-onnx) is a user/impl
  decision** — flagged, not pre-picked.
