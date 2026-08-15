"""``yadgar migrate ...`` subcommand — operator-invoked migrations.

Car D (2026-08-14 train, §3). Wires the corpus re-key migration from
``yadgar.core.migrations.rekey_corpus`` into the top-level CLI
parser as ``yadgar migrate rekey [--map PATH] [--apply] [--force]``.

The subcommand is fail-soft on a missing backend (the migration is a
dry run that uses the corpus-discovery admin op via ``_forward_admin``,
and a missing daemon surfaces as ``no_directories`` rather than an
exception).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from yadgar._shared.observability.observe import observe


@observe(tier="boundary", metric="core.cli.migrate.rekey")
def cmd_migrate_rekey(args: argparse.Namespace) -> int:
    """``yadgar migrate rekey`` handler.

    Dry-run by default — writes only the map TSV (gitignored). With
    ``--apply`` the operator confirms the registry seed and the row
    UPDATE; with ``--force`` an existing map is overwritten.

    Mirrors ``yadgar project seed``'s ``--map``-default-at-handler-time
    pattern (the default path is resolved in the backend module so
    test fixtures can point at a tmp dir without monkey-patching
    argparse).
    """
    from yadgar.core.migrations import rekey_corpus

    map_path = Path(args.map) if args.map else rekey_corpus.DEFAULT_MAP_PATH

    if args.apply and not map_path.exists():
        print(
            f"ERROR: --apply requires an existing map file; generate one first at {map_path}",
            file=sys.stderr,
        )
        return 2

    result = rekey_corpus.run(
        map_path=map_path,
        apply=args.apply,
        force=args.force,
    )

    print(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        reason = result.get("reason", "unknown")
        print(f"migrate rekey: FAILED ({reason})", file=sys.stderr)
        return 1
    return 0


@observe(tier="hot", span=False, metric="core.cli.migrate.register")
def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``yadgar migrate rekey`` to the top-level parser."""
    migrate_p = subparsers.add_parser(
        "migrate",
        help="Operator-invoked migrations (Car D — 2026-08-14 train)",
    )
    migrate_sub = migrate_p.add_subparsers(dest="migrate_command", required=True)

    rekey_p = migrate_sub.add_parser(
        "rekey",
        help=(
            "Corpus re-key migration — derive directory_context -> project_id, "
            "write a map TSV, optionally apply"
        ),
    )
    rekey_p.add_argument(
        "--map",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to the project-id map TSV (default: ./.yadgar/project-id-map.tsv)",
    )
    rekey_p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration (registry seed + row UPDATE)",
    )
    rekey_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing map file. Does not bypass the --apply guard.",
    )
    rekey_p.set_defaults(func=cmd_migrate_rekey)
