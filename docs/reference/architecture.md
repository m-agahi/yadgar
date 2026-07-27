# Yadgar Architecture

Yadgar is a persistent memory engine for Claude Code. It stores, decays, and retrieves memories across sessions so the model accumulates contextual knowledge over time rather than starting fresh each conversation.

## System Overview

```
Claude Code (MCP client)
        │
        │  MCP protocol (streamable-HTTP default / stdio fallback)
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    yadgar/core (:8765)                               │
│  MCP tool handlers (thin routers) / auth / rules / hooks / viz      │
│  memorize → file queue   recall → POST /recall   wiki_* → queue     │
└────────┬───────────────────────────────────────────┬────────────────┘
         │                                           │
         │  file queue (yadgar-queue-data volume)    │ HTTP (YADGAR_EMBED_URL)
         ▼                                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    yadgar/_shared                                    │
│  config (pydantic-settings) / storage contracts / observability      │
│  @observe decorator / security gate / file_queue client             │
└────────┬────────────────────────────────────────────────────────────┘
         │ HTTP (YADGAR_DB_URL / YADGAR_EMBED_URL)
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    yadgar/backend                                    │
│  SurrealDB store (:8000)   embed + rerank service (:8001)           │
│  full retrieval pipeline (POST /recall)                             │
│  queue drainer + consolidation compute                              │
│  ML models baked into image (ADR-0101)                              │
└─────────────────────────────────────────────────────────────────────┘
```

**Three physical layers (ADR-0056 / ADR-0060 / ADR-0062 / ADR-0063; import-linter enforced):**

- **yadgar/core** — MCP server (FastAPI). Thin router: forwards recall to backend via `POST /recall` (ADR-0044); queues writes to the file queue; hosts viz web UI routing layer. No retrieval compute and no direct DB calls here (ADR-0078).
- **yadgar/_shared** — contracts, config (pydantic-settings), storage client protocol, `@observe` decorator, security gate, file_queue client. Visible to both core and backend; backend must not import core.
- **yadgar/backend** — SurrealDB (`:8000`) + embed/rerank service (`:8001`). Owns the full retrieval pipeline, the async write drainer, consolidation compute, and admin endpoints. ML models baked into the Docker image (ADR-0101, not runtime-downloaded).

**Transport:** streamable-HTTP is the default deployed transport; stdio is supported for single-session / no-Docker use. `yadgar daemon configure-mcp` wires Claude Code to the streamable-HTTP server with bearer auth.

## Data Flow

### Write path (`memorize` / `wiki_add`)

1. **Secret scrub** — content checked against credential patterns (AWS, JWT, etc.)
2. **MCP boundary contract validation** — `branch` (or `branch_hint`) and `directory_context` required; missing → hard-reject (`{"error": "missing_branch"}` / `{"error": "missing_directory"}`). Gated by `YADGAR_BRANCH_ENFORCEMENT` / `YADGAR_DIRECTORY_ENFORCEMENT` (default ON).
3. **Rules engine** — custom write-block rules evaluated
4. **File queue enqueue (ADR-0075)** — payload written to `YADGAR_QUEUE_BASE=/data/queue` (shared `yadgar-queue-data` Docker volume). Both core and backend mount this volume.
5. **Drainer enforcement (defense-in-depth)** — backend drainer re-validates branch + directory; missing → DLQ with `failure_reason=missing_branch` or `missing_directory`.
6. **Similarity gate (wiki_add only, drainer-deferred)** — near-duplicate detected → DLQ with `failure_reason=duplicate_detected`. `wait=True` callers receive sync rejection; `wait=False` get `{"queued": True, "similarity_check": "deferred"}`.
7. **Write gate (memorize only)** — similarity scored against recent memories
8. **Embedding** — backend embed service encodes content; cached
9. **Storage** — backend inserts into SurrealDB with `heat=1.0`, tags, `directory_context`, `branch`
10. **Versioning snapshot (wiki_add only)** — `wiki_page_version` row inserted in same compound transaction

### Read path (`recall` / `wiki_read` / `wiki_query`)

**`recall()` is a thin forwarder in core → backend `POST /recall` runs the full pipeline:**

