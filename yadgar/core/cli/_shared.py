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
    from yadgar.core.server.tools._forward import _forward_restore

    return _forward_restore(directory)


def forward_pre_compact_drain(directory: str) -> dict:
    """Run the pre-compact drain writes via the backend /admin forward."""
    from yadgar.core.server.tools._forward import _forward_admin

    return _forward_admin("pre_compact_drain", {"directory": directory})
