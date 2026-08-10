"""context subcommand — lightweight context query (used by SessionStart hooks)."""

from pathlib import Path


def cmd_context(args):
    """Lightweight context query — reads hot memories without loading ML models."""
    from yadgar._shared.config import Settings
    from yadgar._shared.storage import StorageEngine
    from yadgar.core.cli._shared import resolve_cli_project

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    directory = args.directory
    # C4: resolved host-side, non-fatal — this is a SessionStart read path and
    # must not refuse to print context because a tree has no remote.
    # C7: the query below still selects on ``directory_context``; when the read
    # path is re-keyed onto ``project_id`` this is the value it uses.
    resolve_cli_project(getattr(args, "project", None), directory, required=False)

    storage = None
    try:
        storage = StorageEngine(db_path)
        hot = (
            storage._q(
                "SELECT content, heat FROM memory "
                "WHERE directory_context = $dir AND heat >= 0 "
                "ORDER BY heat DESC LIMIT 6",
                {"dir": directory},
            )
            or []
        )
        anchored = (
            storage._q(
                "SELECT content FROM memory "
                "WHERE is_protected = true AND heat > 0 AND $anchor IN tags "
                "ORDER BY created_at DESC LIMIT 4",
                {"anchor": "_anchor"},
            )
            or []
        )
    except Exception:
        return
    finally:
        # Q21: always release SurrealKV lock so daemon can reopen
        if storage is not None:
            storage.close()

    if not hot and not anchored:
        return

    print("# Yadgar — Session Context\n")
    if anchored:
        print("## Critical Facts")
        for row in anchored:
            print(f"- {row['content'][:200]}")
        print()
    if hot:
        print("## Project Context")
        for row in hot:
            content = row["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"- [{row['heat']:.1f}] {content}")
        print()
    print(f"*Context for: {directory}*")


def register(subparsers):
    from yadgar.core.cli._shared import add_project_argument

    p = subparsers.add_parser("context", help="Lightweight context query")
    add_project_argument(p)
    p.add_argument("directory", help="Project directory")
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.set_defaults(func=cmd_context)