1. **Query routing** — classifies query as temporal, code, relational, comparison, or open-domain
2. **Query expansion** — pseudo-HyDE generates synthetic answer for embedding (optional)
3. **Candidate retrieval** — four parallel signals in backend:
   - Vector cosine search (ANN)
   - BM25 full-text search
   - Personalized PageRank on knowledge graph
   - Spreading activation from seed entities
4. **WRRF fusion** — Weighted Reciprocal Rank Fusion blends signal lists
5. **Confidence gate** — low-confidence result sets trigger fallback strategy
6. **Reranking** — CE reranker scores top-K pairs. Primary = **Ettin-32m** (`cross-encoder/ettin-reranker-32m-v1`, ADR-0104); GTE-ModernBERT is the config-revert rollback. ONNX backends removed (ADR-0043/0067).
7. **NLI entailment** — optional DeBERTa entailment signal blended in
8. **MMR diversity** — Maximal Marginal Relevance diversification
9. **Adversarial filter + rules** — score-gap + diversity checks; rules engine pass

**Latency (ADR-0105, corrected):** CE is ~25% of the cold recall wall (not the "~90%" once quoted — that was a dead-metric artifact from the split-container era). Signal-gather (KNN/FTS/PPR/spreading) is the ~45% dominator. Ettin-32m (Train 4) cut CE per-pass ~4.7× vs GTE-ModernBERT → 2.44× end-to-end speedup.

`recall()` accepts `profile` (`"fast"` / `"balanced"` / `"full"` / `"debug"`) + `stage_overrides` (v5.31.0), plus `directory` and `branch_hint` (v5.43.0).

### Consolidation path (background daemon)

`consolidation/orchestrator.py` forwards compute to backend `/consolidate`, then runs core-side post-cycle tasks. Pipeline phases:

1. **Decay** — heat reduced per-memory with modifiers for importance, emotional valence, confidence
2. **Episode processing** — new episodes parsed; co-occurring entities get `co_occurrence` edges
3. **Duplicate merging** — pairs with similarity > `CURATION_SIMILARITY_THRESHOLD` merged
4. **Link similar** — near-duplicate links added to knowledge graph
5. **Causal detection** — causal edge inference from co-occurrence patterns
6. **Memify** — `_memify_prune` self-improvement cycle
7. **CLS consolidation** — episodic patterns promoted to semantic memory
8. **Action log** — unprocessed action-log entries summarised into real memories
9. **Galaxy layout precompute** — 3D node positions computed, signature-cached, served by `/api/graph`
10. **Consolidation log** — timestamped run record inserted

Dream replay runs separately in `_maybe_sleep_cycle()` — triggered at most once every 6 hours after the daily consolidation cycle.

### Nightly cycle (v5.72)

Full end-to-end nightly lifecycle (in-process; no MCP reconnect required):

1. **Maintenance enter** — flips in-process maintenance flag (`POST /api/control/maintenance/enter`). DB-backed MCP tools fast-fail with `{"error": "maintenance"}`.
2. **Pre-backup export** — HTTP export of DB snapshot.
3. **Consolidation** — full cycle over HTTP/server mode.
4. **Dream/sleep cycle** (`_maybe_sleep_cycle`) — dream replay + `reembed_stale` + `auto_narrate`. All run without dropping the MCP connection.
5. **Atomic vacuum** — `vacuum_now()`.
6. **Post-backup export** — second HTTP snapshot after vacuum.
7. **Maintenance exit** — `POST /api/control/maintenance/exit`; daemon resumes.

## Module Responsibilities

> Note: The table below reflects the **three-layer split** (ADR-0056/0060/0062/0063). Modules previously living at the package root (`yadgar/*.py`) are now distributed across `yadgar/core/`, `yadgar/_shared/`, and `yadgar/backend/`. Single-file names below indicate the containing module path.

### yadgar/core (MCP server — router only, no compute)

