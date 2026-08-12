"""restore subcommand — post-compaction context restore.

T2 Car B: thin forwarder to the backend POST /restore endpoint (the restore
compute runs backend-side; see yadgar.backend.restoration). Requires
YADGAR_EMBED_URL — fail-loud RuntimeError when unset (forward-only).
"""


def cmd_restore(args):
    """Post-compaction restore: forward to the backend and print markdown to stdout."""
    from yadgar.core.cli._shared import forward_restore, resolve_cli_project, silence_logging

    silence_logging()
    # C4: resolved host-side, non-fatal (this runs inside
    # post-compact-rehydrate.sh). C10g wires the value through: restore's
    # memory-backed sinks (anchors, hot memories, gaps) are keyed on it, while
    # the checkpoint and memory-block sinks still key on the path.
    #
    # Still NON-FATAL, deliberately. When the mint cannot name a project this
    # passes None and the memory buckets come back empty — the checkpoint and
    # blocks, which are what a post-compact rehydrate most needs, still land.
    # Raising here would lose those too, and this runs unattended in a shell
    # hook where a traceback is a silent no-output.
    _project_id = resolve_cli_project(
        getattr(args, "project", None), args.directory, required=False
    )
    result = forward_restore(args.directory, project_id=_project_id)
    formatted = result.get("formatted", "")
    if formatted:
        print(formatted)


def register(subparsers):
    from yadgar.core.cli._shared import add_project_argument

    p = subparsers.add_parser("restore", help="Post-compaction context restore")
    add_project_argument(p)
    p.add_argument("directory", help="Project directory")
    # --db-path kept for CLI compatibility; ignored since T2 Car B (the restore
    # compute runs backend-side against the backend's own DB connection).
    p.add_argument("--db-path", type=str, default=None, help="Ignored (T2 Car B: forward-only)")
    p.set_defaults(func=cmd_restore)
