"""FileQueue — atomic file-based write queue."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yadgar.paths as _paths
from yadgar.observability.observe import observe

_QUEUE_DIR = "queue"
_ARCHIVE_DIR = "archive"
_DLQ_DIR = "dlq"
_ARCHIVE_MAX_AGE = 30 * 86400  # 30 days in seconds

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JSON serializer for objects not serializable by default json."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


class FileQueue:
    """Atomic file-based write queue."""

    def __init__(self, base_dir: str | Path | None = None, wiki_prefix: str = "") -> None:
        base = Path(base_dir) if base_dir else _paths.DATA_DIR
        self.queue_dir = base / _QUEUE_DIR
        self.archive_dir = base / _ARCHIVE_DIR
        self.wiki_dir = base / _ARCHIVE_DIR / "wiki"
        self.dlq_dir = base / _DLQ_DIR
        self.wiki_prefix = wiki_prefix.strip("-").strip() if wiki_prefix else ""
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.dlq_dir.mkdir(parents=True, exist_ok=True)
        # Per-job completion tracking (v5.41.2 wait flag).
        # Only populated when a caller opts in via register_wait(job_id).
        # v5.41.5: value is tuple[Event, dict | None] — result payload for
        # drainer-side rejections (e.g. similarity gate) returned to wait=True callers.
        self._job_futures: dict[str, tuple[threading.Event, dict | None]] = {}
        self._job_lock: threading.Lock = threading.Lock()

    def _memories_archive_dir(self) -> Path:
        """Return today's memories archive dir, creating it if needed."""
        d = self.archive_dir / "memories" / _today_str()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @observe(tier="boundary", name="queue.enqueue")
    def enqueue(self, op_type: str, payload: dict) -> str:
        """Write a queued operation atomically.

        Returns the job_id (UUID string) for this enqueue operation.
        Pass the job_id to QueueDrainer.wait_for_job() when wait=True to block
        until this specific write has been committed to the database.
        """
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
        return record_id

    @observe(tier="hot", name="queue.register_wait")
    def register_wait(self, job_id: str) -> threading.Event:
        """Register interest in completion of job_id. Returns a threading.Event.

        The event is set by signal_complete() when the drainer commits the job.
        Multiple calls with the same job_id return the same event (idempotent).
        Only allocates an event when wait=True is opted in — zero cost on the
        default async path.
        """
        with self._job_lock:
            if job_id not in self._job_futures:
                self._job_futures[job_id] = (threading.Event(), None)
            return self._job_futures[job_id][0]

    def signal_complete(self, job_id: str) -> None:
        """Mark job_id as committed (success). Sets the event for wait=True callers.

        Safe to call even if no caller registered a wait for this job — no-op.
        Called by QueueDrainer._apply_with_stage_metrics after archive succeeds.

        The event is NOT removed here — wait_for_job() cleans it up after waiting,
        so callers that call signal_complete before wait_for_job() still see the
        pre-set event in register_wait().
        """
        self._signal_complete_with_result(job_id, None)

    @observe(tier="hot", name="queue.signal_complete_with_result")
    def _signal_complete_with_result(self, job_id: str, result: dict | None) -> None:
        """Mark job_id as complete with an optional result payload.

        v5.41.5: used by the drainer similarity-gate rejection path to pass
        the rejection dict back to wait=True callers via get_job_result().
        result=None means success (same semantics as signal_complete).
        """
        with self._job_lock:
            entry = self._job_futures.get(job_id)
            if entry is None:
                # No waiter registered — allocate a pre-set entry so late
                # register_wait() calls see the completed state immediately.
                event = threading.Event()
                event.set()
                self._job_futures[job_id] = (event, result)
                return
            event, _ = entry
            self._job_futures[job_id] = (event, result)
        event.set()

    @observe(tier="hot", name="queue.get_job_result")
    def get_job_result(self, job_id: str) -> dict | None:
        """Return the result payload stored by the drainer for job_id.

        Returns None if job committed successfully (no rejection).
        Returns a rejection dict (e.g. {stored: False, reason: "duplicate_detected"})
        if the drainer's pre-apply check rejected the job.

        Called by wait=True callers AFTER wait_for_job() returns True.
        Safe to call before _cleanup_job().
        """
        with self._job_lock:
            entry = self._job_futures.get(job_id)
        if entry is None:
            return None
        _, result = entry
        return result

    @observe(tier="hot", name="queue.cleanup_job")
    def _cleanup_job(self, job_id: str) -> None:
        """Remove a job from tracking after wait_for_job() has consumed it."""
        with self._job_lock:
            self._job_futures.pop(job_id, None)

    def pending(self) -> list[Path]:
        """Return queue files sorted oldest-first."""
        return sorted(self.queue_dir.glob("*.json"))

    @observe(tier="stage", name="queue.archive")
    def archive(self, path: Path) -> None:
        """Move a confirmed queue file to archive/memories/YYYY-MM-DD/."""
        dest = self._memories_archive_dir() / path.name
        try:
            path.rename(dest)
        except OSError:
            path.unlink(missing_ok=True)

    def _cleanup_date_dir(self, date_dir: Path, cutoff: float) -> int:
        """Delete stale files in one dated archive sub-directory. Returns count deleted."""
        deleted = 0
        for f in date_dir.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                pass
        try:
            date_dir.rmdir()
        except OSError:
            pass
        return deleted

    @observe(tier="stage", name="queue.cleanup_archive")
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
                deleted += self._cleanup_date_dir(date_dir, cutoff)
        # Also clean up any legacy flat archive files (migration)
        for f in self.archive_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                pass
        return deleted

    @observe(tier="stage", name="queue.cleanup_dlq")
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

    @observe(tier="stage", name="queue.write_wiki")
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

    @observe(tier="stage", name="queue.delete_wiki")
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