| Module | Responsibility |
|---|---|
| `server/` | MCP tool handlers (thin routers), session management, action-stream capture (subpackage: `tools/`, `middleware/`, `transport/`) |
| `server/tools/adr.py` | `adr_add` / `adr_get` / `adr_list` — per-ADR wiki pages + thin index |
| `server/tools/wiki.py` | Wiki CRUD + `wiki_write_task_list` (harness task-list mirror, canonical write) |
| `server/tools/db_inspect.py` | `db_inspect` — read-only SurrealQL SELECT, forwarded to backend (ADR-0132) |
| `server/tools/agent_prompts.py` | `agent_prompt_save` / `agent_dispatch_prelude` / `seed_agent_prompts` |
| `server/tools/dispatch_helper.py` | `agent_dispatch_prelude` implementation |
| `cli/` | All `yadgar <subcommand>` CLI entry points |
| `hooks/` | Claude Code hook runner scripts (SessionStart, SubagentStop, PreCompact) |
| `install/` | Hook installation logic (`install_hooks_lib.py`) |
| `daemon/` | systemd-style daemon start/stop/status, MCP transport switching |
| `lifecycle/` | Daemon lifecycle management |
| `auth_middleware/` | Bearer-token auth + default-deny CORS for HTTP routes |
| `code_graph/` | multi-language code-structure digest via codebase-memory-mcp shell-out (successor to the retired repo_wiki AST scanner, ADR-0162) |
| `bootstrap/` | `seed_project`, `bootstrap_project` |
| `restoration/` | checkpoint / restore / anchor logic (core-side; compute forwarded to backend) |
| `export/` | DuckDB exporter for offline analytics (v5.27.0) |

### yadgar/_shared (contracts + config + observability — imported by both core and backend)

| Module | Responsibility |
|---|---|
| `config/` | Pydantic settings (env vars → YAML → defaults); `FIELD_META` registry; three-way-sync |
| `storage/` | SurrealDB client + schema contracts: `ops.py`, `client.py`, `schema.py`, `migrations.py`, `wiki.py`, `memory.py`, `directory.py` |
| `observability/` | `@observe` decorator (tri-signal: span+metric+log); `log_config.py`; `tracing.py`; `metrics.py` |
| `security/` | Secret-gate patterns; `allowlist.py` (tag + pattern bypass, audit log) |
| `sanitize/` | ANSI / C0/C1 / bidi-override scrubbing for auto-capture payloads |
| `rate_limit/` | Per-source token-bucket rate limiter (1000-key OrderedDict) |
| `file_queue/` | Async write queue client + DLQ (similarity gate, branch/directory enforcement, DLQ taxonomy) |
| `contracts/` | Protocol/DI interfaces separating layers |
| `embeddings/` | Sentence-transformer wrapper, LRU embedding cache (local; backend has the remote path) |
| `enrichment/` | Index-time text enrichment (subpackage: `conceptnet.py`, `doc2query.py`; COMET dormant ADR-0004) |
| `rules_engine/` | Write-block and write-allow rules evaluation |
| `knowledge_graph/` | Entity extraction from episodes, relationship edges |
| `thermodynamics.py` | Heat decay formula with importance/valence/confidence modifiers |
| `engram.py` | Engram slot model (excitability, plasticity, stability) |
| `astrocyte_pool.py` | Domain-partitioned async worker pool |
| `cognitive_map.py` | Spatial/topological memory organisation |
| `sensory_buffer.py` | Buffer for incoming tool-action events (action stream) |
| `metacognition/` | Self-monitoring, memory quality scoring |
| `staleness.py` | File-hash-based staleness detection for code memories |
| `conflict_resolver/` | Memory conflict detection + resolution gate |
| `predictive_coding/` | Prediction error signal for surprise gating |
| `prospective/` | Forward-looking memory (plans, intentions) |
| `narrative.py` | Autobiographical story from recent memories |
| `blocks_render.py` | Memory block rendering for context injection (v5.33.0) |
| `models.py` | Shared Pydantic data models |

### yadgar/backend (compute — all heavy work lives here)

