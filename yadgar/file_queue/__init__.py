"""File-based write queue for durable, non-blocking MCP writes.

Write flow:
  1. Caller writes to queue/ (atomic rename, fast)
  2. Returns success immediately
  3. Background QueueDrainer flushes queue/ -> DB
  4. Confirmed writes move to archive/memories/YYYY-MM-DD/

Directory layout under base_dir (default YADGAR_DATA_DIR or /data in Docker):
  queue/                          — pending writes not yet confirmed by DB
  archive/memories/YYYY-MM-DD/   — queue ops confirmed, kept 30 days then pruned
  archive/wiki/                   — always-current wiki .md mirrors
  dlq/                            — files that exhausted retries; .error.json sidecars
"""

from __future__ import annotations

import contextlib
import json
import logging
import re as _re
import threading
import time
from dataclasses import dataclass

from yadgar.file_queue._locals import _drain_local
from yadgar.file_queue.apply import _ApplyMixin
from yadgar.file_queue.dlq import _DLQMixin
from yadgar.file_queue.queue import FileQueue
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)


def _drainer_span():
    """Context manager: OTel root span per drain iteration (I21)."""
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        return _ot.get_tracer("yadgar.file_queue").start_as_current_span("drainer.cycle")
    except Exception:
        return contextlib.nullcontext()


_DRAIN_INTERVAL = 30.0  # seconds between drain passes (configurable via QueueDrainer)
_CLEANUP_EVERY = 120  # drain passes between archive cleanups (~1 hour at 30s interval)


def is_draining() -> bool:
    """Return True if the current thread is inside a QueueDrainer._apply() call."""
    return getattr(_drain_local, "active", False)


def _classify_error(err_str: str) -> str:
    """Classify an error string as 'permanent' (HTTP 4xx) or 'transient' (everything else)."""
    if _re.search(r"\b4\d\d\b", err_str):
        return "permanent"
    return "transient"


@dataclass
class _Attempt:
    """Per-file in-memory retry state. Resets on container restart (acceptable: thresholds are tight
    enough that from-scratch counting cannot spin a CPU core for a meaningful duration)."""

    count: int = 0
    next_retry_at: float = 0.0  # epoch seconds; skip file until this time passes
    last_error: str = ""
    first_failed_at: float = 0.0
    classification: str = "transient"  # set on first failure; "permanent" or "transient"


__all__ = [
    "FileQueue",
    "QueueDrainer",
    "is_draining",
    "_drain_local",
    "_Attempt",
    "_classify_error",
]


