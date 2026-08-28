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

import asyncio
import logging
import os
import threading
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


@observe(tier="stage")
def _run_layout_bootstrap(storage, settings, precompute=None) -> None:
    """Compute + cache the graph layout ONCE if the cache is empty (synchronous).

    viz-render-perf (Car A): the precompute knob was removed, so a fresh deploy
    would otherwise have no cached positions until the first nightly/full cycle —
    the first viz load would pay the slow client cold layout. This warms the cache
    on boot when it is empty. No-op when a cache row already exists. Non-fatal:
    any error is logged and swallowed (the embed/rerank service must still serve).

    The precompute callable is injected for testing; production uses
    ``_maybe_precompute_graph_layout`` (same non-fatal wrapper + signature no-op).
    """
    try:
        if storage.get_graph_layout_cache():
            return  # already warm — nothing to do
        if precompute is None:
            from yadgar._shared.config import get_settings  # noqa: PLC0415
            from yadgar.backend.consolidation.service import (  # noqa: PLC0415
                _maybe_precompute_graph_layout,
            )

            precompute = _maybe_precompute_graph_layout
            settings = settings if settings is not None else get_settings()
        logger.info("graph_layout_bootstrap: empty cache — computing initial layout")
        precompute(storage, settings)
    except Exception as exc:  # noqa: BLE001 — bootstrap is best-effort, non-fatal
        logger.warning("graph_layout_bootstrap failed (non-fatal): %s", exc)


def _bootstrap_graph_layout_if_empty(storage) -> None:
    """Kick the layout-cache bootstrap in a background daemon thread (non-blocking).

    Called from the FastAPI ``lifespan`` after the recall engines + storage are up.
    Threaded so the (potentially multi-second) spring-layout compute never delays
    backend readiness. Daemon so it never blocks shutdown.
    """
    threading.Thread(
        target=_run_layout_bootstrap,
        args=(storage, None),
        name="graph-layout-bootstrap",
        daemon=True,
    ).start()


@observe(tier="stage")
async def _cancel_lifespan_task(task) -> None:
    """Cancel a lifespan background task and swallow whatever it raises.

    The snapshot and warmup tasks were cancelled by two byte-identical five-line
    blocks inline in ``lifespan``. Folded into one helper because ``lifespan``
    was at 149 of the I30 hard 150-line cap, leaving no room for the engine-#2
    steps below — the same reason ``_shutdown_tracing_bounded`` was extracted
    from it earlier. No behaviour change: cancel, await, swallow.
    """
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — cancel-and-await teardown: CancelledError is a BaseException so the tuple is not redundant; the awaited task is arbitrary caller code and shutdown must proceed regardless of what it raised  # fmt: skip
        pass