| Module | Responsibility |
|---|---|
| `retrieval/` | Full recall pipeline: FTS + KNN + PPR + spreading activation → WRRF → CE rerank → NLI → MMR → adversarial → rules. POST /recall endpoint. |
| `consolidation/` | Background daemon loop: `orchestrator.py`, `heat_decay.py`, `cls.py`, `causal.py`, `cleanup.py`. Nightly cycle forwarded here. |
| `curation/` | Duplicate detection, merge, `_memify_prune` |
| `cls_store/` | Complementary Learning Systems: episodic → semantic |
| `causal_discovery/` | Causal edge inference (Meek R1/R2/R3 PC algorithm) |
| `sleep_compute/` | Dream replay, narrative trigger |
| `embed_service/` | Sentence-transformer embed endpoint (`:8001`); CE reranker (Ettin-32m primary) |
| `ml_client/` | LocalMLClient / RemoteMLClient for embed + rerank |
| `cache/` | Unified Cache (N named instances, ScopeVersions invalidation, ADR-0048/0053) |
| `queue_drainer/` | Async drainer: dequeues file queue, runs similarity gate, commits to SurrealDB |
| `graph/` | Graph viz API (`/api/graph`) + galaxy layout precompute (server-side spring positions) |
| `admin_exec/` | Backend admin endpoints (consolidate, reembed, vacuum, read_query) |
| `restoration/` | Backend-side checkpoint / restore compute |

> **Removed modules (no longer exist):** The pre-split top-level files (`yadgar/server.py`, `yadgar/embeddings.py`, `yadgar/retrieval/`, `yadgar/storage/`, `yadgar/consolidation/`, etc.) were split across the three layers in ADR-0056–0130. The old `yadgar/backend/` subpackage holding only `cache.py`/`ml_client.py` is now the full backend layer. `tracing.py` / `metrics.py` / `log_config.py` / `auth_middleware.py` / `sanitize.py` / `rate_limit.py` moved to `yadgar/_shared/observability/` or `yadgar/_shared/security/`. The ONNX CE backend (`GTE_RERANKER_BACKEND=onnx-int8`) was removed (ADR-0043/0067).

## Storage Schema

SurrealDB tables:

| Table | Contents |
|---|---|
| `memory` | Core memory records: content, embedding, heat, confidence, tags, `directory_context: string NOT NULL`, `branch: option<string>` |
| `episodes` | Raw tool-action log chunks before consolidation |
| `entities` | Extracted code/file/concept entities with heat |
| `relationships` | Edges between entities (co_occurrence, causal, etc.) |
| `wiki_page` | Wiki pages (markdown), `directory_context: string NOT NULL`, `branch: option<string>`. Includes task-list pages (`page_type="task_list"`) and ADR pages (`page_type="adr"`) |
| `wiki_page_version` | Immutable version snapshots: full content + change_summary per version (v5.41.0) |
| `wiki_bookmark` | Ordered bookmark entries; powers `bookmark_*` MCP tools |
| `memory_block` | Named scope-bounded text containers (v5.33.0, migration 012). `project` + `global` scopes |
| `memory_archive` | Archived memory records with back-reference |
| `memory_similarity_link` | Near-duplicate link graph between memories |
| `checkpoints` | Saved working state snapshots |
| `profiles` | Structured user attribute records |
| `beliefs` | Higher-order derived beliefs |
| `consolidation_log` | Timestamped record of every consolidation run |
| `graph_layout_cache` | Server-side precomputed galaxy 3D node positions (signature-cached) |

## Transport Modes

- **streamable-HTTP** (`--transport streamable-http --port 8765`, **default for daemon**): Persistent daemon. One process serves all Claude sessions. Required for Docker. `yadgar daemon configure-mcp` writes `~/.claude.json` to use this transport with bearer auth.
- **stdio** (alternative): Claude Code spawns yadgar as a child process. Zero network, lowest latency. Useful for single-session / no-Docker use; no bearer auth. `server.json` declares `"transport": {"type": "stdio"}` — this is intentional PyPI / MCP registry manifest metadata for the stdio install path (pip install + registry discovery). The deployed daemon uses streamable-HTTP by default; stdio is a supported alternative, not the production default.
- **Nightly maintenance mode (v5.72):** Core daemon stays up during the nightly cycle — no MCP reconnect required. In-process maintenance flag fast-fails DB-backed MCP tools while consolidation, vacuum, backup, and dream/sleep steps run.

