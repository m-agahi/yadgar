"""Queue-drainer lifecycle for the embed_service backend.

Split out of ``embed_service.py`` (C1, module-standardization train #18):
``_queue_base_path`` + ``_start_queue_drainer`` + ``_stop_queue_drainer`` — the
backend 5.30.1 P0 wiring that constructs/starts/stops the QueueDrainer during the
FastAPI ``lifespan``.

MODULE-OBJECT ACCESS (recipe crux): the live ``_queue_drainer`` handle is a
REASSIGNED global that lives in ``embed_service.py`` (declared there, reset to
None on ``importlib.reload(embed_service)``, read by the ``/health`` route and by
drainer tests as ``es._queue_drainer``). ``_start``/``_stop`` therefore write it
through the module object (``_es._queue_drainer = drainer``), NOT a local
``global`` — a ``global`` here would target THIS module's namespace and the test
read on ``es`` would never see it. Same reason ``_ensure_recall_engines`` is
called via ``_es`` (its writer + the ``_recall_engines_ready`` guard live in
``embed_service.py``; module-object access honours the test-time rebind).

``lifespan`` (still in ``embed_service.py``) calls ``_start``/``_stop`` through
its own re-exported globals, so ``patch.object(es, "_start_queue_drainer")``
still intercepts them. Re-exported so ``embed_service.embed_service.<name>``
keeps resolving.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yadgar.backend.embed_service.embed_service as _es
from yadgar._shared.observability.observe import observe
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_drainer_running as _drainer_running,
)

logger = logging.getLogger(__name__)


@observe(tier="stage")
def _queue_base_path() -> Path | None:
    """Resolve the shared file-queue root from YADGAR_QUEUE_BASE (R3 Car 0).

    The backend container mounts the shared queue volume rw at /queue-data and
    sets YADGAR_QUEUE_BASE; core mounts the SAME volume at /data. No fallback
    to YADGAR_DATA_DIR here: on the backend /data is the read-only DB mount,
    and unit tests always set YADGAR_DATA_DIR — falling back would silently
    start a drainer where none belongs. Unset → drainer disabled (gauge 0).
    """
    base = os.environ.get("YADGAR_QUEUE_BASE", "").strip()
    return Path(base) if base else None


@observe(tier="stage")
def _start_queue_drainer():
    """Construct + start the backend QueueDrainer (the R3 Car 1 write-half).

    Wiring mirrors the pre-R3 core _get_file_queue construction: FileQueue on
    the queue root + QueueDrainer(storage_factory=_get_storage) with
    drain_interval and DrainerConfig from settings. Ensures the recall engine
    stack (incl. _st._storage / _st._embeddings) is up BEFORE the first drain
    pass — ensure_write_engines and the write_exec replay impls read _st.*.

    Fail-loud: queue root missing/unwritable, or any construction error →
    ERROR log + yadgar_embed_queue_drainer_running=0 + /health drainer=false.
    Never raises — the embed/rerank service must still come up.

    Returns the started QueueDrainer, or None when disabled/failed.
    """
    base = _queue_base_path()
    if base is None:
        _drainer_running.set(0)
        logger.warning(
            "queue_drainer_disabled",
            extra={
                "event": "queue_drainer_disabled",
                "reason": "YADGAR_QUEUE_BASE unset (production backend must set it — R3 Car 0)",
            },
        )
        return None

    try:
        base.mkdir(parents=True, exist_ok=True)
        _probe = base / ".drainer-write-probe"
        _probe.write_text("1")
        _probe.unlink()
    except OSError as exc:
        _drainer_running.set(0)
        logger.error(
            "queue_drainer_start_failed",
            extra={
                "event": "queue_drainer_start_failed",
                "queue_base": str(base),
                "error": str(exc),
            },
        )
        return None

    try:
        import yadgar._shared.runtime.state as _st  # noqa: PLC0415
        from yadgar._shared.config import get_settings  # noqa: PLC0415
        from yadgar._shared.file_queue.queue import FileQueue  # noqa: PLC0415
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415
        from yadgar.backend.queue_drainer import DrainerConfig, QueueDrainer  # noqa: PLC0415

        # Engines first: replay impls + ensure_write_engines read _st._storage.
        # Module-object access so a test-time rebind on the canonical submodule
        # (setattr(es, "_ensure_recall_engines") / es._recall_engines_ready) is honoured.
        _es._ensure_recall_engines()

        settings = get_settings()
        fq = FileQueue(base, wiki_prefix=settings.WIKI_SLUG_PREFIX)
        drainer = QueueDrainer(
            fq,
            _get_storage,
            drain_interval=float(settings.QUEUE_DRAIN_INTERVAL),
            config=DrainerConfig(
                max_permanent_attempts=settings.QUEUE_MAX_PERMANENT_ATTEMPTS,
                max_transient_attempts=settings.QUEUE_MAX_TRANSIENT_ATTEMPTS,
                backoff_base_s=float(settings.QUEUE_BACKOFF_BASE_S),
                backoff_max_s=float(settings.QUEUE_BACKOFF_MAX_S),
                dlq_retention_days=settings.QUEUE_DLQ_RETENTION_DAYS,
            ),
        )
        drainer.start()
    except Exception as exc:  # noqa: BLE001 — embed/rerank must still serve
        _drainer_running.set(0)
        logger.error(
            "queue_drainer_start_failed",
            extra={
                "event": "queue_drainer_start_failed",
                "queue_base": str(base),
                "error": str(exc),
            },
        )
        return None

    _st._file_queue = fq
    _st._queue_drainer = drainer
    # Reassigned global lives in embed_service.py (read by /health + tests as
    # es._queue_drainer); write it through the module object.
    _es._queue_drainer = drainer
    _drainer_running.set(1)
    logger.info(
        "queue_drainer_started",
        extra={
            "event": "queue_drainer_started",
            "queue_base": str(base),
            "drain_interval_s": drainer._drain_interval,
        },
    )
    return drainer


@observe(tier="stage")
def _stop_queue_drainer() -> None:
    """Stop the QueueDrainer on shutdown (no-op when never started)."""
    if _es._queue_drainer is None:
        return
    try:
        _es._queue_drainer.stop()  # sets stop event + joins (5s cap)
        logger.info("queue_drainer_stopped", extra={"event": "queue_drainer_stopped"})
    except Exception as exc:  # noqa: BLE001 — shutdown must proceed
        logger.warning("queue_drainer stop error: %s", exc)
    _es._queue_drainer = None
    _drainer_running.set(0)


# Sentinel: set on first import so embed_service.py force-reloads this sibling
# when importlib.reload(embed_service) runs (keeps module refs coherent). See
# embed_service.py bottom.
_YADGAR_ES_LOADED = True
