# Yadgar Architecture

Yadgar is a persistent memory engine for Claude Code. It stores, decays, and retrieves memories across sessions so the model accumulates contextual knowledge over time rather than starting fresh each conversation.

## System Overview

```
Claude Code (MCP client)
        │
        │  MCP protocol (stdio or streamable-HTTP)
        ▼
┌───────────────────────────────────────────────────────┐
│                    server.py (MCP server)              │
│   memorize / recall / project_brief / anchor /        │
│   checkpoint / restore / wiki_* / forget / seed /...  │
└────────┬──────────────────────────────────────────────┘
         │
         ├──► sensory_buffer.py  (auto-capture tool actions)
         │
         ├──► rules_engine.py    (write-gate: block secrets, custom rules)
         │
         ├──► secrets.py         (always-on credential scrubbing)
         │
         ├──► embeddings.py      (sentence-transformer, LRU cache)
         │
         ├──► enrichment.py      (ConceptNet / COMET / doc2query index-time expansion)
         │
         ├──► storage.py         (SurrealDB: memories, episodes, entities, wiki)
         │
         └──► retrieval/         (multi-signal search, fusion, reranking)
                  core.py
                  wrrf.py
                  routing.py
                  temporal.py
                  adversarial.py
```

The consolidation daemon runs independently in the background:

```
consolidation.py (ConsolidationScheduler)
    │
    ├── thermodynamics.py    heat decay, archiving, per-type thresholds
    ├── curation.py          duplicate merging, _memify_prune
    ├── cls_store.py         episodic → semantic promotion (CLS)
    ├── knowledge_graph.py   entity extraction, co-occurrence relationships
    ├── sleep_compute.py     dream replay, narrative summarisation
    ├── astrocyte_pool.py    domain-partitioned background workers
    ├── causal_discovery.py  causal edge inference
    └── narrative.py         autobiographical story generation
```

## Data Flow

### Write path (`memorize`)

1. **Secret scrub** — content checked against credential patterns (AWS, JWT, etc.)
2. **Rules engine** — custom write-block rules evaluated
3. **Write gate** — similarity scored against recent memories; too similar → rejected (threshold configurable)
4. **Embedding** — sentence-transformer encodes content; cached
5. **Index-time enrichment** — ConceptNet/COMET/doc2query terms appended to embedding text (optional)
6. **Storage** — record inserted into SurrealDB with `heat=1.0`, `confidence`, tags, directory context
7. **Reinjection** — related existing memories surfaced back to the caller (optional)

### Read path (`recall`)

1. **Query routing** — classifies query as temporal, code, relational, comparison, or open-domain
2. **Query expansion** — pseudo-HyDE generates synthetic answer for embedding (optional)
3. **Candidate retrieval** — four parallel signals:
   - Vector cosine search (ANN)
   - BM25 full-text search
   - Personalized PageRank on knowledge graph
   - Spreading activation from seed entities
4. **WRRF fusion** — Weighted Reciprocal Rank Fusion blends signal lists
5. **Confidence gate** — low-confidence result sets trigger fallback strategy
6. **Reranking** — cross-encoder (FlashRank or GTE-ModernBERT) scores top-K pairs
7. **NLI entailment** — optional DeBERTa entailment signal blended in
8. **Multi-passage aggregation** — evidence clusters formed for open-domain queries
9. **Adversarial filter** — score-gap and diversity checks before return

### Consolidation path (background daemon)

Fires after `IDLE_THRESHOLD_SECONDS` of no activity:

1. **Decay** (`apply_decay`) — heat reduced per-memory using `DECAY_FACTOR^hours_elapsed` with modifiers for importance, emotional valence, confidence
2. **Episode processing** (`process_episodes`) — new episodes parsed for file paths, function names, imports, errors; co-occurring entities get `co_occurrence` edges
3. **Duplicate merging** (`merge_duplicates`) — pairs with similarity > `CURATION_SIMILARITY_THRESHOLD` merged (higher-heat survives)
4. **Link similar** (`link_similar`) — near-duplicate links added to knowledge graph
5. **Causal detection** (`detect_causality`) — causal edge inference from co-occurrence patterns
6. **Memify** — `_memify_prune` self-improvement cycle: prune/strengthen/reweight/derive
7. **CLS consolidation** (`cls_consolidation`) — episodic patterns promoted to semantic memory
8. **Action log** — unprocessed action-log entries summarised into real memories
9. **Consolidation log** — timestamped run record inserted

