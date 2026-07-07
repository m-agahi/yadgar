"""capture subcommand — lightweight action capture (used by PostToolCall hooks)."""

import sys
from pathlib import Path


def cmd_capture(args):
    """Lightweight action capture — writes directly to DB without ML models."""
    from datetime import UTC, datetime

    from yadgar._shared.config import Settings
    from yadgar._shared.storage import StorageEngine

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    storage = StorageEngine(db_path)
    try:
        storage.insert_action_log(
            tool_name=args.tool_name,
            tool_input_summary=args.summary or "",
            directory=args.directory or "",
            session_id=args.session or "",
            timestamp=datetime.now(UTC).isoformat(),
        )
    except Exception as e:
        print(f"Failed to capture action: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        storage.close()


def register(subparsers):
    p = subparsers.add_parser("capture", help="Lightweight action capture")
    p.add_argument("--tool", dest="tool_name", required=True, help="Tool name")
    p.add_argument("--summary", type=str, default="", help="Tool input summary")
    p.add_argument("--directory", type=str, default="", help="Working directory")
    p.add_argument("--session", type=str, default="", help="Session ID")
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.set_defaults(func=cmd_capture)
