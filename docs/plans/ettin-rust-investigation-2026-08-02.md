# Ettin-in-Rust Investigation (2026-08-02)

**Status:** Investigation — findings from probing candle's ModernBERT support
against the Ettin-32m architecture. Not a decision to port; not a commitment.
**Context:** The SaaS rewrite plan (PR #25, `docs/plans/saas-rewrite-plan-2026-08-02.md`)
flagged Ettin-in-Rust as the third-highest-risk piece. This doc investigates
whether the risk is real or whether the path is clearer than expected.
**Branch:** `docs/ettin-rust-investigation-2026-08-02` (doc-only)

---

## 1. The headline finding

**candle already has a native Rust ModernBERT implementation.**
`candle-transformers/src/models/modernbert.rs` (verified 2026-08-02 against the
`main` branch) includes:

- `ModernBert` — the full backbone: token embeddings, layer norm, rotary
  embeddings (separate global + local), alternating local/global attention,
  GeGLU MLP, final norm. **This is the heavy part and it's done.**
- `ModernBertForMaskedLM` — fill-mask head (not needed for Ettin).
- `ModernBertForSequenceClassification` — CLS/MEAN pooling + `ModernBertHead`
  (dense + GELU + LayerNorm) + linear classifier + softmax. **~90% of what
  Ettin needs.**

Ettin-32m is a ModernBERT cross-encoder. Its architecture (from the HF model
card) is:

```
CrossEncoder(
  (0): Transformer(architecture=ModernBertModel)     ← candle: ModernBert ✅
  (1): Pooling(pooling_mode=cls)                      ← candle: CLS pooling ✅
  (2): Dense(256→256, bias=False, activation=GELU)    ← candle: ModernBertHead has this ✅
  (3): LayerNorm(256)                                 ← candle: ModernBertHead has this ✅
  (4): Dense(256→1, bias=True, activation=Identity)   ← candle: classifier, but softmax ❌
)
```

**The gap is one line:** Ettin's final layer is a regression head
(`Dense(256→1)` → raw scalar score, no softmax, trained with MSELoss), but
candle's `ModernBertClassifier` applies softmax. The fix is either:
(a) skip the softmax in `ModernBertClassifier`, or
(b) write a ~20-line `ModernBertForCrossEncoder` struct that reuses the
backbone + head + a raw `Linear(256, 1)`.

**This is not a research problem. It's a mechanical port + a benchmark.**

---

## 2. What's implemented in candle's modernbert.rs (verified)

| Component | candle impl | Ettin needs | Status |
|---|---|---|---|
| Token embeddings | `Embedding` | ✅ | ✅ |
| Embedding LayerNorm | `layer_norm_no_bias` | ✅ | ✅ |
| Rotary embeddings (global) | `RotaryEmbedding` with `global_rope_theta` | ✅ | ✅ |
| Rotary embeddings (local) | `RotaryEmbedding` with `local_rope_theta` | ✅ | ✅ |
| QKV projection | `linear_no_bias(hidden, hidden*3)` as `Wqkv` | ✅ | ✅ |
| Attention output projection | `linear_no_bias(hidden, hidden)` as `Wo` | ✅ | ✅ |
| Local/global attention alternation | `layer_id % global_attn_every_n_layers != 0` | ✅ | ✅ |
| Sliding window local attention mask | `get_local_attention_mask` | ✅ | ✅ |
| GeGLU MLP | `Wi` (hidden→intermediate*2) + chunk + `gelu_erf * gate` + `Wo` | ✅ | ✅ |
| Attention LayerNorm (optional) | `layer_norm_no_bias` (loaded with `.ok()`) | ✅ | ✅ |
| MLP LayerNorm | `layer_norm_no_bias` | ✅ | ✅ |
| Final LayerNorm | `layer_norm_no_bias` | ✅ | ✅ |
| CLS pooling | `output.i((.., 0, ..))` | ✅ | ✅ |
| Head: Dense + GELU + LayerNorm | `ModernBertHead` (dense_no_bias + gelu_erf + layer_norm_no_bias) | ✅ | ✅ |
| Regression head (scalar score) | `ModernBertClassifier` has softmax — **needs removal** | raw `Linear(256, 1)` | **~20 lines** |

**What's NOT in candle's modernbert.rs but needed:**

1. **Flash Attention 2.** candle has `candle-flash-attn` (a separate crate,
   v2) but the `modernbert.rs` attention implementation uses standard
   `softmax(q.matmul(k.T))` — not flash attention. Ettin's headline speed
   numbers (6602 pairs/sec on H100, 4497 on RTX 3090) use FA2. Without FA2,
   the candle port will be slower than the PyTorch+FA2 baseline on GPU.
   On CPU, FA2 is irrelevant (Ettin CPU uses SDPA: 92.5 pairs/sec on i7-13700K)
   and candle's standard attention should match.
   **Mitigation:** wire `candle-flash-attn` into the ModernBert attention
   path (the crate provides a `flash_attn_v2` function; the integration is
   a feature-flagged path in the attention `forward`). This is a known
   pattern in candle — other models (LLaMA, Mistral) already use it.

2. **The cross-encoder head.** As noted above — ~20 lines.

3. **Tokenizer.** Ettin uses a BPE tokenizer (ModernBERT's tokenizer is a
   modified BPE). The `tokenizers` crate (HuggingFace, Rust-native, used by
   candle) handles this. Load `tokenizer.json` from the model repo. ✅
   (not in modernbert.rs but in the candle ecosystem).

4. **bf16 inference.** Ettin is trained/published in bf16. candle supports
   bf16 (`DType::BF16`). Load weights with `VarBuilder::from_mmaped_safetensors`
   + `dtype = DType::BF16`. ✅

5. **Weight loading from safetensors.** Ettin weights are safetensors.
   candle loads safetensors natively. ✅

---

## 3. The port — what it looks like

A `ModernBertForCrossEncoder` struct (the Ettin port) in ~60 lines of Rust:

```rust
// crates/yadgar-ml/src/ettin.rs (sketch — not compiled, not tested)

use candle::{Device, Result, Tensor, D};
use candle_nn::{linear, Module, VarBuilder};
use candle_transformers::models::modernbert::{Config, ModernBert, ModernBertHead};

pub struct EttinReranker {
    model: ModernBert,           // the backbone (from candle)
    head: ModernBertHead,        // dense + GELU + LayerNorm (from candle)
    score: candle_nn::Linear,    // Dense(256→1, bias=True) — the ONE new thing
}

impl EttinReranker {
    pub fn load(vb: VarBuilder, config: &Config) -> Result<Self> {
        let model = ModernBert::load(vb.clone(), config)?;
        let head = ModernBertHead::load(vb.pp("head"), config)?;
        // Ettin's final layer: Dense(256→1, bias=True), no activation, no softmax
        let hidden = config.hidden_size;
        let score = linear(hidden, 1, vb.pp("score"))?;
        Ok(Self { model, head, score })
    }

    pub fn forward(&self, input_ids: &Tensor, attention_mask: &Tensor) -> Result<Tensor> {
        let hidden_states = self.model.forward(input_ids, attention_mask)?;
        // CLS pooling — token 0
        let cls = hidden_states.i((.., 0, ..))?.contiguous()?;
        // Head: dense + GELU + LayerNorm (from candle's ModernBertHead)
        let pooled = self.head.forward(&cls)?;
        // Score: Linear(256→1), no softmax — raw regression output
        let scores = self.score.forward(&pooled)?;
        Ok(scores.squeeze(D::Minus1)?)
    }

    /// Score (query, document) pairs — the rerank API.
    pub fn predict(
        &self,
        pairs: &[(String, String)],  // (query, document)
        tokenizer: &tokenizers::Tokenizer,
        device: &Device,
    ) -> Result<Vec<f32>> {
        // tokenize each pair, batch, forward, extract scalar scores
        // ~30 lines of tokenization + batching glue
        todo!()
    }
}
```

**The backbone + head reuse candle's code directly.** The only new code is the
`score: Linear(hidden, 1)` field + its `forward` call (no softmax). The
`predict` method is tokenization glue (~30 lines) using the `tokenizers` crate.

**Total new Rust code: ~80-100 lines** (the struct + forward + predict +
tokenization glue). Everything else (transformer layers, attention, rotary
embeddings, MLP, LayerNorm, safetensors loading, bf16) is candle library code.

---

## 4. The benchmark — what must be proven

The port compiles ≠ the port works. The gate is **recall@5 parity** on the
same benchmark that approved Ettin in the first place.

### 4.1 The existing baseline (from yadgar memory + T4 A/B)

T4 Ettin Reranker A/B Benchmark (2026-07-13), LongMemEval-s, retrieval-only,
120 questions/arm:

| Arm | recall@5 | recall@10 | ndcg@10 | mrr |
|---|---|---|---|---|
| GTE (incumbent) | 0.921 | 0.952 | 0.901 | 0.921 |
| **Ettin-32m (PyTorch)** | **0.944** | 0.964 | 0.896 | 0.896 |
| Ettin-68m | 0.941 | 0.958 | 0.898 | 0.892 |

**The candle port must match Ettin-32m PyTorch's recall@5 = 0.944 (within
the ~0.03-0.05 noise band established by the GTE-vs-GTE-determinism run).**

