"""Yadgar backup helpers — create_snapshot + prune_snapshots (v5.7.0 PR-6).

Provides reusable building blocks for the nightly cycle script (PR-1a).
The nightly cycle runs: backup → consolidation → vacuum → backup.

Timestamp format: YYYY-MM-DD-HHMMSS (dashes, seconds precision).
Snapshot directory naming: <db_basename>.<label>-<YYYY-MM-DD-HHMMSS>

These helpers are intentionally independent of the pre-vacuum snapshot logic
in yadgar.vacuum.phases — that code stays untouched as a v5.7.x cleanup.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knob
# ---------------------------------------------------------------------------


def default_retention() -> int:
    """Return YADGAR_BACKUP_RETENTION (default 3).

    Reads os.getenv live so tests can monkeypatch without module reload.
    """
    return int(os.getenv("YADGAR_BACKUP_RETENTION", "3"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_snapshot(
    db_path: Path,
    snapshot_dir: Path | None = None,
    label: str = "nightly",
) -> Path:
    """Copy ``db_path`` to a timestamped snapshot directory.

    The snapshot is named ``<db_basename>.<label>-<YYYY-MM-DD-HHMMSS>``.
    If a target with that name already exists (timestamp collision), a counter
    suffix is appended: ``-2``, ``-3``, …

    Args:
        db_path:      Source database directory to copy.
        snapshot_dir: Directory in which to create the snapshot. Defaults to
                      ``db_path.parent`` (i.e. the same directory as the DB).
        label:        Label component of the snapshot name (default ``"nightly"``).

    Returns:
        Path of the newly created snapshot directory.

    Raises:
        RuntimeError: If ``db_path`` does not exist or the copy fails.
    """
    if not db_path.exists():
        raise RuntimeError(f"create_snapshot: source db_path does not exist: {db_path}")

    dest_dir = snapshot_dir if snapshot_dir is not None else db_path.parent
    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    base_name = f"{db_path.name}.{label}-{ts}"

    target = dest_dir / base_name
    counter = 2
    while target.exists():
        target = dest_dir / f"{base_name}-{counter}"
        counter += 1

    try:
        shutil.copytree(str(db_path), str(target))
    except OSError as exc:
        raise RuntimeError(f"create_snapshot: failed to copy {db_path} → {target}: {exc}") from exc

    _log.info("backup snapshot created: %s", target)
    return target


def prune_snapshots(
    snapshot_dir: Path,
    pattern: str,
    retention: int,
) -> list[Path]:
    """Remove old snapshots in ``snapshot_dir``, keeping the newest ``retention``.

    Directories matching ``pattern`` (glob) inside ``snapshot_dir`` are sorted
    by mtime descending; any beyond the first ``retention`` are deleted.

    Args:
        snapshot_dir: Directory to search for snapshots.
        pattern:      Glob pattern relative to ``snapshot_dir``
                      (e.g. ``"surreal_db.nightly-*"``).
        retention:    Number of snapshots to keep. 0 means delete all matches.

    Returns:
        List of Path objects that were removed (empty if nothing deleted).

    Idempotent: calling twice with the same retention is a no-op on the second
    call (returns ``[]`` when nothing to delete).
    """
    if not snapshot_dir.exists():
        return []

    glob_pattern = str(snapshot_dir / pattern)
    candidates = sorted(
        (Path(p) for p in glob.glob(glob_pattern)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    to_delete = candidates[retention:]
    removed: list[Path] = []
    for path in to_delete:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(path)
            _log.info("backup pruned snapshot: %s", path)
        except OSError as exc:
            _log.warning("backup: failed to prune %s: %s", path, exc)

    return removed
