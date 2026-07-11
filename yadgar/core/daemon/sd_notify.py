"""Minimal sd_notify helper for systemd readiness/lifecycle signalling.

No libsystemd dependency. Writes \\n-separated key=value pairs to
$NOTIFY_SOCKET (AF_UNIX, SOCK_DGRAM) per sd_notify(3).

Silent no-op when $NOTIFY_SOCKET is unset, empty, or the socket
write fails (e.g. running outside systemd). Daemons must not crash
when run via shell / debugger / non-systemd init.
"""

from __future__ import annotations

import logging
import os
import socket

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


@observe(tier="stage")
def notify(state: str) -> bool:
    """Send a single sd_notify payload.

    Args:
        state: raw payload string. Examples: "READY=1", "STOPPING=1",
               "RELOADING=1\\nMONOTONIC_USEC=12345", "MAINPID=1234".
               Multi-line payloads use literal \\n separators.

    Returns:
        True if a packet was sent (best-effort; no ack from systemd).
        False if NOTIFY_SOCKET is unset/empty or send failed.
    """
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return False
    try:
        # Abstract socket support: leading '@' in $NOTIFY_SOCKET means
        # Linux abstract namespace; replace with '\0'.
        if sock_path.startswith("@"):
            sock_path = "\0" + sock_path[1:]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(state.encode("utf-8"), sock_path)
            return True
        finally:
            s.close()
    except OSError as e:
        logger.debug("sd_notify send failed: %s", e)
        return False


def ready() -> bool:
    """Signal READY=1. Call once after daemon startup completes."""
    return notify("READY=1")


def stopping() -> bool:
    """Signal STOPPING=1. Call at the start of shutdown sequence."""
    return notify("STOPPING=1")


def reloading() -> bool:
    """Signal RELOADING=1. Call before SIGHUP-style config reload."""
    return notify("RELOADING=1")
