# `yadgar/backend/` — the compute layer

Ships in the `yadgar-backend` image (7 CPUs, next to the DB, ML models
loaded). Owns COMPUTE: embedding/rerank/NLI serving, consolidation + sleep
cycle, write-path execution, restore/cognitive-map compute (ADR-0078).
See `AGENTS.md` here for the placement rules.

## Packages

| Package | What it is |
|---|---|
| `embed_service/` | the FastAPI service: `/embed`, `/rerank`, `/recall`, `/restore`, `/consolidate`, `/admin` + its Prometheus collectors |
| `retrieval/` | the recall pipeline behind `POST /recall`: FTS + KNN + PPR + spreading → WRRF fusion → CE rerank → NLI → MMR → adversarial → rules |
| `graph/` | galaxy layout precompute (networkx `spring_layout`) behind `/api/graph` |
| `viz_exec/` | backend halves of the viz API reads |
| `ml_client/` | Local/Remote ML clients + circuit breaker |
| `cache/` | backend LRU caches, namespace budgets, scope-version invalidation |
| `consolidation/` | the nightly brain cycle: decay, CLS, causal, cleanup, cold retention |
| `sleep_compute/` | dream replay, community detection, embed compression |
| `curation/` | ingestion, merge/prune passes, strengthen |
| `cls_store/` | complementary-learning-system store: clustering, patterns, promotion |
| `causal_discovery/` | PC-algorithm causal edge discovery |
| `queue_drainer/` | drains the `_shared/file_queue` writes into the DB (+ DLQ taxonomy) |
| `write_exec/` | memorize phases: embed → store → post-write |
| `admin_exec/` | backend halves of admin tools (memory, wiki, blocks, bookmarks, restoration) **plus the engine-#2 ledger ops.** Ledger task 402 split those by table family: `ledger.py` (task + ADR), `ledger_agent.py` (`agent_pattern` / `agent_discipline`), `ledger_project.py` (the project registry). Each carries its own `_get_sql_storage` seam — a test patching one does NOT cover the others |
| `restoration/` | CheckpointRestore + CognitiveMap compute behind `POST /restore` |
| `predictive_coding/` | WriteGate surprise scoring |
| `narrative/`, `conflict_resolver/`, `prospective/`, `safe_start/` | auto-narration, contradiction resolution, trigger memories, split-brain start guard |

## How it connects

Core reaches backend ONLY over HTTP (embed_service endpoints) or via the
file queue; backend never imports `yadgar.core.*` (import-linter contract 2).
Service-contract changes bump `BACKEND_VERSION` in `yadgar/__init__.py`.