## Docker Deployment

The `docker-compose.yml` (recommended production path) runs yadgar as two containers on `yadgar-net`:

- **`yadgar-backend`** (`openfantasy/yadgar-backend:${BACKEND_VERSION:-5.55.0}`, `Dockerfile.backend`): SurrealDB + embed/rerank microservice. Exposes embed on `:8001` (loopback only). Mounts: `yadgar-db-data` (read-only DB), `yadgar-queue-data` at `/queue-data` (file queue, `YADGAR_QUEUE_BASE=/queue-data`), `yadgar-backend-logs` at `/data/logs`, and the host HuggingFace cache.
- **`yadgar-core`** (`openfantasy/yadgar:${CORE_VERSION:-5.149.0}`, `Dockerfile`): MCP server in streamable-HTTP mode. Exposes `:8765` (MCP). Mounts: `yadgar-queue-data` at `/data` (shared file queue). Connects to `yadgar-backend` via `YADGAR_DB_URL=http://yadgar-backend:8000` and `YADGAR_EMBED_URL=http://yadgar-backend:8001`. `depends_on: backend: condition: service_healthy`.

The knowledge-graph viz UI (`yadgar viz`, default port 42069) is **not** a compose service — it is a separate process launched on the host via `yadgar viz`. It reverse-proxies `/api/*` to the daemon at `:8765`.

**Volumes:** `yadgar-db-data` (SurrealDB) · `yadgar-dev-data` · `yadgar-queue-data` (shared file queue — both containers mount this) · `yadgar-backend-logs`.

Both containers ship Python 3.14; no host Python required. Source `~/.config/yadgar/secrets.env` before `docker compose up`.

On non-Docker hosts, `yadgar-vacuum.service` (systemd oneshot) runs `yadgar vacuum --service-mode=systemd` on a weekly timer. It runs on the host and connects to the MCP daemon over HTTP.

## Branch + Directory Contract (v5.42.x)

**Schema:**
- Every `memory` and `wiki_page` row carries `directory_context: string NOT NULL` — either an absolute project path or the literal `"global"`.
- `branch: option<string>` (NULL-able) — non-NULL = branch-scoped; NULL = canonical (branch-invariant).
- Three semantic categories: **project-canonical** (`directory=path, branch=NULL`), **project-branch-scoped** (`directory=path, branch=name`), **global** (`directory="global", branch=NULL`).

**Caller contract:**
- Writers MUST supply `branch` (or `branch_hint`) AND `directory` — hard-reject at MCP boundary otherwise.
- Drainer re-validates (defense-in-depth) → DLQ on missing.
- Both gated by `YADGAR_BRANCH_ENFORCEMENT` and `YADGAR_DIRECTORY_ENFORCEMENT` (default ON).
- `branch_hint` required since daemon CWD ≠ caller CWD (container scenario).

**Resolution (§25, 4-step):**
- `wiki_read(slug)` resolves via: (1) `directory=caller_dir AND branch=current_branch`; (2) `directory=caller_dir AND branch IS NULL`; (3) `directory='global' AND branch IS NULL`; (4) not found → error dict.

**Special case — canonical writes (`wiki_write_task_list`, ADR pages):** written with `branch=NULL` (canonical) so they resolve from any branch and from non-git projects.

## Knowledge-Graph Viz (galaxy, post-#52)

The viz server (`yadgar viz`, default `http://localhost:42069`) renders an interactive 3D **galaxy layout**. The force-directed / 2D engine is **removed** (ADR-0138) — galaxy is the sole renderer.

**Galaxy layout:** loose low-heat memories form the outer halo; recurring semantic clusters form spiral arms; core/anchor nodes form a central bulge. Heat encodes brightness. Draggable node popups, Fit/Reset camera.

