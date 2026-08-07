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
import os
import re as _re
import threading
import time
from dataclasses import dataclass

from yadgar._shared.file_queue.queue import FileQueue
from yadgar._shared.observability.observe import observe
from yadgar.backend.queue_drainer._locals import _drain_local
from yadgar.backend.queue_drainer.apply import _ApplyMixin
from yadgar.backend.queue_drainer.dlq import _DLQMixin

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

# PR-I: loop telemetry helpers (module-level to avoid adding nesting depth in run())


def _drainer_heartbeat() -> None:
    try:
        from yadgar._shared.observability.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat("queue_drainer")
    except Exception:  # noqa: BLE001
        pass


def _drainer_record_exc(exc: BaseException) -> None:
    try:
        from yadgar._shared.observability.metrics import loop_record_exception  # noqa: PLC0415

        loop_record_exception("queue_drainer", exc)
    except Exception:  # noqa: BLE001
        pass


def is_draining() -> bool:
    """Return True if the current thread is inside a QueueDrainer._apply() call."""
    return getattr(_drain_local, "active", False)


def _classify_error(err_str: str) -> str:
    """Classify an error string as 'permanent' (HTTP 4xx) or 'transient' (everything else).

    v5.10.2: SecretLeakBlocked is always permanent — retrying will never help.
    Car C (#83): slug_exists is permanent — the slug already exists and retrying
    a upsert=False write will always fail.
    """
    if "SecretLeakBlocked" in err_str:
        return "permanent"
    if "slug_exists:" in err_str:
        return "permanent"
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


@dataclass
class DrainerConfig:
    """Bundled retry and DLQ policy for QueueDrainer.

    Extracted from QueueDrainer.__init__ to keep the constructor under the
    param cap (v5.55 complexity campaign, YELLOW tier).  All fields carry the
    same defaults that were previously inline on the constructor.
    """

    max_permanent_attempts: int = 3
    max_transient_attempts: int = 20
    backoff_base_s: float = 30.0
    backoff_max_s: float = 3600.0
    dlq_retention_days: int = 90


