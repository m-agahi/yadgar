# `backend/consolidation/` — the nightly brain cycle

Orchestrates decay, episode→semantic promotion (CLS), causal discovery,
duplicate merge, cleanup, and cold retention. Triggered via
`POST /consolidate` (from core's scheduler) or `consolidate_now`.

- `orchestrator.py` / `service.py` — cycle orchestration + fast/slow tiers
- `heat_decay.py` — the SINGLE writer for heat decay (do not add others)
- `cls.py`, `causal.py`, `cleanup.py`, `cold_retention.py` — phase impls

Heat/staleness writes belong HERE (census verdict #8) — core's
`staleness` package relocates its writes to this seam in Car E1.
