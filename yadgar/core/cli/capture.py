"""capture subcommand — lightweight action capture (used by PostToolCall hooks)."""

import sys


def cmd_capture(args):
    """Lightweight action capture — enqueues an ``action_log`` job on the file queue.

    T2 Car E1 (ADR-0078): the CLI no longer opens the DB directly. The write
    rides the sanctioned file-queue seam; the backend QueueDrainer replays it
    via ``run_action_log_replay``. ``--db-path`` is retained for CLI
    compatibility but unused (the queue location comes from ``YADGAR_DATA_DIR``).
    """
    from datetime import UTC, datetime

    from yadgar._shared.file_queue.queue import FileQueue

    try:
        FileQueue().enqueue(
            "action_log",
            {
                "tool_name": args.tool_name,
                "summary": args.summary or "",
                "directory": args.directory or "",
                "session_id": args.session or "",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    except Exception as e:
        print(f"Failed to capture action: {e}", file=sys.stderr)
        sys.exit(1)


def register(subparsers):
    p = subparsers.add_parser("capture", help="Lightweight action capture")
    p.add_argument("--tool", dest="tool_name", required=True, help="Tool name")
    p.add_argument("--summary", type=str, default="", help="Tool input summary")
    p.add_argument("--directory", type=str, default="", help="Working directory")
    p.add_argument("--session", type=str, default="", help="Session ID")
    p.add_argument(
        "--db-path", type=str, default=None, help="Deprecated: unused (queue seam write)"
    )
    p.set_defaults(func=cmd_capture)
