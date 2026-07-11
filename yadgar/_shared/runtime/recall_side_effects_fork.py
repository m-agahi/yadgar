"""Async fork primitives for the recall side-effect halves (T3 Car 2).

Both inline side-effect halves of the recall response path were on the tool
latency path. This module forks each off the critical path while preserving the
must-holds: side-effects ALWAYS execute (drained on shutdown), errors are logged
(never raised into nowhere), no unbounded task pile-up (bounded → backpressure to
inline), OTEL span parentage is preserved across the fork boundary, and — for the
core session half — per-session transition ordering is kept (single FIFO worker).

Two seams, two runtimes:

  A. CORE SESSION half (``_apply_recall_session_side_effects``) runs inside the
     synchronous MCP tool body on a worker thread. It is deferred to a dedicated
     SINGLE-worker ``ThreadPoolExecutor`` (max_workers=1 → global FIFO ⊇ the
     per-session SR chain order). ``contextvars.copy_context()`` carries the OTEL
     parent span across the executor boundary (a raw ``submit`` would drop it).

  B. BACKEND DB-WRITE half (the batched ``storage.boost_memories_access`` — the
     ~407ms recall tail) runs inside the async FastAPI ``/recall`` handler. The
     in-place heat/last_accessed mutations that feed the RESPONSE stay INLINE at
     the call site (byte-identical payload); only the DB write is forked, as a
     tracked ``asyncio.create_task`` created while the request span is still
     current (``create_task`` copies the contextvars context, so the child span
     nests under the recall trace). Drained at the FastAPI writers-stop seam.

Fork is behind ``YADGAR_RECALL_SIDEEFFECT_FORK`` (default ON) — flip OFF to
restore byte-identical inline behavior. Bounds:
``YADGAR_RECALL_SIDEEFFECT_SESSION_MAX_PENDING`` (core queue depth) and
``YADGAR_RECALL_SIDEEFFECT_DB_MAX_INFLIGHT`` (backend tasks).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait
from typing import Any

from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Knob resolution (env > config.yaml > default). Lazy live-env read first so
# tests/containers can override without clearing the settings lru_cache.
# ---------------------------------------------------------------------------


def _sideeffect_fork_enabled() -> bool:
    """True when the recall side-effect fork is armed. Default ON.

    OFF = the pre-fork inline behavior (byte-identical), the one-line disarm.
    """
    return resolve_knob(
        "YADGAR_RECALL_SIDEEFFECT_FORK", "RECALL_SIDEEFFECT_FORK", _parse_bool, True
    )


def _session_max_pending() -> int:
    """Max QUEUED core session side-effects before submit backpressures to inline.

    The single FIFO worker plus this bounded queue caps memory under recall
    storms; overflow runs inline (the old correct behavior — slower, never lost).
    """
    return max(
        1,
        resolve_knob(
            "YADGAR_RECALL_SIDEEFFECT_SESSION_MAX_PENDING",
            "RECALL_SIDEEFFECT_SESSION_MAX_PENDING",
            int,
            64,
        ),
    )


def _db_max_inflight() -> int:
    """Max in-flight backend DB-write tasks before schedule refuses (inline write)."""
    return max(
        1,
        resolve_knob(
            "YADGAR_RECALL_SIDEEFFECT_DB_MAX_INFLIGHT",
            "RECALL_SIDEEFFECT_DB_MAX_INFLIGHT",
            int,
            64,
        ),
    )


# ---------------------------------------------------------------------------
# A. Core session-half fork — single FIFO worker + bounded pending queue
# ---------------------------------------------------------------------------

_SESSION_POOL: ThreadPoolExecutor | None = None
_SESSION_LOCK = threading.Lock()
# Number of side-effects submitted-but-not-yet-completed. Guarded by _PENDING_LOCK.
_pending: int = 0
_PENDING_LOCK = threading.Lock()
# In-flight worker Futures, so drain can wait with a REAL timeout bound
# (ThreadPoolExecutor.shutdown has no timeout; concurrent.futures.wait does).
# Bounded by _session_max_pending (submit backpressures over the cap), and each
# future self-removes on completion. Guarded by _PENDING_LOCK.
_session_futures: set[Future[Any]] = set()


@observe(tier="stage", span=False)
def _ensure_session_pool() -> ThreadPoolExecutor:
    """Lazily create the single-worker FIFO executor (max_workers=1 → ordering)."""
    global _SESSION_POOL
    with _SESSION_LOCK:
        if _SESSION_POOL is None:
            _SESSION_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yadgar-recall-se")
        return _SESSION_POOL


@observe(tier="stage", span=False)
def _run_and_release(call: Callable[[], Any]) -> None:
    """Worker body: run the deferred call in the captured context; log errors."""
    try:
        call()
    except Exception:  # noqa: BLE001 — a side-effect must never crash the worker
        logger.debug("recall session side-effect failed", exc_info=True)
    finally:
        global _pending
        with _PENDING_LOCK:
            _pending -= 1


@observe(tier="stage", span=False)
def submit_session_side_effect(fn: Callable[[], Any]) -> None:
    """Defer a core session side-effect off the tool-response thread.

    Disabled → run inline (byte-identical). Enabled → submit to the single FIFO
    worker inside a copied contextvars context (OTEL parentage preserved). Over
    the pending cap → run inline (backpressure; never an unbounded pile-up).
    Never raises: a failing side-effect is logged, not propagated.
    """
    if not _sideeffect_fork_enabled():
        _safe_inline(fn)
        return

    global _pending
    with _PENDING_LOCK:
        if _pending >= _session_max_pending():
            # Bounded: overflow runs inline so the queue cannot grow unbounded.
            overflow = True
        else:
            _pending += 1
            overflow = False

    if overflow:
        _safe_inline(fn)
        return

    ctx = contextvars.copy_context()
    try:
        fut = _ensure_session_pool().submit(_run_and_release, lambda: ctx.run(fn))
    except RuntimeError:
        # Pool shut down mid-submit → run inline and undo the reservation.
        with _PENDING_LOCK:
            _pending -= 1
        _safe_inline(fn)
        return
    with _PENDING_LOCK:
        _session_futures.add(fut)
    fut.add_done_callback(_discard_session_future)


@observe(tier="stage", span=False)
def _discard_session_future(fut: Future[Any]) -> None:
    """Drop a completed worker future from the drain-tracking set."""
    with _PENDING_LOCK:
        _session_futures.discard(fut)


@observe(tier="stage", span=False)
def _safe_inline(fn: Callable[[], Any]) -> None:
    """Run a side-effect inline, swallowing + logging any error."""
    try:
        fn()
    except Exception:  # noqa: BLE001
        logger.debug("recall session side-effect (inline) failed", exc_info=True)


@observe(tier="stage")
def drain_session_side_effects(timeout: float = 10.0) -> None:
    """Block (bounded) until pending core session side-effects complete (shutdown seam).

    Called from ``lifecycle.shutdown`` BEFORE ``_buffer.flush()`` + ``storage.close()``
    so no SR-transition write or buffered action-capture is lost. The ``timeout``
    is a REAL bound: ``ThreadPoolExecutor.shutdown`` has no timeout arg, so we
    ``concurrent.futures.wait`` on the tracked worker futures with ``timeout``
    instead. A wedged side-effect therefore cannot hang graceful stop past the
    systemd stop-timeout; the pool is then closed non-blocking (``wait=False``).
    """
    global _SESSION_POOL
    with _PENDING_LOCK:
        pending = list(_session_futures)
    if pending:
        done, not_done = _futures_wait(pending, timeout=timeout)
        if not_done:
            logger.warning(
                "recall session side-effect drain timed out; %d worker(s) still running",
                len(not_done),
            )
    with _SESSION_LOCK:
        pool = _SESSION_POOL
        _SESSION_POOL = None
    if pool is not None:
        # cancel_futures drops any still-queued work; wait=False so a wedged
        # (already-running) side-effect cannot block graceful stop — the bounded
        # _futures_wait above already gave in-flight work its drain window.
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # pragma: no cover — py<3.9 has no cancel_futures
            pool.shutdown(wait=False)


@observe(tier="stage", span=False)
def reset_session_executor() -> None:
    """Test hook: drop the session pool + pending counter. Idempotent."""
    global _SESSION_POOL, _pending
    with _SESSION_LOCK:
        pool = _SESSION_POOL
        _SESSION_POOL = None
    if pool is not None:
        pool.shutdown(wait=True)
    with _PENDING_LOCK:
        _pending = 0
        _session_futures.clear()


# ---------------------------------------------------------------------------
# B. Backend DB-write fork — tracked asyncio tasks + bounded in-flight
# ---------------------------------------------------------------------------

_DB_TASKS: set[asyncio.Task[Any]] = set()


@observe(tier="stage", span=False)
async def _run_db_write(coro: Coroutine[Any, Any, Any]) -> None:
    """Await a forked DB-write coroutine, logging (never raising) on failure."""
    try:
        await coro
    except Exception:  # noqa: BLE001 — a forked write must not crash the loop
        logger.debug("recall DB side-effect (forked) failed", exc_info=True)


@observe(tier="stage", span=False)
def schedule_db_write(coro: Coroutine[Any, Any, Any]) -> bool:
    """Fork a backend DB-write coroutine as a tracked task off the response path.

    MUST be called while the request span is current so ``create_task`` copies
    the OTEL context and the child span nests under the recall trace.

    Returns True when the write was forked (caller must NOT run it inline);
    False when the fork is disabled OR the in-flight cap is hit (caller keeps the
    inline write — backpressure). On False the caller owns ``coro`` (this
    function does not consume it).
    """
    if not _sideeffect_fork_enabled():
        return False
    if len(_DB_TASKS) >= _db_max_inflight():
        # Bounded: over the cap, refuse so the caller runs the write inline.
        return False
    task = asyncio.create_task(_run_db_write(coro))
    _DB_TASKS.add(task)
    task.add_done_callback(_DB_TASKS.discard)
    return True


def pending_db_tasks() -> int:
    """Number of forked DB-write tasks still in flight."""
    return len(_DB_TASKS)


@observe(tier="stage")
async def drain_db_tasks(timeout: float = 10.0) -> None:
    """Await all forked DB-write tasks (FastAPI writers-stop seam).

    Called in the lifespan teardown BEFORE the queue drainer / surreal stop so
    every forked heat/thermo write lands before the store closes. Bounded by
    ``timeout``; on overrun the remaining tasks are cancelled (best-effort — the
    write is an idempotent boost, and the stop-timeout must be honored).
    """
    pending = list(_DB_TASKS)
    if not pending:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
    except TimeoutError:
        logger.warning("recall DB side-effect drain timed out; cancelling %d task(s)", len(pending))
        for t in pending:
            t.cancel()


@observe(tier="stage", span=False)
def reset_db_tasks() -> None:
    """Test hook: cancel + drop all tracked DB tasks. Idempotent."""
    for t in list(_DB_TASKS):
        t.cancel()
    _DB_TASKS.clear()