**Features (v5.86 → current):**
- Filter by node type (memory / wiki / entity), heat slider, search.
- **Traces replay** — replay a Tempo trace from the UI.
- **In-browser config panel** (System → Config) — bearer-auth gated writes; SOURCE badges; category-grouped, alpha-sorted knobs.
- Four menus: **Graph** · **Bookmarks** · **System** {Config, Health, Stats} · **Help** {Guide, Config Reference, About, Debug}.
- CPU: render loop pauses on idle / tab-switch.
- **Precomputed layout:** nightly cycle computes 3D positions via backend `graph/` module (`yadgar/backend/graph/graph_layout.py`). Server-side layout uses `networkx.spring_layout` (networkx remains a dependency; `networkx>=3.0` in `pyproject.toml`). Galaxy layout is the sole UI renderer; networkx spring is the server-side fallback for non-galaxy graph precompute. Positions are signature-cached in `graph_layout_cache`; `/api/graph` serves precomputed coordinates. Client runs a cold layout on a cache miss.

## ADR System

Architecture Decision Records are stored as canonical wiki pages (`yadgar-adr-NNNN`) with a thin index page (`yadgar-adr-index`). `recall()` is the read path — pages are recall-visible (tagged `["adr"]`) and never decay (canonical, branch=NULL).

**MCP tools:** `adr_add` (⚡) appends 11-field schema ADR + updates index; `adr_get` (⚡) fetches one ADR by ID; `adr_list` (⚡) reads the index with optional status filter. Stop-hook captures decisions at session end.

**Yadgar dogfoods this system** — 138+ real ADRs at time of writing.

## Harness Task-List Mirror

`wiki_write_task_list(project, content, directory)` persists the Claude Code harness task list to the wiki store as a canonical page (`{project}-task-list`, `page_type="task_list"`, branch=NULL). The stop-hook checkpoint step (step 4) calls this; the SessionStart restore-nudge re-injects open tasks.

Why a dedicated tool: a raw `wiki_add` in a git directory hard-requires branch (missing → reject). This tool routes through the server-side `_wiki_write_canonical` path, bypassing the git-branch requirement while remaining structurally bounded to the task-list slug (ADR-0127/0133/0137).

## Agent-Prompt Library

Reusable subagent dispatch prompts stored as tagged wiki pages (`agent-prompt-<pattern>`, tagged `["agent-prompt"]`). Not visible in normal recall — only via `recall(type="wiki", tags=["agent-prompt"])`.

**MCP tools:** `agent_prompt_save(pattern, content, purpose)` upserts; `agent_dispatch_prelude(pattern, task_topic)` builds a prelude (recall-first contract + `## Yadgar findings` footer); `seed_agent_prompts()` seeds starters.

## Read-Only DB Inspection (`db_inspect`, ADR-0132)

`db_inspect(query, params, limit)` executes a SurrealQL SELECT via a read-only viewer client (`YADGAR_RO_PASS`). Core forwards to backend `/api/debug/read_query`. Gated by `YADGAR_DEBUG_APIS_ENABLED`. No writes possible from this surface.

## Test Infrastructure (v5.104, ADR-0036, ADR-0064)

- **Directory mirrors package** (ADR-0064): `yadgar/tests/` mirrors the three-layer structure (`tests/core/`, `tests/_shared/`, `tests/backend/`).
- **Module-scoped `storage` fixture** — `StorageEngine` inits schema ONCE per test file (~5.3× setup win).
- **Batched SurrealDB wipe** — teardown wipes all namespaces in one pass (~18× teardown win).
- **Backend harness** (ADR-0065) — shared `_backend_harness.py`, autouse in root conftest.
- Net: CI shards ~2× faster (11–22 min → 4:27–9:47).

## Security (v5.0)

Bearer-token middleware wraps `/api/*`, `/hooks/*`, and `/mcp` routes. `/health` and `/metrics` exempt on loopback. `hmac.compare_digest` for timing-safety.

Default-deny CORS — loopback origins only unless `YADGAR_ALLOWED_ORIGINS` overrides.

`install_hooks` ships as a real Python script at `yadgar/core/scripts/hook_runner.py`; path goes through `shlex.quote` before insertion into `settings.json`, eliminating shell-injection.

Secret patterns block AWS, GCP service-account JSON, Stripe, Slack, OpenAI (legacy + `sk-proj-`), Anthropic, JWT, GitHub PATs, private keys, DB connection URIs. Always-on, not user-configurable.