### 4.2 The benchmark procedure

```
1. Run the existing yadgar LongMemEval harness with the PyTorch Ettin-32m
   → record recall@5, recall@10, MRR (the baseline).
2. Swap the reranker to the candle Ettin port (same weights, same query set)
   → record the same metrics.
3. Compare. Pass gate if candle recall@5 is within ±0.03 of PyTorch.
4. If it drops → investigate: tokenizer difference? attention mask bug?
   bf16 vs fp32? rotary embedding precision? local attention window?
```

### 4.3 Known risks that could cause a regression

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Tokenizer mismatch** — candle's `tokenizers` crate vs HF Python tokenizers. They should be identical (same Rust crate under the hood) but pair truncation / padding strategy could differ | medium | verify token IDs match for 100 sample pairs before benchmarking |
| **bf16 numerical divergence** — candle's bf16 ops vs PyTorch's bf16 ops can diverge at the 4th decimal place, which compounds through 22 layers | low-medium | run the A/B in fp32 first to isolate, then bf16 |
| **Local attention boundary** — the sliding window mask in `get_local_attention_mask` uses `local_attention / 2` as max_distance. Verify this matches Ettin's config | low | check `config.local_attention` value in Ettin's `config.json` |
| **Rotary embedding precision** — candle computes `inv_freq` in f32; PyTorch may compute in f64 then cast. Can cause drift on long sequences | low | Ettin max_length is 7999; test on 512 and 2048 first |
| **Flash attention numerical equivalence** — FA2 is mathematically identical to standard attention but numerically different (tiled matmul). If the candle port uses FA2 on GPU and the PyTorch baseline uses FA2 on GPU, they should match; if one uses SDPA and the other FA2, there will be small differences | low | use the same attention implementation on both sides for the A/B |
| **Missing weight** — Ettin's sentence-transformers model has modules named differently than vanilla ModernBERT (e.g. `0.transformer`, `1.pooling`, `2.dense`, `3.layernorm`, `4.dense`). The weight key mapping must be correct | high | this is the most likely bug; write a key-mapping test that loads the safetensors and asserts every key is consumed |

