"""Core-side lifecycle: file-queue accessor, sd_notify emit, shutdown wrapper,
and the SIGINT/SIGTERM signal handler (R2a Car D2).

These four concerns used to live in ``yadgar._shared.runtime.lifecycle`` where
each imported a ``yadgar.core.*`` module (``core.file_queue``, ``core.sd_notify``,
``core.drain``, ``core.sensitive_lock``) — the LAST four ``_shared → core``
layering violations. They moved HERE so those imports become ``core → core``
in-layer edges and ``_shared`` imports ZERO core.

The shutdown SPLIT (the delicate one): ``_shared.runtime.lifecycle.shutdown`` now
does ONLY the shared teardown and takes two OPTIONAL callbacks
(``on_stopping`` / ``snapshot_caches``) that it invokes at the EXACT positions the
inline ``core.sd_notify.stopping()`` / ``core.drain.snapshot_embed_caches()`` calls
occupied before. This ``core.lifecycle.shutdown`` wrapper injects the real core
callbacks. Because the ``_st._shutdown_done`` idempotency guard stays INSIDE the
shared ``shutdown``, ``on_stopping`` fires exactly once across the double-call path
(``main()`` ``finally`` + signal handler both call shutdown) — order and
once-semantics are preserved by construction (no reorder vs. the pre-D2 inline
sequence).

Callers repoint here via the ``yadgar.core.server`` re-export shim so the ~90
``server.X`` callers/tests are unchanged.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import shutdown as _shared_shutdown

logger = logging.getLogger(__name__)


# ── file queue accessor + init (was _shared.lifecycle._get_file_queue) ──


@observe(tier="stage")
def _get_file_queue():
    """Return the process-wide FileQueue, building it on first use (enqueue side).

    R3 Car 1 (write-half): core owns ONLY the FileQueue (the enqueue endpoint).
    The QueueDrainer (drain-replay) is a backend concern started by the backend
    lifecycle half — it assigns _st._queue_drainer. Core no longer constructs or
    starts the drainer here; that removes the core → drainer construction edge on
    the enqueue path.
    """
    if _st._file_queue is None:
        with _st._queue_lock:
            if _st._file_queue is None:
                from pathlib import Path  # noqa: PLC0415

                from yadgar._shared.file_queue.queue import FileQueue

                _settings = get_settings()
                base = Path(os.environ.get("YADGAR_DATA_DIR", _settings.DATA_DIR))
                fq = FileQueue(base, wiki_prefix=_settings.WIKI_SLUG_PREFIX)
                _st._file_queue = fq
                # Sync back to the server module's __dict__ so tests that
                # monkeypatch.setattr(server, "_file_queue", None) and then call
                # _get_file_queue() see the live object instead of stale None.
                import sys as _sys  # noqa: PLC0415

                _srv = _sys.modules.get("yadgar.core.server")
                if _srv is not None:
                    # Use setattr so _ServerModule.__setattr__ keeps both
                    # server.__dict__ and _state.__dict__ in sync.
                    _srv._file_queue = fq
    return _st._file_queue


@observe(tier="stage")
def _init_file_queue() -> None:
    """Start the file queue drainer; non-fatal on failure.

    Extracted from init_engines to reduce its cyclomatic complexity.
    """
    try:
        _get_file_queue()
    except Exception as exc:
        logger.warning("File queue init failed (non-fatal): %s", exc)


# ── sd_notify READY=1 emit (was _shared.lifecycle._emit_sd_ready) ───────


@observe(tier="stage")
def _emit_sd_ready() -> None:
    """v5.49.4: emit READY=1 via sd_notify after init_engines() completes.

    Extracted to keep init_engines() under the I13 cyclomatic-complexity cap (≤15).
    Silent no-op when NOTIFY_SOCKET is unset (outside systemd / container surrogate).
    """
    try:
        from yadgar.core.daemon import sd_notify as _sd_notify  # noqa: PLC0415

        _sd_notify.ready()
    except Exception:  # noqa: BLE001
        pass


# ── shutdown wrapper: inject the two core callbacks at exact positions ──


@observe(tier="stage")
def _emit_sd_stopping() -> None:
    """sd_notify STOPPING=1 callback injected into the shared shutdown (position 1)."""
    try:
        from yadgar.core.daemon import sd_notify as _sd_notify  # noqa: PLC0415

        _sd_notify.stopping()
    except Exception:  # noqa: BLE001
        pass


@observe(tier="stage")
def _snapshot_embed_caches() -> None:
    """snapshot_embed_caches callback injected into the shared shutdown (post buffer.flush)."""
    try:
        from yadgar.core.daemon.drain import snapshot_embed_caches as _snap  # noqa: PLC0415

        _snap()
    except Exception:  # noqa: BLE001
        pass


@observe(tier="boundary")
def shutdown():
    """Core-side graceful shutdown wrapper (R2a Car D2).

    Delegates to the shared ``lifecycle.shutdown`` and injects the two core
    callbacks it now accepts:

      * ``on_stopping``     → ``_emit_sd_stopping`` (``core.sd_notify.stopping()``) —
        fired at the SAME position (very first step, before pool teardown) the
        inline call held.
      * ``snapshot_caches`` → ``_snapshot_embed_caches``
        (``core.drain.snapshot_embed_caches()``) — fired at the SAME position (after
        buffer.flush, before storage.close) the inline call held.

    The ``_st._shutdown_done`` guard lives inside the shared shutdown, so both
    callbacks fire exactly once across the double-call path (main() finally +
    signal handler). Order is byte-identical to the pre-D2 inline sequence.
    """
    _shared_shutdown(on_stopping=_emit_sd_stopping, snapshot_caches=_snapshot_embed_caches)


# ── sensitive-lock drain + signal handler (were in _shared.lifecycle) ───

_SENSITIVE_DRAIN_POLL_SEC = 0.05  # poll interval while draining (models drain.py)


@observe(tier="stage")
def _drain_sensitive_lock(timeout: float) -> bool:
    """Bounded synchronous wait for an in-process sensitive job to release its lock.

    Models ``yadgar.drain.drain_in_flight_requests`` (poll-until-clear with a
    deadline) but synchronous — the signal handler runs in the main thread, not an
    event loop.  Returns True if the lock cleared (released or became stale) before
    the timeout, False on timeout.  NEVER shuts down on timeout — the caller
    REFUSES the shutdown instead, so a still-running swap is never interrupted.
    """
    from yadgar.core import sensitive_lock  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    while sensitive_lock.is_held_by_live_job():
        if time.monotonic() >= deadline:
            logger.warning(
                "sensitive-job drain timed out after %.1fs — REFUSING shutdown "
                "(job still holds the lock; will not interrupt mid-swap)",
                timeout,
            )
            return False
        time.sleep(_SENSITIVE_DRAIN_POLL_SEC)
    logger.info("sensitive-job lock cleared — proceeding with shutdown")
    return True


@observe(tier="stage")
def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown.

    v5.69 P3 — sensitive-job drain:  if a sensitive job (vacuum) holds the lock,
    an EXTERNAL shutdown signal must NOT interrupt it mid-swap (the 06-16
    data-loss mode).  We distinguish the vacuum's OWN teardown from an external
    operator stop via the lock's pid:

      * lock held by a LIVE job whose ``pid == os.getpid()``  → the sensitive job
        runs in THIS process; an external signal targeting it would interrupt the
        swap → DRAIN (bounded) and only shut down once the lock clears; on timeout
        REFUSE (return without shutting down).
      * lock held by a LIVE job whose ``pid != os.getpid()``  → the job runs in a
        SEPARATE process (the vacuum runs as ``yadgar-vacuum.service``, not inside
        core).  That separate vacuum stops core via ``ServiceController.stop()``
        (systemctl stop yadgar yadgar-backend), which delivers core THIS same
        SIGTERM.  We must let that teardown proceed — blocking it would deadlock
        the vacuum's own stop → SIGKILL.  So we PROCEED (immediate shutdown).
      * no lock, or a STALE lock (dead pid / TTL-expired)     → behave exactly as
        before P3: immediate shutdown.

    DOCUMENTED RESIDUAL RACE (narrow, accepted for 5.69):  an EXTERNAL operator
    ``systemctl stop yadgar`` arriving at core WHILE a separate-process vacuum
    holds the lock is indistinguishable here from the vacuum's own
    ``ServiceController.stop()`` — both are a SIGTERM to core with the lock pid !=
    core's pid — so we proceed and core shuts down.  This is acceptable because
    the vacuum's atomic-swap design (P2) never leaves the canonical empty/partial
    even if core dies (the swap is gated behind a verified side-build; crash
    mid-swap is recovered at next start).  The clean fix is systemd
    ``RefuseManualStop`` on yadgar.service — out of scope for 5.69, tracked as a
    follow-up.
    """
    logger.info("Received signal %s", signum)
    try:
        from yadgar.core import sensitive_lock  # noqa: PLC0415

        payload = sensitive_lock.read()
        if sensitive_lock.is_held_by_live_job():
            in_process = (payload or {}).get("pid") == os.getpid()
            if in_process:
                # External stop targeting THIS process while it runs a sensitive
                # job → drain before shutting down; refuse on timeout.
                _settings = get_settings()
                timeout = float(getattr(_settings, "SENSITIVE_DRAIN_TIMEOUT_SEC", 300.0))
                logger.warning(
                    "signal %s arrived while an in-process sensitive job (job=%s) "
                    "holds the lock — draining up to %.1fs before shutdown",
                    signum,
                    (payload or {}).get("job"),
                    timeout,
                )
                if not _drain_sensitive_lock(timeout):
                    # REFUSED: do not shut down, do not exit — never interrupt
                    # the swap.  systemd will eventually SIGKILL if it must, but
                    # we will not voluntarily empty the store mid-vacuum.
                    return
            else:
                # Separate-process job holds the lock (vacuum stopping core) —
                # proceed so the vacuum's own teardown is not deadlocked.
                logger.info(
                    "signal %s while a separate-process sensitive job (pid=%s) holds "
                    "the lock — proceeding (vacuum-initiated stop is authorized)",
                    signum,
                    (payload or {}).get("pid"),
                )
    except Exception:  # noqa: BLE001 — never let lock logic block a real shutdown
        logger.debug("sensitive-lock check in signal handler failed (non-fatal)", exc_info=True)

    logger.info("shutting down (signal %s)", signum)
    shutdown()
    sys.exit(0)
