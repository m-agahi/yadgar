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
         ├──► sanitize.py        (ANSI/C0/bidi-override scrubbing)
         │
         ├──► rate_limit.py      (per-source token-bucket, 1000-key bounded)
         │
         ├──► auth_middleware.py (bearer-token, default-deny CORS)
         │
         ├──► tracing.py         (OpenTelemetry @trace_span helpers)
         │
         ├──► metrics.py         (Prometheus counter/gauge/histogram registry)
         │
         ├──► embeddings.py      (sentence-transformer, LRU cache)
         │
         ├──► enrichment/        (subpackage: ConceptNet / COMET / doc2query)
         │
         ├──► storage/           (subpackage: ops.py, client.py, schema.py, directory.py — SurrealDB + directory scoping)
         │
         ├──► file_queue/        (subpackage: queue + dlq.py drainer + similarity gate)
         │
         └──► retrieval/         (multi-signal search, fusion, reranking)
                  core.py
                  pipeline.py    (v5.31.0 plugin architecture)
                  stages/        (subpackage of RetrievalStage instances)
                  wrrf.py
                  routing.py
                  temporal.py
                  adversarial.py
```

The consolidation daemon runs independently in the background:

```
consolidation/orchestrator.py (ConsolidationScheduler)
    │
    ├── consolidation/heat_decay.py    heat decay, archiving, per-type thresholds
    ├── curation/                      duplicate merging, _memify_prune (subpackage)
    ├── cls_store/                     episodic → semantic promotion (subpackage)
    ├── knowledge_graph.py             entity extraction, co-occurrence relationships
    ├── sleep_compute/                 dream replay, narrative summarisation (subpackage)
    ├── astrocyte_pool.py              domain-partitioned background workers
    ├── causal_discovery/              causal edge inference (subpackage)
    └── narrative.py                   autobiographical story generation