---

## 5. The weight key mapping (the most likely bug)

Ettin is a `sentence-transformers.CrossEncoder` — its safetensors keys follow
the sentence-transformers convention, NOT the vanilla ModernBERT convention.
The model card shows the architecture as a sequential `CrossEncoder` with
numbered modules `(0)`, `(1)`, `(2)`, `(3)`, `(4)`. The safetensors keys will
look like:

```
0.model.embeddings.tok_embeddings.weight
0.model.embeddings.norm.weight
0.model.embeddings.norm.bias
0.model.layers.0.attn.Wqkv.weight
0.model.layers.0.attn.Wo.weight
0.model.layers.0.mlp.Wi.weight
0.model.layers.0.mlp.Wo.weight
...
0.model.final_norm.weight
2.dense.weight              ← no bias (bias=False in the model card)
3.layernorm.weight
3.layernorm.bias
4.linear.weight             ← the score head (Dense 256→1)
4.linear.bias
```

candle's `ModernBert::load` expects keys like `model.embeddings.tok_embeddings.weight`
(without the `0.` prefix). The `ModernBertHead::load` expects `head.dense.weight`
and `head.norm.weight` (not `2.dense.weight` and `3.layernorm.weight`).

**The port needs a key remapping step** — load the safetensors, rename keys
(strip the `0.` prefix from backbone keys, map `2.dense` → `head.dense`,
`3.layernorm` → `head.norm`, `4.linear` → `score`). This is ~30 lines of
string manipulation on the `HashMap<String, Tensor>` before constructing
the `VarBuilder`.

**This is the single most likely source of a silent regression** — a
misnamed key that loads zeros instead of weights, producing garbage scores
that still sort in a plausible order (so recall@5 drops but doesn't
collapse). The defense is a key-consumption assertion: after loading, every
key in the safetensors file must have been consumed, and every VarBuilder
request must have found its key.

---

## 6. CPU vs GPU — where the candle port wins and loses

From the Ettin model card (PyTorch, sentence-transformers):

| Hardware | Ettin-32m pairs/sec | Notes |
|---|---|---|
| H100 80GB (bf16+FA2) | 6,602 | GPU, flash attention 2 |
| RTX 3090 24GB (bf16+FA2) | 4,497 | consumer GPU |
| Intel i7-13700K (fp32+SDPA) | 92.5 | CPU |

