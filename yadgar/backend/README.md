# `yadgar/backend/` — the compute layer

Ships in the `yadgar-backend` image (7 CPUs, next to the DB, ML models
loaded). Owns COMPUTE: embedding/rerank/NLI serving, consolidation + sleep
cycle, write-path execution, restore/cognitive-map compute (ADR-0078).
See `AGENTS.md` here for the placement rules.

## Packages

| Package | What it is |
|---|---|
| `embed_service/` | the FastAPI service: `/embed`, `/rerank`, `/recall`, `/restore`, `/consolidate`, `/admin` + its Prometheus collectors |
| `ml_client/` | Local/Remote ML clients + circuit breaker |
| `cache/` | backend LRU caches, namespace budgets, scope-version invalidation |
| `consolidation/` | the nightly brain cycle: decay, CLS, causal, cleanup, cold retention |
| `sleep_compute/` | dream replay, community detection, embed compression |
| `curation/` | ingestion, merge/prune passes, strengthen |
| `cls_store/` | complementary-learning-system store: clustering, patterns, promotion |
| `causal_discovery/` | PC-algorithm causal edge discovery |
| `queue_drainer/` | drains the `_shared/file_queue` writes into the DB (+ DLQ taxonomy) |
| `write_exec/` | memorize phases: embed → store → post-write |
| `admin_exec/` | backend halves of admin tools (memory, wiki, blocks, bookmarks, restoration) |
| `restoration/` | CheckpointRestore + CognitiveMap compute behind `POST /restore` |
| `predictive_coding/` | WriteGate surprise scoring |
| `narrative/`, `conflict_resolver/`, `prospective/`, `safe_start/` | auto-narration, contradiction resolution, trigger memories, split-brain start guard |

## How it connects

Core reaches backend ONLY over HTTP (embed_service endpoints) or via the
file queue; backend never imports `yadgar.core.*` (import-linter contract 2).
Service-contract changes bump `BACKEND_VERSION` in `yadgar/__init__.py`.