@observe(tier="stage")
async def _migrate_engine_two() -> str | None:
    """``alembic upgrade head`` on engine #2 at backend boot (car D).

    THE NAMED CALLER for the Alembic chain, mirroring how SurrealDB's schema
    init is wired: ``StorageEngine.__init__`` calls ``_init_schema``
    (``_shared/storage/__init__.py:292``) the moment its connection exists. Same
    shape here, one step later — engine #2 is composed inside
    ``_ensure_recall_engines`` (``init_engines(sql_storage=True)``), which the
    lifespan already awaits via ``_start_queue_drainer``, so this runs directly
    after that and before the app reports ready.

    WHY NOT NEXT TO THE CONSTRUCTION, THE WAY SURREAL DOES IT. ``init_engines``
    is sync and runs in a worker thread, while alembic here must drive an ASYNC
    connection. Migrating there would need a private event loop, and
    ``AsyncAdaptedQueuePool`` would then cache a connection bound to a loop that
    dies with the thread — the exact hazard car C kept construction
    connectionless to avoid. The lifespan is on the real loop.

    FATAL — the precondition this docstring used to carry as a warning. It read
    "THE MOMENT THE KNOB TRAIN REPOINTS READS THIS MUST BECOME FATAL, or the
    daemon serves defaults from a schema-less database", and cars D/F/G/I/K of
    PR #40 repointed exactly those reads. So a migration that RAN and failed now
    propagates: the error is still logged with its traceback first (PR #32's
    review flagged the silently-swallowed version), and then it is re-raised so
    boot stops instead of continuing onto a database with no tables. ADR-0222
    measured what the swallow buys — logged as an error, health check green,
    systemd active, the daemon running BROKEN.

    ABSENT IS NOT FAILED, and the distinction is deliberate. Every host without
    MariaDB composes no engine #2 (``_init_sql_storage`` degrades to None), and
    boot there is correct; only a migration that ran and raised is fatal.

    WHICH ACCOUNT IT CONNECTS AS. Not the runtime one. ``sql_storage.engine``
    authenticates as the app account, whose grant is per-table
    SELECT/INSERT/UPDATE/DELETE/REFERENCES with no CREATE/ALTER/INDEX/DROP
    (D19), so driving the chain through it dies on 002's first
    ``op.create_table`` with ``(1142, "CREATE command denied to user
    'yadgar_app'@'localhost' for table `yadgar`.`task`")``. When the
    entrypoint's migration option file is present the chain runs on a
    throwaway engine built from THOSE credentials and disposed immediately, so
    no DDL-capable connection outlives it. The runtime engine is never widened.

    THE FOUR BOOT CASES, all of them intended:

    ===================  ============  =======  =====================================
    engine #2            migrate.cnf   at head  outcome
    ===================  ============  =======  =====================================
    absent               —             —        skip, INFO (absent is not failed)
    present              present       —        migrate as the migration account
    present              absent        yes      no-op — alembic only READS
                                                ``alembic_version``, which the app
                                                account may do
    present              absent        no       FATAL — there is no credential here
                                                that can create a table, and
                                                pretending otherwise is how a
                                                schema-less database gets served
    ===================  ============  =======  =====================================

    Returns:
        The head revision id, or None when engine #2 is absent.

    Raises:
        Exception: whatever ``upgrade_to_head`` raised, re-raised unchanged.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    sql_storage = _get_sql_storage()
    if sql_storage is None:
        logger.info("engine #2 absent — skipping alembic upgrade head")
        return None

    try:
        from yadgar._shared.storage.sql.config import (  # noqa: PLC0415
            default_migrate_option_file_path,
        )
        from yadgar._shared.storage.sql.migrate import (  # noqa: PLC0415
            describe_dbapi_error,
            upgrade_to_head,
            upgrade_to_head_as_migrator,
        )

        migrate_cnf = default_migrate_option_file_path()
        if migrate_cnf.is_file():
            head = await upgrade_to_head_as_migrator(migrate_cnf)
        else:
            # No migration credentials on this host. Alembic still runs, but
            # the only thing the app account can do is read the stamp — which
            # is exactly right when the schema is already at head, and fails
            # loudly (1142) when it is not.
            logger.info(
                "engine #2 migration credentials absent at %s — "
                "checking the stamp with the runtime account",
                migrate_cnf,
            )
            head = await upgrade_to_head(sql_storage.engine)
    except Exception as exc:
        # ``describe_dbapi_error`` puts the driver's errno + message into the
        # record's own fields. The traceback alone did not: it is truncated at
        # TRACEBACK_MAX_CHARS from the FRONT, and the DBAPI message is the last
        # line — so the one string identifying the failure was the one string
        # cut off, and every failure read as a bare "OperationalError".
        logger.exception(
            "engine #2 migration FAILED — the relational schema is not at head",
            extra={
                "component": "engine_two",
                "action": "alembic_upgrade",
                "outcome": "error",
                **describe_dbapi_error(exc),
            },
        )
        raise

    logger.info("engine #2 migrated to alembic head %s", head)
    return head


@observe(tier="stage")
async def _dispose_engine_two() -> None:
    """Release engine #2's connection pool on shutdown (car C's flagged gap).

    HERE AND NOT IN ``lifecycle.shutdown``. That function is SYNC while
    ``MariaStorageEngine.dispose`` is a coroutine, so disposing there would need
    ``asyncio.run`` — a private event loop, tearing down a pool whose
    connections belong to the server loop. This is the same reason car C kept
    construction connectionless, and it is why the correct point is the lifespan
    teardown, which is already awaiting (``_drain_db_tasks`` two steps up).

    Called after ``_stop_queue_drainer`` so writers are down first, and never
    raises: shutdown must proceed.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    sql_storage = _get_sql_storage()
    if sql_storage is None:
        return
    try:
        await sql_storage.dispose()
    except Exception as _exc:  # noqa: BLE001 — shutdown must proceed
        logger.warning("engine #2 dispose failed: %s", _exc)


# Sentinel: set on first import so embed_service.py force-reloads this sibling
# when importlib.reload(embed_service) runs (keeps module refs coherent). See
# embed_service.py bottom.
_YADGAR_ES_LOADED = True
