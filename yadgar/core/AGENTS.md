# AGENTS.md — `yadgar/core/` (router/session/ops layer)

Rules for agents editing this layer. The layer README explains what lives
here; this file is the placement law. Full rationale: the layer-boundary
train plan (T2, 2026-07-09) + ADR-0078 / ADR-0084 / ADR-0056.

## What belongs here

Core is the ROUTER, not the worker. It runs in the thin container (1 CPU, no
ML models) and owns:

- MCP tool surface (`server/`), HTTP endpoints, response caches (`cache/`)
- Session state (SR/action buffers), forward/offload/queue plumbing
- Host-ops that must run while containers are DOWN: `backup/`,
  `_surreal_runner/`, `vacuum/`, `daemon/`, `install/`, `update/`, `cli/`
  (codified pattern: container-down ops = host = core wheel)
- Viz HTTP server (`viz/`) — data assembly + layout compute forward to
  backend (Car E3)

## What does NOT belong here

- COMPUTE (numpy/scoring/matrix, stateless-over-DB-data) → `backend/`
  (semantic law — wins even when all current importers are core).
- Raw DB WRITES. Core write paths go through the file-queue seam or backend
  endpoints (ADR-0078). The remaining raw-write sites (seed/_generate,
  server/http, staleness, cli/capture, admin_other) are Car E1 relocation
  targets — do NOT add new ones.

## Import direction

- `core` must NOT import `yadgar.backend.*` — enforced by import-linter
  contract 3 (pre-commit, hard-fail). Backend work is reached via HTTP
  (`/recall`, `/rerank`, `/restore`, …) or the composition root's injected
  handles, never a direct import.
- `core` may import `_shared/` freely.

## Don't

- Don't add a new flat `.py` at this layer root (ADR-0084 no-lone-files; the
  flat files still here are back-compat PEP-562 shims from the T2 moves).
- Don't add DB write calls — see above; reads via `_shared.storage` are
  tolerated until the post-T2 storage sink.
- Don't move host-ops INTO backend "because it touches the DB" — quiesced
  snapshots/vacuum run with both containers stopped; they cannot live in
  backend runtime (census verdicts #12/#13).
