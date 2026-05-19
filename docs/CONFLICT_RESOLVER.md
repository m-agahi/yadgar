# Conflict Resolver (C4 — v5.3.4)

Yadgar's LLM-based conflict resolution on write. Mem0 parity. Ollama-only in v5.3.

## How it works

On every `memorize()` call (sync/drain path only), the resolver:

1. Retrieves the top-K most heat-ranked existing memories.
2. Builds a structured prompt comparing the candidate to similar existing memories.
3. POSTs to Ollama `/api/generate` (JSON mode).
4. Parses the response as one of: `ADD`, `UPDATE`, `DELETE`, `NOOP`.
5. The caller honours the decision before any insert.

## Operations

| Op | Behaviour |
|----|-----------|
| `ADD` | Proceed with normal insert (default / fail-soft). |
| `UPDATE` | Update the target row's content + tags. No new row created. |
| `DELETE` | Delete the target row. No new row created. |
| `NOOP` | Skip insert. Memory is a duplicate or redundant. |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YADGAR_CONFLICT_RESOLVER` | `off` | Set to `on` to enable. Any other value disables. |
| `YADGAR_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint. |
| `YADGAR_OLLAMA_MODEL` | `qwen3:8b` | Model name for conflict resolution prompts. |
| `YADGAR_CONFLICT_K` | `5` | Top-K similar memories retrieved per candidate. |

## Enabling

```bash
export YADGAR_CONFLICT_RESOLVER=on
export YADGAR_OLLAMA_URL=http://localhost:11434
export YADGAR_OLLAMA_MODEL=qwen3:8b
```

Ollama must be running locally with the specified model pulled:

```bash
ollama pull qwen3:8b
ollama serve
```

## Disabling

Unset `YADGAR_CONFLICT_RESOLVER` or set it to anything other than `on`:

```bash
unset YADGAR_CONFLICT_RESOLVER
# or
export YADGAR_CONFLICT_RESOLVER=off
```

## Fail-soft behaviour

- Resolver disabled → `NOOP` returned immediately (no Ollama call).
- Ollama unreachable / timeout (30s) → degrade to `ADD` (optimistic insert).
- Non-JSON or unknown op in response → degrade to `ADD`.

The user-facing `memorize()` call never fails due to Ollama errors.

## Dependencies

- `httpx>=0.27` (already in `pyproject.toml` as a core dep).
- Local Ollama installation (`ollama.ai`).
- No external API keys required.

## Scope and future work

Anthropic API support is deferred to v6 per strategy wiki fork #3
(`plan-v5-3-yadgar-feature-release-cycle` → "Deferred to v6").

The conflict resolver applies only to the async drain path (sync insert).
The async enqueue path returns immediately; conflict resolution runs during
the drain worker's replay of queued items.

## Source

- `yadgar/conflict_resolver.py` — resolver logic.
- `yadgar/server/tools/memorize.py` — wiring (search for `C4:`).
- `yadgar/tests/test_conflict_resolver.py` — 6 tests.
