"""Shared helpers used by multiple CLI subcommands.

T2 Car B: ``init_replay_lightweight`` (local engine construction) is GONE —
CheckpointRestore moved to ``yadgar.backend.restoration`` behind the backend
``POST /restore`` forward, so the CLI no longer builds a local replay stack.
The drain/restore subcommands call the backend over HTTP via the same
``_forward_restore`` / ``_forward_admin`` helpers the MCP server uses
(``YADGAR_EMBED_URL`` convention; fail-loud RuntimeError when unset).
"""


def silence_logging() -> None:
    """Suppress all library logging — hooks must only output data to stdout."""
    import logging

    logging.disable(logging.CRITICAL)


def forward_restore(directory: str) -> dict:
    """POST /restore on the backend and return the restore payload dict."""
    from yadgar.core.forward import _forward_restore

    return _forward_restore(directory)


def forward_pre_compact_drain(directory: str, transcript_path: str | None = None) -> dict:
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

    return _forward_admin(
        "pre_compact_drain",
        {
            "directory": directory,
            "transcript_path": transcript_path,
            "in_flight": in_flight,
        },
    )
