"""One-time migration of the backend SurrealDB store off the legacy named volume.

Bug 11 moved the backend's ``/data`` onto the XDG data dir as a host bind mount.
``install_systemd_service`` and the install-tree templates complied; the
``yadgar daemon start`` path (``daemon.py::start_backend``) kept mounting the named
volume ``yadgar-db-data`` until task 0100 finished the job. Existing
``daemon start`` users therefore hold their entire DB inside that volume, and
flipping the mount without moving the data points the backend at an empty host
directory — which presents as total data loss.

This module runs the copy at the one moment nothing holds the store: inside
``start_backend``, before the backend container is launched.

Every design choice here is a guard against the failure mode that is NOT
recoverable. The store is surrealkv, and ADR-0090 records that a half-flushed or
half-copied surrealkv directory is corrupt-on-reopen — so:

* **All four trigger conditions must hold** (volume exists, volume holds a
  ``surreal_db``, host has none, nothing is running). Any of them
  *indeterminate* — a container runtime that will not answer — skips and warns
  rather than guessing. A user left on the named volume is inconvenienced; a
  user with a partial store is not recoverable.
* **The copy goes through a throwaway container**, never by reading
  ``/var/lib/containers/storage/volumes/...`` from the host: that path is
  podman-internal, is not stable API, and under rootless podman is not readable
  by the invoking user at all. ``--user root --security-opt label=disable``
  matches the canonical ownership the store carries under a rootless userns.
* **The copy lands on a temp sibling and is renamed into place**, so an
  interrupted copy can never leave a partial ``surreal_db`` for the next backend
  start to open as the live DB.
* **The named volume is never deleted.** It is the only rollback. Reaping it is
  deliberately left to a later release.

Cost note: while the legacy volume still exists, every ``daemon start`` runs one
``--rm`` probe container (a ``test -d``) to decide whether it holds a store. That
is deliberate — the loud "both locations have a store" warning is only honest if
we actually looked — and ``daemon start`` is not a hot path.

Guarded by ``yadgar/tests/core/test_backend_db_volume_migration.py``.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from yadgar._shared.observability.observe import observe

#: Printed by the probe container when the named volume really holds a store.
#: The probe is satisfied by this string on stdout, NOT by exit code 0 — a
#: runtime that exits 0 while printing nothing must never reach the copy.
PROBE_SENTINEL = "YADGAR_SURREAL_DB_PRESENT"

PROBE_SH = f"test -d /src/surreal_db && echo {PROBE_SENTINEL}"

#: The copy, run inside the throwaway container against ``$SRC`` / ``$DST``
#: / ``$TMP``. Exported (and executed for real against temp dirs) by the test
#: suite, so the shipped string is the one whose semantics are proven.
MIGRATE_SH = (
    "set -e\n"
    # An empty variable would turn the reap below into a catastrophic glob.
    'if [ -z "$SRC" ] || [ -z "$DST" ] || [ -z "$TMP" ]; then exit 2; fi\n'
    'if [ ! -d "$SRC/surreal_db" ]; then\n'
    '  echo "yadgar-migrate: $SRC/surreal_db vanished" >&2; exit 3\n'
    "fi\n"
    'if [ -e "$DST/surreal_db" ]; then\n'
    '  echo "yadgar-migrate: $DST/surreal_db already exists — refusing to overwrite" >&2\n'
    "  exit 4\n"
    "fi\n"
    # Reap temp siblings a previous interrupted attempt left behind; without this
    # a repeatedly-failing migration parks a full-size copy per `daemon start`.
    'rm -rf "$DST"/surreal_db.migrating-*\n'
    'cp -a "$SRC/surreal_db" "$DST/$TMP"\n'
    'mv "$DST/$TMP" "$DST/surreal_db"\n'
)

_PROBE_TIMEOUT_S = 60.0
_LIST_TIMEOUT_S = 30.0


def _warn(message: str) -> None:
    print(f"[yadgar-migrate] {message}", file=sys.stderr, flush=True)


def _throwaway(runtime: str, volume: str) -> list[str]:
    """The common prefix for a throwaway container over the legacy volume.

    ``--user root`` + ``--security-opt label=disable`` are required, not
    cosmetic: under a rootless userns the store's files are owned by the
    container-canonical uid, and SELinux relabelling of a shared volume would
    otherwise deny the read.
    """
    return [
        runtime,
        "run",
        "--rm",
        "--user",
        "root",
        "--security-opt",
        "label=disable",
        "-v",
        f"{volume}:/src:ro",
    ]


@observe(tier="stage")
def _volume_present(runtime: str, volume: str) -> bool | None:
    """True / False, or None when the runtime would not answer."""
    result = subprocess.run(
        [runtime, "volume", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        timeout=_LIST_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    return volume in (result.stdout or "").split()


@observe(tier="stage")
def _containers_holding(runtime: str, names: Sequence[str]) -> list[str] | None:
    """Running containers among *names*, or None when the runtime would not answer."""
    result = subprocess.run(
        [runtime, "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=_LIST_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    running = set((result.stdout or "").split())
    return [n for n in names if n in running]


@observe(tier="stage")
def _volume_holds_store(runtime: str, volume: str, image: str) -> bool:
    result = subprocess.run(
        [*_throwaway(runtime, volume), image, "sh", "-c", PROBE_SH],
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
    )
    return PROBE_SENTINEL in (result.stdout or "")


@observe(tier="stage")
def _copy_store(runtime: str, volume: str, data_dir: Path, image: str) -> dict:
    tmp_name = f"surreal_db.migrating-{int(time.time())}"
    _warn(
        f"legacy named volume {volume!r} holds the SurrealDB store and "
        f"{data_dir / 'surreal_db'} does not — copying it to the host bind mount. "
        f"This runs once; the volume is KEPT as the rollback."
    )
    result = subprocess.run(
        [
            *_throwaway(runtime, volume),
            "-v",
            f"{data_dir}:/dst",
            "-e",
            "SRC=/src",
            "-e",
            "DST=/dst",
            "-e",
            f"TMP={tmp_name}",
            image,
            "sh",
            "-c",
            MIGRATE_SH,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _warn(
            f"copy FAILED (exit {result.returncode}); nothing was written to "
            f"{data_dir / 'surreal_db'} and the volume {volume!r} is untouched. "
            f"The backend will start on an empty store — stop it and restore from "
            f"the volume before writing anything. {(result.stderr or '').strip()}"
        )
        return {"status": "failed", "reason": "copy_failed", "volume": volume}
    _warn(f"copy complete: {data_dir / 'surreal_db'} (rollback: volume {volume!r})")
    return {"status": "migrated", "volume": volume, "data_dir": str(data_dir)}


@observe(tier="boundary")
def migrate_named_volume_db(
    *,
    runtime: str,
    volume: str,
    data_dir: Path,
    image: str,
    container_names: Sequence[str],
) -> dict:
    """Copy the backend DB out of the legacy named volume onto the host bind mount.

    Returns ``{"status": "migrated" | "skipped" | "failed", "reason": ...}``. Never
    raises into the caller's start path: a migration that cannot prove it is safe
    skips, so the worst case is that the user keeps running on the named volume.
    """
    present = _volume_present(runtime, volume)
    if present is None:
        _warn(
            f"could not determine whether the legacy volume {volume!r} exists "
            f"({runtime} volume ls failed) — skipping the DB migration. If this "
            f"install predates the host bind mount, its DB is still in that volume."
        )
        return {"status": "skipped", "reason": "runtime_indeterminate"}
    if not present:
        # Fresh install (or the volume was already reaped): nothing to migrate,
        # and nothing worth saying about it.
        return {"status": "skipped", "reason": "volume_absent"}

    holding = _containers_holding(runtime, container_names)
    if holding is None:
        _warn(
            f"could not determine which containers are running ({runtime} ps "
            f"failed) — skipping the DB migration rather than risk copying a live "
            f"surrealkv store, which reopens corrupt (ADR-0090)."
        )
        return {"status": "skipped", "reason": "runtime_indeterminate"}
    if holding:
        _warn(
            f"{', '.join(holding)} still running — skipping the DB migration. "
            f"Copying a live surrealkv store yields a corrupt-on-reopen directory "
            f"(ADR-0090). Stop it (`yadgar daemon stop`) and start again."
        )
        return {"status": "skipped", "reason": "containers_running"}

    if not _volume_holds_store(runtime, volume, image):
        return {"status": "skipped", "reason": "volume_has_no_db"}

    host_store = data_dir / "surreal_db"
    if host_store.exists():
        _warn(
            f"BOTH locations hold a store: the legacy volume {volume!r} AND "
            f"{host_store}. Leaving the host store untouched and migrating "
            f"nothing — merging two surrealkv stores is not possible. The volume "
            f"is retained; if it is the one you want, stop yadgar and move "
            f"{host_store} aside before starting again."
        )
        return {"status": "skipped", "reason": "host_db_present"}

    data_dir.mkdir(parents=True, exist_ok=True)
    return _copy_store(runtime, volume, data_dir, image)