**Where the candle port wins:**
- **CPU inference.** candle's CPU backend is optimized (MKL on x86,
  Accelerate on macOS). Ettin-32m on CPU is the yadgar solo-user path —
  no GPU. candle should match or beat PyTorch here (no Python overhead,
  no GIL, no PyTorch dispatch overhead). **This is the case that matters
  for solo yadgar.** Needs benchmarking but the expectation is parity or
  better, not regression.
- **Cold start.** candle loads safetensors via mmap (~ms); PyTorch +
  sentence-transformers + transformers imports take 3-7s. The candle
  binary starts in <100ms. **This is a real operational win** — the
  current yadgar embed_service has a 3-7s cold load that the rewrite
  eliminates.
- **Memory.** candle's Rust tensor arena is leaner than PyTorch's
  allocator. Ettin-32m in candle should use ~150-200MB RSS vs ~300-500MB
  in PyTorch. Matters for the solo binary's footprint.
- **Binary size.** A static candle binary with Ettin baked in: ~20-30MB
  (candle-core + candle-nn + candle-transformers + the model code).
  PyTorch + transformers + sentence-transformers: ~2GB. **This is the
  client-side collapse** — from a 2GB Python stack to a 30MB binary.

**Where the candle port might lose:**
- **GPU with FA2.** candle's `candle-flash-attn` exists but the
  `modernbert.rs` attention path uses standard SDPA, not FA2. On GPU,
  the candle port will be slower than PyTorch+FA2 until FA2 is wired in.
  For yadgar's use case (CPU solo, or GPU SaaS with batched inference),
  this may not matter — but if it does, the fix is a feature-flagged
  FA2 path in the attention forward, which is a known pattern in candle.
- **Batched inference throughput.** PyTorch's batched matmul is highly
  tuned. candle's should be competitive but may not match on GPU until
  the flash-attn path is wired. On CPU, the difference is negligible.

**Verdict:** for the solo/CPU path (the one that matters for yadgar's
default deploy), the candle port should match or beat PyTorch. For the
GPU/SaaS path, it needs FA2 wiring to match, and that's a known
engineering task, not a research risk.

---

## 7. The embed model — the other half of yadgar-ml

Ettin is the reranker. yadgar also needs an embedder. The current default
is `all-MiniLM-L6-v2` (sentence-transformers, 22M params, 384-dim vectors).

**candle has BERT support** (`bert.rs` in candle-transformers) and
all-MiniLM-L6-v2 is a BERT-family model. The port path is the same as
Ettin: load the backbone from candle's BERT impl, add a pooling layer
(mean pooling for embeddings), load the safetensors weights.

This is LOWER risk than Ettin because:
- BERT in candle is older, more battle-tested than ModernBERT.
- all-MiniLM-L6-v2 is a standard BERT encoder — no cross-encoder head,
  no regression output, just mean pooling + normalize.
- The embedding output is a vector, not a scalar — more forgiving of
  small numerical differences (cosine similarity is robust to scale).

**Embed model port estimate:** ~50 lines of Rust (backbone from candle's
`bert.rs` + mean pooling + L2 normalize). Gate: cosine similarity between
candle embeddings and PyTorch embeddings on 100 sample texts must be >0.999.

---

## 8. NLI model — the third ML model

yadgar has an NLI (Natural Language Inference) model for the
`MULTI_PASSAGE_RERANK_ENABLED` path. Currently off by default. If the
rewrite keeps it, it's another cross-encoder (BERT-family, classification
head with softmax — which candle's `ModernBertForSequenceClassification`
or `BertForSequenceClassification` handles directly).

**Lower priority.** NLI is off by default. Port it only if the feature
survives the rewrite. If it does, it's a standard classification head —
the easiest of the three ports.

---

## 9. The big-bang context — why this is a clean-slate investigation

The user clarified: the SaaS rewrite is a **new project, new repos, new
account**. Not a strangler migration from the current Python yadgar.
Nothing in current development is blocked. This changes the Ettin
investigation from "can we port without disrupting the running service?"
to "can we build the ML service in Rust from day one?"

**Implications for the Ettin port:**
- No hybrid period (Rust gateway + Python ML). The ML service is Rust
  from the start.
- No need for the "Python sidecar" option (option 1 in the SaaS plan).
  The candle port IS the implementation.
- The benchmark is against the existing PyTorch numbers (from the T4 A/B),
  not against a running system. A one-time A/B on the golden set is
  sufficient.
