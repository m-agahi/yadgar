# Plan: Test-suite hardening train — lazy fixtures + zero-skip + zero-warning

**Status:** AGREED (user combined the two tasks 2026-07-09). Three cars, sequential, one worktree; car = PR.
**Date:** 2026-07-09
**Why one train:** all three rework the same seams (root conftest, fixture graph, pyproject). Car order is load-bearing: skips audited before fixtures are built for dead tests; warnings gate flips after the fixture churn so the rework can't ship new warnings.

## Car 1 — Zero-skip audit (0.5–1d)

CI's env is controlled: a test that ALWAYS skips in CI is dead or mis-gated — no third option.

1. Inventory every skip with reasons: `pytest -rs` across the four leg selections (~46 skips: 21 fast-leg, 18 + 7 core-legs at last count).
2. Verdict per skip:
   - **DEAD** — feature removed (onnx-gated skips should have died with ADR-0067) or premise gone → DELETE the test.
   - **MIS-GATED** — condition false in CI but capability exists (env var, marker, import guard) → fix the gate so it RUNS.
   - **LEGIT-CONDITIONAL** (platform/hardware) → explicit reason string + entry in a checked-in skip inventory.
3. Enforcement: CI gate failing on skips whose reason isn't in the inventory (parse `-rs` output or a pytest hook). Target: 0 unexplained skips, inventory as the only growth path.

## Car 2 — Lazy surreal/model fixtures (the big one, 2–4d)

The durable fix for the CI-OOM class (4 collisions on 2026-07-09 alone despite dind cap + wave topology + `-n 4`; ADR-0079 runner co-hosted). Per-worker RAM today ≈ surreal (~300MB) + embedding model (~1.5–2GB) loaded for EVERY worker regardless of need.

1. Fixture graph rework in root conftest: `surreal_server` and model/embedding fixtures become lazy — spawned/loaded only when a test (transitively) requests them. Non-DB tests never spawn surreal; non-ML tests never load models.
2. Mechanism: request-scoped gating (the harness already gates the autouse backend-harness fixture on fixturenames — extend the same pattern), NOT markers-by-hand; a test's needs derive from the fixtures it uses.
3. Expected effect: majority of the ~7,000 unit tests are logic-only → per-worker RAM drops from ~2GB to ~200MB for those workers; the `-n 4` caps and wave staggering in ci-pr.yaml become headroom, candidates for relaxation (measure before touching — ADR-0073 stays until proven).
4. Verify: RAM profile of a full leg before/after (peak RSS per worker); full sweep parity (same pass/skip counts as Car 1 baseline); e2e untouched (own conftest).
5. Interaction with Car 1: import-guard skips (e.g. optional deps) become lazy-import failures if mishandled — the Car 1 inventory is the checklist.

## Car 3 — Zero-warning gate + skip-gate hardening (1d)

**Skip-gate hardening (ADR-0087, user 2026-07-09 "no skips added without a valid reason"):**
- Keep all 13 sanctioned skips (macOS ruled a shipping target — pipx/PyPI wheel installs there).
- Pre-commit static check: staged test files with NEW skip/skipif markers whose reason isn't
  in `yadgar/tests/skip_inventory.json` → fail AT COMMIT, not first-CI. Scoped to marker
  decorators + module-level skips; dynamic `pytest.skip()` calls stay the CI gate's job.
- Inventory governance (I30/I33 pattern): reason ≥40 chars, stale-entry hard-fail (test
  gone → entry must go), no wildcard entries.

1. Fix our two starlette deprecation classes at the source:
   - `httpx`+`starlette.testclient` → install/migrate `httpx2` (~5 test files: test_v579_smart_sessionstart, test_graph_api_contract, test_daemon_obs_gauges, +grep).
   - async-generator lifespans → `@contextlib.asynccontextmanager` (production lifespan code — post-#173/#176 embed_service + core app; coordinate with whatever lifespan shape those left).
2. Fold in the old backlog remainder: `datetime.utcnow()` → `datetime.now(UTC)` (session-end-capture.py, py3.14 removal deadline).
3. Flip `filterwarnings = ["error", ...]` in pyproject with NARROW, commented third-party ignores (each with revisit note). Warnings become CI failures permanently.
4. Order-dependent: runs LAST — Cars 1–2 churn conftest/fixtures and must not land warnings under the old tolerance.

## Sequencing
After: anchor-signal-gap PR + hook re-measurement (current in-flight). Before: recall T2 restructure (user's clean-code-before-refactor directive extends here — T2 lands on a suite that is lean, skip-free, warning-free).
Collision check per car against whatever is in flight at dispatch time.

## References
ADR-0067 (onnx removal — orphaned skip guards), ADR-0073/0079 (CI memory saga — Car 2 is the cure), ADR-0074 (span budget — any new fixture helpers follow it), tests-layout wiki page (harness/seams), old backlog items #36 (lazy fixtures, .test_durations refresh) + #37 (warnings).
