"""drain subcommand — pre-compaction context drain.

T2 Car B: thin forwarder — the drain writes (epoch increment + auto-checkpoint
upsert) run backend-side via the POST /admin op ``pre_compact_drain``
(CheckpointRestore moved to yadgar.backend.restoration). Requires
YADGAR_EMBED_URL — fail-loud RuntimeError when unset (forward-only).
"""


def cmd_drain(args):
    """Pre-compaction drain: forward to the backend before Claude compacts."""
    import json

    from yadgar.core.cli._shared import forward_pre_compact_drain, silence_logging

    silence_logging()
    result = forward_pre_compact_drain(args.directory, getattr(args, "transcript_path", None))
    # Output JSON to stdout so hook can parse it if needed
    print(json.dumps(result))


def register(subparsers):
    p = subparsers.add_parser("drain", help="Pre-compaction context drain")
    p.add_argument("directory", help="Project directory")
    # HOOKS Car 2: optional transcript path for in-flight orchestration capture.
    p.add_argument(
        "--transcript-path",
        type=str,
        default=None,
        help="Session transcript JSONL (captures in-flight agents/shells/worktrees)",
    )
    # --db-path kept for CLI compatibility; ignored since T2 Car B (the drain
    # writes run backend-side against the backend's own DB connection).
    p.add_argument("--db-path", type=str, default=None, help="Ignored (T2 Car B: forward-only)")
    p.set_defaults(func=cmd_drain)
