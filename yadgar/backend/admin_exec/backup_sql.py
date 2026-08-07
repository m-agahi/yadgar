"""Backend admin-op body for ``mariadb_dump`` — engine #2's backup arm (car F).

WHY THIS IS AN ADMIN OP AND NOT A HOST-SIDE CALL
------------------------------------------------
The nightly cycle runs HOST-SIDE, and every route from there to engine #2 is a
trap:

* ``client.cnf`` (car C, ADR-0212) carries a CONTAINER-ABSOLUTE socket path —
  ``/data/mariadb/mysqld.sock``. On the host that path does not exist; the
  bind-mount presents the same bytes at ``~/.local/share/yadgar/mariadb/``.
* ``MariaStorageEngine`` construction is CONNECTIONLESS, so a host-side
  ``_init_sql_storage()`` returns a handle that can never connect and fails
  SILENTLY — a backup that appears to succeed and contains nothing.
* mariadbd runs ``--skip-networking`` (ADR-0212): there is no TCP fallback, not
  even on loopback.
* The host has no ``mariadb-dump`` binary at all. It ships with the
  ``mariadb-server`` apt install baked into ``Dockerfile.backend``, which is the
  backend container and nowhere else.

Running the dump in the backend process puts it in the one filesystem namespace
where the option file's socket path is true, next to the binary that can use it.
It also means this module imports NOTHING from the ``sql`` extra — it shells a
binary, so ``asyncmy``/``sqlalchemy``/``alembic`` are irrelevant here and the
yadgar-ci image rebuild (``Dockerfile.ci:116``) is not a prerequisite for it.

THE DESTINATION IS THE SAME TRAP IN A SECOND COSTUME
----------------------------------------------------
An absolute path handed in by a host caller would resolve inside the
CONTAINER's namespace: the op would create it in the container's writable
layer, report success, and the host would see nothing. So the payload carries a
LABEL and never a path; the destination is resolved here, from this process's
own view of the data root, and the response reports a BASENAME for the caller
to match under its own root. A ``path`` key in the payload is inert.

ADR-0212 defers physical ``mariadb-backup`` (full + incremental via LSN) to the
spine train. For a table whose target state is zero rows (ADR-0203) a logical
dump proves the arm end to end and needs no datadir access.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.sql.config import read_client_option_file

logger = logging.getLogger(__name__)

# Basename of the subtree the dumps land in, under the shared data root — a
# SIBLING of ``surreal_db`` and of the surql export dir, for the same reason the
# datadir is (ADR-0212): every vacuum copytree/rmtree/reap glob is
# ``surreal_db``-prefixed, so nothing here is inside the vacuum's blast radius.
DUMP_SUBDIR = Path("backups") / "mariadb"

# Wall-clock ceiling for one dump. Generous for a zero-row table by design: the
# point is that a WEDGED mariadb-dump cannot hold the maintenance window open
# forever, not to tune throughput. The window is a full MCP outage (ADR-0210).
DUMP_TIMEOUT_SEC = 900.0

# Marker that proves the artifact carries schema rather than only a header. A
# zero-row table means "succeeded" and "empty" look alike on size alone.
_SCHEMA_MARKER = "CREATE TABLE"


@observe(tier="stage")
def _dump_dir(option_file: Path) -> Path:
    """Resolve the dump destination CONTAINER-SIDE, mirroring the option-file ladder.

    Order (first hit wins):

    1. ``YADGAR_SQL_BACKUP_DIR`` — explicit override for scratch instances.
    2. ``SURREAL_DATA_ROOT``/``backups``/``mariadb`` — the container shape
       (``SURREAL_DATA_ROOT`` defaults to ``/data`` in ``entrypoint-backend.sh``).
    3. The option file's own grandparent: the file lives at
       ``<root>/mariadb/client.cnf``, so ``<root>`` is where the datadir already
       proved the shared root to be. Deriving it from the file we just read
       keeps the destination and the socket on the same footing rather than on
       two independent guesses.
    """
    override = os.environ.get("YADGAR_SQL_BACKUP_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    data_root = os.environ.get("SURREAL_DATA_ROOT", "").strip()
    root = Path(data_root).expanduser() if data_root else option_file.parent.parent
    return root / DUMP_SUBDIR


@observe(tier="boundary", metric="backend.admin.mariadb_dump")
def mariadb_dump(payload: dict) -> dict:
    """Take a logical ``mariadb-dump`` snapshot of engine #2 over the local socket.

    payload:
        ``label`` — artifact label, default ``"nightly-quiesce"``. Sanitised to
        ``[A-Za-z0-9._-]`` so a caller cannot walk out of the dump directory.
        Any other key (notably ``path``) is IGNORED — see the module docstring.

    Returns:
        ``{"ok": True, "filename": <basename>, "path": <container path>,
        "bytes": int, "database": str, "label": str}``

    Raises:
        RuntimeError: binary absent, non-zero exit, timeout, or an empty
            artifact. Every one of those is a hard failure ON PURPOSE — a soft
            return here yields a backup that reports success and restores
            nothing, which is the 2026-06-16 shape.
    """
    binary = shutil.which("mariadb-dump")
    if binary is None:
        raise RuntimeError(
            "mariadb-dump not found on PATH. Engine #2's backup arm runs INSIDE the "
            "backend container, where the mariadb-server apt install (Dockerfile.backend) "
            "provides it; a host process has neither the binary nor a reachable socket."
        )

    cfg = read_client_option_file()
    raw_label = str(payload.get("label") or "nightly-quiesce")
    label = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in raw_label)[:64]

    dest_dir = _dump_dir(cfg.option_file)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"mariadb.{cfg.database}.{label}-{stamp}.sql"

    cmd = [
        binary,
        # --defaults-file must come FIRST; mariadb-dump rejects it elsewhere in
        # argv. It is also the whole reason no credential enters this argv: the
        # password stays in the 0600 file and never reaches a process listing.
        f"--defaults-file={cfg.option_file}",
        # InnoDB-consistent snapshot without taking a global lock. The
        # maintenance gate already stops new writes (ADR-0204); this makes the
        # dump self-consistent even so, and keeps it from blocking the drainer.
        "--single-transaction",
        "--skip-lock-tables",
        # Emit CREATE DATABASE/USE so the artifact restores into a scratch
        # instance standalone — car G's restore-verification needs that.
        "--databases",
        cfg.database,
    ]

    try:
        with target.open("wb") as handle:
            completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
                cmd,
                stdout=handle,
                stderr=subprocess.PIPE,
                timeout=DUMP_TIMEOUT_SEC,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"mariadb-dump timed out after {DUMP_TIMEOUT_SEC}s") from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"mariadb-dump could not write {target}: {exc}") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode(errors="replace").strip()
        # Remove the partial: a truncated .sql sitting beside good ones reads as
        # a valid backup to anything that only globs for the pattern.
        target.unlink(missing_ok=True)
        raise RuntimeError(f"mariadb-dump exited {completed.returncode}: {stderr[:400]}")

    size = target.stat().st_size
    if size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"mariadb-dump produced an empty artifact at {target}")

    logger.info(
        "engine #2 logical dump written",
        extra={
            "component": "admin.mariadb_dump",
            "action": "mariadb_dump",
            "outcome": "ok",
            "bytes": size,
            "database": cfg.database,
        },
    )
    return {
        "ok": True,
        "filename": target.name,
        "path": str(target),
        "bytes": size,
        "database": cfg.database,
        "label": label,
        "schema_marker": _SCHEMA_MARKER,
    }
