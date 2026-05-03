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
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_QUEUE_DIR = "queue"
_ARCHIVE_DIR = "archive"
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


class FileQueue:
    """Atomic file-based write queue."""

    def __init__(self, base_dir: str | Path | None = None, wiki_prefix: str = "") -> None:
        base = Path(base_dir or Path.home() / ".yadgar")
        self.queue_dir = base / _QUEUE_DIR
        self.archive_dir = base / _ARCHIVE_DIR
        self.wiki_dir = base / _ARCHIVE_DIR / "wiki"
        self.wiki_prefix = wiki_prefix.strip("-").strip() if wiki_prefix else ""
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

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
        self, queue: FileQueue, storage_factory, drain_interval: float = _DRAIN_INTERVAL
    ) -> None:
        super().__init__(daemon=True, name="yadgar-queue-drainer")
        self._queue = queue
        self._storage_factory = storage_factory  # callable -> StorageEngine
        self._stop_event = threading.Event()
        self._drain_count = 0
        self._drain_interval = drain_interval

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

    def _drain_once(self) -> int:
        files = self._queue.pending()
        processed = 0
        if files:
            for path in files:
                try:
                    data = json.loads(path.read_text())
                    self._apply(data)
                    self._queue.archive(path)
                    processed += 1
                except Exception as exc:
                    logger.warning("Failed to drain %s: %s", path.name, exc)

        # Periodic archive cleanup (roughly once per hour)
        self._drain_count += 1
        if self._drain_count % _CLEANUP_EVERY == 0:
            deleted = self._queue.cleanup_archive()
            if deleted:
                logger.debug("Archive cleanup: removed %d old files", deleted)

        return processed

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
