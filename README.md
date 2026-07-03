<div align="center">

<img src="yadgar/static/yadgar.png" alt="yadgar logo" width="320">

[![CI](https://codeberg.org/maxagahi/yadgar/actions/workflows/ci.yaml/badge.svg?branch=master)](https://codeberg.org/maxagahi/yadgar/actions?workflow=ci.yaml)
[![Version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fcodeberg.org%2Fapi%2Fv1%2Frepos%2Fmaxagahi%2Fyadgar%2Ftags&query=%24%5B0%5D.name&label=version&color=blue)](https://codeberg.org/maxagahi/yadgar/tags)
[![Python](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

</div>

[Changelog](docs/CHANGELOG.md) · [Benchmark](#benchmark) · [Architecture](#architecture) · [Roadmap](#roadmap) · [JS/TS SDK](docs/sdk-js.md) · [Agents guide](AGENTS.md)

> **AI coding agents:** the operational guide lives in [`AGENTS.md`](AGENTS.md) — setup commands, dev environment, test runner, code style, PR rules, security gates. This README is the human overview.

**A persistent memory engine for Claude Code.** Tell it what matters and it survives across sessions — decaying what you stop touching, promoting what repeats, filtering recall to the git branch you're on, and pairing every memory with a curated wiki that searches through the same ranking pipeline.

*Yadgar* (یادگار) is Persian for "memento, keepsake."

---

## What it is

Claude Code forgets everything when a session ends, when you `/clear`, or when context compacts. Yadgar is the layer that remembers. It runs as an [MCP](https://modelcontextprotocol.io/) server alongside Claude Code and gives it two durable stores:

1. **Memory** — episodic + semantic facts, each carrying a `heat` value that decays over time. Access reheats; disuse decays; recurring episodes get promoted to semantic memory by a nightly consolidation cycle modelled on how brains sleep.
2. **A curated wiki** — long-lived, versioned, branch-scoped pages: conventions, module purpose, past decisions, where subsystems live. Written deliberately, not captured incidentally.

A single `recall()` query searches **both stores at once**, fuses and re-ranks the results, weights them by heat, gates out decayed entries, and boosts matches on your current git branch. Critical context can be *anchored* so it never decays; whole working states can be *checkpointed* and *restored* across a `/compact`.

### Why

- **Survives sessions.** Heat decay drops what you stopped using; surprise gating drops duplicates on arrival; anchors pin what must never be lost.
- **Branch-aware.** Recall boosts current-branch matches 1.5×; wiki pages resolve in branch precedence (current → default → unscoped) so canonical knowledge stays reachable from feature work.
- **One ranking pipeline for memory + wiki.** Curated knowledge and episodic memory come back in a single ranked result, not two separate searches.
- **Consolidates while you sleep.** A nightly brain cycle decays heat, promotes episodes to semantics, discovers causal links, forms associative links, and merges duplicates — without dropping the MCP connection.
- **Self-documenting.** Architecture Decision Records, a reusable agent-prompt library, and an interactive 3D knowledge-graph all live in the same store.

---

## Key features

### Memory
- **Heat-decay lifecycle** — every memory carries `heat ∈ [0,1]` decaying exponentially; access reheats, disuse decays, unused items eventually archive and purge.
- **Surprise-gated writes** — a similarity gate rejects near-duplicate writes on arrival (rejections land in the DLQ, inspectable via `dlq_inspect`).
- **Write-time contradiction detection** — a lightweight negation + action-divergence heuristic flags contradicting memories and decays their confidence, without blocking the write.
- **Anchors** — protected memories that never decay; `audit_anchors()` surfaces redundant, oversize, expired, or near-duplicate (cosine ≥ 0.95) anchors across projects.
- **Checkpoint / restore** — `checkpoint(directory)` snapshots your working state before compaction; `restore(directory)` reconstructs it afterward via a hippocampal-replay blend of checkpoint + anchors + hot memories + predicted context. Use `restore`, not `recall`, to resume after `/clear` or `/compact`.
- **Memory blocks** — Letta-style named, scoped, char-limited core-memory containers (`block_*`) for always-in-context working state.

### Curated wiki
- **Versioned pages** — full history (`wiki_history`, `wiki_diff`, `wiki_read_version`, `wiki_restore`); `wiki_add` commits directly (no draft step).
- **Branch-scoped resolution** — `wiki_read(slug)` resolves current branch → default branch → unscoped.
- **Surgical editing** — anchor-text and positional edit tools (`wiki_replace_text`, `wiki_insert_before/after`, `wiki_append_section`, `wiki_replace_markdown_block`, positional `wiki_*_at`) so pages mutate in place instead of full rewrites.
- **Auto-linking** — `wiki_autolink` inserts `[[slug]]` cross-references by matching page titles; validated so it never manufactures broken refs.
- **Repo wiki** — `repo_wiki_generate` builds code-structure pages from a Python repo via AST scanning (no LLM), writing directly into the wiki store with SHA256-parity staleness detection. Sync helpers: `wiki_refresh_stale`, `wiki_cleanup_merged_branches`, `wiki_coverage`, `wiki_lint`.
- **Bookmarks** — pin wiki pages in the viz UI (`bookmark_*`); drag-to-reorder, dense-integer positions.

### Unified recall
- One `recall()` query retrieves **memory + wiki together**, fused through WRRF, cross-encoder reranked, NLI- and MMR-filtered, and run through the rules engine — heat-weighted, decay-gated, branch-scoped. Filter by `type=` (`"memory"`, `"wiki"`, or both). `wiki_query` remains for wiki-only search.

### Nightly consolidation (the "brain cycle")
Runs nightly while the daemon stays up in maintenance mode (no MCP reconnect). Phases:
`apply_decay → process_episodes → merge_duplicates → link_similar → detect_causality → memify → cls_consolidation` plus a dream/sleep phase.
- **CLS promotion** — recurring episodic patterns promote to semantic memory (Complementary Learning Systems).
- **Causal discovery** — PC-algorithm causal-DAG inference over co-occurring memories.
- **Dream replay** — associative `co_occurrence` linking + insight generation; insights cap at 21 days if unaccessed.
- **Community detection** — graph clustering surfaced as clusters in the viz.
- Re-embeds stale memories; optionally precomputes the graph layout (below).

### Architecture Decision Records
- `adr_add` writes to a per-project ADR log in the wiki store, enforcing an 11-field schema and auto-incrementing IDs (ADR-0001, ADR-0002, …). A Stop-hook prompt captures decisions at session end. Yadgar dogfoods its own ADR system (ADR-0004, 0007, 0011, 0013 are all real entries).

### Agent-prompt library
- Reusable subagent dispatch prompts, stored as tagged wiki pages (`agent-prompt-<pattern>`), versioned and improving over time. `agent_prompt_save(pattern, content, purpose)` upserts one page per pattern; `agent_dispatch_prelude(pattern)` builds a prelude to prepend to a subagent prompt; `seed_agent_prompts()` seeds four starter patterns. Lookup is via tagged recall (`recall(type="wiki", tags=["agent-prompt"])`), kept out of normal recall noise.

### Knowledge-graph viz
- `yadgar viz` serves an interactive force-directed graph of memories, wiki pages, entities, and relationships at **http://localhost:42069** (2D + 3D; memory = sphere, wiki = octahedron, anchor = cube; heat encoded as color). Filter by node type, edge type, and heat; focus mode, hover-neighborhood highlight, search, and connection-count badges.
- **Precomputed server-side layout** (`VIZ_PRECOMPUTED_LAYOUT_ENABLED`, default off): the nightly cycle computes 3D node positions once (`networkx.spring_layout`, signature-cached) so the graph renders pre-laid-out instead of a cold client-side layout per load. Otherwise the client warm-starts from localStorage.
- The UI is organized into four menus: **Graph** · **Bookmarks** · **System** {Config, Health, Stats} · **Help** {Guide, Config Reference, About, Debug}.

### Config system
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) with precedence **env vars (`YADGAR_*`) > `~/.config/yadgar/config.yaml` > defaults**.
- **System → Config** is an in-browser editor (part of the viz UI): category-grouped knobs, alpha-sorted, hover tooltips, `ⓘ` deep-links to a Config Reference page, and SOURCE badges distinguishing env-var / config-file / default origins. Writes are bearer-auth gated.

### Security & observability
- **Bearer-token MCP auth** on `/api/*`, `/hooks/*`, `/mcp` — default-deny CORS, timing-safe compare, loopback-only by default. `/health` and `/metrics` exempt on loopback.
- **Always-on secret gate** blocks AWS / GCP / Stripe / Slack / OpenAI / Anthropic keys, JWTs, GitHub PATs, private keys, and DB URIs before they reach the store (cannot be disabled; context-aware allowlist for known-good fixtures).
- **Auto-capture sanitization** strips ANSI escapes, control chars, and Unicode bidi-override before action-log insert.
- **Prometheus `/metrics`** + OpenTelemetry distributed tracing across core + backend (core→backend traceparent joins one trace); structured JSON logs; per-phase consolidation duration markers. **Tri-signal standard** (v5.101, ADR-0034): every in-scope function emits span+metric+log via the `@observe` decorator, ratcheted by the I33 coverage lint.
- **Async write queue** with retry/backoff, dead-letter for permanent failures, and DLQ inspection tools (`dlq_inspect`, `dlq_requeue`, `dlq_dismiss`).

---

## Architecture

```
┌──────────────┐         MCP (stdio / streamable-http)        ┌──────────────────────────┐
│  Claude Code │ ◄──────────────────────────────────────────► │  yadgar core (:8765)     │
│   + hooks    │   memorize / recall / wiki_* / checkpoint …   │  FastAPI + MCP server    │
└──────────────┘                                               │  retrieval pipeline      │
                                                               │  consolidation scheduler │
                                                               │  viz web UI (:42069)     │
                                                               └────────────┬─────────────┘
                                                                            │ HTTP
                                                               ┌────────────▼─────────────┐
                                                               │  yadgar-backend           │
                                                               │  SurrealDB store (:8000)  │
                                                               │  embed + rerank (:8001)   │
                                                               └───────────────────────────┘
```

- **yadgar core** — the MCP server (FastAPI + the `mcp` SDK). Hosts the retrieval pipeline, the nightly consolidation scheduler, the rules engine, the security middleware, and the viz web UI on `:42069`. Talks to Claude Code over stdio or streamable-HTTP.
- **yadgar-backend** — SurrealDB as the vector + full-text store (`:8000`) and the embedding / cross-encoder rerank service (`:8001`). Versioned on an independent track from core.
- **Retrieval pipeline** — `recall()` runs FTS + KNN vector + personalized-PageRank + spreading-activation + temporal candidate generation → WRRF fusion → cross-encoder rerank → NLI → MMR diversity → adversarial detection → rules engine. Branch filter (`branch IN (current, default, NULL)`) applies post-fetch; current-branch matches get a 1.5× boost. Behavior is pinned by characterization tests.
- **Consolidation** — a nightly cycle (19:00 UTC) plus a weekly vacuum; runs in-process while the daemon stays up.

Deeper detail: [docs/architecture.md](docs/architecture.md) · [docs/retrieval.md](docs/retrieval.md) · [docs/memory-lifecycle.md](docs/memory-lifecycle.md).

---

## Install

Python 3.14+ on the host (or use the Docker path for zero host Python). All paths reach the same `yadgar setup` post-install step.

**pipx (recommended for isolated install):**
```bash
pipx install yadgar
yadgar setup
```

**Nix flake:**
```bash
nix profile install codeberg:maxagahi/yadgar
yadgar setup
```

**Plain pip:**
```bash
pip install yadgar
yadgar setup
```

**Repo checkout (development):**
```bash
git clone https://codeberg.org/maxagahi/yadgar.git
cd yadgar
make setup            # install + hooks + agents + units + seed anchors
```

`yadgar setup` writes `~/.config/yadgar/config.yaml`, generates `~/.config/yadgar/secrets.env` (chmod 600) with random `YADGAR_MCP_AUTH_TOKEN` + `SURREAL_PASS` + `YADGAR_RW_PASS` + `YADGAR_RO_PASS`, installs Claude Code hooks + subagent templates + rules, seeds anchors, and prepares systemd (Linux) or launchd (macOS) user units. Idempotent — re-run after upgrades.

Then start the daemon and register it with Claude Code:

```bash
set -a && . ~/.config/yadgar/secrets.env && set +a
yadgar daemon start
yadgar daemon configure-mcp   # writes ~/.claude.json with Authorization: Bearer header
```

Restart Claude Code, then verify:

```bash
yadgar daemon status
yadgar stats
```

Per-platform detail: [`docs/INSTALL.md`](docs/INSTALL.md).

### Stdio-only (no daemon, no Docker)

Single-session use. Skip `yadgar setup`. Add to `~/.claude.json`:

```json
{ "mcpServers": { "yadgar": { "command": "yadgar", "args": [] } } }
```

Restart Claude Code. No bearer auth; embed / rerank degrade gracefully without the backend.

### Docker

Two containers — backend (SurrealDB + embed service) and core (MCP server + viz). Note the **independent version tracks**: core tracks the project version, backend tracks its own.

```bash
docker network create yadgar-net

docker run -d --name yadgar-backend --network yadgar-net \
  -v yadgar-db-data:/data \
  -e SURREAL_USER=$SURREAL_USER \
  -e SURREAL_PASS=$SURREAL_PASS \
  openfantasy/yadgar-backend:5.8.0

docker run -d --name yadgar --network yadgar-net \
  -v yadgar-data:/data \
  -p 127.0.0.1:8765:8765 \
  -p 127.0.0.1:42069:42069 \
  -e YADGAR_DB_URL=http://yadgar-backend:8000 \
  -e YADGAR_EMBED_URL=http://yadgar-backend:8001 \
  -e YADGAR_MCP_AUTH_TOKEN=$YADGAR_MCP_AUTH_TOKEN \
  openfantasy/yadgar:5.88.2
```

Containers bundle Python 3.14 — no host Python required. Source `~/.config/yadgar/secrets.env` (or generate the required vars yourself) before launching.

### Updating

```bash
yadgar update --check   # probes PyPI; prints upgrade command; exit 0
```

For `pipx` the suggested command is `pipx upgrade yadgar`; then re-run `yadgar setup` (idempotent) to refresh hooks and units. An opt-in update orchestrator (`yadgar update --install`, default off via `update.install_enabled: false`) coordinates snapshot → image pull → graceful drain → restart → health-check → CLI upgrade, with automatic rollback (`yadgar update --rollback`). See `MIGRATION_NOTES.md`.

---

## CLI

```
yadgar                              # MCP server (stdio); --transport {stdio,sse,streamable-http}
yadgar daemon start|stop|logs|health|status
yadgar daemon configure-mcp         # write ~/.claude.json with bearer header
yadgar setup                        # first-run config + secrets + MCP snippet
yadgar stats [--project /path]      # memory statistics
yadgar viz                          # knowledge graph at http://localhost:42069
yadgar vacuum                       # compact the SurrealKV store
yadgar seed <directory>             # bootstrap memory for an existing project
yadgar repo-wiki <directory>        # generate code-structure wiki pages
yadgar rules export|import          # policy rules
yadgar config init|list|get|set     # configuration
yadgar update --check|--install|--rollback
yadgar export duckdb --output snap.duckdb   # analytics snapshot (pip install yadgar[analytics])
yadgar install-hooks|install-subagents      # wire Claude Code hooks + agent templates
```

---

## MCP tools

Yadgar exposes **75 MCP tools**. ⚡ marks `power=True` tools (gated in the minimal MCP profile). A categorized selection — full list via the running server's tool catalog:

<details><summary><b>Memory</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `memorize(content, context, tags)` | | Store a memory; auto-captures branch + surprise gate |
| `recall(query, type, max_results)` | | Unified branch-aware search over memory **and** wiki |
| `recent_memories()` | | Newest-first listing, no classifier dependency |
| `memory_get(id)` | | Fetch by numeric ID |
| `memory_update(id, fields)` | ⚡ | Patch `content` / `tags` / flags |
| `forget(id)` | | Mark for deletion |
| `anchor(content, context, reason)` | ⚡ | Protected memory; never decays |
| `checkpoint(directory, …)` | ⚡ | Snapshot pre-compaction |
| `restore(directory)` | ⚡ | Reconstruct post-compaction (hippocampal replay) |
| `memory_stats()` | | Health + counts |
| `validate_memory()` | ⚡ | Check validity against current file state |

</details>

<details><summary><b>Wiki</b> (core + editing + maintenance)</summary>

Core: `wiki_add` · `wiki_read` ⚡ · `wiki_query` · `wiki_list` ⚡ · `wiki_get` · `wiki_update` ⚡ · `wiki_delete` ⚡ · `wiki_history` · `wiki_diff` · `wiki_read_version` · `wiki_restore` ⚡ · `wiki_check_duplicate` · `wiki_lint` ⚡

Editing: `wiki_append_section` ⚡ · `wiki_replace_text` ⚡ · `wiki_delete_text` ⚡ · `wiki_insert_before/after` ⚡ · `wiki_replace_at` / `wiki_delete_at` / `wiki_insert_at` ⚡ · `wiki_replace_markdown_block` ⚡ · `wiki_autolink` ⚡ · `wiki_set_metadata` ⚡

Maintenance: `wiki_coverage` · `wiki_refresh_stale` ⚡ · `wiki_cleanup_merged_branches` ⚡ · `repo_wiki_generate`

> `wiki_add` commits directly — there is no draft/approve workflow on the production write path. (`wiki_drafts` / `wiki_approve` / `wiki_discard` exist but no path produces drafts.)

</details>

<details><summary><b>Bookmarks &amp; Blocks</b></summary>

Bookmarks: `bookmark_add` · `bookmark_remove` · `bookmark_list` · `bookmark_reorder`

Blocks (Letta-style core memory, all ⚡): `block_create` · `block_get` · `block_update` · `block_delete` · `block_list` · `block_replace` · `block_append`

</details>

<details><summary><b>Project state</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `project_brief(directory, mode)` | | Layered bootstrap — `catalog` (~500 tok), full (~1050 tok), or `signals` |
| `bootstrap_project(directory, content)` | ⚡ | Set `_project_init` |
| `update_active_work(directory, content)` | ⚡ | Atomic replace of `_active_work` |
| `seed_project(directory)` | ⚡ | Bootstrap memory from README + top-level docs |
| `install_hooks(scope)` | ⚡ | Wire Claude Code hooks; inject bearer token |

`get_project_context()` is a deprecated alias of `project_brief(mode="catalog")`.

</details>

<details><summary><b>Consolidation &amp; ops</b></summary>

`consolidate_now()` ⚡ · `vacuum_now()` ⚡ · `vacuum_checkpoints()` ⚡ · `reembed_all()` ⚡ · `check_invariants(repair)` ⚡ · `sync_instructions()` ⚡ · `archive_purge()` ⚡ · `add_rule(...)` ⚡ · `get_rules(...)` ⚡ · `audit_anchors()` ⚡

</details>

<details><summary><b>Agent-prompt library &amp; ADR</b></summary>

Agent prompts: `agent_prompt_save(pattern, content, purpose)` ⚡ · `agent_dispatch_prelude(pattern)` · `seed_agent_prompts()` ⚡

ADR: `adr_add(...)` ⚡ — append an 11-field Architecture Decision Record to the project ADR log

</details>

<details><summary><b>Dead-letter queue</b></summary>

`dlq_inspect(filter)` · `dlq_requeue(id)` ⚡ · `dlq_dismiss(id)` ⚡

</details>

A typed **JavaScript / TypeScript SDK** wraps the tool surface as async methods (Node.js, Vercel Edge, Cloudflare Workers, Deno) — see [`docs/sdk-js.md`](docs/sdk-js.md).

---

## Configuration

```bash
yadgar config init        # write ~/.config/yadgar/config.yaml
yadgar config set retrieval_profile fast
```

Priority: env vars (`YADGAR_*`) > `~/.config/yadgar/config.yaml` > defaults. Knobs are also editable in-browser via **System → Config** in the viz UI. Key vars (full reference in [docs/configuration.md](docs/configuration.md)):

| Var | Default | Purpose |
|---|---|---|
| `YADGAR_REQUIRE_AUTH` | `1` | Bearer auth on `/api/*` `/hooks/*` `/mcp`. Set `0` only during initial rollout. |
| `YADGAR_MCP_AUTH_TOKEN` | (required) | Bearer token. `yadgar setup` generates one. |
| `YADGAR_DB_PASS` | (required) | SurrealDB password. No `root:root` fallback. |
| `YADGAR_HOST` | `127.0.0.1` | Bind interface. Loopback by default. |
| `YADGAR_ALLOWED_ORIGINS` | loopback | CORS allowlist. |
| `YADGAR_METRICS_ENABLED` | `1` | Expose Prometheus `/metrics` (loopback, unauthenticated). |
| `YADGAR_LOG_FORMAT` | `human` | Set `json` for structured logs. |
| `YADGAR_VIZ_MAX_MEMORIES` / `_WIKI` / `_ENTITIES` | `500` / `200` / `2000` | Viz node caps (`0`/`-1` = unlimited). |
| `VIZ_PRECOMPUTED_LAYOUT_ENABLED` | `false` | Serve nightly-precomputed 3D layout. |
| `YADGAR_MODEL_IDLE_EVICTION_SECONDS` | `0` | Unload heavy ML models after idle seconds (`0` = stay loaded). |

---

## Subagent integration

Yadgar ships a `SubagentStop` hook that captures memory findings from Claude Code subagents: when a subagent completes, its final report is scanned for a `## Yadgar findings` section and each bullet is persisted with `provenance_agent` set to the agent type.

To opt your subagents into the protocol, paste [`docs/CLAUDE_SUBAGENT_CONTRACT.md`](docs/CLAUDE_SUBAGENT_CONTRACT.md) into your `~/.claude/CLAUDE.md`, then run `yadgar install-hooks --scope global`. The contract is opt-in — Yadgar works without it.

---

## Benchmark

Yadgar is evaluated on [**LongMemEval**](https://arxiv.org/abs/2410.10813) (ICLR 2025), the standard academic benchmark for long-term conversational memory. The `longmemeval_s` variant runs 500 questions across 6 categories against ~50 sessions of synthetic history per query, scored on **Phase 1 retrieval** (does the memory layer surface gold-context sessions in top-k?) and **Phase 2 QA accuracy** (does the reader produce the gold answer, judged by an LLM grader?).

**Headline (v5.26.0, full 500q, Sonnet 4.6 reader + judge — the most recent full run; subsequent versions targeted infrastructure, viz, wiki, and recall-unification rather than retrieval quality):**

| System | Reader | LongMemEval-s QA | vs yadgar |
|---|---|---|---|
| **yadgar v5.26.0** | **Sonnet 4.6** | **69.4%** (347/500) | — |
| mem0 V3 | GPT-4o | 94.4% | +25.0 pp |
| Zep / Graphiti | GPT-4o | 63.8% | **−5.6 pp** |

Yadgar beats Zep by 5.6 pp on the same 500-question sample. mem0 leads by 25 pp via LLM-extract-on-ingest. **Phase 1 retrieval:** MRR 0.928, Recall@10 0.906, NDCG@10 0.863 — the memory layer surfaces gold context for ~91% of queries, so the remaining QA gap is mostly reader synthesis, not retrieval.

Full methodology and per-type breakdown: [`docs/BENCHMARK_RESULTS.md`](docs/BENCHMARK_RESULTS.md).

---

## Roadmap

### Shipped highlights (v5.78 → v5.104)
- **Unified recall** (v5.78–v5.81) — memory + wiki merged into one ranked, heat-weighted, branch-scoped result; now the default.
- **ADR tooling** (`adr_add`, v5.85) + capture-first Stop-hook prompt.
- **Agent-prompt library rework** (v5.85, ADR-0007) — wiki-backed, tagged-recall lookup.
- **Wiki autolink + repo-wiki store-bridge** (v5.85).
- **Viz overhaul + precomputed server-side layout + System → Config editor** (v5.86–v5.88).
- **Recall perf + accounting** (v5.97–v5.104) — fusion/MMR N+1 batches, GTE-ModernBERT reranker (Lever-1, v5.98), spreading-activation N+1 batched (v5.104); latency now FULLY accounted via MCP-tool traces (CE ~90%, fusion pass quality-load-bearing — ADR-0035).
- **Tri-signal observability standard** (v5.100–v5.101, ADR-0034) — span+metric+log per function via the `@observe` decorator, I33 coverage lint, core→backend traceparent, OTLP → Tempo.
- **Test-speed train** (v5.104, ADR-0036) — module-scoped `storage` fixture + batched SurrealDB wipe → CI shards ~2× faster.
- **Observability train** (v5.83) — `/health` 503-on-degraded, OTLP circuit breaker, off-loop span logs.

### Major upcoming
- **v6 — Nightly LLM curator.** A local agent (Ollama; two-tier deepseek-r1 + qwen3:8b) runs each night to detect staleness, annotate contradictions, find semantic correlations beyond co-occurrence, propose merges/forgets, and dedupe wiki pages — skipping cleanly if Ollama is offline.
- **v7 — Real-time synthesis.** `recall(synthesize=True)` / `wiki_query(synthesize=True)` append a synthesized answer alongside raw records; a new `ask()` tool returns synthesis-only output. Depends on a sub-10 s local synthesis model.

Full history: [CHANGELOG.md](docs/CHANGELOG.md).

---

## Documentation

- [AGENTS.md](AGENTS.md) — operational guide for AI coding agents (setup, dev env, tests, code style, PR rules)
- [Architecture](docs/architecture.md) — component map, retrieval, security, observability
- [Memory lifecycle](docs/memory-lifecycle.md) — heat, archiving, pruning, consolidation phases
- [Retrieval](docs/retrieval.md) — fusion, rerank, branch filter, pipeline stages
- [Configuration](docs/configuration.md) — configuration reference (env vars, precedence, key knobs)
- [Install](docs/INSTALL.md) — per-platform setup + the `yadgar setup --doctor` probe
- [JS/TS SDK](docs/sdk-js.md) — typed client for the MCP tool surface
- [Release runbook](docs/RELEASE.md) · [Migration notes](MIGRATION_NOTES.md) · [Subagent contract](docs/CLAUDE_SUBAGENT_CONTRACT.md)

---

## Contributing

Every change to `yadgar/**` must update `README.md` and `docs/` in the same PR. Conventional Commits format. No `Co-Authored-By:` trailers. AI coding agents working on this repo: read [`AGENTS.md`](AGENTS.md) for the full dev / test / PR contract.

## Related projects

- **[ccpm](https://codeberg.org/maxagahi/ccpm)** — Claude Code Plugin Marketplace. Ships `code-review`, `confluence-rfc`, `git-flow`, `repo-wiki`, `tf-naming-check`, and `update-jira` plugins that compose with yadgar.

## Tribute

Inspired by [Zikkaron](https://github.com/amanhij/Zikkaron) by [@amanhij](https://github.com/amanhij). Different architecture, same north star.

## License

Apache 2.0. See [LICENSE](LICENSE).

### Third-party licenses (key dependencies)

- **SurrealDB** (default storage backend) — [Business Source License 1.1](https://github.com/surrealdb/surrealdb/blob/main/LICENSE) (BSL). The Additional Use Grant permits embedded use; yadgar bundles + operates SurrealDB as a single-tenant per-deployment store, which falls under that grant. **Non-compliant trigger:** offering a hosted multi-tenant managed yadgar service exposing the SurrealDB API directly to third-party customers — get a commercial SurrealDB license or migrate to Postgres + pgvector if that becomes a goal.
- **`surrealdb` Python SDK** — Apache 2.0 (separately licensed from the server).
- **embed / rerank models** — `sentence-transformers/all-MiniLM-L6-v2`, `cross-encoder/ms-marco-MiniLM-L-6-v2`, `cross-encoder/nli-deberta-v3-small`: Apache 2.0 weights via Hugging Face.
- **Benchmarks** (`benchmarks/`): scripts Apache 2.0, but datasets carry their own licenses — **LoCoMo is CC BY-NC 4.0 (non-commercial only)**; LongMemEval MIT. See `benchmarks/README.md`.

Full per-dependency audit: `docs/LICENSE_COMPLIANCE_AUDIT_2026-05-30.md`.
