> Archived 2026-07-16 — re-scoped into task #32 + ADR-0129 (run_perf_loadtest.py found bogus; histogram-delta is canonical).

# Perf load-test — remaining work (DEFERRED for review)

**Status:** DEFERRED — awaiting user review + two decisions (D1, D2). No implementation until those are made. **Date:** 2026-07-14.
**Split from:** `perf-loadtest-contract-2026-06-30.md` (now archived — its record-only harness + contract checker + CI job shipped). This doc carries ONLY the unshipped remainder, written self-contained so it can be picked up cold.

## TL;DR (read this if you remember nothing else)
yadgar already has a **working but incomplete** perf load-test: a measuring harness + a pass/fail contract checker + a manual CI job all exist and run **today** (record-only, non-gating). "Finishing" it is blocked on **two decisions that are yours** (D1, D2). Once decided, ~4 mostly-mechanical steps (R1–R4) remain.

## What already SHIPPED (context — do NOT rebuild)
- `benchmarks/run_perf_loadtest.py` — record-only harness: sequential recall p50/p95/mean + error rate (Phases W0+A only). Drives an **already-running live daemon**.
- `benchmarks/perf_contract.py` — pass/fail checker: delta% vs tolerance + an incomparability guard. Plus CE-span capture (added later, sourced from obs-velocity).
- `make perf` (Makefile:317-326) — record-only, auto-skips without a daemon.
- `.forgejo/workflows/perf.yaml` — manual `workflow_dispatch` job, **non-gating** (NOT in the PR gate).
- Tests: `test_perf_contract.py`, `test_perf_contract_ce_source_psb.py`.
- `benchmarks/reports/perf_baseline.json` — a **STUB** (`snapshot_id="STUB-uncalibrated"`, all zeros — placeholder, not a real baseline).
- 4 real record-only runs exist but are mutually **incomparable** (ad-hoc `snapshot_id` stamps — this is the crux of D1).

## THE FORK — the harness diverged from the original design
The original plan (§1.4/§2.4) wanted each run pinned to a **quiesced snapshot** (`cp -r` a frozen DB, pin it) for reproducible comparability. The shipped harness instead drives a **live running daemon** and hand-stamps `snapshot_id`. Consequence: runs aren't reproducibly comparable across PRs. This is what D1 decides.

## DECISIONS NEEDED — yours (D1 is load-bearing)
- **D1 — comparability model:**
  - **A. Snapshot-pin retrofit** (~2–3 days) — quiesce + freeze the DB per run → true cross-PR comparability. Higher effort.
  - **B. Ratify the live-daemon model** (cheap, already built) — comparability then rests on operator discipline (same box, same warm state, ≥6 warm runs, median).
  - Blocks **R2**: you can't promote a real baseline until you pick the model it's measured under.
- **D2 — gating vs record-only:**
  - Keep **permanent record-only** (informational; `main()` always exits 0), OR flip to **gating** (fail a run on regression).
  - If gating: keep it on deliberate `workflow_dispatch` **ONLY**, never per-PR — multi-agent pytest contention causes 14–47 false regressions (anchor mem 518987).

## REMAINING implementation (after D1 + D2)
- **R1** — extend the workload contract: Phase B (8-concurrent recall), C (`wiki_query`), D (memorize → drain), E (`/health/live` under load). The driver is already thread-capable; extend `METRIC_KEYS`. *(Independent — buildable anytime.)*
- **R2** — promote a **real baseline** (replace the STUB). *Blocked on D1.*
- **R3** — gating flip (**only if D2 = gating**): `main()` returns non-zero on contract breach. *Blocked on D2 + R2.*
- **R4** — per-PR human-readable `docs/benchmarks/perf_*.md` reports. Confirmed unshipped. *(Independent.)*

## How to pick this up
Answer **D1 + D2** first → then R1 (anytime), R2 (needs D1), R3 (needs D2 + R2), R4 (anytime). The honest source of truth for current behavior is the harness docstring `run_perf_loadtest.py:1-73`, NOT the archived plan doc.