class QueueDrainer(_DLQMixin, _ApplyMixin, threading.Thread):
    """Background thread: drain FileQueue -> StorageEngine via tool replay."""

    def __init__(
        self,
        queue: FileQueue,
        storage_factory,
        drain_interval: float = _DRAIN_INTERVAL,
        max_permanent_attempts: int = 3,
        max_transient_attempts: int = 20,
        backoff_base_s: float = 30.0,
        backoff_max_s: float = 3600.0,
        dlq_retention_days: int = 90,
    ) -> None:
        super().__init__(daemon=True, name="yadgar-queue-drainer")
        self._queue = queue
        self._storage_factory = storage_factory  # callable -> StorageEngine
        self._stop_event = threading.Event()
        self._drain_count = 0
        self._drain_interval = drain_interval
        self._max_permanent = max_permanent_attempts
        self._max_transient = max_transient_attempts
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._dlq_retention_days = dlq_retention_days
        # In-memory per-file retry state; keyed by filename.
        # Resets on container restart — acceptable because thresholds are tight enough that
        # even from-scratch counting cannot sustain meaningful DB CPU for long.
        self._attempts: dict[str, _Attempt] = {}

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with _drainer_span():
                    self._drain_once()
            except Exception as exc:
                logger.warning("Queue drain error: %s", exc)
            self._stop_event.wait(timeout=self._drain_interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)

    def drain_now(self) -> int:
        """Force an immediate drain pass. Returns number of items processed."""
        return self._drain_once()

    def reset_attempt(self, filename: str) -> None:
        """Clear retry state for a file (called by dlq_requeue after moving back to queue)."""
        self._attempts.pop(filename, None)

    @trace_span("drainer.drain_cycle")
    def _drain_once(self) -> int:  # noqa: C901 — pre-existing complexity, tracked for P13 refactor
        _cycle_t0 = time.monotonic()
        files = self._queue.pending()
        processed = 0
        now = time.time()
        logger.info("Queue drain pass: %d pending files", len(files))

        # P11: update queue/dlq depth gauges
        try:
            from yadgar.metrics import yadgar_dlq_size, yadgar_queue_depth  # noqa: PLC0415

            yadgar_queue_depth.labels(queue="queue").set(len(files))
            dlq_count = sum(
                1
                for _f in self._queue.dlq_dir.iterdir()
                if _f.suffix == ".json" and not _f.name.endswith(".error.json")
            )
            yadgar_dlq_size.set(dlq_count)
            yadgar_queue_depth.labels(queue="dlq").set(dlq_count)
        except Exception:
            pass

        if files:
            for path in files:
                fname = path.name
                attempt = self._attempts.get(fname, _Attempt())

                # Respect backoff window
                if attempt.count > 0 and now < attempt.next_retry_at:
                    continue

                op_type = "unknown"
                try:
                    data = json.loads(path.read_text())
                    op_type = data.get("op", "unknown")
                except Exception as exc:
                    # Parse error: can never succeed → treat as permanent
                    self._record_failure(attempt, str(exc)[:500], "permanent", now)
                    self._attempts[fname] = attempt
                    logger.warning("Failed to parse %s (attempt %d): %s", fname, attempt.count, exc)
                    if attempt.count >= self._max_permanent:
                        self._move_to_dlq(path, attempt, op_type)
                        self._attempts.pop(fname, None)
                    continue

                # §26 Option Z — wiki_add validation before apply
                if op_type == "wiki_add":
                    reject_reason = self._validate_wiki_add(data)
                    if reject_reason:
                        attempt.count = self._max_permanent  # treat as permanent failure
                        attempt.last_error = reject_reason
                        attempt.classification = "permanent"
                        attempt.first_failed_at = now
                        self._move_to_dlq(path, attempt, op_type)
                        self._attempts.pop(fname, None)
                        logger.warning("wiki_add rejected (DLQ): %s — %s", fname, reject_reason)
                        continue

                try:
                    # P11: observe drainer lag (enqueue_ts -> drain start)
                    try:
                        from yadgar.metrics import yadgar_drainer_lag_ms  # noqa: PLC0415

                        enqueue_ts = data.get("ts", now)
                        yadgar_drainer_lag_ms.observe((now - enqueue_ts) * 1000)
                    except Exception:
                        pass
                    self._apply(data)
                    self._attempts.pop(fname, None)
                    self._queue.archive(path)
                    processed += 1
                except Exception as exc:
                    err_str = str(exc)
                    classification = _classify_error(err_str)
                    max_attempts = (
                        self._max_permanent
                        if classification == "permanent"
                        else self._max_transient
                    )
                    self._record_failure(attempt, err_str[:500], classification, now)
                    self._attempts[fname] = attempt
                    logger.warning(
                        "Failed to drain %s (attempt %d, %s): %s",
                        fname,
                        attempt.count,
                        classification,
                        err_str[:200],
                    )
                    if attempt.count >= max_attempts:
                        self._move_to_dlq(path, attempt, op_type)
                        self._attempts.pop(fname, None)

        _cycle_ms = round((time.monotonic() - _cycle_t0) * 1000, 1)
        logger.info(
            "drain_cycle_complete",
            extra={
                "component": "drainer",
                "action": "drain_cycle",
                "outcome": "ok",
                "processed": processed,
                "pending": len(files),
                "latency_ms": _cycle_ms,
            },
        )

        # P11: observe drain cycle duration
        try:
            from yadgar.metrics import yadgar_drain_cycle_duration_ms  # noqa: PLC0415

            yadgar_drain_cycle_duration_ms.observe(_cycle_ms)
        except Exception:
            pass

        # Periodic archive + DLQ cleanup (roughly once per hour)
        self._drain_count += 1
        if self._drain_count % _CLEANUP_EVERY == 0:
            deleted = self._queue.cleanup_archive()
            if deleted:
                logger.debug("Archive cleanup: removed %d old files", deleted)
            dlq_deleted = self._queue.cleanup_dlq(self._dlq_retention_days)
            if dlq_deleted:
                logger.info(
                    "DLQ cleanup: %d entries deleted after %d+ days — data permanently lost",
                    dlq_deleted,
                    self._dlq_retention_days,
                )

        return processed

    def _record_failure(
        self, attempt: _Attempt, err_str: str, classification: str, now: float
    ) -> None:
        """Increment attempt counter, set first_failed_at on first call, compute next backoff."""
        if attempt.count == 0:
            attempt.first_failed_at = now
            attempt.classification = classification
        attempt.count += 1
        attempt.last_error = err_str
        attempt.next_retry_at = now + min(
            self._backoff_max, self._backoff_base * (2.0 ** (attempt.count - 1))
        )
