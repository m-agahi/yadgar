"""snapshot subcommand — host-ops snapshot/backup operator surface.

Car J (2026-08-14 train): ``restore_snapshot`` (core/backup/backup.py) was only
callable from tests.  This file exposes it via ``yadgar snapshot restore
--snapshot <path> [--backend-url <url>]`` so an operator can recover a daemon
without going through the nightly cycle.

The parent ``snapshot`` is a subcommand shell so future snapshot host-ops
(create, list, prune) can attach under it; only ``restore`` is wired today.

The CLI calls :func:`yadgar.core.backup.restore_snapshot` directly — it does
NOT forward to the backend over HTTP.  ``restore_snapshot`` is itself an HTTP
caller (POST /import to the live SurrealDB), so the host CLI invoking it is
the right place: a containerised backend cannot reach the user's local
``backend_url``, and a token resolution would still hand the host its own
secrets.  The CLI is the operator path; the nightly cycle is the automated
one.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _default_backend_url() -> str:
    """Default for ``--backend-url``: $YADGAR_DB_URL or loopback:8000.

    Mirrors the defaulting in :mod:`yadgar.core.cli.vacuum` so a snapshot
    restore looks like the same kind of operation to the operator (it also
    POSTs to the backend's /import endpoint).
    """
    return os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8000")


def cmd_snapshot_restore(args: argparse.Namespace) -> int:
    """Run ``restore_snapshot`` against the live backend at ``--backend-url``.

    Resolves the snapshot path, delegates to the core helper, returns 0 on
    success and surfaces RuntimeError as a non-zero exit with the message on
    stderr.  No DB, no auth — ``restore_snapshot`` is the only thing that
    needs to talk to the backend, and it already raises cleanly on every
    failure mode.
    """
    from yadgar.core.backup import restore_snapshot

    snapshot_path = Path(args.snapshot)
    if not snapshot_path.exists():
        print(
            f"snapshot restore: snapshot does not exist: {snapshot_path}",
            file=sys.stderr,
        )
        return 2

    backend_url = args.backend_url
    try:
        restore_snapshot(snapshot_path=snapshot_path, backend_url=backend_url)
    except RuntimeError as exc:
        print(f"snapshot restore: {exc}", file=sys.stderr)
        return 1

    print(f"snapshot restored from {snapshot_path} into {backend_url}")
    return 0


def register(subparsers) -> None:
    """Attach ``yadgar snapshot restore`` to the top-level parser."""
    snapshot_p = subparsers.add_parser(
        "snapshot",
        help="Snapshot/backup host-ops (Car J: restore only)",
    )
    snapshot_sub = snapshot_p.add_subparsers(
        dest="snapshot_action",
        required=True,
    )

    restore_p = snapshot_sub.add_parser(
        "restore",
        help="Restore a .surql export snapshot into the live backend",
    )
    restore_p.add_argument(
        "--snapshot",
        type=str,
        required=True,
        help="Path to the .surql export produced by create_snapshot",
    )
    restore_p.add_argument(
        "--backend-url",
        type=str,
        default=_default_backend_url(),
        help="Live SurrealDB backend URL (default: $YADGAR_DB_URL or http://127.0.0.1:8000)",
    )
    restore_p.set_defaults(func=cmd_snapshot_restore)
