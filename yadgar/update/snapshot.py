"""v5.49.0 Phase 8 — Upgrade snapshot artefact module.

Manages pre-upgrade state snapshots used by the upgrade orchestrator (Phase 9)
to record context for rollback and forward-log tracking.

Snapshot directory layout:
  ~/.yadgar/upgrade-snapshots/<timestamp>/
    prev_image_tag         — plain text, single line: docker image tag
    prev_unit_file         — full systemd unit file content (or empty)
    prev_cli_version       — plain text, single line: version string
    forward_log.json       — JSON array of {ts, state, detail} entries
    rollback_log.json      — JSON array, only present if rollback fired

Timestamp format: 2026-06-08T19-42-00-123456Z (ISO 8601 with `:` and `.`
replaced by `-` for filesystem safety, microsecond resolution). UTC.

Atomic writes: each write uses tempfile + os.replace (POSIX atomic rename).
Permissions: snapshot dir is chmod 700; files inside are chmod 600.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOTS_DIR = Path.home() / ".yadgar" / "upgrade-snapshots"

# Timestamp format used for snapshot directory names.
# Microsecond resolution prevents collisions even in tight loops.
_TS_FMT = "%Y-%m-%dT%H-%M-%S-%fZ"


def _now_utc() -> datetime:
    """Return current UTC datetime (overridable in tests)."""
    return datetime.now(tz=UTC)


def _ts_to_dirname(dt: datetime) -> str:
    """Convert a UTC datetime to a snapshot directory name."""
    return dt.strftime(_TS_FMT)


def _dirname_to_ts(name: str) -> datetime:
    """Parse a snapshot directory name back to a UTC datetime."""
    dt = datetime.strptime(name, _TS_FMT)
    return dt.replace(tzinfo=UTC)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temp file + os.replace.

    Temp file is created in the same directory as *path* so that os.replace
    is guaranteed to be atomic (same filesystem). On failure the temp file
    is removed and the original file (if any) is unaffected.

    Resulting file is chmod 600 (user read/write only).
    """
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="tmp")
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_log(path: Path) -> list[dict]:
    """Read a JSON log file, returning an empty list if missing."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[return-value]


@dataclass
class Snapshot:
    """In-memory handle to a snapshot directory on disk."""

    path: Path
    created_at: datetime

    def write_prev_image_tag(self, tag: str) -> None:
        """Write the previous Docker image tag to disk atomically."""
        _atomic_write(self.path / "prev_image_tag", tag)

    def write_prev_unit_file(self, content: str) -> None:
        """Write the previous systemd unit file content to disk atomically."""
        _atomic_write(self.path / "prev_unit_file", content)

    def write_prev_cli_version(self, version: str) -> None:
        """Write the previous CLI version string to disk atomically."""
        _atomic_write(self.path / "prev_cli_version", version)

    def append_forward_log(self, state: str, detail: dict | None = None) -> None:
        """Append an entry to forward_log.json atomically."""
        self._append_log("forward_log.json", state, detail)

    def append_rollback_log(self, state: str, detail: dict | None = None) -> None:
        """Append an entry to rollback_log.json atomically."""
        self._append_log("rollback_log.json", state, detail)

    def _append_log(self, filename: str, state: str, detail: dict | None) -> None:
        """Read-append-write a JSON log file atomically."""
        log_path = self.path / filename
        entries = _read_log(log_path)
        entries.append(
            {
                "ts": _now_utc().isoformat(),
                "state": state,
                "detail": detail,
            }
        )
        _atomic_write(log_path, json.dumps(entries, indent=2))

    def read_prev_image_tag(self) -> str | None:
        """Return the stored image tag, or None if not present."""
        return self._read_plain("prev_image_tag")

    def read_prev_unit_file(self) -> str | None:
        """Return the stored unit file content, or None if not present."""
        return self._read_plain("prev_unit_file")

    def read_prev_cli_version(self) -> str | None:
        """Return the stored CLI version, or None if not present."""
        return self._read_plain("prev_cli_version")

    def _read_plain(self, filename: str) -> str | None:
        p = self.path / filename
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")


def create_snapshot(base_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> Snapshot:
    """Create a fresh snapshot directory named with the current UTC timestamp.

    The directory is created with chmod 700 (user-only access).
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    now = _now_utc()
    dir_name = _ts_to_dirname(now)
    snap_path = base_dir / dir_name
    snap_path.mkdir(parents=False, exist_ok=False)
    os.chmod(snap_path, 0o700)
    return Snapshot(path=snap_path, created_at=now)


def list_snapshots(base_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> list[Snapshot]:
    """Return all existing snapshots, sorted newest-first.

    Sorting is by parsed timestamp from directory name, not mtime.
    Directories that cannot be parsed as a timestamp are silently skipped.
    """
    if not base_dir.exists():
        return []

    snaps: list[Snapshot] = []
    for child in base_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            ts = _dirname_to_ts(child.name)
        except ValueError:
            continue
        snaps.append(Snapshot(path=child, created_at=ts))

    snaps.sort(key=lambda s: s.created_at, reverse=True)
    return snaps


def latest_snapshot(base_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> Snapshot | None:
    """Return the most recent snapshot, or None if no snapshots exist."""
    snaps = list_snapshots(base_dir=base_dir)
    return snaps[0] if snaps else None


def prune_old_snapshots(retention: int, base_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> int:
    """Delete snapshots beyond the most recent *retention* count.

    Returns the number of directories deleted.

    If *retention* is <= 0, no deletion occurs and a WARNING is emitted.
    """
    if retention <= 0:
        logger.warning(
            "prune_old_snapshots: retention=%d is zero or negative — keeping all snapshots",
            retention,
        )
        return 0

    snaps = list_snapshots(base_dir=base_dir)
    to_delete = snaps[retention:]
    for snap in to_delete:
        shutil.rmtree(snap.path)
        logger.debug("prune_old_snapshots: deleted %s", snap.path)

    return len(to_delete)