Dream replay runs separately in `_maybe_sleep_cycle()` — triggered at most once every 6 hours, after the daily consolidation cycle completes. It examines random memory pairs for latent relationships and drives narrative summarisation.

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `server/` | MCP tool handlers, session management, action-stream capture (subpackage: `tools/`, `middleware/`, `transport/`) |
| `storage/` | All SurrealDB reads/writes; schema ownership (subpackage: `ops.py`, `client.py`, `schema.py`) |
| `consolidation/` | Background daemon loop, coordinates all consolidation stages (subpackage: `scheduler.py`, `phases/`) |
| `retrieval/` | Multi-signal search, fusion, reranking pipeline (subpackage: `core.py`, `wrrf.py`, `routing.py`, `temporal.py`, `adversarial.py`) |
| `thermodynamics.py` | Heat decay formula with importance/valence/confidence modifiers |
| `curation.py` | Duplicate detection, merge, `_memify_prune` for action-stream cleanup |
| `embeddings.py` | Sentence-transformer wrapper, LRU embedding cache |
| `enrichment.py` | Index-time text enrichment (ConceptNet, COMET, doc2query) |
| `knowledge_graph.py` | Entity extraction from episodes, relationship edges |
| `cls_store.py` | Complementary Learning Systems: episodic → semantic promotion |
| `sleep_compute.py` | Dream replay, narrative generation trigger |
| `narrative.py` | Autobiographical story from recent memories |
| `sensory_buffer.py` | Buffer for incoming tool-action events (action stream) |
| `rules_engine.py` | Write-block and write-allow rules evaluation |
| `secrets.py` | Always-on credential pattern scrubbing |
| `restoration.py` | checkpoint / restore / anchor logic |
| `wiki.py` | Wiki page CRUD, draft/approve workflow |
| `config.py` | Pydantic settings (env vars → YAML → defaults) |
| `config_yaml.py` | `yadgar config` CLI subcommands, FIELD_META documentation |
| `daemon.py` | systemd-style daemon start/stop/status, MCP transport switching |
| `seed.py` | One-shot project bootstrap (`yadgar seed <directory>`) |
| `viz_server.py` | Knowledge graph visualisation server |
| `file_queue.py` | Async write queue for wiki and storage operations |
| `metacognition.py` | Self-monitoring, memory quality scoring |
| `astrocyte_pool.py` | Domain-partitioned async worker pool |
| `causal_discovery.py` | Causal edge inference from co-occurrence patterns |
| `cognitive_map.py` | Spatial/topological memory organisation |
| `engram.py` | Engram slot model (excitability, plasticity, stability) |
| `predictive_coding.py` | Prediction error signal for surprise gating |
| `prospective.py` | Forward-looking memory (plans, intentions) |
| `staleness.py` | File-hash-based staleness detection for code memories |
| `remote_embeddings.py` | HTTP embedding service for Docker deployments |
| `embed_service.py` | Embedding microservice server |
| `models.py` | Shared Pydantic data models |

## Storage Schema

SurrealDB tables:

| Table | Contents |
|---|---|
| `memory` | Core memory records: content, embedding, heat, confidence, tags, directory_context, `branch: option<string>` (auto-captured from `git rev-parse --abbrev-ref HEAD` on `memorize`/`anchor`/`checkpoint`/`wiki_add`) |
| `episodes` | Raw tool-action log chunks before consolidation |
| `entities` | Extracted code/file/concept entities with heat |
| `relationships` | Edges between entities (co_occurrence, causal, etc.) |
| `wiki_page` | User-approved wiki pages (markdown), `branch: option<string>` (same auto-capture as `memory`) |
| `wiki_page_version` | Immutable version snapshots for wiki pages: full content + change_summary per version (v5.41.0, migration 013). Powers `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_append_section`. |
| `wiki_drafts` | Pending drafts awaiting approval |
| `checkpoints` | Saved working state snapshots |
| `profiles` | Structured user attribute records |
| `beliefs` | Higher-order derived beliefs |
| `consolidation_log` | Timestamped record of every consolidation run |

## Transport Modes

- **stdio** (default): Claude Code spawns yadgar as a child process. Zero network, lowest latency.
- **streamable-HTTP** (`--transport streamable-http --port 8765`): Persistent daemon. One process serves all Claude sessions. Required for Docker.

## Docker Deployment

The included `Dockerfile` and `docker-compose.yml` run yadgar as two containers:

- **`yadgar-backend`** (`Dockerfile.backend`): SurrealDB + embedding microservice (`embed_service.py`). Exposes port 8001 (embed) on loopback only.
- **`yadgar-core`** (`Dockerfile`): MCP server in streamable-HTTP mode. Connects to `yadgar-backend` for DB and embeddings. Exposes port 8765.

`yadgar-core` waits for `yadgar-backend` healthcheck before starting (`depends_on: condition: service_healthy`).

