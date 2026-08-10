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
    # post-compact-rehydrate.sh). C7 re-keys the restore read from
    # ``directory`` onto this value; until then it is resolved but unused,
    # which is deliberate — nothing here substitutes a project when the mint
    # cannot name one.
    resolve_cli_project(getattr(args, "project", None), args.directory, required=False)
    result = forward_restore(args.directory)
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
