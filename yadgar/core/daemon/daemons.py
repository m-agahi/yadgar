"""Core-side daemon-thread machinery (R2a Car D1).

The self-contained background thread-target loops that used to live in
``yadgar._shared.runtime.lifecycle`` moved HERE so their ``yadgar.core`` imports
(``core.graph_api``, ``core.viz_server``, ``core.update.check``) become
``core -> core`` in-layer edges instead of ``_shared -> core`` layering
violations. These are pure thread-target loops with NO shutdown-ordering
subtlety — they are spawned as daemon=True threads and torn down by process exit.

The daemon-start trigger (``_start_daemon_threads``) is invoked by
``core.bootstrap.core_init_engines`` AFTER the core-only engines (incl.
``_st._staleness``) are built — strictly safer than the old in-``init_engines``
call site, which ran before ``_st._staleness`` existed on the full path.

Loop-telemetry helpers (``_lifecycle_span`` / ``_lc_heartbeat`` /
``_lc_record_exc``) moved with the loops because ONLY these loops used them.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings, resolve_knob
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

settings = get_settings()


@observe(
    exempt="span factory: returns an OTel start_as_current_span context manager, not a work unit; wrapping the factory call adds a spurious span with no body timing"
)
def _lifecycle_span(name: str):
    """Context manager: OTel root span for lifecycle background threads.

    Falls back to nullcontext when OTel is unavailable (I21).
    """
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        return _ot.get_tracer("yadgar.lifecycle").start_as_current_span(name)
    except ImportError:
        return contextlib.nullcontext()


# ── PR-I: loop telemetry helpers ────────────────────────────────────────


@observe(tier="stage")
def _lc_heartbeat(loop: str) -> None:
    """PR-I: set loop heartbeat gauge. Never raises."""
    try:
        from yadgar._shared.observability.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat(loop)
    except Exception:  # noqa: BLE001
        pass


@observe(tier="stage")
def _lc_record_exc(loop: str, exc: BaseException) -> None:
    """PR-I: increment loop error counter. Never raises."""
    try:
        from yadgar._shared.observability.metrics import loop_record_exception  # noqa: PLC0415

        loop_record_exception(loop, exc)
    except Exception:  # noqa: BLE001
        pass


# ── Daemon-thread loops ─────────────────────────────────────────────────


@observe(
    exempt="opens a manual start_as_current_span/OTel root span in-body for a background thread; @observe would double-span the work unit (I21)"
)
def _metrics_loop(pid: int, db_path: str, storage: object) -> None:
    """Background thread: sample system metrics every 5 s (PR-I).

    Extracted from init_engines closure to module level so it is a named
    function (improves traceability in thread dumps). Captures pid/db_path/
    storage via explicit args (same semantics as the previous default-arg closure).
    """
    from yadgar.core.daemon.system_metrics import sample_system_metrics  # noqa: PLC0415

    sample_system_metrics(pid, db_path, storage)  # prime CPU delta baseline
    while True:
        time.sleep(5)
        try:
            with _lifecycle_span("lifecycle.metrics_sample"):
                result = sample_system_metrics(pid, db_path, storage)
                # §9 Q6: update under lock to prevent torn reads.
                with _st._metrics_lock:
                    _st._system_metrics_cache.update(result)
        except Exception:  # noqa: BLE001 — daemon loop: one bad metrics sample must never kill the sampler thread
            pass


@observe(
    exempt="opens a manual start_as_current_span/OTel root span in-body for a background thread; @observe would double-span the work unit (I21)"
)
def _reranker_idle_loop() -> None:
    """Background thread: unload idle rerankers (PR-I).

    Extracted from init_engines closure; frees ~500 MB after the idle-unload
    threshold of no recall activity. Emits heartbeat + error counter via PR-I
    helpers. Check interval + unload threshold are config.yaml-authoritative
    (RERANKER_IDLE_CHECK_INTERVAL_SEC / RERANKER_IDLE_UNLOAD_SEC) — v5.95.
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415 -- avoid import cycle

    while True:
        _lc_heartbeat("model_unload")  # PR-I: heartbeat at top of every iteration
        _cfg = get_settings()
        time.sleep(_cfg.RERANKER_IDLE_CHECK_INTERVAL_SEC)
        try:
            with _lifecycle_span("lifecycle.reranker_idle_check"):
                if _st._retriever is not None:
                    _st._retriever.unload_rerankers_if_idle(
                        idle_seconds=_cfg.RERANKER_IDLE_UNLOAD_SEC
                    )
        except Exception as _exc:  # noqa: BLE001 — daemon loop: a reranker-unload fault is counted and the loop continues; it must never kill the thread
            _lc_record_exc("model_unload", _exc)  # PR-I: loop error counter


