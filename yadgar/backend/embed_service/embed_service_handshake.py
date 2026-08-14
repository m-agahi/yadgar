"""Car F (task #61) — backend-side version handshake helpers.

Mirror ``yadgar.core.server.http._handshake_block`` so the shape both
sides emit is byte-identical, which lets a single parser consume
either side. See ``yadgar._shared.version_compat`` for the bound
policy.

Extracted to a sibling of ``embed_service.py`` so the I13-accepted
single-file case (the embed_service.py module docstring pins the
~500 soft cap rationale and the ACCEPTED status) is not widened by
Car F's three-function addition. The new file stays well under 100
LOC; the existing file gains only the ``versions_compatible`` field
on the /health payload (one new key in the dict literal, no logic).
"""

from __future__ import annotations

import json
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.version_compat import handshake_status


@observe(tier="stage")
def backend_health_version() -> str:
    """Read the backend version out of the bundled server.json.

    Mirrors ``yadgar.core.daemon.runtime._backend_version`` — same source
    of truth (``server.json::backend_version``) — but defined here so
    the backend container does not need to import the host-side daemon
    module. Walks up from this file to find the repo root (contains
    ``pyproject.toml``) rather than re-implementing ``_source_root``
    from the host-side daemon. Returns ``"unknown"`` on any I/O / parse
    error so a fresh install never refuses itself.
    """
    try:
        here = Path(__file__).resolve().parent
        for candidate in [here, *here.parents]:
            if (candidate / "pyproject.toml").exists():
                return json.loads((candidate / "server.json").read_text()).get(
                    "backend_version", "unknown"
                )
    except Exception:  # noqa: BLE001 — best-effort, never loop self
        pass
    return "unknown"


@observe(tier="boundary")
def backend_handshake_block() -> dict:
    """Compute the ``versions_compatible`` block the backend advertises.

    The backend cannot probe the core on every /health call (the core
    is not always up when the backend is — that's the deploy window
    task #61 measures), so the *peer* side is left as ``"unknown"``
    until the core announces itself in a request header. The *self*
    side is always evaluable from server.json. Mirrors the core's
    "unverifiable" pass — never refuse, always surface.
    """
    return handshake_status(backend_health_version(), "unknown", side="backend")
