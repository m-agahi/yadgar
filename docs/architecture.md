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
│   memorize / recall / get_project_context / anchor /  │
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

1. **Decay** — heat reduced per-memory using `DECAY_FACTOR^hours_elapsed` with modifiers for importance, emotional valence, confidence
2. **Archiving** — memories below `COLD_THRESHOLD` (or `ACTION_STREAM_COLD_THRESHOLD` for action-stream entries) set to `heat=0.0`
3. **Prune** — action-stream memories that cooled, have low confidence, and were never recalled are permanently deleted
4. **Entity extraction** — new episodes parsed for file paths, function names, imports, errors
5. **Relationship building** — co-occurring entities in same episode get `co_occurrence` edges
6. **Duplicate merging** — pairs with similarity > `CURATION_SIMILARITY_THRESHOLD` merged (higher-heat survives)
7. **CLS promotion** — episodic patterns promoted to semantic memory
8. **Dream replay** — random memory pairs examined for latent relationships
9. **Wiki proposals** — notable clusters drafted as wiki pages for human review

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `server.py` | MCP tool handlers, session management, action-stream capture |
| `storage.py` | All SurrealDB reads/writes; schema ownership |
| `consolidation.py` | Background daemon loop, coordinates all consolidation stages |
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
| `memories` | Core memory records: content, embedding, heat, confidence, tags, directory_context |
| `episodes` | Raw tool-action log chunks before consolidation |
| `entities` | Extracted code/file/concept entities with heat |
| `relationships` | Edges between entities (co_occurrence, causal, etc.) |
| `wiki_pages` | User-approved wiki pages (markdown) |
| `wiki_drafts` | Pending drafts awaiting approval |
| `checkpoints` | Saved working state snapshots |
| `profiles` | Structured user attribute records |
| `beliefs` | Higher-order derived beliefs |
| `consolidation_log` | Timestamped record of every consolidation run |

## Transport Modes

- **stdio** (default): Claude Code spawns yadgar as a child process. Zero network, lowest latency.
- **streamable-HTTP** (`--transport streamable-http --port 8765`): Persistent daemon. One process serves all Claude sessions. Required for Docker.

## Docker Deployment

The included `Dockerfile` and `docker-compose.yml` run yadgar in HTTP mode. The SurrealDB data directory is mounted as `/data` (volume `yadgar-data`). Configuration can be injected via environment variables (`YADGAR_*`) without rebuilding the image, or by mounting a `config.yaml` at `/root/.yadgar/config.yaml`.
