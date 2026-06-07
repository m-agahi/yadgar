"""v5.48.0 — `yadgar update [--check]` CLI subcommand.

Detects the install method and probes PyPI for the latest version.
Prints the upgrade command for the user to run manually.

v5.48 ships CHECK-ONLY:
  --install flag is NOT shipped (pipx upgrade kills the daemon mid-call).
  Deferred to v5.49 once a graceful-restart primitive exists.

Usage:
  yadgar update           # same as --check
  yadgar update --check   # probe PyPI, print upgrade command, exit 0
"""

from __future__ import annotations

import argparse
import sys


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `update` subcommand."""
    parser = subparsers.add_parser(
        "update",
        help="Check for a newer yadgar version and print the upgrade command.",
        description=(
            "Probes PyPI for the latest yadgar version and prints the correct upgrade command "
            "for your install method. Run the printed command yourself to upgrade.\n\n"
            "v5.48: check-only. --install is deferred to v5.49."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Probe PyPI and print upgrade command (default, read-only).",
    )
    parser.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> None:
    """Execute the update check."""
    from yadgar import __version__  # noqa: PLC0415
    from yadgar.update import install_methods  # noqa: PLC0415
    from yadgar.update.check import probe_latest_version  # noqa: PLC0415

    method = install_methods.detect_install_method()
    cmd = install_methods.upgrade_command(method)
    can_self = install_methods.can_self_install(method)

    print(f"yadgar {__version__}")
    print(f"Install method: {method}")

    try:
        result = probe_latest_version()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not check for updates — {exc}", file=sys.stderr)
        print(f"Upgrade command: {cmd}")
        sys.exit(0)

    available = result.available_version
    update_available = available != __version__

    if update_available:
        print(f"Update available: {available}")
        print(f"Release notes: https://pypi.org/project/yadgar/{available}/")
    else:
        print(f"Up to date ({__version__})")

    if not can_self:
        print("Manual upgrade required:")
    else:
        print("Upgrade command:")

    print(f"  {cmd}")
    sys.exit(0)
