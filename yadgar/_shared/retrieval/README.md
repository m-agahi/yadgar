# `_shared/retrieval/` — recall pipeline

Scoring, fusion, query analysis, and reranking for `recall()` — the
multi-signal retrieval pipeline (semantic + keyword + heat + branch).

- `core.py` — candidate collection over storage
- `scoring.py` / `fusion.py` — signal scoring + cross-type fusion
- `reranking.py` / `_reranking_heuristic.py` — CE/NLI rerank via injected
  MLClientProtocol (never imports backend directly)
- `query_analysis.py` — query classification

Status: dual today (core recall path + `backend/predictive_coding`). After
Car E2 forwards landscape recall to the backend, this package SINKS to
`backend/` — do not add new core-only dependencies on it.