On non-Docker hosts, `yadgar-vacuum.service` (systemd oneshot, v4.8+) runs `yadgar vacuum --service-mode=systemd` on a weekly timer (`yadgar-vacuum.timer`). It is not a Docker service — it runs on the host and connects to the MCP daemon over HTTP.

Configuration can be injected via environment variables (`YADGAR_*`) without rebuilding the image, or by mounting a `config.yaml` at `/root/.yadgar/config.yaml`.

## Branch-Aware Retrieval (v5.0)

Every `memory` and `wiki_page` row carries an optional `branch` column captured at write time via `git -C <directory> rev-parse --abbrev-ref HEAD` (30-second LRU cache). Pre-v5 rows backfill to `'master'` in transactional migration #004.

Default branch is resolved via `git symbolic-ref refs/remotes/origin/HEAD` (5-minute LRU cache).

`recall()` and `wiki_query()` filter `branch IN (current, default, NULL)` post-fetch. Results where `branch == current` get a convex-combination boost (`score + (1 - score) * 0.2`) and re-sort. Non-git directories degenerate to `branch IN (default, NULL)` with no boost.

`wiki_read(slug)` resolves via three-step lookup: exact slug on current branch → on default branch → on `branch IS NONE` (legacy). `wiki_cleanup_merged_branches(directory, dry_run)` removes wikis whose branch is gone.

## Retrieval Pipeline Decomposition (v5.0)

`retrieval/core.py::recall()` decomposed from a 517-line function into a thin orchestrator over eight named pipeline stages:

1. `_collect_fts_scores` — FTS BM25 + entity-FTS + COMET expansion
2. `_collect_vector_scores` — KNN vector search; returns `(vector_memory_ids, query_embedding)`
3. `_collect_ppr_scores` — Personalized PageRank from seeds
4. `_collect_spreading_scores` — spreading activation
5. `_collect_temporal_scores` — temporal signal
6. `_fuse_scores` — confidence gating + WRRF / convex fusion
7. `_build_initial_results` — assemble results + CE diversity injection
8. `_apply_rerank_pipeline` — heuristic → comparison → CE → NLI → multi-passage → profile/belief merge → MMR → trim → adversarial → rules engine → engram links → metacognition

`causal_discovery.py::pc_algorithm` similarly decomposed: Meek R1, R2, R3 each extracted as methods. Behavior pinned by characterization tests in `yadgar/tests/test_retrieval_core_characterization.py` and `yadgar/tests/test_causal_discovery_characterization.py`.

## Security (v5.0)

Bearer-token middleware (`yadgar/auth_middleware.py`) wraps `/api/*`, `/hooks/*`, and `/mcp` routes. `/health` and `/metrics` are exempt on loopback. Token comparison uses `hmac.compare_digest` for timing-safety.

Default-deny CORS — loopback origins only (`http://127.0.0.1:*`, `http://localhost:*`) unless `YADGAR_ALLOWED_ORIGINS` overrides.

`install_hooks` ships as a real Python script at `yadgar/scripts/hook_runner.py`; the path goes through `shlex.quote` before insertion into `settings.json`, eliminating the shell-injection vector.

`yadgar/sanitize.py` strips ANSI escapes, C0/C1 control chars, and Unicode bidi-override characters (U+200B–U+200F, U+202A–U+202E, U+2066–U+2069, U+FEFF) from auto-capture payloads before action-log insert. Per-source token-bucket rate limiter (`yadgar/rate_limit.py`) bounded to 1000 keys via `OrderedDict`.

Secret patterns block storage of AWS, GCP service-account JSON, Stripe (`sk_live_`), Slack (`xoxb-`/`xoxa-`/`xoxp-`), OpenAI (legacy + `sk-proj-`), Anthropic, JWT, GitHub PATs, private keys, DB connection URIs. Always-on, not user-configurable.

## Observability (v5.0)

`/metrics` Prometheus endpoint (gated by `YADGAR_METRICS_ENABLED`, loopback-only by bind). Collectors: consolidation phase durations, queue depths (`queue/` `archive/` `dlq/`), DB query p50/p95, embedding cache hit ratio, request counts by route, action-batch size.

Structured JSON logs via `YADGAR_LOG_FORMAT=json`. `RequestLoggingMiddleware` (in `yadgar/log_config.py`) emits one INFO line per request with `request_id`, `tool_name`, `duration_ms`, `status`, `trace_id` (from `x-request-id` header).

Consolidation phase markers use `phase_start: <name>` / `phase_end: <name> duration_ms=N`. Wiki snapshot loop in `entrypoint-backend.sh` writes `/data/wiki_*.jsonl` every 6 hours with 14-day retention.
