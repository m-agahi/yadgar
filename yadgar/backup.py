"""Yadgar backup helpers — create_snapshot + prune_snapshots (v5.7.0 PR-6).

Provides reusable building blocks for the nightly cycle script (PR-1a).
The nightly cycle runs: backup → consolidation → vacuum → backup.

Timestamp format: YYYY-MM-DD-HHMMSS (dashes, seconds precision).
Snapshot directory naming: <db_basename>.<label>-<YYYY-MM-DD-HHMMSS>

These helpers are intentionally independent of the pre-vacuum snapshot logic
in yadgar.vacuum.phases — that code stays untouched as a v5.7.x cleanup.

Two snapshot modes (v5.69 P4 — quiesced/consistent backup, #45)
---------------------------------------------------------------
A plain ``shutil.copytree`` of a LIVE, lock-held ``surrealkv://`` directory can
capture a torn segment (the 1484/3622 partial that hit recovery on 2026-06-16).
``create_snapshot`` therefore supports two artifact kinds:

  * **logical export** (``backend_url`` given): ``GET /export`` → a single
    ``.surql`` file.  Transactionally consistent regardless of concurrent
    writes — the safe DEFAULT for ad-hoc callers that CANNOT guarantee the store
    is quiesced.  Restored via :func:`restore_snapshot` (POST /import).
  * **quiesced copytree** (``backend_url`` omitted): a directory copy.  Only
    safe when the caller has already brought the store down — the nightly cycle
    (v5.69 P5 stops BOTH ``yadgar`` AND ``yadgar-backend`` before snapshotting,
    releasing the surrealkv lock) and BC-F1 (stops the backend first).

The RAW export is used (never the vacuum's action_log-stripped variant): a
backup must be a COMPLETE copy.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from yadgar.observability.observe import observe

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


@observe(tier="boundary")
def create_snapshot(
    db_path: Path,
    snapshot_dir: Path | None = None,
    label: str = "nightly",
    backend_url: str | None = None,
) -> Path:
    """Snapshot ``db_path`` to a timestamped artifact under ``snapshot_dir``.

    Two artifact kinds (v5.69 P4):

      * ``backend_url`` given → **logical export**: ``GET {backend_url}/export``
        is written to ``<db_basename>.<label>-<TS>.surql`` — a transactionally
        consistent point-in-time copy regardless of concurrent writes.  This is
        the consistent DEFAULT for ad-hoc callers that cannot quiesce the store.
        Restore with :func:`restore_snapshot`.
      * ``backend_url`` omitted → **quiesced copytree**: a directory copy named
        ``<db_basename>.<label>-<TS>``.  ONLY safe when the caller has already
        stopped the backend (released the surrealkv lock) — copying a live,
        lock-held dir can capture a torn segment.

    If a target with the timestamped name already exists (timestamp collision),
    a counter suffix is appended: ``-2``, ``-3``, …

    Args:
        db_path:      Source database directory (must exist for both modes; for
                      the export mode it identifies the snapshot basename).
        snapshot_dir: Directory in which to create the snapshot. Defaults to
                      ``db_path.parent`` (i.e. the same directory as the DB).
        label:        Label component of the snapshot name (default ``"nightly"``).
        backend_url:  If given, take a logical ``.surql`` export from this live
                      SurrealDB backend instead of copying the on-disk dir.

    Returns:
        Path of the newly created snapshot (a ``.surql`` file in export mode, a
        directory in copytree mode).

    Raises:
        RuntimeError: If ``db_path`` does not exist, the export request fails,
            or the copy fails.
    """
    if not db_path.exists():
        raise RuntimeError(f"create_snapshot: source db_path does not exist: {db_path}")

    dest_dir = snapshot_dir if snapshot_dir is not None else db_path.parent
    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    base_name = f"{db_path.name}.{label}-{ts}"

    if backend_url is not None:
        return _create_export_snapshot(dest_dir, base_name, backend_url)

    target = dest_dir / base_name
    counter = 2
    while target.exists():
        target = dest_dir / f"{base_name}-{counter}"
        counter += 1

    try:
        shutil.copytree(str(db_path), str(target))
    except OSError as exc:
        raise RuntimeError(f"create_snapshot: failed to copy {db_path} → {target}: {exc}") from exc

    # Stamp snapshot directory mtime to now so prune_snapshots (mtime-sorted) always
    # treats a just-created snapshot as newest. shutil.copytree with copy2 propagates
    # the source directory's mtime to the snapshot, which can be arbitrarily old
    # (the DB dir mtime is the last write, not the copy time). Without this stamp,
    # a newly created post-backup snapshot can sort as "oldest" and get pruned
    # immediately in the same nightly cycle (v5.10.5 Bug 2).
    try:
        target.touch()  # os.utime(target, None) — sets atime+mtime to current time
    except OSError:
        pass  # non-fatal: snapshot is created, mtime stamp is best-effort

    _log.info("backup snapshot created: %s", target)
    return target


@observe(tier="stage")
def _create_export_snapshot(dest_dir: Path, base_name: str, backend_url: str) -> Path:
    """Write a consistent ``GET /export`` artifact to ``<base_name>.surql``.

    Reuses the vacuum export-credential headers (root IAM).  The RAW export is
    written verbatim — NOT the vacuum's action_log-stripped variant — because a
    backup must be a complete copy.  ``GET /export`` is transactionally
    consistent, so the artifact is a valid point-in-time even under concurrent
    writes (the BC-F3 contract).
    """
    import httpx

    from yadgar.config import get_settings as _get_settings
    from yadgar.vacuum.phases import _surreal_headers

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{base_name}.surql"
    counter = 2
    while target.exists():
        target = dest_dir / f"{base_name}-{counter}.surql"
        counter += 1

    timeout = float(_get_settings().BACKEND_IMPORT_TIMEOUT_SEC)
    try:
        resp = httpx.get(f"{backend_url}/export", headers=_surreal_headers(), timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"create_snapshot: export request to {backend_url}/export failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"create_snapshot: export from {backend_url} returned HTTP "
            f"{resp.status_code}\n{resp.text[:500]}"
        )

    target.write_text(resp.text, encoding="utf-8")
    _log.info("backup export snapshot created: %s (%d bytes)", target, len(resp.text))
    return target


@observe(tier="boundary")
def restore_snapshot(snapshot_path: Path, backend_url: str) -> None:
    """Restore a snapshot into the live SurrealDB at ``backend_url``.

    Handles BOTH snapshot kinds produced by :func:`create_snapshot`:

      * a ``.surql`` file (export mode) → bootstrap the ``yadgar/main``
        namespace, ``POST /import``, then re-define the non-root yadgar users
        (SurrealDB ``/import`` wipes ROOT-level user definitions).
      * a directory (copytree mode) → there is nothing to import into a running
        backend; the directory IS the store.  A directory restore means pointing
        a backend at the copied dir, so this raises to make the misuse loud
        rather than silently no-op.

    This is the production counterpart that brings a daemon back to full state
    after a restore (BC-F2): import + user re-bootstrap is exactly what the
    vacuum side-build does, shared here so backups round-trip identically.

    Args:
        snapshot_path: A ``.surql`` file produced by ``create_snapshot`` with a
                       ``backend_url``.
        backend_url:   Live SurrealDB backend to import into (already running,
                       reachable; will be left holding the restored data).

    Raises:
        RuntimeError: If the snapshot is a directory (use a backend pointed at
            the dir instead), or the import fails.
    """
    import httpx

    from yadgar.config import get_settings as _get_settings
    from yadgar.vacuum import _bootstrap_namespace, _build_http_client, _redefine_users_post_import

    if snapshot_path.is_dir():
        raise RuntimeError(
            f"restore_snapshot: {snapshot_path} is a directory (copytree snapshot). "
            "A directory snapshot IS the store — point a backend at it directly "
            "instead of importing. restore_snapshot handles .surql exports only."
        )
    if not snapshot_path.exists():
        raise RuntimeError(f"restore_snapshot: snapshot does not exist: {snapshot_path}")

    _bootstrap_namespace(backend_url)

    surql = snapshot_path.read_bytes()
    client = _build_http_client(backend_url)
    import_headers = {"Content-Type": "text/plain", **dict(client.headers)}
    client.close()

    timeout = float(_get_settings().BACKEND_IMPORT_TIMEOUT_SEC)
    resp = httpx.post(
        f"{backend_url}/import",
        content=surql,
        headers=import_headers,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"restore_snapshot: import to {backend_url} returned HTTP "
            f"{resp.status_code}\n{resp.text[:500]}"
        )

    # SurrealDB /import wipes ROOT-level user defs — re-create yadgar-rw/-ro so
    # the daemon can authenticate after restore.
    _redefine_users_post_import(backend_url)
    _log.info("backup snapshot restored into %s from %s", backend_url, snapshot_path)


@observe(tier="boundary")
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
