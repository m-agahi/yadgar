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


@observe(tier="stage", metric="backend.consolidation.maybe_precompute_graph_layout")
def _maybe_precompute_graph_layout(storage, settings) -> None:
    """Precompute + cache the 3D graph layout (nightly / full paths only).

    T2 Car E3 (census verdict #11): moved from the core orchestrator — the
    graph assembly + spring-layout compute run next to the DB on the backend's
    CPUs, inside the same full/nightly cycle that owns the rest of the heavy
    compute.

    viz-render-perf (Car A): the VIZ_PRECOMPUTED_LAYOUT_ENABLED knob was removed —
    precompute now runs unconditionally (supersedes ADR-0010's default-OFF stance).
    Two gates remain so it never blocks: (1) a graph-signature no-op — when the
    live graph shape matches the cached signature nothing is recomputed, (2) only
    called from the nightly/full paths, never the light budget. Non-fatal.

    finish-viz (galaxy): when ``VIZ_GALAXY_LAYOUT`` is on (default), the Milky-Way
    ``galaxy_layout`` generator runs instead of ``networkx.spring_layout`` — loose
    nodes → dense core bulge, multi-member clusters → K log-spiral arms. The cache
    row records ``layout_mode`` so the client freezes physics on a galaxy payload.
    The signature no-op also invalidates on a mode flip (mode is folded into the
    cached-signature comparison) so toggling the knob recomputes on the next cycle.
    """
    try:
        import time as _time  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415

        from yadgar.backend.graph.graph_api import GraphAPI  # noqa: PLC0415
        from yadgar.backend.graph.graph_layout import (  # noqa: PLC0415
            compute_graph_layout,
            galaxy_layout,
            galaxy_membership,
            graph_signature,
        )

        # Lay out the FULL uncapped graph (caps=0) so positions stay stable when
        # the per-request /api/graph node caps change.
        data = GraphAPI(storage).get_full_graph(0, 8, False, None, 0, 0)
        nodes, edges = data.get("nodes", []), data.get("edges", [])
        clusters = data.get("clusters", [])
        galaxy_on = bool(getattr(settings, "VIZ_GALAXY_LAYOUT", True))
        mode = "galaxy" if galaxy_on else "spring"
        arms = int(getattr(settings, "VIZ_GALAXY_ARMS", 4))
        spiral_pitch = float(getattr(settings, "VIZ_GALAXY_SPIRAL_PITCH", 0.30))
        core_density = float(getattr(settings, "VIZ_GALAXY_CORE_DENSITY", 1.0))
        # ADR-0152 R6: fold the layout-code version + galaxy params into the
        # signature so new math (bumped _LAYOUT_VERSION) or a changed VIZ_GALAXY_*
        # setting invalidates the cache even on an unchanged graph SHAPE — else the
        # nightly no-ops and keeps serving the old positions.
        sig = graph_signature(
            nodes,
            edges,
            params={"arms": arms, "pitch": spiral_pitch, "coredens": core_density}
            if galaxy_on
            else None,
        )
        cached = storage.get_graph_layout_cache()
        # Recompute when the graph SHAPE changes OR the layout mode flips (so a
        # knob toggle re-lays-out on the next cycle rather than serving stale
        # positions of the other kind).
        if (
            cached
            and cached.get("signature") == sig
            and cached.get("layout_mode", "spring") == mode
        ):
            return  # graph shape + mode unchanged — keep the cached layout

        _t = _time.monotonic()
        logger.info("phase_start: precompute_graph_layout mode=%s nodes=%d", mode, len(nodes))
        membership: dict = {}
        if galaxy_on:
            positions = galaxy_layout(
                nodes,
                edges,
                clusters,
                arms=arms,
                spiral_pitch=spiral_pitch,
                core_density=core_density,
            )
            # Cache the loose/arm membership sibling so the serve path stamps it
            # onto nodes without recomputing (single source of truth for Car B).
            membership = galaxy_membership(nodes, edges, clusters, arms)
        else:
            iterations = getattr(settings, "VIZ_LAYOUT_ITERATIONS", 50)
            positions = compute_graph_layout(nodes, edges, dim=3, iterations=iterations)
        storage.set_graph_layout_cache(
            sig, positions, datetime.now(UTC).isoformat(), mode, membership=membership
        )
        _dur_ms = int((_time.monotonic() - _t) * 1000)
        logger.info("phase_end: precompute_graph_layout mode=%s duration_ms=%d", mode, _dur_ms)
    except Exception as _exc:
        from yadgar._shared.observability.exception_telemetry import (
            record_exception,  # noqa: PLC0415
        )

        record_exception("consolidation.phase_precompute_graph_layout", _exc)
        logger.exception("Precompute graph layout failed")


@observe(tier="boundary", metric="backend.consolidation.run_cycle")
def run_consolidation_cycle(mode: str = "light") -> dict:
    """Run one consolidation compute cycle for the given mode. Returns cycle stats.

    mode="light":   consolidation cycle only (decay/episodes/merge/cls/causal).
    mode="full":    cycle + a FORCED sleep cycle (manual full trigger).
    mode="nightly": cycle + a GATED (6h) sleep cycle + full similarity reconcile.

    The anchor-audit, invariant-check and auto-vacuum tail are CORE-side (the
    core orchestrator runs them around this forwarded compute — R3 Car 1 D3).
    The graph-layout precompute runs HERE on the full/nightly paths (T2 Car E3
    — it is graph assembly + spring-layout compute over DB rows).
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"consolidation mode {mode!r} invalid; use one of {_VALID_MODES}")

    scheduler = _get_scheduler()
    if mode == "nightly":
        stats = scheduler.run_nightly_consolidation()
    elif mode == "full":
        stats = scheduler.run_full_consolidation()
    else:
        return scheduler.force_consolidate()

    # T2 Car E3: full/nightly tail — layout precompute next to the DB.
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    _maybe_precompute_graph_layout(_st._storage, get_settings())
    return stats
