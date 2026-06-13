# PLAN — v5.90: Grandfathered Complexity Cleanup — SUPERSEDED

**Status:** **SUPERSEDED 2026-06-13 by `docs/PLAN_V5_55_COMPLEXITY_PAYDOWN.md`.**

The complexity-debt work is now a single unified campaign under **v5.55**. Per user
direction (2026-06-13), the former v5.90 grandfathered-cleanup track was folded into
v5.55 so there is one complexity campaign, not two.

What moved into v5.55:
- The tiered paydown strategy (Tier 1 cyclomatic → Tier 2 function-LOC → Tier 3 file-LOC).
- The per-release contract (plan doc, TDD/characterization tests, baseline shrink, no new
  violations, benchmark gate on hot paths).
- The tooling (`baseline_diff.py`, `list_grandfathered.py`).
- The risk mitigations (snapshot tests, perf-regression revert threshold, pausable cadence).

What v5.55 ADDS that this plan lacked:
- A **configurable** cap source (`.complexity-config.json`).
- A **gated rationale allowlist** (`.complexity-allowlist.json`) replacing silent HARD
  baselining — essential complexity is justified, not grandfathered.
- Invariant **I30** enforcing the above.
- `docs/COMPLEXITY_POLICY.md`.

Do not implement against this file. See `docs/PLAN_V5_55_COMPLEXITY_PAYDOWN.md`.
