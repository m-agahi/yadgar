"""Backend consolidation compute entry (R3 Car 1 D2).

Module-level ``run_consolidation_cycle(mode)`` builds (once) the backend
``ConsolidationScheduler`` compute singleton and runs one cycle. Served by the
backend FastAPI ``/consolidate`` route (embed_service.py), mirroring the
``/recall`` route's lazy-engine-init.

The scheduler is a PROCESS SINGLETON — not rebuilt per request — so the
sleep-cycle 6-hour gate (``_last_sleep_cycle``) survives across calls, and the
nightly + consolidate_now(full) paths share the same gate (double-fire
avoidance is automatic).
"""

from __future__ import annotations

import logging
import threading

from yadgar._shared.observability.observe import observe

logger = logging.getLogger("yadgar.consolidation")

# Process singleton + guard (mirrors embed_service._recall_engines_ready).
_scheduler = None
_scheduler_lock = threading.Lock()

_VALID_MODES = ("light", "full", "nightly")


@observe(tier="stage", metric="backend.consolidation.get_scheduler")
def _get_scheduler():
    """Build (once) and return the backend ConsolidationScheduler compute singleton.

    Reuses the slim engine set the /recall path already builds (storage,
    embeddings, standalone AstrocytePool on _st._pool). The scheduler adopts the
    injected _st._pool verbatim (pool= kwarg) so it does not re-run
    init_processes() and stays the same object as _st._pool.

    Idempotent: subsequent calls return the same instance.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler

        import yadgar._shared.runtime.state as _st  # noqa: PLC0415
        from yadgar._shared.config import get_settings  # noqa: PLC0415
        from yadgar.backend.consolidation import ConsolidationScheduler  # noqa: PLC0415
        from yadgar.backend.embed_service import _ensure_recall_engines  # noqa: PLC0415

        # Ensure the slim shared engines (storage/embeddings/pool) exist via the
        # SAME single-init guard the /recall route uses (_recall_engines_ready).
        # Calling _init_engines directly here would bypass that guard and risk a
        # second slim build (double pool init_processes) if /consolidate lands
        # before /recall. _ensure_recall_engines is idempotent + no-op once ready.
        _ensure_recall_engines()

        _scheduler = ConsolidationScheduler(
            _st._storage, _st._embeddings, get_settings(), pool=_st._pool
        )
        # Expose the inner sleep/cls engines as backend-side globals so any
        # backend caller reaching for them via state finds them (parity with the
        # old core bootstrap that set _st._sleep / _st._cls).
        _st._consolidation = _scheduler
        _st._sleep = _scheduler._sleep_engine
        _st._cls = _scheduler.cls
    return _scheduler


@observe(tier="boundary", metric="backend.consolidation.run_cycle")
def run_consolidation_cycle(mode: str = "light") -> dict:
    """Run one consolidation compute cycle for the given mode. Returns cycle stats.

    mode="light":   consolidation cycle only (decay/episodes/merge/cls/causal).
    mode="full":    cycle + a FORCED sleep cycle (manual full trigger).
    mode="nightly": cycle + a GATED (6h) sleep cycle + full similarity reconcile.

    The graph-layout precompute, anchor-audit, invariant-check and auto-vacuum
    tail are CORE-side (the core orchestrator runs them around this forwarded
    compute — R3 Car 1 D3). This function returns ONLY the compute stats.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"consolidation mode {mode!r} invalid; use one of {_VALID_MODES}")

    scheduler = _get_scheduler()
    if mode == "nightly":
        return scheduler.run_nightly_consolidation()
    if mode == "full":
        return scheduler.run_full_consolidation()
    return scheduler.force_consolidate()
