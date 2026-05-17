"""seed subcommand — bootstrap memory for an existing project."""

import sys
from pathlib import Path


def cmd_seed(args):
    """Bootstrap memory for an existing project by scanning its structure."""
    import json

    from yadgar.seed import seed_project

    directory = str(Path(args.directory).resolve())
    print(f"Seeding project: {directory}", file=sys.stderr)

    result = seed_project(
        directory=directory,
        db_path=args.db_path,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(
            f"\n[DRY RUN] Would create {result['memories_generated']} memories for {result['project']}\n",
            file=sys.stderr,
        )
        for mem in result.get("memories", []):
            tags = ", ".join(mem["tags"])
            print(f"  [{tags}] {mem['content'][:120]}...", file=sys.stderr)
    else:
        replaced_msg = f", replaced {result['replaced']} old" if result.get("replaced") else ""
        print(
            f"\nSeeded {result['project']}: "
            f"{result['created']} created{replaced_msg} "
            f"(from {result['memories_generated']} total)",
            file=sys.stderr,
        )

    print(json.dumps(result))


def register(subparsers):
    p = subparsers.add_parser("seed", help="Bootstrap memory for an existing project")
    p.add_argument("directory", help="Project directory to scan and seed")
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.add_argument(
        "--dry-run", action="store_true", help="Scan and show what would be stored without storing"
    )
    p.set_defaults(func=cmd_seed)
