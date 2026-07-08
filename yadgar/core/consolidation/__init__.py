"""Core-side consolidation orchestration (R3 Car 1 D1/D3).

The consolidation COMPUTE (decay/episodes/merge/cls/causal/memify/sleep) lives
in the BACKEND (``yadgar.backend.consolidation``) because it uses the curator +
phase engines, which are backend now. This package holds only the CORE half:
the thin orchestration that fires host-lifecycle / viz / admin work around the
compute — nightly scheduling, ``_fire_vacuum_service``, graph-layout precompute,
and invariant checks.

The orchestration NEVER imports or instantiates the backend compute engines. It
forwards the compute to the backend ``/consolidate`` endpoint over HTTP (mirrors
forward-only recall #45: core validates + forwards, backend computes), then runs
the core-only post-cycle tasks (invariants + auto-vacuum) on the returned stats.
"""

from __future__ import annotations

from yadgar.core.consolidation.orchestrator import (
    run_consolidate_now,
    run_nightly_consolidation,
)

__all__ = ["run_consolidate_now", "run_nightly_consolidation"]
