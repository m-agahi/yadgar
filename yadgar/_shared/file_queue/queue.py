"""FileQueue — atomic file-based write queue."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe

_QUEUE_DIR = "queue"
_ARCHIVE_DIR = "archive"
_DLQ_DIR = "dlq"
_ARCHIVE_MAX_AGE = 30 * 86400  # 30 days in seconds
_WAIT_POLL_INTERVAL = 0.05  # seconds between terminal-file polls (wait=True path)

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

    def _memories_archive_dir(self) -> Path:
        """Return today's memories archive dir, creating it if needed."""
        d = self.archive_dir / "memories" / _today_str()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @observe(tier="boundary", metric="queue.enqueue")
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

    @observe(tier="stage", metric="queue.wait_for_job")
    def wait_for_job(self, job_id: str, timeout: float = 5.0) -> dict:
        """Poll the shared dir for the terminal state of job_id (wait=True path).

        Cross-process safe: the drainer runs in the backend process and archives
        (success) or DLQs (failure) the queue file. This method polls those two
        terminal locations for a file whose name ends with ``_<job_id>.json``
        (the enqueue() filename embeds the job_id as its record id).

        Returns one of:
          {"status": "ok"}                    — archived (committed to DB)
          {"status": "rejected", "result": <rejection-dict|None>}
                                              — DLQ'd (e.g. similarity gate);
                                                result is the .error.json rejection
                                                payload if present, else None
          {"status": "timeout"}               — no terminal file within timeout

        No threading.Event, no in-process signalling — archiving / DLQ IS the signal.
        """
        deadline = time.monotonic() + timeout
        suffix = f"_{job_id}.json"
        while True:
            # Failure terminal: dlq/<name> (+ optional .error.json sidecar).
            dlq_hit = self._find_terminal(self.dlq_dir, suffix)
            if dlq_hit is not None:
                return {"status": "rejected", "result": self._read_dlq_rejection(dlq_hit)}
            # Success terminal: archive/memories/<date>/<name>.
            if self._archive_has(job_id, suffix):
                return {"status": "ok"}
            if time.monotonic() >= deadline:
                return {"status": "timeout"}
            time.sleep(_WAIT_POLL_INTERVAL)

    def _find_terminal(self, directory: Path, suffix: str) -> Path | None:
        """Return a job file ending in *suffix* under *directory* (non-recursive), or None."""
        try:
            for f in directory.glob(f"*{suffix}"):
                if f.name.endswith(".error.json"):
                    continue
                return f
        except OSError:
            return None
        return None

    def _archive_has(self, job_id: str, suffix: str) -> bool:
        """True if a job file ending in *suffix* exists under archive/memories/<date>/."""
        memories = self.archive_dir / "memories"
        if not memories.exists():
            return False
        try:
            for date_dir in memories.iterdir():
                if not date_dir.is_dir():
                    continue
                for f in date_dir.glob(f"*{suffix}"):
                    if f.name.endswith(".error.json"):
                        continue
                    return True
        except OSError:
            return False
        return False

    def _read_dlq_rejection(self, dlq_path: Path) -> dict | None:
        """Read the rejection payload from a DLQ .error.json sidecar, if present.

        Returns the rejection dict the drainer stored (candidates + reason) so the
        wait=True caller can surface a synchronous duplicate_detected rejection.
        """
        sidecar = dlq_path.parent / (dlq_path.name + ".error.json")
        try:
            meta = json.loads(sidecar.read_text())
        except (OSError, ValueError):  # fmt: skip
            return None
        reason = meta.get("failure_reason")
        if reason == "duplicate_detected":
            fm = meta.get("failure_metadata") or {}
            return {
                "stored": False,
                "reason": "duplicate_detected",
                "candidates": fm.get("candidates", []),
            }
        # Car C (#83): slug_exists — upsert=False collision surfaces synchronously.
        if reason == "slug_exists":
            fm = meta.get("failure_metadata") or {}
            return {
                "stored": False,
                "reason": "slug_exists",
                "slug": fm.get("slug", ""),
                "hint": fm.get("hint", ""),
            }
        return None

    def pending(self) -> list[Path]:
        """Return queue files sorted oldest-first."""
        return sorted(self.queue_dir.glob("*.json"))

    @observe(tier="stage", metric="queue.archive")
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

    @observe(tier="stage", metric="queue.cleanup_archive")
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

    @observe(tier="stage", metric="queue.cleanup_dlq")
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

    @observe(tier="stage", metric="queue.write_wiki")
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

    @observe(tier="stage", metric="queue.delete_wiki")
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
