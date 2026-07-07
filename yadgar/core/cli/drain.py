"""drain subcommand — pre-compaction context drain."""


def cmd_drain(args):
    """Pre-compaction drain: save context to DB before Claude compacts."""
    import json

    from yadgar.core.cli._shared import init_replay_lightweight

    directory = args.directory
    storage, replay = init_replay_lightweight(args.db_path)
    try:
        result = replay.pre_compact_drain(directory)
        # Output JSON to stdout so hook can parse it if needed
        print(json.dumps(result))
    finally:
        storage.close()


def register(subparsers):
    p = subparsers.add_parser("drain", help="Pre-compaction context drain")
    p.add_argument("directory", help="Project directory")
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.set_defaults(func=cmd_drain)