`yadgar/_shared/security/allowlist.py` (v5.13.0) provides tag-based + pattern-based allowlist bypass (audit log per bypass hit).

## Observability (v5.0)

`/metrics` Prometheus endpoint. Structured JSON logs via `YADGAR_LOG_FORMAT=json`. `RequestLoggingMiddleware` emits one INFO line per request with `request_id`, `tool_name`, `duration_ms`, `status`, `trace_id`.

Distributed tracing wraps the optional OTLP exporter in a circuit breaker (opens after 5 consecutive export failures for 60 s). Per-span `span_end` log lines emitted off the event-loop thread via `QueueHandler/QueueListener`. Core → backend W3C `traceparent` via `HTTPXClientInstrumentor` (core spans + backend spans join one trace in Tempo).

### Tri-signal Observability Standard (v5.101, ADR-0034)

Every in-scope function emits span + metric + log via the `@observe` decorator (lives in `yadgar/_shared/observability/observe.py`). I33 coverage lint (`scripts/check_observe_coverage.py`) is the CI ratchet. Export: `YADGAR_OTLP_ENDPOINT` → Tempo. Never set `OTEL_SDK_DISABLED` in tests (ADR-0037) — disable export via `YADGAR_OTLP_ENDPOINT=''` only.

## Retrieval Pipeline (v5.31.0 plugin architecture)

`recall()` core dispatches to backend `POST /recall`. Backend runs:

Monolithic 8-stage path (default when `profile` not supplied):
1. `_collect_fts_scores` — FTS BM25 + entity-FTS + doc2query expansion
2. `_collect_vector_scores` — KNN vector search
3. `_collect_ppr_scores` — Personalized PageRank
4. `_collect_spreading_scores` — spreading activation
5. `_collect_temporal_scores` — temporal signal
6. `_fuse_scores` — confidence gating + WRRF / convex fusion
7. `_build_initial_results` — assemble + CE diversity injection
8. `_apply_rerank_pipeline` — CE → NLI → multi-passage → MMR → adversarial → rules → engram links

Plugin pipeline (`retrieval/pipeline.py::RetrievalPipeline`): 12 `RetrievalStage` instances. Activated by `recall(profile=...)`:
- `"fast"` — FTS + KNN + WRRF; skips PPR, spreading, reranking, NLI.
- `"balanced"` — adds PPR + CE reranker.
- `"full"` — all 12 stages including NLI + adversarial.
- `"debug"` — full + per-stage candidate dump.

## Retrieval Profiles (v5.31.0)

Same as above — see Retrieval Pipeline section.

## In-Context Memory Blocks (v5.33.0)

`memory_block` table (migration 012): named char-capped text containers always injected into `restore()`. `project` scope (per-directory) and `global` scope. MCP tools: `block_create`, `block_get`, `block_update`, `block_delete`, `block_list`, `block_replace`, `block_append` (all ⚡).

## Similarity Gate (v5.41.5, drainer-deferred)

`wiki_add` calls `find_similar_wiki_pages` before commit (in drainer). Async default (`wait=False`): `{"queued": True, "similarity_check": "deferred"}`; rejections → DLQ `failure_reason=duplicate_detected`. Sync (`wait=True`): blocks, returns rejection payload. Bypass: `force=True` or `replace_slug=...`.

## DLQ Taxonomy (v5.42.0+)

`failure_reason` values: `permanent_error` · `duplicate_detected` · `policy_rejected` · `missing_branch` · `missing_directory`.

MCP tools: `dlq_inspect(filter)` · `dlq_requeue(id)` ⚡ · `dlq_dismiss(id)` ⚡. File queue location: `YADGAR_QUEUE_BASE/queue/` (default `/data/queue/`).

## Directory Contract (v5.42.5)

Three semantic categories: project-canonical (`directory=path, branch=NULL`) · project-branch-scoped · global (`directory="global", branch=NULL`). Enforcement at MCP boundary + drainer (defense-in-depth). See Branch + Directory Contract section above for full detail.
