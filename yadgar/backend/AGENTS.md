# AGENTS.md — `yadgar/backend/` (compute layer)

Rules for agents editing this layer. The layer README explains what lives
here; this file is the placement law. Full rationale: the layer-boundary
train plan (T2, 2026-07-09) + ADR-0078.

## What belongs here

Backend owns COMPUTE: anything needing numpy/matrix/scoring, ML models,
heavy transformation, or stateless work over DB data. It runs next to the DB
with 7 CPUs (ADR-0078); core has 1. When in doubt between core and backend
for compute-shaped code: backend.

- The recall pipeline behind `POST /recall` (`retrieval/`) — the largest
  package in the layer
- Embedding/rerank/NLI model serving (`embed_service/`, `ml_client/`)
- Backend halves of the admin tools, including the engine-#2 ledger ops
  (`admin_exec/`; ledger task 402 split those into `ledger.py` /
  `ledger_agent.py` / `ledger_project.py` by table family)
- Consolidation + sleep cycle (`consolidation/`, `sleep_compute/`,
  `curation/`, `cls_store/`, `causal_discovery/`)
- Write-path execution (`write_exec/`, `queue_drainer/`, `predictive_coding/`)
- Restore/checkpoint compute (`restoration/` — CheckpointRestore +
  CognitiveMap, census verdict #7)

## Import direction

- `backend` must NOT import `yadgar.core.*` — enforced by import-linter
  contract 2 (pre-commit, hard-fail). No waivers exist; keep it that way.
- `backend` may import `_shared/` freely.
- New API surface for core = a new HTTP endpoint on `embed_service`, never a
  direct core import. Bump `BACKEND_VERSION` (`yadgar/__init__.py`) when the
  service contract changes.

## Don't

- Don't import core, ever — including in tests' fixtures that construct
  backend objects.
- Don't add a new flat `.py` at this layer root (ADR-0084 no-lone-files). This
  layer is already clean — `yadgar/backend/*.py` is `__init__.py` and nothing
  else, so the "flat files still here are back-compat shims" clause this line
  used to carry described a set that is empty (verified 2026-08-28).
- Don't reach for `yadgar._shared.runtime.lifecycle` to grab core-ish
  singletons — receive dependencies via injection (Protocols from
  `_shared/contracts/`).
