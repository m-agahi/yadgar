# Yadgar v7 Plan — Real-Time Synthesis Layer (Outline)

## Status

Outline only. Full spec written after v6.0.0 ships and hardware/model landscape is re-assessed.

**Dependency:** v6.0.0 merged. Do not start before.

Version bump: `6.x → 7.0.0`.

---

## Context

v6 builds the nightly LLM curator (deepseek-r1:8b, background, latency-insensitive).
v7 adds the real-time synthesis layer — making every retrieval call intelligent, not just
the nightly pass. This is the "second brain" interface: ask Yadgar a question, get a
reasoned answer, not a JSON blob.

Deferred from v6 because: two concurrent 8B models (nightly curator + real-time synthesis)
exceed current hardware. By v7 window, expect: faster quantized models, better hardware,
or dedicated inference acceleration.

---

## 1. Core Features

### 1a. `recall(query, synthesize=True)`

Raw memory records returned unchanged + synthesis field appended.
`synthesize=False` default until validated.

```json
{
  "result": [...],
  "synthesis": "...",
  "synthesis_model": "...",
  "synthesis_confidence": 0.86
}
```

### 1b. `wiki_query(query, synthesize=True)`

Same pattern. LLM reads top-N matching wiki pages, synthesizes direct answer
instead of returning raw page content.

### 1c. `ask(question)` — new MCP tool

Synthesis-only output. No raw records. For conversational callers (opencode, gemini-cli,
future chat UI) that want answers, not JSON.

Internally: `recall(synthesize=True)` + `wiki_query(synthesize=True)` combined,
synthesis merged into single response.

---

## 2. Model Requirements

Current blocker: deepseek-r1:8b takes ~69s per synthesis task. Unacceptable for
interactive use. Target: **< 10s end-to-end** for a synthesis call.

Options to re-assess at v7 planning time:
- Faster quantized model (Q4 vs Q8 — significant speed gain, small quality loss)
- Dedicated synthesis model lighter than 8B (3B-4B class if quality holds)
- Hardware upgrade (GPU with dedicated VRAM)
- Disable thinking/CoT for synthesis tasks (saves ~600 reasoning tokens, estimated 30-40s)
- Streaming response (first tokens in < 2s even if full response takes longer)
- Cloud API fallback (Haiku at $0.004/call if local model still too slow)

Benchmark before committing: run T4 (wiki synthesis) on candidate model, must complete
< 10s. If no candidate meets bar, defer again to v8.

---

## 3. Architecture Notes (from v6 design)

`SynthesisClient` protocol already exists (v6). `OllamaSynthesisClient` already exists (v6).
v7 adds:
- `YADGAR_SYNTHESIS_MODEL_REALTIME` config key (separate from nightly FAST/REASONING models)
- Async synthesis path: `recall` returns raw data immediately, synthesis delivered async
- Streaming support in `OllamaSynthesisClient` (`stream=True`)
- Cost/latency counter in `memory_stats`: `synthesis_realtime_calls_today`, `synthesis_realtime_p50_ms`

---

## 4. Open Questions (resolve at v7 planning)

1. Is there a sub-10s synthesis model available by v7 window?
2. Streaming vs blocking synthesis — does the MCP protocol support streaming tool responses?
3. Should `synthesize=True` become the default for `recall` in v7, or stay opt-in?
4. `ask()` scope: Yadgar-knowledge-only, or does it also reason over live project context (current files, git state)?
5. Cost visibility: if cloud API fallback used, expose per-call cost in response.

---

## 5. Deferred to v8+

- Cross-project synthesis (ask question spanning yadgar + qwfm + nix memories)
- Persistent conversation context for `ask()` (multi-turn dialogue with Yadgar)
- Web chat UI over `ask()` endpoint
- Fine-tuned synthesis model on Yadgar's own memory corpus
