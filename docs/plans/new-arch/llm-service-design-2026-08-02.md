# The LLM Service — Full Design Plan (2026-08-02)

**Status:** DRAFT — designs the 14th service in the SaaS architecture.
Fills a gap in the SaaS rewrite plan (PR #25) which had 13 services but no
LLM service. The user flagged that an LLM service is needed for
consolidation quality, recall augmentation, and the reader/judge pattern.
**Branch:** `docs/move-new-arch-plans-to-subdir` (doc-only, no-release)
**Amends:** `docs/plans/new-arch/saas-rewrite-plan-2026-08-02.md` §3 (add
service #14), `docs/plans/new-arch/protocol-crate-design-2026-08-02.md` §2
(add the `Llm` trait to the protocol crate).

---

## TL;DR

`yadgar-llm` is the 14th service — a unified LLM gateway that abstracts
which model (Claude, GPT, local Ollama, candle) and which provider (Anthropic
API, OpenAI API, local `claude -p`, self-hosted) behind a single trait.
Every other service that needs LLM inference (consolidation for narrative
generation, recall for answer augmentation, metacognition for gap analysis,
the benchmark for reader+judge) calls this service, never the model directly.

**Why a service, not a library:** the LLM is a shared expensive resource
(rate limits, cost, GPU/CPU contention, model selection per tenant). It
needs its own backpressure (token-bucket rate limiting per tenant per
model), its own circuit breaker (API provider down → degrade or fail), its
own cost accounting (metering records token usage), and its own model
selection logic (free tier → local Ollama; pro tier → Claude Haiku;
enterprise → Claude Sonnet). A library in each service would duplicate all
of this; a service centralizes it.

**Why it's not in the original 13:** the original plan focused on the data
plane (recall, write, embed, rerank) + SaaS spine (IAM, vault, etc.). The
LLM is a *compute* service like `yadgar-ml` (embed/rerank), but for text
generation rather than vector scoring. It belongs in the data plane, next
to `yadgar-ml`, as a peer that other services call when they need generated
text.

**Effort: ~2 weeks** for the service skeleton + the `Llm` trait + one
provider impl (Anthropic API). Additional providers (OpenAI, Ollama,
candle) are additive — each is a new impl crate, no service code changes.

---

## 1. What the current system does (and what's missing)

### 1.1 Narrative generation is string concatenation, not LLM

`yadgar/backend/narrative/narrative.py:66` — `generate_narrative()` builds a
project story by collecting memories, extracting decisions/events/entities,
and concatenating them into a template string:

```python
parts = [f"In {directory}, during {period_desc}: {count} memories recorded."]
if decisions:
    parts.append(f"Key decisions: {', '.join(decisions[:5])}.")
# ... more string concatenation
summary = " ".join(parts)
```

**This is not an LLM.** It's a template. The "narrative" is a mechanical
list, not a synthesized story. An LLM would take the raw memories +
decisions + events and generate a coherent narrative summary — "This week,
the team focused on the split-store decision, debating MariaDB vs Postgres,
ultimately choosing Postgres for RLS support..." — which is dramatically
more useful for session restoration and project context.

### 1.2 Dream replay generates "insight" memories without an LLM

`yadgar/backend/sleep_compute/dream.py:15` — `dream_replay()` finds
cross-domain memory pairs with high similarity and creates a synthetic
"dream insight" memory. But the "insight" is just: "Found a connection
between {memory_a} and {memory_b}" — a template string, not a synthesized
insight. An LLM would take both memories and generate: "The split-store
decision's cross-engine quiesce point (ADR-0196) is analogous to the
backup verification gate's atomicity concern — both solve the 'partial
state across two systems' problem." That's a real insight; the template
isn't.

### 1.3 Gap detection is structural, not semantic

`yadgar/_shared/metacognition/gap_detection.py:12` — `detect_gaps()` finds
isolated entities, stale regions, low-confidence memories, missing
connections. All structural (graph topology + heat + confidence scores).
An LLM would take the gap list + the existing memory corpus and generate:
"You have extensive ADR coverage for storage decisions but zero coverage
for the retrieval pipeline's reranking strategy — the Ettin A/B benchmark
is referenced in 3 memories but no ADR formalizes the choice." That's a
semantic gap analysis the structural detection can't produce.

### 1.4 Recall returns raw memories; no answer synthesis

The current `recall()` tool returns a list of memory/wiki dicts. The client
(the AI agent) reads them and synthesizes an answer. For some use cases
(SaaS API consumers who aren't AI agents — dashboards, search UIs,
monitoring), an LLM-augmented recall that returns a synthesized answer +
citations would be valuable. The LongMemEval benchmark already proves this
pattern: the "reader" LLM takes retrieved passages + the question and
generates an answer, which the "judge" LLM scores.

### 1.5 The benchmark uses `claude -p` as a subprocess

`benchmarks/run_longmemeval.py:731` — `call_claude_pipe()` shells out to
`claude -p --output-format json`. This is a benchmark-only pattern, not a
production service. The LLM service replaces the subprocess shell-out with
a typed HTTP API that any service (including the benchmark) can call.

### 1.6 No LLM in the SaaS rewrite plan

The SaaS rewrite plan (PR #25) has 13 services. None is an LLM. The
`yadgar-ml` service handles embed + rerank (vector scoring), not text
generation. The consolidation service needs LLM for narrative/dream/gap
analysis. The recall service could use LLM for answer augmentation. The
metacognition system needs LLM for semantic gap detection. None of these
have a service to call.

**Defect:** the architecture has no LLM abstraction. Each consumer would
either shell out to `claude -p` (unreliable, no rate limiting, no cost
accounting) or call an API directly (hardcoded provider, no swap path, no
tenant-aware model selection). The LLM service fixes this.

---

## 2. Where `yadgar-llm` fits in the architecture

### 2.1 The service inventory (revised to 14)

```
Data plane (hot path):
  1. yadgar-gateway       — MCP HTTP, tool router
  2. yadgar-recall        — retrieval pipeline
  3. yadgar-ml            — embed + CE rerank + NLI (vector scoring)
  4. yadgar-llm           — LLM text generation (NEW — this service)
  5. yadgar-write         — queue drainer + write-apply + curation
  6. yadgar-cache         (Valkey) — cache + rate-limit + sessions + queues

SaaS spine:
  7. yadgar-iam           — AAA
  8. yadgar-vault         — encryption
  9. yadgar-metering      — usage/quotas
 10. yadgar-scheduler     — job registry
 11. yadgar-backup        — snapshots
 12. yadgar-control       — admin ops

Background:
 13. yadgar-consolidation — nightly batch (calls yadgar-llm for narrative/dream/gap)
 14. yadgar-viz           — galaxy + UI (optional)
```

### 2.2 Who calls `yadgar-llm`

| Caller | Use case | Frequency | Latency budget |
|---|---|---|---|
| `yadgar-consolidation` | narrative generation (project stories from memory history) | nightly per directory | minutes (batch, not latency-critical) |
| `yadgar-consolidation` | dream replay insight synthesis (cross-domain connections) | nightly, a few pairs | minutes |
| `yadgar-consolidation` | semantic gap analysis (what's missing from the knowledge graph) | nightly per directory | minutes |
| `yadgar-consolidation` | memory compression (gist extraction from full content) | nightly, bulk | minutes |
| `yadgar-recall` | answer augmentation (synthesized answer + citations from recalled memories) | per request (opt-in via `profile=full`) | seconds (latency-sensitive) |
| `yadgar-gateway` | MCP tool: `ask` (free-form question → recall + LLM answer) | per request (user-initiated) | seconds |
| benchmark | reader (answer generation from retrieved passages) + judge (answer scoring) | per question | seconds |

**Most calls are batch (consolidation) — minutes of latency budget, not
seconds.** The one latency-sensitive path is recall augmentation, which is
opt-in (profile=full) and can degrade to un-augmented results if the LLM
is slow or down.

---

## 3. The `Llm` trait (addition to the protocol crate)

```rust
// crates/yadgar-protocol/src/llm.rs — NEW trait

/// LLM text generation. Abstracts which model + which provider.
/// Every service that needs generated text calls this trait, never
/// the model API directly.
#[async_trait]
pub trait Llm: Send + Sync {
    /// Generate text from a prompt. The canonical call.
    async fn generate(&self, request: &LlmRequest) -> Result<LlmResponse, LlmError>;

    /// Generate text from a prompt with retrieved context (RAG pattern).
    /// The LLM service formats the context + question into a single prompt.
    async fn generate_with_context(
        &self,
        question: &str,
        context: &[ContextPassage],
        options: &LlmOptions,
    ) -> Result<LlmResponse, LlmError>;

    /// Embed a chat conversation (multi-turn). Used by consolidation
    /// for narrative generation where the "prompt" is a series of
    /// system + user + assistant messages.
    async fn chat(&self, messages: &[ChatMessage], options: &LlmOptions) -> Result<LlmResponse, LlmError>;

    /// Check if the LLM is available (model loaded, API reachable).
    async fn health(&self) -> Result<LlmHealth, LlmError>;

    /// List available models (for model-selection logic in the gateway).
    async fn models(&self) -> Result<Vec<ModelInfo>, LlmError>;
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmRequest {
    pub prompt: String,
    pub system_prompt: Option<String>,
    pub options: LlmOptions,
    pub tenant_id: TenantId,        // for per-tenant model selection + cost accounting
    pub idempotency_key: Option<Uuid>,
    #[serde(default)]
    pub schema_version: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmOptions {
    pub model: Option<String>,       // None = use tenant's default
    pub max_tokens: Option<u32>,     // None = provider default
    pub temperature: Option<f32>,    // None = provider default (0.0 for factual, 0.7 for creative)
    pub timeout_ms: Option<u32>,     // None = service default (30s)
    pub stream: bool,                // false = wait for full response, true = SSE stream
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmResponse {
    pub text: String,
    pub model: String,               // which model actually generated (may differ from requested)
    pub usage: TokenUsage,
    pub finish_reason: FinishReason, // Stop, Length, ContentFilter, Error
    pub latency_ms: u32,
    #[serde(default)]
    pub schema_version: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenUsage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
    pub estimated_cost_usd: Option<f32>,  // for metering
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum FinishReason { Stop, Length, ContentFilter, Error(String) }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContextPassage {
    pub content: String,
    pub source: String,              // "memory:123" or "wiki:slug" or "adr:0094"
    pub score: Option<f32>,          // retrieval score if from recall
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: ChatRole,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ChatRole { System, User, Assistant }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmHealth {
    pub available: bool,
    pub provider: String,            // "anthropic", "openai", "ollama", "candle"
    pub default_model: String,
    pub rate_limit_remaining: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelInfo {
    pub name: String,                // "claude-sonnet-4-6", "gpt-4o", "llama3-70b"
    pub provider: String,
    pub context_window: u32,
    pub cost_per_1k_input: Option<f32>,
    pub cost_per_1k_output: Option<f32>,
    pub capabilities: Vec<ModelCapability>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ModelCapability { Chat, Completion, Vision, ToolUse, JsonMode }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum LlmError {
    ProviderUnavailable { provider: String, reason: String },
    RateLimited { retry_after_sec: u32 },
    TokenLimitExceeded { requested: u32, limit: u32 },
    ContentFiltered { reason: String },
    Timeout { timeout_ms: u32 },
    InvalidModel { model: String },
    TenantQuotaExceeded { quota: String, used: u64, limit: u64 },
    Internal { reason: String },
}
```

### 3.1 Protocol crate update

The protocol crate's trait count goes from 18 to **19** (adding `Llm`).
The crate structure adds `llm.rs`. The swappable surface table adds:

| Trait | Solo impl | SaaS impl | Future impls |
|---|---|---|---|
| `Llm` | `OllamaLlm` (local model) or `ClaudeCliLlm` (`claude -p`) | `AnthropicLlm` (API) or `OpenAiLlm` (API) | `CandleLlm` (Rust-native), `AzureOpenAiLlm`, `BedrockLlm` |

---

## 4. The `yadgar-llm` service

### 4.1 API surface

```
yadgar-llm (Rust/axum):
  POST /v1/generate              — LlmRequest → LlmResponse
  POST /v1/generate-with-context — {question, context[], options} → LlmResponse (RAG)
  POST /v1/chat                  — {messages[], options} → LlmResponse
  GET  /v1/health                — LlmHealth
  GET  /v1/models                — Vec<ModelInfo>
  GET  /healthz                  — liveness
  GET  /readyz                   — readiness (checks provider reachability)
```

### 4.2 Model selection per tenant

```
gateway → yadgar-llm /v1/generate
  → LlmRequest { tenant_id, options: { model: None } }
  → yadgar-llm: looks up tenant's configured model from yadgar-config
    → free tier: "ollama/llama3-8b" (local, no cost)
    → pro tier: "anthropic/claude-haiku-4" (cheap, fast)
    → enterprise tier: "anthropic/claude-sonnet-4-6" (best quality)
  → routes to the provider impl for that model
  → records token usage in yadgar-metering
```

Model selection is a **config knob** (`llm.default_model` per tenant plan,
overridable per-tenant via `config_set`). The LLM service reads it from
`yadgar-config` (the `ConfigProvider` trait). Changing the model for a
tenant is a config update, not a redeploy.

### 4.3 Backpressure

The LLM is the most expensive resource in the system (API cost + latency).
Backpressure is critical:

- **Rate limiting:** `yadgar-metering` enforces per-tenant per-model rate
  limits (e.g. free tier: 10 LLM calls/hour; pro: 100/hour; enterprise:
  unlimited). The LLM service checks `meter.check_rate(tenant, "llm")`
  before calling the provider. Over limit → `LlmError::RateLimited` →
  gateway returns 429.
- **Queue:** batch LLM calls (consolidation) go through `queue:llm` (Valkey
  list) so the scheduler's nightly run doesn't overwhelm the API. Real-time
  calls (recall augmentation) bypass the queue (synchronous, with timeout).
- **Circuit breaker:** if the provider API is down (Anthropic returns 5xx
  repeatedly), the circuit opens → `LlmError::ProviderUnavailable` →
  consolidation skips the LLM step (degraded but functional), recall
  returns un-augmented results.
- **Token budget:** per-tenant per-month token budget enforced by
  `yadgar-metering`. Over budget → `LlmError::TenantQuotaExceeded` →
  gateway returns 429.

### 4.4 Cost accounting

Every `LlmResponse` carries `TokenUsage { prompt_tokens, completion_tokens,
total_tokens, estimated_cost_usd }`. The LLM service records this in
`yadgar-metering` as a usage event:

```
meter.record(UsageEvent {
    tenant_id,
    action: "llm.generate",
    quantity: total_tokens,
    metadata: { model, cost_usd, prompt_tokens, completion_tokens },
})
```

This is the data the billing system consumes. The LLM service is the
single point where all LLM cost is tracked — no service calls the API
directly, so no cost leaks.

---

## 5. The provider impls

Each provider is a separate impl crate, implementing the `Llm` trait. The
LLM service links the impl(s) at the composition root.

```
crates/
  yadgar-llm-anthropic/  — AnthropicLlm (Claude API: claude-sonnet, claude-haiku)
  yadgar-llm-openai/     — OpenAiLlm (GPT-4o, GPT-4o-mini)
  yadgar-llm-ollama/     — OllamaLlm (local Ollama: llama3, mistral, etc.)
  yadgar-llm-claude-cli/ — ClaudeCliLlm (shells out to `claude -p` — the benchmark pattern)
  yadgar-llm-candle/     — CandleLlm (Rust-native, future — candle-transformers)
```

**Solo mode:** links `OllamaLlm` (if Ollama is installed) or `ClaudeCliLlm`
(if `claude` CLI is available). No API key needed for Ollama; Claude CLI
uses the subscription, not the API.

**SaaS mode:** links `AnthropicLlm` and/or `OpenAiLlm`. Model selection per
tenant (config knob). API keys are service-level secrets (env vars or
Vault, not per-tenant).

**The swap is at the composition root:**

```rust
// solo
let llm: Arc<dyn Llm> = Arc::new(OllamaLlm::new("http://localhost:11434"));

// SaaS (multiple providers, model-selection routes to the right one)
let llm: Arc<dyn Llm> = Arc::new(MultiProviderLlm::new(vec![
    Arc::new(AnthropicLlm::new(&env("ANTHROPIC_API_KEY"))),
    Arc::new(OpenAiLlm::new(&env("OPENAI_API_KEY"))),
    Arc::new(OllamaLlm::new("http://ollama:11434")),  // fallback for free tier
]));
```

`MultiProviderLlm` is a router impl that holds multiple `Arc<dyn Llm>` and
dispatches based on the model name in the request. It's the only impl that
knows about multiple providers; each provider impl knows only about its
own API.

---

## 6. How consolidation uses the LLM

### 6.1 Narrative generation (replaces the string template)

```
yadgar-consolidation (nightly, per directory):
  → collect period memories (decisions, events, entities, high-heat topics)
  → format as context passages:
      ContextPassage { content: memory.content, source: "memory:123", score: heat }
  → call yadgar-llm.generate_with_context(
      question: "Synthesize a concise narrative summary of this project's
                 activity in the last {N} hours. Focus on decisions made,
                 problems encountered, and current focus areas.",
      context: passages,
      options: { model: None, temperature: 0.3, max_tokens: 500 }
    )
  → LlmResponse.text = "This week, the team focused on the split-store
     decision, debating MariaDB vs Postgres. They ultimately chose Postgres
     for its RLS support, which is critical for multi-tenant isolation..."
  → store as NarrativeEntry with the LLM-generated summary
```

**Degradation:** if LLM is down, fall back to the current string-template
narrative (the template still works — it's just less useful). The
consolidation service catches `LlmError::ProviderUnavailable` and uses the
template. This is the graceful-degradation pattern from the architecture
principles.

### 6.2 Dream replay insight synthesis

```
yadgar-consolidation (nightly, for each high-similarity cross-domain pair):
  → memory_a + memory_b
  → call yadgar-llm.generate(
      prompt: "Memory A: {memory_a.content}\nMemory B: {memory_b.content}\n
               What is the non-obvious connection between these two memories?
               Generate a one-sentence insight."
      options: { temperature: 0.5, max_tokens: 100 }
    )
  → LlmResponse.text = "The split-store quiesce point and the backup
     verification gate both solve the 'partial state across two systems'
     problem — the first proactively (snapshot ordering), the second
     reactively (restore-time verification)."
  → store as a "dream insight" memory with the LLM-generated text
```

### 6.3 Semantic gap analysis

```
yadgar-consolidation (nightly, per directory):
  → structural gaps from gap_detection (isolated entities, stale regions, etc.)
  → recent memory corpus (titles + tags, not full content — token budget)
  → call yadgar-llm.generate_with_context(
      question: "Analyze the knowledge gaps in this project's memory corpus.
                 What important topics have no coverage? What decisions are
                 referenced but not formalized? What areas have stale
                 information?",
      context: gap_list + memory_summaries,
      options: { temperature: 0.2, max_tokens: 500 }
    )
  → store the semantic gap analysis as a memory (tagged "gap-analysis")
  → feed into the next session's restore() as a predicted-context signal
```

### 6.4 Memory compression (gist extraction)

```
yadgar-consolidation (nightly, for memories above compression threshold):
  → memory with full content (2000 chars)
  → call yadgar-llm.generate(
      prompt: "Extract the key fact from this memory in one sentence:\n{content}"
      options: { temperature: 0.0, max_tokens: 100 }
    )
  → LlmResponse.text = "Postgres RLS enforces tenant isolation at the engine
     layer, preventing data leaks even if application code forgets to filter."
  → store as compression_level=1 (gist) with original_content preserved
```

---

## 7. Answer-first recall — the product vision

### 7.1 The inversion

> **Given the correct amount of resources, the recall response should be
> around 1 second and so accurate that we only respond with the answer
> instead of the instance calling it wasting tokens on deciphering through
> results and deducing what it needs, greatly improving performance and
> accuracy.**

This is the SaaS product vision, and it inverts the current architecture
fundamentally:

| | Current (Python yadgar) | SaaS rewrite (answer-first) |
|---|---|---|
| What recall returns | A list of memory/wiki dicts (raw hits) | A synthesized answer + citations |
| Who synthesizes | The client (the AI agent reads 5-10 dicts, deduces the answer) | The server (recall + LLM synthesizes server-side) |
| Token cost to the client | High — 5-10 dicts × ~500 tokens each = 2.5-5k input tokens to parse | Low — one answer string ~200-500 tokens |
| Latency | ~200ms (retrieval only) | ~1s (retrieval ~200ms + LLM generation ~800ms) |
| Accuracy | Depends on the client's ability to parse + deduce | Higher — the LLM is specialized for synthesis, with the exact retrieved context |

**The current model wastes the client's token budget on raw data
transport.** The agent receives 5-10 memory dicts, parses them, identifies
which ones are relevant, synthesizes an answer, and responds. That's
2.5-5k tokens of input just for the recall results, plus the agent's
reasoning tokens. The SaaS model does the synthesis server-side with a
fast LLM (Claude Haiku, ~800ms) and returns a 200-token answer. The agent
gets a ready-to-use answer — no parsing, no deduction, no wasted tokens.

### 7.2 The latency budget — 1 second target

```
recall request arrives at gateway
  → IAM authn + authz                           ~5ms
  → recall pipeline:
      → embed query (yadgar-ml)                  ~20ms  (candle, CPU)
      → KNN + FTS + fusion (yadgar-recall)       ~50ms  (Surreal + in-process)
      → rerank top-K (yadgar-ml, Ettin)          ~50ms  (candle, CPU)
      → total retrieval                          ~120ms
  → LLM answer synthesis (yadgar-llm):
      → format context + prompt                  ~5ms
      → LLM generate (Claude Haiku, 500 tokens)  ~600-800ms
      → total LLM                                ~800ms
  → gateway response                             ~5ms
  ─────────────────────────────────────────────────────
  total                                          ~930ms ≈ 1s
```

**1 second is achievable with:**
- Candle embed + Ettin rerank in-process (~70ms total, no network hop)
- Claude Haiku (or equivalent fast model) for answer generation (~800ms
  for 500 tokens at temperature 0.0)
- Parallel where possible (embed + FTS can run in parallel)

**With GPU (SaaS enterprise tier):**
- Embed + rerank on GPU: ~10ms
- Claude Sonnet (better quality, still ~600ms for 500 tokens)
- Total: ~700ms

**With a local model (solo, Ollama):**
- Embed + rerank: ~70ms
- Llama3-8B on CPU: ~2-5s (slower — solo is not the 1s target)
- Solo answer-first is best-effort, not guaranteed

### 7.3 The response shape — answer-first, citations-second

```rust
// crates/yadgar-protocol/src/domain/recall.rs — REVISED

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallResponse {
    /// The synthesized answer. This is the PRIMARY payload.
    /// The client should use this directly, not parse the citations.
    pub answer: String,

    /// Citations — the memories/wiki pages that support the answer.
    /// Secondary: for verification, "show me the source," or when the
    /// client wants to dig deeper. NOT required for the common case.
    pub citations: Vec<Citation>,

    /// Whether the answer was LLM-synthesized or template-generated.
    /// false = LLM was down/slow, answer is a concatenation of top hits.
    /// The client can check this to decide whether to fall back to
    /// parsing citations itself.
    pub synthesized: bool,

    /// The retrieval results, for clients that want raw access.
    /// Most clients should use `answer` + `citations`, not this.
    /// Included for backward compat and power users.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub raw_results: Vec<MemoryHit>,

    /// Performance breakdown (for observability + client-side timeout decisions)
    pub timing: RecallTiming,

    #[serde(default)]
    pub schema_version: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Citation {
    pub source: String,           // "memory:123" or "wiki:yadgar-adr-0195"
    pub snippet: String,          // the relevant excerpt that supports the answer
    pub score: f32,               // retrieval score
    pub url: Option<String>,      // deep link if available (SaaS web UI)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallTiming {
    pub retrieval_ms: u32,
    pub llm_ms: Option<u32>,      // None if not synthesized
    pub total_ms: u32,
}
```

**The `answer` field is the primary payload.** The `citations` are
secondary. The `raw_results` are tertiary (backward compat). This is the
inversion: the current system returns `raw_results` as the primary (and
only) payload; the SaaS returns `answer` as the primary.

### 7.4 The prompt — precision is everything

The LLM prompt for answer synthesis is the critical path. It must be
engineered for precision, not creativity:

```
System: You are a memory retrieval assistant. Answer the user's question
using ONLY the provided context passages. If the answer is not in the
context, say "I don't have enough information to answer this." Do not
hallucinate. Do not use prior knowledge. Cite each passage as [1], [2],
etc. Be concise — one paragraph maximum.

User: {question}

Context:
[1] (score: 0.94) {memory_1.content}
[2] (score: 0.91) {memory_2.content}
[3] (score: 0.88) {wiki_page.content}
...
```

**Temperature: 0.0** — deterministic, no creativity. The LLM is a
synthesizer, not a generator. It extracts and composes, it does not invent.

**Max tokens: 500** — concise answers. The agent doesn't need a 2000-token
essay; it needs a 200-token answer it can use directly.

**"I don't have enough information"** — if the context doesn't contain the
answer, the LLM says so. This is more honest than the current system, which
returns 5 irrelevant hits and lets the agent figure out they're irrelevant.

### 7.5 Degradation — when the LLM is down or slow

```
recall request → retrieval pipeline → top-K results

IF llm is available AND latency_budget allows:
  → call yadgar-llm.generate_with_context(timeout_ms: remaining_budget)
  → IF LLM responds in time:
    → return RecallResponse { answer: llm_text, citations, synthesized: true }
  → IF LLM times out or errors:
    → FALLBACK: answer = concatenate top-3 snippets with "Based on retrieved memories: ..."
    → return RecallResponse { answer: template_answer, citations, synthesized: false }

IF llm is NOT available:
  → answer = concatenate top-3 snippets
  → return RecallResponse { answer: template_answer, citations, synthesized: false }
```

**Degradation is graceful and transparent.** The `synthesized: false`
field tells the client "this answer is not LLM-synthesized, it's a
template — you may want to parse the citations yourself for higher
quality." The client can decide: trust the template answer (fast, good
enough for simple queries) or fall back to parsing citations (slower,
better for complex queries).

### 7.6 The latency budget is per-request, not per-service

The gateway sets a deadline on the recall request (default: 1500ms for
SaaS, 5000ms for solo). The recall service tracks the deadline:

```
gateway → recall /v1/recall { deadline_ms: 1500 }
  → recall: retrieval starts
  → retrieval done at t=120ms (1280ms remaining)
  → recall: LLM call with timeout_ms: 1200 (leave 80ms for response formatting)
  → IF LLM responds at t=900ms:
    → return answer (total: 905ms, under budget)
  → IF LLM doesn't respond by t=1320ms (120ms + 1200ms):
    → cancel LLM call, return template answer (total: ~1320ms, under budget)
  → IF retrieval itself takes >1500ms:
    → return partial results + template answer (total: 1500ms, at budget)
```

**The deadline propagates through the protocol.** `RecallRequest` carries
`deadline_ms`. The recall service allocates its budget: retrieval first
(estimated ~200ms), LLM second (remaining budget). If retrieval is slow,
LLM gets less budget. If retrieval exceeds the budget, LLM is skipped
(template answer only).

This is the `deadline_ms` field that already exists on the current Python
`RecallRequest` (`embed_service_models.py:79`) — the SaaS rewrite keeps
the pattern and makes it load-bearing for the answer-first target.

### 7.7 What this means for the gateway + MCP

The MCP `recall` tool's return shape changes:

```
// Current (Python yadgar):
recall(query="split-store decision") → [
  { id: 123, content: "ADR-0195: backend runs TWO engines...", heat: 1.0, ... },
  { id: 124, content: "ADR-0196: identity belongs to the engine...", heat: 1.0, ... },
  ...
]

// SaaS rewrite (answer-first):
recall(query="split-store decision") → {
  answer: "The split-store decision (ADR-0195) runs two engines: SurrealDB
           for graph/memory/wiki bodies + embeddings, and Postgres for the
           relational set (tasks, ADR metadata, runtime config). Bodies stay
           in Surreal because moving them would break crossref reachability
           and embedding cost. The relational set moves to per-service
           Postgres databases for independent swap and tenant isolation via RLS.",
  citations: [
    { source: "wiki:yadgar-adr-0195", snippet: "backend runs TWO engines...", score: 0.94 },
    { source: "wiki:yadgar-adr-0196", snippet: "identity belongs to the engine...", score: 0.91 },
  ],
  synthesized: true,
  timing: { retrieval_ms: 120, llm_ms: 780, total_ms: 905 },
}
```

**The agent reads the `answer` and responds to the user.** It doesn't need
to parse 5 memory dicts, identify the relevant parts, synthesize a
response. The server did that work. The agent's token cost drops from
~3k input tokens (parsing 5 dicts) to ~200 input tokens (reading the
answer). The response latency drops from "agent reads + reasons + responds"
(~5-10s) to "agent reads answer + responds" (~2-3s total including the
1s recall).

### 7.8 When raw results are still needed

Answer-first is the default, but some use cases need raw results:
- **The agent wants to verify the answer** — read the citations directly.
- **The agent wants to write a memory about the retrieved facts** —
  needs the full content, not the synthesized answer.
- **A non-AI client (dashboard, search UI)** — wants to display the list,
  not a synthesized paragraph.
- **The query is a broad exploration** — "what do I know about this
  project?" doesn't have a single answer; a list is more useful.

**Solution:** the `recall` tool accepts a `response_format` parameter:

```
recall(query, response_format: "answer")    → RecallResponse { answer, citations }  // DEFAULT
recall(query, response_format: "raw")       → RecallResponse { raw_results }        // explicit raw
recall(query, response_format: "both")      → RecallResponse { answer, citations, raw_results }  // power user
```

**Default is "answer"** — the 1s target, the token-saving, the accuracy
improvement. "raw" is the escape hatch. "both" is for power users who want
both (costs more tokens but gives maximum flexibility).

---

## 8. Loose coupling (the LLM service boots alone)

The LLM service follows the same loose-coupling protocol as every other
service:

- **`/healthz`** = 200 if the process is alive (doesn't check the provider).
- **`/readyz`** = 200 if at least one provider is reachable (checks
  `Llm::health()`). 503 if all providers are down.
- **Consolidation without LLM:** degrades to template narrative + structural
  gap detection (the current behavior). No data loss, just lower quality.
- **Recall without LLM:** returns un-augmented results. No data loss, just
  no synthesized answer.
- **Circuit breaker:** if the provider API is down, the circuit opens.
  Consolidation/recall get `LlmError::ProviderUnavailable` and degrade.

**The LLM is never on the critical path for data integrity.** Memories are
stored without LLM involvement (the write service doesn't call the LLM).
The LLM only enhances: better narratives, better gap analysis, synthesized
answers. If it's down, the system degrades to the current quality, not to
data loss.

---

## 9. What this adds to the protocol crate

| New item | Type | Approx LOC |
|---|---|---|
| `Llm` trait | trait | ~60 |
| `LlmRequest`, `LlmResponse`, `LlmOptions` | wire types | ~50 |
| `TokenUsage`, `FinishReason` | wire types | ~20 |
| `ContextPassage`, `ChatMessage`, `ChatRole` | wire types | ~30 |
| `LlmHealth`, `ModelInfo`, `ModelCapability` | wire types | ~30 |
| `LlmError` | error enum | ~20 |
| **Total** | | **~210 LOC** |

Protocol crate size budget: ~2600 → **~2810 LOC**. Still well under the
5000 LOC size check limit.

**Trait count: 18 → 19** (adding `Llm`).

---

## 10. What this adds to the SaaS rewrite plan

| Change | Detail |
|---|---|
| Service count | 13 → 14 (add `yadgar-llm`) |
| Data plane | `yadgar-ml` (vector scoring) + `yadgar-llm` (text generation) are peers |
| Consolidation | Now calls `yadgar-llm` for narrative/dream/gap/compression |
| Recall | Opt-in answer augmentation via `yadgar-llm` (profile=full) |
| Metering | Records LLM token usage + cost per call |
| Config | `llm.default_model` per tenant plan (config knob) |
| Migration order | Insert `yadgar-llm` after `yadgar-ml` (step 8.5) |

### 10.1 Revised migration order (insertion)

| Step | Service | Effort | Notes |
|---|---|---|---|
| 8 | `yadgar-ml` (embed + Ettin) | 2-3 weeks | vector scoring |
| **8.5** | **`yadgar-llm`** | **2 weeks** | **LLM text generation — the `Llm` trait + AnthropicLlm impl + OllamaLlm impl. Needed by consolidation (step 13) and recall augmentation (step 9)** |
| 9 | `yadgar-recall` | 2-3 months | the IP, eval-gated. Optional LLM augmentation. |
| ... | | | |
| 13 | `yadgar-consolidation` | 1-2 months | calls `yadgar-llm` for narrative/dream/gap/compression |

**The LLM service lands before consolidation (step 8.5 < step 13) and
before recall (step 8.5 < step 9)** so both consumers have it available
when they're built. It also lands before the retrieval port — the LLM
service is mechanical (API wrapper + model selection), not the IP-risk
piece, so it can be built in parallel with the retrieval port.

---

## 11. Open questions

1. **Streaming vs non-streaming.** The `LlmOptions.stream` field supports
   SSE streaming for long generations. Should the gateway expose this to
   MCP clients? MCP doesn't currently support streaming tool results.
   **Recommendation:** day-1 is non-streaming (wait for full response).
   Streaming is a future enhancement for the gateway's HTTP API (not MCP).
2. **Local model (Ollama) in solo mode.** Should the solo binary require
   Ollama, or fall back to no-LLM (template narrative only)?
   **Recommendation:** no-LLM fallback. The solo binary detects Ollama at
   boot; if present, wires `OllamaLlm`; if absent, wires `NullLlm` (every
   call returns `LlmError::ProviderUnavailable`). Consolidation degrades to
   templates. This keeps the solo binary zero-dependency.
3. **`NullLlm` in the protocol crate?** Like `NullCache` and `NullMLClient`
   in the current Python system, a `NullLlm` that returns
   `ProviderUnavailable` for every call. Useful for tests and for solo
   mode without Ollama. **Recommendation:** yes, put it in the protocol
   crate alongside the trait (it's a no-impl default, no SDK needed).
4. **Multi-provider routing.** `MultiProviderLlm` holds multiple
   `Arc<dyn Llm>` and routes by model name. Where does this live?
   **Recommendation:** in `yadgar-llm` (the service crate), not the
   protocol crate. It's service logic (routing + model selection), not a
   protocol contract.
5. **Context window management.** If the context passages exceed the
   model's context window, who truncates? **Recommendation:** the LLM
   service, not the caller. The service knows the model's context window
   (`ModelInfo.context_window`); it truncates or summarizes the context to
   fit. The caller just passes the passages; the service handles the
   constraint. This keeps the caller simple and centralizes the
   model-specific logic.
6. **Caching LLM responses.** Should identical prompts be cached (Valkey)
   to avoid redundant API calls? **Recommendation:** yes, with a TTL
   (configurable per model). The cache key is `hash(prompt + system_prompt
   + model + temperature)`. Narrative generation (same directory, same
   period) is idempotent within a consolidation cycle — caching prevents
   redundant calls if consolidation retries. **Implementation:** the LLM
   service uses the `Cache` trait (already in the protocol) to check
   before calling the provider.

---

## 12. The one-sentence summary

**`yadgar-llm` is the 14th service — a unified LLM gateway that abstracts
provider (Anthropic, OpenAI, Ollama, candle) behind the `Llm` trait, with
per-tenant model selection, token-billing metering, rate-limit backpressure,
and graceful degradation so consolidation falls back to templates and recall
falls back to un-augmented results when the LLM is down.**