```

## Data Flow

### Write path (`memorize` / `wiki_add`)

1. **Secret scrub** — content checked against credential patterns (AWS, JWT, etc.)
2. **MCP boundary contract validation (v5.42.3 / v5.42.5)** — `branch` (or `branch_hint`) and `directory_context` required; missing → hard-reject at MCP boundary with `{"error": "missing_branch"}` or `{"error": "missing_directory"}`. Gated by `YADGAR_BRANCH_ENFORCEMENT` and `YADGAR_DIRECTORY_ENFORCEMENT` env knobs (v5.42.6, default ON); when OFF, logs WARN + increments `yadgar_writes_with_enforcement_relaxed_total` and proceeds with `branch=None`/`directory="global"`.
3. **Rules engine** — custom write-block rules evaluated
4. **Drainer enforcement (defense-in-depth, v5.42.3 / v5.42.5)** — for queued writes, drainer re-validates branch + directory presence; missing → DLQ with `failure_reason=missing_branch` or `failure_reason=missing_directory`. Same knob gating as step 2.
5. **Similarity gate (wiki_add only, drainer-deferred — v5.41.5)** — gate runs in drainer pre-apply stage (not request path; preserves I9 ≤5ms p50). Near-duplicate detected → DLQ with `failure_reason=duplicate_detected`. `wait=True` callers receive sync rejection payload; `wait=False` callers see `{"queued": True, "similarity_check": "deferred"}`.
6. **Write gate (memorize only)** — similarity scored against recent memories; too similar → rejected (threshold configurable)
7. **Embedding** — sentence-transformer encodes content; cached
8. **Index-time enrichment** — ConceptNet/COMET/doc2query terms appended to embedding text (optional)
9. **Storage** — record inserted into SurrealDB with `heat=1.0`, `confidence`, tags, `directory_context`, `branch`
10. **Versioning snapshot (wiki_add only, v5.41.0)** — wiki_page write triggers `wiki_page_version` snapshot insert in the same compound `BEGIN; CREATE...; CREATE...; COMMIT` transaction (v5.41.1 atomicity).
11. **Reinjection** — related existing memories surfaced back to the caller (optional)

### Read path (`recall` / `wiki_read` / `wiki_query`)

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

`recall()` accepts a `profile` kwarg (`"fast"`, `"balanced"`, `"full"`, `"debug"`) + `stage_overrides` (v5.31.0), plus `directory` and `branch_hint` (v5.43.0) for caller-context-driven branch detection — same daemon-CWD-avoidance pattern as `wiki_read`. Profile routes through the `RetrievalPipeline` stage orchestrator (`retrieval/pipeline.py`) instead of the monolithic path. Each stage is a `RetrievalStage` instance composable via plugin registry.

`wiki_read(slug)` uses §25 4-step directory-aware resolution (v5.42.5, `storage/wiki.py:314`): (1) `directory=caller_dir AND branch=current_branch`, (2) `directory=caller_dir AND branch IS NULL`, (3) `directory='global' AND branch IS NULL`, (4) not found → error dict. When no `directory` is supplied, falls back to legacy 3-step branch-only resolution. `branch_hint` parameter (v5.42.3/v5.42.6) supplies the caller's branch when daemon-side `_detect_branch` returns None (container scenario).

### Consolidation path (background daemon)

`consolidation/orchestrator.py::ConsolidationScheduler` (v5.1 subpackage decomposition) fires after `IDLE_THRESHOLD_SECONDS` of no activity:

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

**Embedded-mode compatibility (v5.63):** `storage/client.py` runs `batch_writes` per-statement in embedded SurrealDB Python SDK mode (not a single batch call). The `_inline_int_record_ids` helper rewrites `type::record('t', $int)` syntax — rejected by the embedded SDK — to `t:{int}` syntax before execution. Without this, every nightly consolidation run failed silently in embedded mode.

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `server/` | MCP tool handlers, session management, action-stream capture (subpackage: `tools/`, `middleware/`, `transport/`) |
| `storage/` | All SurrealDB reads/writes; schema ownership (subpackage: `ops.py`, `client.py`, `schema.py`, `migrations.py`, `wiki.py`, `memory.py`, `directory.py` — eligible-set predicate + SQL builder, v5.62) |
| `consolidation/` | Background daemon loop (subpackage: `orchestrator.py`, `heat_decay.py`, `cls.py`, `causal.py`, `cleanup.py`) |
| `retrieval/` | Multi-signal search, fusion, reranking (subpackage: `core.py`, `pipeline.py` v5.31.0 plugin arch, `stages/`, `wrrf.py`, `routing.py`, `temporal.py`, `adversarial.py`) |
| `enrichment/` | Index-time text enrichment (subpackage: `conceptnet.py`, `comet.py`, `doc2query.py`) |
| `curation/` | Duplicate detection, merge, `_memify_prune` (subpackage: `ingestion.py`, `prune_passes.py`) |
| `cls_store/` | Complementary Learning Systems: episodic → semantic (subpackage) |
| `causal_discovery/` | Causal edge inference (subpackage; Meek R1/R2/R3 PC algorithm decomposed) |
| `sleep_compute/` | Dream replay, narrative trigger (subpackage) |
| `metacognition/` | Self-monitoring, memory quality scoring (subpackage) |
| `file_queue/` | Async write queue + drainer (`dlq.py`: similarity gate, branch/directory enforcement, DLQ taxonomy) |
| `vacuum/` | Scheduled database vacuum phases (subpackage; strip + orchestrator) |
| `security/` | Allowlist logic for secret-gate bypass (v5.13.0) |
| `export/` | DuckDB exporter for offline analytics (v5.27.0) |
| `cli/` | All `yadgar <subcommand>` CLI entry points |
| `hooks/` | Claude Code hook runner scripts (SessionStart, SubagentStop, etc.) |
| `observability/` | Timing helpers for stage-level profiling |
| `thermodynamics.py` | Heat decay formula with importance/valence/confidence modifiers |
| `embeddings.py` | Sentence-transformer wrapper, LRU embedding cache |
| `knowledge_graph.py` | Entity extraction from episodes, relationship edges |
| `narrative.py` | Autobiographical story from recent memories |
| `sensory_buffer.py` | Buffer for incoming tool-action events (action stream) |
| `rules_engine.py` | Write-block and write-allow rules evaluation |
| `secrets.py` | Always-on credential pattern scrubbing |
| `sanitize.py` | ANSI / C0/C1 / bidi-override scrubbing for auto-capture payloads |
| `rate_limit.py` | Per-source token-bucket rate limiter (1000-key OrderedDict bounded) |
| `auth_middleware.py` | Bearer-token auth + default-deny CORS for HTTP routes |
| `tracing.py` | OpenTelemetry span helpers (`@trace_span` decorator) |
| `metrics.py` | Prometheus counter/gauge/histogram registry (all metrics defined here) |
| `log_config.py` | Structured JSON logging + `RequestLoggingMiddleware` |
| `restoration.py` | checkpoint / restore / anchor logic |
| `wiki.py` | Wiki page CRUD, draft/approve workflow |
| `config.py` | Pydantic settings (env vars → YAML → defaults) |
| `config_yaml.py` | `yadgar config` CLI subcommands, FIELD_META documentation |
| `config_registry.py` | I25 three-way-sync registry (code ↔ FIELD_META ↔ registry) |
| `daemon.py` | systemd-style daemon start/stop/status, MCP transport switching |
| `seed.py` | One-shot project bootstrap (`yadgar seed <directory>`) |
| `viz_server.py` | Knowledge graph visualisation server |
| `graph_api.py` | REST API for knowledge graph visualization |
| `astrocyte_pool.py` | Domain-partitioned async worker pool |
| `cognitive_map.py` | Spatial/topological memory organisation |
| `engram.py` | Engram slot model (excitability, plasticity, stability) |
| `predictive_coding.py` | Prediction error signal for surprise gating |
| `prospective.py` | Forward-looking memory (plans, intentions) |
| `staleness.py` | File-hash-based staleness detection for code memories |
| `remote_embeddings.py` | HTTP embedding service for Docker deployments |
| `backend/` | Backend-only modules moved here from top-level `yadgar/` in v5.60 (subpackage: `cache.py`, `ml_client.py`, `embed_service.py`, `embed_service_metrics.py`) |
| `backup.py` | Database backup/restore utilities |
| `blocks_render.py` | Memory block rendering for context injection (v5.33.0) |
| `conflict_resolver.py` | Memory conflict detection + resolution gate |
| `exception_telemetry.py` | Exception capture + structured error reporting |
| `install_hooks_lib.py` | Hook installation logic (library, invoked by CLI) |
| `ops.py` | Low-level SurrealDB operation primitives |
| `models.py` | Shared Pydantic data models |

## Storage Schema

SurrealDB tables:

| Table | Contents |
|---|---|
| `memory` | Core memory records: content, embedding, heat, confidence, tags, `directory_context: string NOT NULL` (v5.42.5 migration 016), `branch: option<string>` (caller-supplied via `branch_hint` or explicit `branch`; v5.42.3 hard-rejects missing) |
| `episodes` | Raw tool-action log chunks before consolidation |
| `entities` | Extracted code/file/concept entities with heat |
| `relationships` | Edges between entities (co_occurrence, causal, etc.) |
| `wiki_page` | User-approved wiki pages (markdown), `directory_context: string NOT NULL` (v5.42.5 migration 016, backfilled v5.42.6 migration 018), `branch: option<string>` (caller-supplied) |
| `wiki_page_version` | Immutable version snapshots: full content + change_summary per version (v5.41.0, migration 013). Powers `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_append_section`. |
| `wiki_draft` | Pending drafts awaiting approval. `branch: option<string>` (v5.42.3 migration 015), `directory_context: option<string>` (v5.42.5 migration 016). |
| `wiki_bookmark` | Ordered bookmark entries for wiki pages; powers `wiki_bookmark_add/list/remove/reorder` MCP tools (v5.23.0). |
| `memory_block` | Named scope-bounded text containers (v5.33.0, migration 012). Two scopes: `project` (per-directory) + `global`. Always injected on `restore()`. |
| `memory_archive` | Archived memory records with `original_memory_id` back-reference; written by thermodynamics when heat drops below archive threshold. Retention policy expanded in planned v5.49.0. |
| `memory_similarity_link` | Near-duplicate link graph between memories; written during duplicate-merge consolidation pass. |
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

On non-Docker hosts, `yadgar-vacuum.service` (systemd oneshot) runs `yadgar vacuum --service-mode=systemd` on a weekly timer (`yadgar-vacuum.timer`). It is not a Docker service — it runs on the host and connects to the MCP daemon over HTTP.

Configuration can be injected via environment variables (`YADGAR_*`) without rebuilding the image, or by mounting a `config.yaml` at `/root/.yadgar/config.yaml`.

## Branch + Directory Contract (v5.0 → v5.42.x)

**Schema:**
- Every `memory`, `wiki_page`, and `wiki_draft` row carries `directory_context: string NOT NULL` (v5.42.5 migration 016) — either an absolute project path or the literal `"global"`.
- `branch: option<string>` (NULL-able) — non-NULL = branch-scoped fact; NULL = canonical (branch-invariant).
- Three semantic categories: **project-canonical** (`directory=path, branch=NULL`), **project-branch-scoped** (`directory=path, branch=name`), **global** (`directory="global", branch=NULL`).
- Pre-v5 rows backfill to `'master'` in transactional migration #004. v5.42.6 migration 018 backfills `directory_context` via tag-based heuristic for rows pre-v5.42.5.

**Caller contract (post v5.42.3 / v5.42.5):**
- Writers (`memorize`, `anchor`, `checkpoint`, `update_active_work`, `wiki_add`) MUST supply `branch` (or `branch_hint`) AND `directory` — hard-reject at MCP boundary with `{"error": "missing_branch"}` / `{"error": "missing_directory"}` otherwise.
- Drainer re-validates (defense-in-depth) → DLQ with `failure_reason=missing_branch` or `failure_reason=missing_directory` if missing.
- Both layers gated by `YADGAR_BRANCH_ENFORCEMENT` and `YADGAR_DIRECTORY_ENFORCEMENT` env knobs (v5.42.6, default ON). When OFF: writes proceed with `branch=None`/`directory="global"`, WARN logged, `yadgar_writes_with_enforcement_relaxed_total` incremented. Knobs are debug-only — production keeps them ON.
- Daemon CWD ≠ caller CWD. `_detect_branch(os.getcwd())` returns the daemon container's branch. Callers MUST pass `branch_hint` (v5.1.9 + v5.42.3/v5.42.6 symmetric expansion) since SessionStart hooks know the host's branch.

**Resolution (§25, 4-step post-v5.42.5):**
- `wiki_read(slug)` resolves via: (1) `directory=caller_dir AND branch=current_branch`; (2) `directory=caller_dir AND branch IS NULL`; (3) `directory='global' AND branch IS NULL`; (4) not found → error dict.
- When no `directory` is supplied, falls back to legacy 3-step branch-only resolution (WARNING logged).
- `branch_hint` parameter (v5.42.3 `wiki_add` → v5.42.6 `wiki_read` → v5.43.0 `recall` + `wiki_query`) supplies caller branch when daemon-side detection returns None.

**Retrieval filter + boost:**
- Default branch resolved via `git symbolic-ref refs/remotes/origin/HEAD` (5-minute LRU cache).
- `recall(directory, branch_hint)` (v5.43.0) filters `branch IN (current, default, NULL)` post-fetch + boosts current-branch results via convex combination (`score + (1 - score) * BRANCH_BOOST_WEIGHT`, default 0.2, v5.1.0; replaced earlier hard 1.5× multiplier). Branch detection: `_detect_branch(directory or os.getcwd())` → `branch_hint` → `None`.
- `wiki_query(directory, branch_hint)` (v5.43.0) filters identically but still applies the legacy **1.5× hard multiplier** to current-branch results (`server/tools/wiki.py:584`) — not the convex combination used by `recall`. Same branch detection resolution as `recall`. Discrepancy on multiplier unchanged since v5.1.
- Non-git directories degenerate to `branch IN (default, NULL)` with no boost.

**Lifecycle:**
- `wiki_cleanup_merged_branches(directory, dry_run)` removes wiki pages whose branch is gone (e.g., post-merge).
- See `[[yadgar-directory-branch-contract-v5-42-3-5-architecture]]` wiki page for the full semantic model.

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

v5.31.0 added a parallel **plugin pipeline** (`retrieval/pipeline.py::RetrievalPipeline`) that wraps each stage as a `RetrievalStage` object under `retrieval/stages/` (12 stage files: `fts`, `knn`, `ppr`, `spreading`, `temporal`, `fusion`, `ce_rerank`, `nli`, `mmr`, `adversarial`, `rules`, `query_analysis`). Activated when `recall(profile=...)` kwarg is set (`"fast"` / `"balanced"` / `"full"` / `"debug"`). Per-stage telemetry via `yadgar_recall_stage_duration_seconds` + `yadgar_recall_stage_candidates_in/out`. The monolithic 8-stage path remains the default for callers that do not supply `profile`.

## Security (v5.0)

Bearer-token middleware (`yadgar/auth_middleware.py`) wraps `/api/*`, `/hooks/*`, and `/mcp` routes. `/health` and `/metrics` are exempt on loopback. Token comparison uses `hmac.compare_digest` for timing-safety.

Default-deny CORS — loopback origins only (`http://127.0.0.1:*`, `http://localhost:*`) unless `YADGAR_ALLOWED_ORIGINS` overrides.

`install_hooks` ships as a real Python script at `yadgar/scripts/hook_runner.py`; the path goes through `shlex.quote` before insertion into `settings.json`, eliminating the shell-injection vector.

`yadgar/sanitize.py` strips ANSI escapes, C0/C1 control chars, and Unicode bidi-override characters (U+200B–U+200F, U+202A–U+202E, U+2066–U+2069, U+FEFF) from auto-capture payloads before action-log insert. Per-source token-bucket rate limiter (`yadgar/rate_limit.py`) bounded to 1000 keys via `OrderedDict`.

Secret patterns block storage of AWS, GCP service-account JSON, Stripe (`sk_live_`), Slack (`xoxb-`/`xoxa-`/`xoxp-`), OpenAI (legacy + `sk-proj-`), Anthropic, JWT, GitHub PATs, private keys, DB connection URIs. Always-on, not user-configurable.

`yadgar/security/allowlist.py` (v5.13.0) provides a tag-based + pattern-based allowlist bypass for the secret gate, reducing false positives on test fixtures, plan documents, and CHANGELOG-style content. Configured via `YADGAR_SECRET_GATE_ALLOWLIST_PATH` (YAML). Every bypass hit is appended to `YADGAR_SECRET_GATE_AUDIT_DIR/<date>.jsonl` for auditability. `secrets.py` accepts `tags=` and `source=` kwargs; allowlist check runs BEFORE pattern scan.

## Observability (v5.0)

`/metrics` Prometheus endpoint (gated by `YADGAR_METRICS_ENABLED`, loopback-only by bind). Collectors: consolidation phase durations, queue depths (`queue/` `archive/` `dlq/`), DB query p50/p95, embedding cache hit ratio, request counts by route, action-batch size, `wiki_add` rejection rate by reason (`yadgar_wiki_add_rejected_total`, labels: `reason={duplicate_detected, policy_rejected}`), DLQ taxonomy gauge (`yadgar_dlq_rejection_count`), enforcement-relaxed write counter (`yadgar_writes_with_enforcement_relaxed_total`, v5.42.6), per-stage recall pipeline duration + candidate count histograms (`yadgar_recall_stage_duration_seconds`, `yadgar_recall_stage_candidates_in/out`, v5.31.0).

Structured JSON logs via `YADGAR_LOG_FORMAT=json`. `RequestLoggingMiddleware` (in `yadgar/log_config.py`) emits one INFO line per request with `request_id`, `tool_name`, `duration_ms`, `status`, `trace_id` (from `x-request-id` header).

Consolidation phase markers use `phase_start: <name>` / `phase_end: <name> duration_ms=N`. Wiki snapshot loop in `entrypoint-backend.sh` writes `/data/wiki_*.jsonl` every 6 hours with 14-day retention.

## Secret Gate Allowlist (v5.13.0)

`yadgar/security/allowlist.py` provides a tag-based + pattern-based bypass for the secret gate to reduce false positives on test fixtures, plan documents, and CHANGELOG-style content. Without this layer, every `wiki_add` containing example tokens (`ghp_…`, `sk-proj-…`, etc.) was blocked.

- **Allowlist source:** YAML at `YADGAR_SECRET_GATE_ALLOWLIST_PATH`. Each entry: `tag` (matches if write tagged with it) OR `regex` (matches against content).
- **Audit:** every bypass appended to `YADGAR_SECRET_GATE_AUDIT_DIR/<date>.jsonl` with caller_agent + source + matched_rule + content_hash. Powers I28 invariant (every allowlist hit must have audit entry) enforced by pre-commit hook.
- **Order:** allowlist check runs BEFORE pattern scan. Matched → bypass + audit + proceed. Unmatched → fall through to pattern scan.
- **Source detection:** `inspect.stack()` heuristic identifies caller — distinguishes "user wrote a fixture" from "memorize captured a real prod log line."

## Retrieval Profiles (v5.31.0)

`retrieval/pipeline.py::RetrievalPipeline` orchestrates `RetrievalStage` plugin instances under `retrieval/stages/` (12 stage files). Each stage exposes a `run(candidates, ctx) → candidates` interface. Pipeline composition + per-stage telemetry enable A/B routing without touching `recall()`.

- **Profiles** (callers select via `recall(profile=...)`):
  - `"fast"` — FTS + KNN + WRRF; skips PPR, spreading, reranking, NLI. Lowest latency.
  - `"balanced"` — Default-equivalent; adds PPR + reranker.
  - `"full"` — All 12 stages including NLI + adversarial.
  - `"debug"` — Full + per-stage candidate dump for analysis.
- **Per-stage overrides:** `recall(stage_overrides={"nli": {"enabled": False}})` disables a single stage without changing profile. Used for benchmarks and incident bypass.
- **Metrics per stage:** `yadgar_recall_stage_duration_seconds`, `yadgar_recall_stage_candidates_in/out`. Surfaces hot spots.
- **Backward compat:** callers without `profile=` route through the legacy monolithic `recall.py` 8-stage path (unchanged since v5.0).

## In-Context Memory Blocks (v5.33.0)

`memory_block` table (migration 012) stores named char-capped text containers always injected into `restore()`. Two scope categories:

- **`project` scope** — keyed by directory; visible only to that project.
- **`global` scope** — visible across all projects.

**MCP tools** (`block_create`, `block_get`, `block_update`, `block_delete`, `block_list`): per-block `max_chars` enforced server-side; total `MEMORY_BLOCKS_TOTAL_BUDGET_CHARS` ceiling reserved at restore time. Block content rendered by `blocks_render.py`. Use cases: persistent task lists, decision logs, semi-permanent context that should never decay.

v5.42.5 F3 enforces: `block_create(scope="project", directory=None)` rejected at MCP boundary (`blocks.py:33` — directory required when scope is project).

## Similarity Gate (v5.39.0, drainer-deferred v5.41.5)

`wiki_add` calls `find_similar_wiki_pages` before commit to catch near-duplicate writes. Threshold calibrated via real-embedding measurement (`WIKI_SIM_CONTENT_THRESHOLD` default `0.80`; near-dup cluster scores 0.956-0.993; distinct content 0.439-0.713; separation margin 0.24).

- **Async (v5.41.5 default):** `wait=False` returns immediately with `{"queued": True, "similarity_check": "deferred"}`. Drainer pre-apply runs the gate; rejections route to DLQ with `failure_reason=duplicate_detected` (not stored). Restores I9 ≤5ms p50 on the request path.
- **Sync (opt-in):** `wait=True` blocks until drainer completes. Gate fires → `{"stored": False, "reason": "duplicate_detected", "candidates": [...]}` returned to caller. Bypass via `force=True` (skip gate) or `replace_slug=...` (overwrite existing).
- **Auto-detect on read:** `wiki_check_duplicate` (dry-run) auto-detects caller branch via `_get_default_branch(cwd)` (v5.42.2 fix). Scope becomes `{None, default_branch}` so both new canonical pages AND pre-v5.42.2 legacy `branch="master"` pages are considered.

## Wiki Versioning (v5.41.0)

`wiki_page_version` table (migration 013) holds immutable per-write snapshots:

- **Trigger:** every successful `wiki_add` / `wiki_update` / `wiki_restore` / `wiki_append_section` inserts a `wiki_page_version` row in the same compound `BEGIN; CREATE wiki_page; CREATE wiki_page_version; COMMIT` transaction (v5.41.1 atomicity).
- **Auto change_summary:** `"+N -M lines | sections: ... | size: X → Y"` computed at write time.
- **Snapshot fields:** full content (immutable), `version_number`, `change_summary`, `created_at`, `parent_page_id`.

**MCP tools** (5 total): `wiki_history(slug)` returns version chain; `wiki_read_version(slug, version)` reads a specific snapshot; `wiki_diff(slug, v1, v2)` produces unified diff; `wiki_restore(slug, version)` reverts to a snapshot (creates new version); `wiki_append_section(slug, section_heading, content, position)` section-atomic write that prevents the 2026-05-31 corruption pattern (short-snippet overwrites).

`wait` flag (v5.41.2): both async (default) + sync paths supported; sync uses `wait_for_job` queue future.

## DLQ Taxonomy (v5.42.0 + extensions)

Dead-letter queue holds writes that failed validation or exhausted retries. Located at `~/.yadgar/queue/dlq/`. Each entry carries:

- `op_type` — `wiki_add`, `memorize`, etc.
- `attempts` — retry count
- `classification` — `transient` / `permanent` / `rejection`
- `failure_reason` — taxonomy value (below)
- `last_error` — string error message
- `moved_to_dlq_at` — timestamp

**`failure_reason` values:**
- `permanent_error` (or absent) — unrecoverable error during apply
- `duplicate_detected` — similarity gate fired (v5.39 / v5.41.5 drainer-deferred)
- `policy_rejected` — rules engine rejected
- `missing_branch` — drainer enforced branch contract (v5.42.3)
- `missing_directory` — drainer enforced directory contract (v5.42.5)

**MCP tools:** `dlq_inspect(filter)` lists entries (filter: `all`/`rejections`/`failures`); `dlq_requeue(filename, force)` re-sends through queue (blocks on `missing_branch` / `missing_directory` unless `force=True`); `dlq_dismiss(filename)` acknowledges + drops.

**Metric:** `yadgar_dlq_rejection_count` gauge — current count of rejection-taxonomy entries.

## Directory Contract (v5.42.5)

Detailed semantics for `directory_context` field — companion to the Branch + Directory Contract section above.

**Required value:** absolute project path (e.g., `/home/max/git/yadgar`) OR literal `"global"`. NEVER empty / NULL / undefined.

**Three semantic categories** (combining `directory` + `branch`):

| Category | `directory` | `branch` | Use case |
|---|---|---|---|
| project-canonical | `"/path/to/proj"` | `NULL` | Project fact valid across all branches (architecture, conventions, infra) |
| project-branch-scoped | `"/path/to/proj"` | `"feat/x"` | WIP/exploratory work; GC'd on merge via `wiki_cleanup_merged_branches` |
| global | `"global"` | `NULL` | Cross-project knowledge (LLM API references, shared conventions) |

The fourth combination (`directory="global", branch=non-NULL`) is intentionally undefined — global facts are branch-invariant.

**Enforcement layers** (both gated by `YADGAR_DIRECTORY_ENFORCEMENT` env knob):

1. MCP boundary — Pydantic validator rejects empty/None/whitespace `directory` parameter on every write tool. Error response: `{"error": "missing_directory", "stored": false, "message": "...", "field": "directory"}`.
2. Drainer pre-apply — re-validates queued payloads (defense-in-depth for direct-file_queue writes). Missing → DLQ with `failure_reason=missing_directory`.

**Knob behavior:** `YADGAR_DIRECTORY_ENFORCEMENT=true` (default) — strict. `false` — accept missing, fall back to `"global"`, WARN log, `yadgar_writes_with_enforcement_relaxed_total{enforcement="directory"}` increment. Knob is for debug/old-schema recovery only.

**Carve-out:** payload field `_internal=True` bypasses validation for system writes (migrations, wiki_approve internals, consolidation `_try_store_action_summary`, hook callbacks). Tagged in code with `# _internal-only` comment.

**Migration history:**
- 016 (v5.42.5): added `directory_context` NOT NULL to `wiki_page` / `memory` / `wiki_draft` with tag-based heuristic backfill.
- 018 (v5.42.6): repair for 016 — Python-side filter caught field-absent rows that `WHERE directory_context IS NONE` SurrealDB query missed. Backfilled the remaining 200 legacy rows.

See `[[yadgar-directory-branch-contract-v5-42-3-5-architecture]]` wiki page for the full semantic model + decision history.
