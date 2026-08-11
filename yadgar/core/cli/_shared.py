"""Shared helpers used by multiple CLI subcommands.

T2 Car B: ``init_replay_lightweight`` (local engine construction) is GONE —
CheckpointRestore moved to ``yadgar.backend.restoration`` behind the backend
``POST /restore`` forward, so the CLI no longer builds a local replay stack.
The drain/restore subcommands call the backend over HTTP via the same
``_forward_restore`` / ``_forward_admin`` helpers the MCP server uses
(``YADGAR_EMBED_URL`` convention; fail-loud RuntimeError when unset).
"""


def add_project_argument(parser) -> None:
    """Attach the C4 ``--project`` flag to a project-scoped subcommand.

    One wording, one default, one place — mirrors the pre-existing
    ``--project`` on ``yadgar code-graph query|refresh``.
    """
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="project_id (owner/repo). Default: minted from the working tree",
    )


def resolve_cli_project(
    project: str | None, directory: str, *, required: bool = True
) -> str | None:
    """Return the CLI's effective ``project_id``, or exit non-zero.

    C4 (0047 PR#40 §5). The CLI runs **host-side**, so unlike anything under
    ``yadgar/backend/**`` or ``yadgar/_shared/**`` it MAY call the C2 mint
    directly — the same carve-out the SessionStart hook has, and the honest
    one: this process can see the git worktree, the containers cannot.

    Precedence: an explicit ``--project`` wins (the cross-project override),
    otherwise the tree is minted. There is no third tier and no default —
    ADR-0227: "A missing or unresolved project_id FAILS LOUD […] it is never
    defaulted, never inferred, never silently substituted."

    Failure is ``SystemExit(2)`` carrying the actionable notice, which by
    construction contains no candidate key: an operator must not be able to
    copy a plausible-looking id out of the error and pass it back in.

    ``required=False`` is for the two COMPACTION-HOOK commands, ``drain`` and
    ``restore`` (and the local-read ``context``). They are invoked from
    ``pre-compact-drain.sh`` / ``post-compact-rehydrate.sh``. C10g made
    ``restore`` CONSUME the value (its memory-backed sinks are keyed on it),
    and C11 still owes the checkpoint column. Exiting there would mean an
    unresolvable tree
    silently loses the checkpoint the drain exists to save, which is the same
    "worse than reporting it" trade ADR-0227 accepts for the nightly cycle.
    They therefore pass the value through when it resolves and pass ``None``
    when it does not; they still never substitute one.

    Args:
        project: the caller's ``--project`` value, or ``None``.
        directory: working tree to mint from when ``project`` is absent.
        required: exit non-zero when the mint fails (default). ``False``
            returns ``None`` instead — see above; it never invents a value.

    Returns:
        The effective project_id, or ``None`` when ``required=False`` and the
        tree has no resolvable identity. Never returns on a required failure.
    """
    import sys

    if project is not None and project.strip():
        return project.strip()

    from yadgar.core.hooks._identity_mint import (
        UnresolvableProjectError,
        mint_failure_notice,
        mint_project_id,
    )

    try:
        return mint_project_id(directory)
    except UnresolvableProjectError as exc:
        if not required:
            return None
        print(mint_failure_notice(directory, str(exc)), file=sys.stderr)
        print(
            "[yadgar] Or name it for this command: --project owner/repo",
            file=sys.stderr,
        )
        sys.exit(2)


def silence_logging() -> None:
    """Suppress all library logging — hooks must only output data to stdout."""
    import logging

    logging.disable(logging.CRITICAL)


def forward_restore(directory: str, project_id: str | None = None) -> dict:
    """POST /restore on the backend and return the restore payload dict.

    C10g (0047 PR#40 §5): threads the host-resolved project_id alongside the
    path. Restore's sinks key on different columns — the memory-backed ones on
    the project_id, the checkpoint + memory-block ones still on the path.
    ``None`` (this CLI resolves non-fatally) means the memory buckets come back
    empty; it never widens them.
    """
    from yadgar.core.forward import _forward_restore

    return _forward_restore(directory, project_id=project_id)


def forward_pre_compact_drain(
    directory: str, transcript_path: str | None = None, project_id: str | None = None
) -> dict:
    """Run the pre-compact drain writes via the backend /admin forward.

    HOOKS Car 2: optional transcript_path threads in-flight orchestration capture
    through the CLI (Path B) to match the HTTP hook path. None → pre-Car-2.

    Car fix-drain-inflight (v5.135): the in-flight capture is done HERE, on the
    HOST, because this CLI process runs on the host where the ``.claude``
    transcript + the git worktree tree are visible. In the containerized deploy
    the backend cannot see either, so parsing there produced an empty in_flight
    (the bug). We parse host-side and carry the result in the /admin payload; the
    backend persists it verbatim. ``capture_in_flight`` never raises.
    """
    from yadgar.core.forward import _forward_admin

    in_flight = None
    if transcript_path:
        from yadgar._shared.restoration.transcript_parse import capture_in_flight

        in_flight = capture_in_flight(transcript_path, directory)

    # C11: ``project_id`` rides the payload so the auto-checkpoint row can be
    # stamped once migration 033 adds the ``checkpoint.project_id`` column.
    # ``pre_compact_drain`` picks the keys it knows, so carrying it now is
    # inert — but it means C11 has a value to consume instead of a hole.
    return _forward_admin(
        "pre_compact_drain",
        {
            "directory": directory,
            "transcript_path": transcript_path,
            "in_flight": in_flight,
            "project_id": project_id,
        },
    )
