"""vacuum subcommand — compact the SurrealKV DB."""

import sys


def cmd_vacuum(args):
    """Compact the SurrealKV DB via export → snapshot → swap → reimport.

    The yadgar-backend daemon must be running (vacuum calls /export over HTTP).
    Stop/start of both daemons is handled automatically by the service-mode
    abstraction (systemd | docker | manual).

    See yadgar/vacuum.py for the full implementation.
    """
    from yadgar.vacuum import cmd_vacuum_impl

    exit_code = cmd_vacuum_impl(args)
    if exit_code != 0:
        sys.exit(exit_code)


def register(subparsers):
    p = subparsers.add_parser(
        "vacuum",
        help="Compact the SurrealKV DB via export → snapshot → swap → reimport",
    )
    p.add_argument("--db-path", type=str, default=None, help="Database path override")
    p.add_argument(
        "--backend-url",
        type=str,
        default="http://127.0.0.1:8080",
        help="yadgar-backend HTTP endpoint (default: http://127.0.0.1:8080)",
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
