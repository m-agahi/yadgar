"""restore subcommand — post-compaction context restore."""


def cmd_restore(args):
    """Post-compaction restore: reconstruct context and print markdown to stdout."""
    from yadgar.cli._shared import init_replay_lightweight

    directory = args.directory
    storage, replay = init_replay_lightweight(args.db_path)
    try:
        result = replay.restore(directory)
        formatted = result.get("formatted", "")
        if formatted:
            print(formatted)
    finally:
        storage.close()


def register(subparsers):
    p = subparsers.add_parser("restore", help="Post-compaction context restore")
    p.add_argument("directory", help="Project directory")
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.set_defaults(func=cmd_restore)
