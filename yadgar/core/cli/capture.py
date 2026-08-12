"""capture subcommand — lightweight action capture (used by PostToolCall hooks)."""

import sys


def cmd_capture(args):
    """Lightweight action capture — enqueues an ``action_log`` job on the file queue.

    T2 Car E1 (ADR-0078): the CLI no longer opens the DB directly. The write
    rides the sanctioned file-queue seam; the backend QueueDrainer replays it
    via ``run_action_log_replay``. ``--db-path`` is retained for CLI
    compatibility but unused (the queue location comes from ``YADGAR_DATA_DIR``).

    C4 (0047 PR#40 §5): the payload carries ``project_id``, resolved HERE
    because this is the host-side process that can see the working tree. The
    consolidation summariser groups on that value; a row without one is
    skipped and counted rather than attributed to a guess. A tree with no
    resolvable identity exits non-zero — this command is invoked by the
    operator, not by an installed hook, so failing is safe and correct.
    """
    from datetime import UTC, datetime

    from yadgar._shared.file_queue.queue import FileQueue
    from yadgar.core.cli._shared import resolve_cli_project

    directory = args.directory or ""
    project_id = resolve_cli_project(getattr(args, "project", None), directory)

    try:
        FileQueue().enqueue(
            "action_log",
            {
                "tool_name": args.tool_name,
                "summary": args.summary or "",
                "directory": directory,
                "session_id": args.session or "",
                "project_id": project_id,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    except Exception as e:
        print(f"Failed to capture action: {e}", file=sys.stderr)
        sys.exit(1)


def register(subparsers):
    from yadgar.core.cli._shared import add_project_argument

    p = subparsers.add_parser("capture", help="Lightweight action capture")
    add_project_argument(p)
    p.add_argument("--tool", dest="tool_name", required=True, help="Tool name")
    p.add_argument("--summary", type=str, default="", help="Tool input summary")
    p.add_argument("--directory", type=str, default="", help="Working directory")
    p.add_argument("--session", type=str, default="", help="Session ID")
    p.add_argument(
        "--db-path", type=str, default=None, help="Deprecated: unused (queue seam write)"
    )
    p.set_defaults(func=cmd_capture)
