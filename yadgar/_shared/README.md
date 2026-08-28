# `yadgar/_shared/` — the shared layer

Code imported by BOTH `core/` and `backend/` (dual-import law). Anything
consumed by a single layer lives in that layer instead; compute-shaped code
belongs in `backend/` even when core is its only importer today (semantic
law). See `AGENTS.md` here for the placement rules and forward seams.

## Packages

| Package | What it is |
|---|---|
| `storage/` | SurrealDB storage engine — the only code that talks to the DB directly |
| `retrieval/` | recall PROFILES only — the scoring/fusion/rerank pipeline itself already sank to `backend/retrieval/`; do not grow this package |
| `config/` | Settings + knob registry + YAML config I/O |
| `observability/` | `@observe`, tracing, metrics, log_config, exception telemetry |
| `security/` | I26 secret gate, allowlist, enforcement counters |
| `contracts/` | pydantic models, DI Protocols, EngramAllocator |
| `wiki/` | wiki contract + WikiStore (dual: core tools + backend exec) |
| `embeddings/` | local + remote embedding engines |
| `runtime/` | composition root (`lifecycle.py`, ADR-0056 waivers), offload, recall pipeline |
| `file_queue/` | async write queue — THE sanctioned core→backend write seam |
| `paths/` | XDG path constants (lazy env resolution) |
| `restoration/` | checkpoint contract (impl lives in `backend/restoration/`) |
| `enrichment/`, `metacognition/`, `schemas/`, `write_exec/` | write-path enrichment, gap detection, JSON schemas, write execution |
| `server_helpers/` | shared MCP-tool helpers (`_file_hash`, `_compute_valid_until`, …) |
| `knowledge_graph/` | entity extraction, relationship edges |
| `rules_engine/` | write-block / write-allow rule evaluation |
| `astrocyte_pool/` | domain-specialist consensus retrieval pool (landscape mode) |
| `thermodynamics/` | heat / decay math |
| `sensory_buffer/`, `rate_limit/`, `blocks_render/` | pre-write buffering, rate limiting, memory-block rendering |
| singles (`astrocyte_pool/`, `thermodynamics/`, `knowledge_graph/`, `rules_engine/`, `sensory_buffer/`, `rate_limit/`, `blocks_render/`, `server_helpers/`) | one-module packages per the no-lone-files law |

The flat `.py` files at this root are back-compat PEP-562 shims from the T2
layer-boundary moves — do not add new ones.

## How it connects

`core` and `backend` both import this layer; this layer imports NEITHER
(import-linter contract 1; sole waivers: the composition-root edges in
`runtime/lifecycle.py`). Docs per module live in docstrings.
