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

import json
import logging
import re as _re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_QUEUE_DIR = "queue"
_ARCHIVE_DIR = "archive"
_DLQ_DIR = "dlq"
_DRAIN_INTERVAL = 30.0  # seconds between drain passes (configurable via QueueDrainer)
_ARCHIVE_MAX_AGE = 30 * 86400  # 30 days in seconds
_CLEANUP_EVERY = 120  # drain passes between archive cleanups (~1 hour at 30s interval)

# Thread-local flag: True while QueueDrainer._apply() is executing.
# Write tools check this to skip re-enqueueing during crash-recovery replay.
_drain_local = threading.local()


def is_draining() -> bool:
    """Return True if the current thread is inside a QueueDrainer._apply() call."""
    return getattr(_drain_local, "active", False)


def _json_default(obj):
    """JSON serializer for objects not serializable by default json."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


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


class FileQueue:
    """Atomic file-based write queue."""

    def __init__(self, base_dir: str | Path | None = None, wiki_prefix: str = "") -> None:
        base = Path(base_dir or Path.home() / ".yadgar")
        self.queue_dir = base / _QUEUE_DIR
        self.archive_dir = base / _ARCHIVE_DIR
        self.wiki_dir = base / _ARCHIVE_DIR / "wiki"
        self.dlq_dir = base / _DLQ_DIR
        self.wiki_prefix = wiki_prefix.strip("-").strip() if wiki_prefix else ""
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.dlq_dir.mkdir(parents=True, exist_ok=True)

    def _memories_archive_dir(self) -> Path:
        """Return today's memories archive dir, creating it if needed."""
        d = self.archive_dir / "memories" / _today_str()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def enqueue(self, op_type: str, payload: dict) -> str:
        """Write a queued operation atomically. Returns the queue file path."""
        record_id = str(uuid.uuid4())
        data = {
            "op": op_type,
            "id": record_id,
            "payload": payload,
            "ts": time.time(),
        }
        fname = f"{int(time.time() * 1000):016d}_{record_id}.json"
        tmp = self.queue_dir / (fname + ".tmp")
        target = self.queue_dir / fname
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=_json_default))
        tmp.rename(target)  # atomic on POSIX
        return str(target)

    def pending(self) -> list[Path]:
        """Return queue files sorted oldest-first."""
        return sorted(self.queue_dir.glob("*.json"))

    def archive(self, path: Path) -> None:
        """Move a confirmed queue file to archive/memories/YYYY-MM-DD/."""
        dest = self._memories_archive_dir() / path.name
        try:
            path.rename(dest)
        except OSError:
            path.unlink(missing_ok=True)

    def cleanup_archive(self) -> int:
        """Delete archive files older than _ARCHIVE_MAX_AGE. Returns count deleted."""
        cutoff = time.time() - _ARCHIVE_MAX_AGE
        deleted = 0
        # Walk memories/ and wiki/ dated subdirectories
        for subdir in (self.archive_dir / "memories", self.wiki_dir):
            if not subdir.exists():
                continue
            for date_dir in subdir.iterdir():
                if not date_dir.is_dir():
                    continue
                for f in date_dir.iterdir():
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink(missing_ok=True)
                            deleted += 1
                    except OSError:
                        pass
                # Remove empty date dirs
                try:
                    date_dir.rmdir()
                except OSError:
                    pass
        # Also clean up any legacy flat archive files (migration)
        for f in self.archive_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                pass
        return deleted

    def cleanup_dlq(self, max_age_days: int = 90) -> int:
        """Delete DLQ entries older than max_age_days. Returns count of main files deleted.

        DLQ items represent unrecovered data — logs prominently before each deletion.
        The .events.log audit trail is never pruned.
        """
        if not self.dlq_dir.exists():
            return 0
        cutoff = time.time() - (max_age_days * 86400)
        deleted = 0
        for f in sorted(self.dlq_dir.glob("*.json")):
            if f.name.endswith(".error.json") or f.name.startswith("."):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    logger.warning(
                        "DLQ expiry: deleting %s (>%d days old). "
                        "Data is permanently discarded — run dlq_requeue before this deadline to recover.",
                        f.name,
                        max_age_days,
                    )
                    f.unlink(missing_ok=True)
                    (self.dlq_dir / (f.name + ".error.json")).unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                pass
        return deleted

    def _wiki_date_dir(self) -> Path:
        """Return today's wiki archive dir, creating it if needed."""
        d = self.wiki_dir / _today_str()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_wiki(self, slug: str, content: str) -> None:
        """Persist a wiki page as a date-stamped .md in archive/wiki/YYYY-MM-DD/."""
        import re

        safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", slug)
        if self.wiki_prefix:
            safe = f"{self.wiki_prefix}-{safe}"
        date_dir = self._wiki_date_dir()
        wiki_path = date_dir / (safe + ".md")
        # Verify resolved path stays inside wiki_dir (defense-in-depth)
        if not str(wiki_path.resolve()).startswith(str(self.wiki_dir.resolve())):
            raise ValueError(f"Slug {slug!r} resolves outside wiki directory")
        tmp = date_dir / (safe + ".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(wiki_path)

    def delete_wiki(self, slug: str) -> None:
        """Remove .md mirror(s) for a deleted wiki page across all dated dirs."""
        import re

        safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", slug)
        if self.wiki_prefix:
            safe = f"{self.wiki_prefix}-{safe}"
        if not self.wiki_dir.exists():
            return
        for date_dir in self.wiki_dir.iterdir():
            if not date_dir.is_dir():
                continue
            wiki_path = date_dir / (safe + ".md")
            if not str(wiki_path.resolve()).startswith(str(self.wiki_dir.resolve())):
                continue
            wiki_path.unlink(missing_ok=True)


class QueueDrainer(threading.Thread):
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

    def _drain_once(self) -> int:
        files = self._queue.pending()
        processed = 0
        now = time.time()
        logger.info("Queue drain pass: %d pending files", len(files))

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

                try:
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

        logger.info("Queue drain pass complete: %d processed", processed)

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

    def _move_to_dlq(self, path: Path, attempt: _Attempt, op_type: str) -> None:
        """Atomically move a queue file to DLQ, write a .error.json sidecar, append events log."""
        now_ts = datetime.now(UTC).isoformat()
        first_failed = (
            datetime.fromtimestamp(attempt.first_failed_at, UTC).isoformat()
            if attempt.first_failed_at
            else now_ts
        )
        meta = {
            "op_type": op_type,
            "first_failed_at": first_failed,
            "last_failed_at": now_ts,
            "attempts": attempt.count,
            "classification": attempt.classification,
            "last_error": attempt.last_error,
            "moved_to_dlq_at": now_ts,
        }

        dlq_path = self._queue.dlq_dir / path.name
        try:
            path.rename(dlq_path)
        except OSError as exc:
            logger.error("Failed to move %s to DLQ: %s", path.name, exc)
            return

        # Write error sidecar atomically
        sidecar = self._queue.dlq_dir / (path.name + ".error.json")
        tmp = self._queue.dlq_dir / (path.name + ".error.json.tmp")
        try:
            tmp.write_text(
                json.dumps(meta, ensure_ascii=False, default=_json_default), encoding="utf-8"
            )
            tmp.rename(sidecar)
        except OSError as exc:
            logger.warning("Failed to write DLQ sidecar for %s: %s", path.name, exc)

        # Append to audit events log (never pruned by cleanup_dlq)
        events_log = self._queue.dlq_dir / ".events.log"
        event = {"event": "dlq_move", "ts": now_ts, "file": path.name, **meta}
        try:
            with open(events_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=_json_default) + "\n")
        except OSError as exc:
            logger.warning("Failed to append DLQ event log: %s", exc)

        logger.error(
            "MOVED TO DLQ: %s (%d attempts, %s) — %s",
            path.name,
            attempt.count,
            attempt.classification,
            attempt.last_error[:200],
        )

    def _apply(self, record: dict) -> None:
        """Replay a queued write by re-invoking the tool function.

        Sets _drain_local.active = True so write tools skip re-enqueueing
        during this call, preventing exponential queue growth on replay.
        """
        _drain_local.active = True
        try:
            self._apply_inner(record)
        finally:
            _drain_local.active = False

    def _apply_inner(self, record: dict) -> None:
        op = record["op"]
        p = record["payload"]

        if op == "memorize":
            from yadgar.server import memorize as _memorize

            _memorize(
                content=p["content"],
                context=p["context"],
                tags=p.get("tags", []),
                is_protected=p.get("is_protected", False),
            )
        elif op == "anchor":
            from yadgar.server import anchor as _anchor

            _anchor(
                content=p["content"],
                context=p["context"],
                reason=p.get("reason", ""),
            )
        elif op == "checkpoint":
            from yadgar.server import checkpoint as _checkpoint

            _checkpoint(
                directory=p["directory"],
                current_task=p.get("current_task", ""),
                files_being_edited=p.get("files_being_edited"),
                key_decisions=p.get("key_decisions"),
                open_questions=p.get("open_questions"),
                next_steps=p.get("next_steps"),
                active_errors=p.get("active_errors"),
                custom_context=p.get("custom_context", ""),
            )
        elif op == "wiki_add":
            from yadgar.server import wiki_add as _wiki_add

            _wiki_add(
                title=p["title"],
                content=p["content"],
                category=p.get("category", "reference"),
                tags=p.get("tags"),
                source_memory_ids=p.get("source_memory_ids"),
                confidence=p.get("confidence", "medium"),
                append=p.get("append", False),
            )
        else:
            logger.debug("Unknown queue op %r — skipping", op)