__all__ = [
    "FileQueue",
    "QueueDrainer",
    "DrainerConfig",
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
        config: DrainerConfig | None = None,
    ) -> None:
        super().__init__(daemon=True, name="yadgar-queue-drainer")
        _cfg = config if config is not None else DrainerConfig()
        self._queue = queue
        self._storage_factory = storage_factory  # callable -> StorageEngine
        self._stop_event = threading.Event()
        self._drain_count = 0
        self._drain_interval = drain_interval
        self._max_permanent = _cfg.max_permanent_attempts
        self._max_transient = _cfg.max_transient_attempts
        self._backoff_base = _cfg.backoff_base_s
        self._backoff_max = _cfg.backoff_max_s
        self._dlq_retention_days = _cfg.dlq_retention_days
        # In-memory per-file retry state; keyed by filename.
        # Resets on container restart — acceptable because thresholds are tight enough that
        # even from-scratch counting cannot sustain meaningful DB CPU for long.
        self._attempts: dict[str, _Attempt] = {}
        # Serializes _drain_once: the background run() loop and a synchronous
        # drain_now() must never execute a pass concurrently. Without this, two
        # passes read the same pending() file list and race — one applies and
        # removes a file while the other finds it gone ("file theft"), so
        # drain_now() can return before the queued write is durable and a caller
        # that reads storage immediately sees NOT-FOUND. This is the CI flake #53
        # root cause (test_memory_behavior / test_project_brief_modes under
        # -n auto). flush_barrier already documents the same intent of avoiding
        # concurrent _drain_once; the lock enforces it for drain_now(). Non-
        # reentrant: nothing calls _drain_once recursively (flush_barrier only
        # polls pending(); _process_pending_file does not recurse) → no deadlock.
        self._drain_lock = threading.Lock()

    def run(self) -> None:
        while not self._stop_event.is_set():
            _drainer_heartbeat()  # PR-I: heartbeat at top of every iteration
            try:
                with _drainer_span():
                    self._drain_once()
            except Exception as exc:
                from yadgar._shared.observability.exception_telemetry import (
                    record_exception,  # noqa: PLC0415
                )

                record_exception("file_queue.drainer", exc)
                _drainer_record_exc(exc)  # PR-I: loop error counter
                logger.warning("Queue drain error: %s", exc)
            self._stop_event.wait(timeout=self._drain_interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)

    @observe(tier="stage", metric="drainer.flush_barrier")
    def flush_barrier(self, timeout: float) -> bool:
        """Block until in-memory queue is drained to storage or timeout expires.

        Returns True if drained cleanly, False if timed out. Caller must still
        call stop() to terminate the drainer thread; flush_barrier guarantees
        the IN-PROGRESS items have been applied.

        Implementation: polls queue depth (checking pending files) at
        50ms intervals. The background drainer thread continues to run and
        process items. flush_barrier does NOT call _drain_once itself to
        avoid concurrent access with the running drainer thread.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._queue.pending():
                return True
            time.sleep(0.05)
        # Final check after timeout
        return not bool(self._queue.pending())

    def drain_now(self) -> int:
        """Force an immediate drain pass. Returns number of items processed."""
        return self._drain_once()

    def reset_attempt(self, filename: str) -> None:
        """Clear retry state for a file (called by dlq_requeue after moving back to queue)."""
        self._attempts.pop(filename, None)

    @observe(tier="boundary", metric="drainer.drain_cycle")
    def _drain_once(self) -> int:
        # Serialize the whole pass: the background loop and drain_now() share one
        # drainer instance; concurrent passes steal each other's pending files
        # (CI flake #53). The lock guarantees a pass applies its files to storage
        # before any other pass starts, so drain_now() returns only after its
        # writes are durable.
        with self._drain_lock:
            return self._drain_once_locked()

    @observe(tier="stage", metric="drainer.drain_once_locked")
    def _drain_once_locked(self) -> int:
        _cycle_t0 = time.monotonic()
        files = self._queue.pending()
        now = time.time()
        logger.info("Queue drain pass: %d pending files", len(files))

        if files:
            # R3: build the write-pipeline engines (write gate / curator /
            # prospective) before replaying — core no longer constructs them
            # and the write phases silently no-op without them. Lazy + idempotent.
            from yadgar.backend.write_exec import ensure_write_engines  # noqa: PLC0415

            ensure_write_engines()

        # P11: update queue/dlq depth gauges (v5.42.0: also rejection count)
        self._update_dlq_gauges(len(files))

        processed = sum(self._process_pending_file(path, now) for path in files)

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
            from yadgar._shared.observability.metrics import (
                yadgar_drain_cycle_duration_ms,  # noqa: PLC0415
            )

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

    @observe(tier="hot", metric="drainer.process_pending_file")
    def _process_pending_file(self, path, now: float) -> int:
        """Process one pending queue file. Returns 1 on success, 0 otherwise.

        Handles backoff, parse errors, validation rejections, and apply.
        """
        fname = path.name
        attempt = self._attempts.get(fname, _Attempt())

        # Respect backoff window
        if attempt.count > 0 and now < attempt.next_retry_at:
            return 0

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
            return 0

        # §26 Option Z — wiki_add validation before apply
        if op_type == "wiki_add":
            reject_reason = self._validate_wiki_add(data)
            if reject_reason:
                self._reject_permanent_to_dlq(
                    path, fname, attempt, op_type, reject_reason, data, now
                )
                return 0

        return self._apply_pending(fname, path, data, op_type, now)

    @observe(tier="hot", metric="drainer.reject_permanent_to_dlq")
    def _reject_permanent_to_dlq(
        self,
        path,
        fname: str,
        attempt: _Attempt,
        op_type: str,
        reject_reason: str,
        data: dict,
        now: float,
    ) -> None:
        """Move a file to DLQ as a permanent policy rejection (§26 Option Z).

        ADR-0215 removed the memory-op branch rejection path, so every current
        caller is a wiki_add rejection; the helper stays op-type agnostic.
        """
        attempt.count = self._max_permanent
        attempt.last_error = reject_reason
        attempt.classification = "permanent"
        attempt.first_failed_at = now
        failure_reason, failure_metadata = self._build_rejection_reason_and_meta(
            reject_reason, data, op_type
        )
        self._move_to_dlq(
            path,
            attempt,
            op_type,
            failure_reason=failure_reason,
            failure_metadata=failure_metadata,
        )
        self._attempts.pop(fname, None)
        logger.warning("%s rejected (DLQ): %s — %s", op_type, fname, reject_reason)

    def _build_rejection_reason_and_meta(
        self, reject_reason: str, data: dict, op_type: str
    ) -> tuple[str, dict | None]:
        """Return (failure_reason, failure_metadata) for a permanent policy rejection."""
        if reject_reason.startswith("missing_directory"):
            # v5.42.5: directory_context missing → DLQ with missing_directory
            return "missing_directory", self._build_missing_directory_metadata(data, op_type)
        # Car C (#83): slug_exists — upsert=False collision, keep the slug in metadata.
        if reject_reason == "slug_exists":
            p = data.get("payload", {})
            return "slug_exists", {
                "slug": p.get("slug", ""),
                "hint": (
                    f"Page already exists at slug {p.get('slug', '')!r}. "
                    "Pass upsert=True to overwrite or use a different slug."
                ),
            }
        return "policy_rejected", None

    def _observe_drainer_lag(self, data: dict, now: float) -> None:
        """Observe P11 drainer lag metric (enqueue_ts -> drain start). Swallows all errors."""
        try:
            from yadgar._shared.observability.metrics import yadgar_drainer_lag_ms  # noqa: PLC0415

            yadgar_drainer_lag_ms.observe((now - data.get("ts", now)) * 1000)
        except Exception:
            pass

    def _observe_secret_blocked_metric(self, err_str: str) -> None:
        """v5.10.2: record writegate metric for SecretLeakBlocked DLQ entries."""
        if "SecretLeakBlocked" not in err_str:
            return
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_writegate_outcome,  # noqa: PLC0415
            )

            yadgar_writegate_outcome.labels(outcome="rejected_secret_at_storage").inc()
        except Exception:
            pass

    @observe(tier="hot", metric="drainer.apply_pending")
    def _apply_pending(self, fname: str, path, data: dict, op_type: str, now: float) -> int:
        """Attempt to apply one queue item. Returns 1 on success, 0 on error.

        Records failure state and routes to DLQ when retry cap is exceeded.
        """
        attempt = self._attempts.get(fname, _Attempt())
        # P11: observe drainer lag (enqueue_ts -> drain start) — outside apply try/except
        # so lag metric errors never mask DB errors.
        self._observe_drainer_lag(data, now)
        try:
            self._apply_with_stage_metrics(data, path)
            self._attempts.pop(fname, None)
            return 1
        except Exception as exc:
            err_str = str(exc)
            # Car C (#83): slug_exists is an immediate-permanent rejection — no retry.
            # wiki_add(upsert=False) on an existing slug always fails; retrying wastes
            # drain cycles. Bypass the attempt counter and DLQ directly so wait=True
            # callers see the rejection without waiting for retry exhaustion.
            if "slug_exists:" in err_str and op_type == "wiki_add":
                self._reject_permanent_to_dlq(
                    path, fname, attempt, op_type, "slug_exists", data, now
                )
                return 0
            classification = _classify_error(err_str)
            max_attempts = (
                self._max_permanent if classification == "permanent" else self._max_transient
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
            self._observe_secret_blocked_metric(err_str)
            if attempt.count >= max_attempts:
                self._move_to_dlq(path, attempt, op_type)
                self._attempts.pop(fname, None)
            return 0

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

    @observe(tier="hot", metric="drainer.archive_with_metrics")
    def _archive_with_metrics(self, path) -> None:
        """Archive a queue file and record stage timing."""
        _t0 = time.perf_counter()
        try:
            self._queue.archive(path)
        finally:
            try:
                from yadgar._shared.observability.metrics import (
                    yadgar_drain_stage_ms,  # noqa: PLC0415
                )

                yadgar_drain_stage_ms.labels(stage="archive").observe(
                    (time.perf_counter() - _t0) * 1000
                )
            except Exception:
                pass

    # ── v5.42.0 helpers ───────────────────────────────────────────────────────

    @observe(tier="stage", metric="drainer.scan_dlq_counts")
    def _scan_dlq_counts(self) -> tuple[int, int]:
        """Scan DLQ directory and return (dlq_count, rejection_count).

        Counts non-error .json files as queue entries; reads their .error.json
        sidecars to detect policy rejections (failure_reason not in permanent_error/None).
        Raises on iterdir() failure so the outer try/except in _update_dlq_gauges
        can suppress the whole gauge-set (matching original behavior).
        """
        dlq_count = 0
        rejection_count = 0
        for _f in self._queue.dlq_dir.iterdir():
            if _f.suffix != ".json" or _f.name.endswith(".error.json"):
                continue
            dlq_count += 1
            _sidecar = self._queue.dlq_dir / (_f.name + ".error.json")
            if not _sidecar.exists():
                continue
            try:
                _meta = json.loads(_sidecar.read_text())
                if _meta.get("failure_reason", "permanent_error") not in (
                    "permanent_error",
                    None,
                ):
                    rejection_count += 1
            except Exception:
                pass
        return dlq_count, rejection_count

    def _update_dlq_gauges(self, queue_depth: int) -> None:
        """Update P11 queue/dlq depth gauges and v5.42.0 rejection count gauge (I23).

        Extracted from _drain_once to keep cyclomatic complexity bounded (I13).
        """
        try:
            from yadgar._shared.observability.metrics import (  # noqa: PLC0415
                yadgar_dlq_rejection_count,
                yadgar_dlq_size,
                yadgar_queue_depth,
            )

            yadgar_queue_depth.labels(queue="queue").set(queue_depth)
            dlq_count, rejection_count = self._scan_dlq_counts()
            yadgar_dlq_size.set(dlq_count)
            yadgar_queue_depth.labels(queue="dlq").set(dlq_count)
            yadgar_dlq_rejection_count.set(rejection_count)
        except Exception:
            pass

    @observe(tier="hot", metric="drainer.handle_sim_rejection")
    def _handle_sim_rejection(self, path, rejection: dict, job_id: str | None) -> None:
        """Route a drainer similarity-gate rejection to DLQ (v5.42.0).

        Extracted from _apply_with_stage_metrics to keep function complexity bounded.
        Builds failure_metadata from rejection dict and calls _move_to_dlq with
        failure_reason="duplicate_detected". Signals wait=True callers via
        _signal_complete_with_result so the v5.41.5 contract is preserved.
        """
        try:
            _cwd = os.getcwd()
        except Exception:
            _cwd = ""
        try:
            from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415

            _threshold = getattr(_get_settings(), "WIKI_SIM_CONTENT_THRESHOLD", 0.80)
        except Exception:
            _threshold = 0.80
        # Car B (#83): the rejection may carry any gate-supplied reason (e.g. a
        # similarity duplicate) — carry it through instead of hardcoding
        # duplicate_detected, so the DLQ taxonomy classifies it correctly.
        _reason = rejection.get("reason", "duplicate_detected")
        failure_metadata = {
            "candidates": rejection.get("candidates", []),
            "rejection_threshold_used": _threshold,
            "caller_context": {"directory": _cwd},
        }
        if rejection.get("errors"):
            failure_metadata["errors"] = rejection.get("errors")
        _rej_attempt = _Attempt(
            count=1,
            last_error=_reason,
            classification="permanent",
            first_failed_at=time.time(),
        )
        self._move_to_dlq(
            path,
            _rej_attempt,
            "wiki_add",
            failure_reason=_reason,
            failure_metadata=failure_metadata,
        )
        # R3 Car 1 (write-half): the DLQ .error.json sidecar (failure_metadata.candidates)
        # IS the rejection signal — wait=True callers poll it (FileQueue.wait_for_job).
        _ = (job_id, rejection)  # no in-process signalling

    # ── end v5.42.0 helpers ───────────────────────────────────────────────────

    @observe(tier="hot", metric="drainer.apply_with_stage_metrics")
    def _apply_with_stage_metrics(self, data: dict, path) -> None:
        """Apply one queue item and archive it, timing each stage for PR-E metrics.

        v5.41.5: for wiki_add ops, runs the v5.39 similarity gate BEFORE _apply()
        (I9 fix — gate moved from MCP handler to drainer, per I1/I6/I9 invariants).
        On gate rejection: archives the file (so it doesn't replay), signals
        wait=True callers with the rejection payload, skips _apply(). I6: gate runs
        once here, not again when _apply() re-invokes wiki_add (is_draining=True takes
        _wiki_add_sync_write path which skips the gate entirely).

        v5.42.0: similarity gate rejections are now routed to DLQ with
        failure_reason="duplicate_detected" instead of archive. wait=True
        callers still receive the rejection payload via _signal_complete_with_result.
        """
        job_id = data.get("id")

        # v5.41.5: similarity gate pre-apply check (wiki_add only).
        # v5.42.0: rejection → DLQ (not archive) via _handle_sim_rejection().
        if data.get("op") == "wiki_add":
            rejection = self._sim_gate_for_drainer(data.get("payload", {}))
            if rejection is not None:
                self._handle_sim_rejection(path, rejection, job_id)
                return

        # PR-E: time the insert stage
        _insert_t0 = time.perf_counter()
        try:
            self._apply(data)
        finally:
            try:
                from yadgar._shared.observability.metrics import (
                    yadgar_drain_stage_ms,  # noqa: PLC0415
                )

                yadgar_drain_stage_ms.labels(stage="insert").observe(
                    (time.perf_counter() - _insert_t0) * 1000
                )
            except Exception:
                pass

        self._archive_with_metrics(path)
        # R3 Car 1 (write-half): no per-job signalling — archiving IS the terminal
        # signal. wait=True callers poll the archive/dlq dirs (FileQueue.wait_for_job).
