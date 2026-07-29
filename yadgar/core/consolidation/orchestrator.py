"""Core-side consolidation orchestrator (R3 Car 1 D3).

Thin. Forwards the consolidation COMPUTE to the backend ``/consolidate``
endpoint (the compute uses the backend curator + phase engines), then runs the
CORE-ONLY tasks around it:

  * ``_run_core_post_cycle_tasks``     — invariant checks + auto-vacuum trigger

T2 Car E3: the graph-layout precompute moved to the backend consolidation
cycle (``backend.consolidation.service._maybe_precompute_graph_layout``) —
graph assembly + spring-layout is compute over DB rows (census verdict #11).

These were the surviving backend→core edges when everything lived under
``backend/consolidation``; splitting them out (compute→backend, orchestration→
core) makes both halves single-layer.

State that used to live on the ConsolidationScheduler instance
(``_last_vacuum_at``) is held here as a module-level global — in-memory, resets
on restart, identical semantics to the old per-instance attribute.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar.core.ops import VacuumTriggerNotConfiguredError, _fire_vacuum_service

logger = logging.getLogger("yadgar.consolidation")

# v4.9 auto-vacuum cooldown timestamp (in-memory; resets on restart). Was
# ConsolidationScheduler._last_vacuum_at; now a core-side module global because
# the auto-vacuum trigger is a host-lifecycle concern owned by core.
_last_vacuum_at: datetime | None = None


# ---------------------------------------------------------------------------
# Backend forwarder (mirrors core/server/tools/recall.py:_forward_to_backend)
# ---------------------------------------------------------------------------


@observe(tier="boundary", metric="consolidation.forward_to_backend")
def _forward_to_backend(mode: str, timeout_s: float = 1800.0) -> dict:
    """Forward the consolidation compute to the backend ``/consolidate`` endpoint.

    mode: one of "light" | "full" | "nightly" (see backend service.py). The
        backend owns the compute engines and the sleep-cycle 6-hour gate.

    Backend URL: derived from YADGAR_EMBED_URL (the same base URL recall forwards
    to for /recall). Forward-only: no in-core fallback.

    timeout_s: a full/nightly consolidation can take 5–15 min; default 1800s.

    Raises:
        RuntimeError: if YADGAR_EMBED_URL is not configured.
        httpx.HTTPError: if the backend request fails.
    """
    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward consolidation to backend. "
            "R3 Car 1: consolidation compute is forward-only — no in-core fallback."
        )

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    resp = httpx.post(
        f"{backend_base}/consolidate",
        json={"mode": mode},
        headers=headers,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("stats", {})


# ---------------------------------------------------------------------------
# Core-only orchestration around the forwarded compute
# ---------------------------------------------------------------------------


def _in_window(now: datetime, window_start: str, window_end: str) -> bool:
    """Return True if *now* (naive local datetime) falls within [start, end).

    Supports cross-midnight windows (e.g. start=23:00, end=02:00). Equal start
    and end is a zero-length window → always False.
    """
    sh, sm = (int(x) for x in window_start.split(":"))
    eh, em = (int(x) for x in window_end.split(":"))
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    now_m = now.hour * 60 + now.minute
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


@observe(tier="stage", metric="consolidation.maybe_auto_vacuum")
def _maybe_auto_vacuum(storage, settings) -> None:
    """v4.9: Fire yadgar-vacuum.service if DB is over threshold and in window.

    Cooldown: VACUUM_AUTO_COOLDOWN_HOURS since last auto-fire (in-memory module
    global; resets on restart). config.yaml-authoritative (v5.95).

    Core-owned: uses core.ops._fire_vacuum_service (host lifecycle).
    """
    global _last_vacuum_at
    threshold = settings.VACUUM_AUTO_THRESHOLD_BYTES

    _COOLDOWN_HOURS = float(settings.VACUUM_AUTO_COOLDOWN_HOURS)
    if _last_vacuum_at is not None:
        hours_since = (datetime.now(UTC) - _last_vacuum_at).total_seconds() / 3600.0
        if hours_since < _COOLDOWN_HOURS:
            return

    db_size_info = storage.get_db_size()
    size = db_size_info.get("db_size_bytes", 0)
    if size <= threshold:
        return  # Below threshold — nothing to do

    now_local = datetime.now()
    if _in_window(now_local, settings.VACUUM_AUTO_WINDOW_START, settings.VACUUM_AUTO_WINDOW_END):
        try:
            _fire_vacuum_service()
        except VacuumTriggerNotConfiguredError:
            # No watcher on this surface (task:0044 D1). Do NOT stamp the
            # cooldown — the DB is genuinely over threshold and the operator
            # needs to see this every cycle until they configure a watcher.
            logger.error(
                "Auto-vacuum wanted (db=%d MiB > %d MiB threshold) but "
                "YADGAR_VACUUM_TRIGGER_PATH is unset — this install surface ships "
                "no vacuum trigger watcher, so no vacuum will run",
                size >> 20,
                threshold >> 20,
            )
            return
        _last_vacuum_at = datetime.now(UTC)
        logger.warning(
            "Auto-vacuum triggered: db=%d MiB > %d MiB threshold",
            size >> 20,
            threshold >> 20,
        )
    else:
        logger.warning(
            "DB over auto-vacuum threshold (%d MiB) but outside window (%s–%s); deferred",
            size >> 20,
            settings.VACUUM_AUTO_WINDOW_START,
            settings.VACUUM_AUTO_WINDOW_END,
        )


@observe(tier="stage", metric="consolidation.core_post_cycle_tasks")
def _run_core_post_cycle_tasks(storage, settings) -> None:
    """Non-fatal core post-consolidation tasks: invariant checks + auto-vacuum.

    Runs AFTER the backend compute returns. The compute-side post-cycle tasks
    (insert_consolidation_log, mtree_probe) stay backend-side; these two are the
    core-only tail (invariants uses core.server._run_check_invariants; auto-vacuum
    fires the host vacuum service).
    """
    # Invariant checks — violations are logged CRITICAL.
    # R3 Car 3d: the checks + auto-repair DELETEs run backend-side (owns the DB);
    # forward via /admin. Non-fatal: unset YADGAR_EMBED_URL / transport error is
    # swallowed by the surrounding except.
    try:
        from yadgar.core.server.tools._forward import _forward_admin  # noqa: PLC0415

        inv = _forward_admin("check_invariants", {})
        if not inv["ok"]:
            logger.critical(
                "check_invariants: %d violation(s) detected after consolidation: %s",
                len(inv["violations"]),
                inv["violations"],
            )
    except Exception:
        logger.debug("check_invariants failed (non-fatal)", exc_info=True)

    # v4.9: threshold auto-trigger vacuum — non-fatal end-of-cycle check.
    if settings.VACUUM_AUTO_ENABLED:
        try:
            _maybe_auto_vacuum(storage, settings)
        except Exception:
            logger.debug("auto-vacuum check failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Public entrypoints — forward compute, then run core orchestration
# ---------------------------------------------------------------------------


@observe(tier="boundary", metric="consolidation.run_consolidate_now")
def run_consolidate_now(mode: str = "light") -> dict:
    """MCP consolidate_now entrypoint: forward compute to backend, then core tail.

    mode="light": cycle only (backend). mode="full": cycle + forced sleep cycle
    (backend) + graph-layout precompute (core). The anchor-audit pass stays in
    the consolidate_now MCP tool (already core-side).

    The sleep-cycle 6-hour gate lives on the backend scheduler singleton, so the
    nightly cron and consolidate_now(full) naturally share it — no core-side poke
    of the gate timestamp is needed (or possible: core no longer holds the
    scheduler).
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    stats = _forward_to_backend(mode)

    settings = get_settings()
    # T2 Car E3: the graph-layout precompute (mode=full) now runs inside the
    # backend cycle — nothing viz-related remains on this side.
    _run_core_post_cycle_tasks(_st._storage, settings)
    return stats


@observe(tier="boundary", metric="consolidation.run_nightly")
def run_nightly_consolidation(storage=None, settings=None) -> dict:
    """Nightly entrypoint: forward the gated nightly compute, then core orchestration.

    The backend "nightly" mode runs the consolidation cycle + the gated sleep
    cycle (6h) + the post-sleep full similarity-link reconcile. Core then layers
    the graph-layout precompute (viz) + the invariant / auto-vacuum tail.

    storage/settings default to the shared runtime state / config so the nightly
    script can call this with no args (the DB/config are process-global).
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    if settings is None:
        settings = get_settings()
    if storage is None:
        storage = _st._storage

    stats = _forward_to_backend("nightly")
    # T2 Car E3: layout precompute runs inside the backend nightly cycle.
    _run_core_post_cycle_tasks(storage, settings)
    return stats