@observe(tier="stage")
def _viz_loop(host: str, port: int) -> None:
    """Background thread: run the viz server (auto-started with daemon).

    Extracted from init_engines closure. Binds the same interface as the
    MCP server (settings.HOST). Containers override via YADGAR_HOST=0.0.0.0
    so the host-side docker port mapping (-p 127.0.0.1:42069:42069) works.
    OSError is caught separately to emit a specific port-conflict warning.
    """
    try:
        from yadgar.core.viz.viz_server import run_viz_server  # noqa: PLC0415

        logger.info("Viz server starting on http://%s:%d", host, port)
        run_viz_server(host=host, port=port)
    except OSError as exc:
        logger.warning("Viz server could not bind port %d: %s", port, exc)
    except Exception as exc:  # noqa: BLE001 — daemon thread boundary: the viz server is optional and any fault must be logged, never propagate out of the thread
        logger.warning("Viz server error: %s", exc)


@observe(tier="stage")
def _start_daemon_threads(_settings) -> None:
    """Start background daemon threads (metrics, reranker-idle, viz).

    Called only when start_daemons=True. Extracted from init_engines to
    reduce its cyclomatic complexity; preserves exact thread startup order.
    """
    # v5.7.0 PR-0: consolidation daemon removed; cron takes over in PR-1.
    # _st._consolidation.start() intentionally removed.
    # Car K: the staleness watchdog start went with the watch_directory param —
    # its only production caller passed None, so it never fired.

    # Background system-metrics sampler for /api/system and SSE events
    _pid = os.getpid()
    _db_path = _settings.DB_PATH
    _storage_ref = _st._storage  # capture at call time
    threading.Thread(target=_metrics_loop, args=(_pid, _db_path, _storage_ref), daemon=True).start()

    # Idle reranker unloader — frees ~500 MB after 10 min of no recall activity
    threading.Thread(target=_reranker_idle_loop, daemon=True).start()

    # Auto-start viz server alongside the daemon.
    _viz_port = getattr(_settings, "VIZ_PORT", 42069)
    _viz_host = getattr(_settings, "HOST", "127.0.0.1")
    threading.Thread(target=_viz_loop, args=(_viz_host, _viz_port), daemon=True).start()


# ── Update-check daemon ──────────────────────────────────────────────────


@observe(tier="stage")
def _run_update_check() -> None:
    """Background thread target: probe PyPI for a newer yadgar version.

    Non-fatal: any exception is logged at WARNING and swallowed so the
    calling thread (daemon startup) is not affected.

    Runs once on daemon start when UPDATE_CHECK_ON_START=True.
    No periodic scheduling — v5.49+ candidate.
    """
    try:
        from yadgar import __version__  # noqa: PLC0415
        from yadgar.core.update.check import probe_latest_version  # noqa: PLC0415

        _settings = settings  # module-level singleton
        result = probe_latest_version(
            url=_settings.UPDATE_PYPI_URL,
            timeout=_settings.UPDATE_CHECK_TIMEOUT_SECONDS,
        )
        if result.available_version != __version__:
            logger.warning(
                "yadgar update available: %s → %s  |  run: %s",
                __version__,
                result.available_version,
                "yadgar update --check  (for upgrade command)",
            )
        else:
            logger.info("yadgar is up to date (%s)", __version__)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update check failed (non-fatal): %s", exc)


@observe(tier="stage")
def _maybe_auto_check_for_update() -> None:
    """Spawn a background update-check thread if UPDATE_CHECK_ON_START=True.

    The thread is daemon=True so it does not prevent process exit.
    Returns immediately — probe latency does NOT block daemon startup.

    Reads env directly (bypasses lru_cache) so tests can monkeypatch the env
    and observe the correct behavior without restarting the process.
    """
    check_on_start = resolve_knob(
        "YADGAR_UPDATE_CHECK_ON_START",
        "UPDATE_CHECK_ON_START",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )
    if not check_on_start:
        return

    t = threading.Thread(
        target=_run_update_check,
        name="yadgar-update-check",
        daemon=True,
    )
    t.start()
    logger.debug("update check thread started (daemon=True)")
