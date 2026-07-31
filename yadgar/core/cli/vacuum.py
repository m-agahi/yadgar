"""vacuum subcommand — compact the SurrealKV DB."""

import os
import sys


def _default_db_path() -> str | None:
    """Default for --db-path: $YADGAR_DATA_DIR/surreal_db if the env var is set,
    else None (vacuum_impl falls back to ~/.local/share/yadgar/surreal_db).
    Containers set YADGAR_DATA_DIR=/data and need that honored without
    redundant CLI flags in the systemd unit.
    """
    data_dir = os.environ.get("YADGAR_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "surreal_db")
    return None


def _default_backend_url() -> str:
    """Default for --backend-url: $YADGAR_DB_URL if set, else loopback:8000.
    Containers set YADGAR_DB_URL=http://yadgar-backend:8000.
    """
    return os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8000")


def cmd_vacuum(args):
    """Compact the SurrealKV DB via export → snapshot → swap → reimport.

    The yadgar-backend daemon must be running (vacuum calls /export over HTTP).
    Stop/start of both daemons is handled automatically by the service-mode
    abstraction (systemd | docker | manual).

    See yadgar/vacuum.py for the full implementation.
    """
    from yadgar.core.vacuum import cmd_vacuum_impl

    exit_code = cmd_vacuum_impl(args)
    if exit_code != 0:
        sys.exit(exit_code)


def register(subparsers):
    p = subparsers.add_parser(
        "vacuum",
        help="Compact the SurrealKV DB via export → snapshot → swap → reimport",
    )
    p.add_argument(
        "--db-path",
        type=str,
        default=_default_db_path(),
        help="Database path override (default: $YADGAR_DATA_DIR/surreal_db, else ~/.local/share/yadgar/surreal_db)",
    )
    p.add_argument(
        "--backend-url",
        type=str,
        default=_default_backend_url(),
        help="yadgar-backend HTTP endpoint (default: $YADGAR_DB_URL, else http://127.0.0.1:8000)",
    )
    p.add_argument(
        "--service-mode",
        choices=["systemd", "docker", "manual"],
        default=None,
        help="Service stop/start mode (default: auto-detected)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip confirmation prompt",
    )
    p.set_defaults(func=cmd_vacuum)