- The weight key mapping is the main risk (§5), and it's a testable risk,
  not a design risk.

**This makes the candle port the default path, not the "end state" path.**
The SaaS plan said "start with Python sidecar, end with candle." With
clean-slate, there's no reason to start with a sidecar — start with candle,
gate on the benchmark, and only fall back to a sidecar if the benchmark
fails.

---

## 10. Effort estimate (revised)

| Task | Effort | Risk | Gate |
|---|---|---|---|
| `EttinReranker` struct (backbone + head + score) | 1-2 days | low | compiles, loads weights |
| Weight key remapping + consumption assertion | 1 day | **medium** (most likely bug) | all keys consumed, no zeros |
| Tokenizer integration (`tokenizers` crate) | 1 day | low | token IDs match PyTorch on 100 samples |
| Forward pass + batching | 2 days | low | scores match PyTorch on 100 pairs (Pearson r > 0.99) |
| Flash attention wiring (GPU path, feature-flagged) | 2-3 days | medium | GPU throughput within 20% of PyTorch+FA2 |
| `all-MiniLM-L6-v2` embedder port | 2 days | low | cosine sim > 0.999 vs PyTorch |
| LongMemEval A/B benchmark | 1 day (run time) | **the gate** | candle recall@5 within ±0.03 of PyTorch (0.944) |
| HTTP service wrapper (axum `/embed`, `/rerank`) | 2 days | low | matches the yadgar-ml API contract |

**~2-3 weeks for a working, benchmarked Rust ML service.** This is faster
than the SaaS plan's estimate (2-3 weeks for the sidecar + 2-4 weeks for
the candle port later) because the clean-slate context eliminates the
sidecar phase.

**The gate is narrow:** if the LongMemEval A/B shows recall@5 within
±0.03 of 0.944, the port is done. If it drops, the investigation phase
(§4.3 risks) starts — and the most likely cause is the weight key mapping
(§5), which is debuggable in hours, not weeks.

---

## 11. What to do next (if this investigation is accepted)

1. **Spin up a prototype repo** (the new project/repo). Crate:
   `yadgar-ml-prototype` depending on `candle-core`, `candle-nn`,
   `candle-transformers`, `tokenizers`.
2. **Write the `EttinReranker` struct** (§3 sketch) + the weight key
   remapping (§5).
3. **Write a score-parity test**: load Ettin-32m in both candle and
   PyTorch (side-by-side on the same machine), score 100 (query, document)
   pairs, assert Pearson correlation > 0.99. This catches numerical
   divergences before the full benchmark.
4. **Run the LongMemEval A/B** (§4). Gate: recall@5 within ±0.03 of 0.944.
5. **If it passes** → the ML service is Rust-native from day one. Wire it
   into the SaaS architecture as `yadgar-ml` (no Python sidecar).
6. **If it fails** → investigate per §4.3. If unfixable, fall back to the
   Python sidecar (option 1 in the SaaS plan) — the sidecar is still
   viable, just not preferred.

---

## 12. Summary

| Question | Answer |
|---|---|
| Does candle support ModernBERT? | **Yes** — `modernbert.rs` in candle-transformers, full backbone |
| Does candle support Ettin's cross-encoder head? | **Almost** — `ModernBertForSequenceClassification` is 90% there; the regression head (no softmax) is ~20 lines |
| Is the port a research problem? | **No** — it's a mechanical port + a benchmark |
| What's the main risk? | Weight key mapping (sentence-transformers naming → candle naming) — testable, debuggable in hours |
| What's the gate? | LongMemEval recall@5 within ±0.03 of PyTorch's 0.944 |
| How long? | ~2-3 weeks for a working, benchmarked Rust ML service |
| Does clean-slate change anything? | Yes — no sidecar phase, candle is the default path from day one |
| Does the candle port win on CPU? | Expected yes (no Python overhead, no GIL, mmap load) — needs benchmarking |
| Does the candle port win on GPU? | Needs FA2 wiring to match PyTorch+FA2 — known engineering task |
| What about the embedder (all-MiniLM-L6-v2)? | Lower risk than Ettin (standard BERT, mean pooling); ~2 days |

**Bottom line:** the Ettin-in-Rust risk flagged in the SaaS plan is lower
than expected. candle's ModernBERT support covers the hard part (the
backbone). The remaining work is mechanical (head + key mapping +
tokenizer + benchmark). The gate is a recall@5 A/B that takes a day to run.
If it passes, the ML service is pure Rust from day one — no Python
sidecar, no 2GB PyTorch dependency, no 3-7s cold start.
