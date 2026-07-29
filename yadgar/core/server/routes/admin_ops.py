"""Admin-op HTTP routes — host-side callers that cannot use the MCP transport.

Endpoint:
  POST /api/check_invariants — run the consistency checks + auto-repairs

Why this route exists (task:0045).  ``yadgar/core/vacuum`` runs as its own
one-shot systemd unit and verifies a freshly swapped-in DB by POSTing
``{core}/api/check_invariants``.  That URL was written in the vacuum module and
registered NOWHERE — it 404'd on every run for months, which the finalize path
read as "unverified" and used to roll the swap back.  Two forks were possible:
serve the route here, or point the vacuum at the backend ``/admin`` op directly.
The second needs ``YADGAR_EMBED_URL`` in ``yadgar-vacuum.service``, which the
unit does not set (it carries only ``YADGAR_DB_URL`` + ``YADGAR_DATA_DIR``, plus
``YADGAR_MCP_AUTH_TOKEN`` via its secrets ``EnvironmentFile``) — an out-of-repo
nix edit, and the same edit again on every other install surface.  Core already
has ``YADGAR_EMBED_URL``, so serving the route here is a zero-blast-radius fix.

Auth: bearer only.  ``/api/`` is a protected prefix in ``BearerAuthMiddleware``,
and ``/api/check_invariants`` is deliberately NOT under ``_DEBUG_API_PREFIXES``
— the vacuum unit runs without ``YADGAR_DEBUG_APIS_ENABLED`` and a debug-gated
route would 403 exactly like the 404 it replaces.

The handler is a thin wrapper over the existing ``check_invariants`` tool shell
(``yadgar.core.server.tools.admin_invariants``), which forwards to the backend
``/admin`` op — the route and the MCP tool therefore cannot drift.

Registered as a side-effect import in yadgar/core/server/__init__.py.
"""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.server._app import mcp_server

logger = logging.getLogger(__name__)


@observe(tier="boundary")
async def check_invariants_handler(request: Request) -> JSONResponse:
    """POST /api/check_invariants — forward to the ``check_invariants`` tool shell.

    Runs off the event loop (``asyncio.to_thread``): the backend op walks every
    memory + wiki row and is observed at 30-120 s on production-sized datasets,
    so blocking the loop here would starve ``/health/live``.

    A transport/config failure (``YADGAR_EMBED_URL`` unset, backend down) returns
    503 rather than 500 — it is an availability condition, not a bad request.
    Callers must treat a non-2xx as "could not check", never as "check failed".
    """
    from yadgar.core.server.tools.admin_invariants import check_invariants  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(check_invariants)
    except Exception as exc:  # noqa: BLE001 — any forward failure is an availability answer
        logger.warning("check_invariants route failed: %s", exc)
        return JSONResponse(
            {"ok": False, "error": f"check_invariants unavailable: {exc}"},
            status_code=503,
        )
    return JSONResponse(result)


@mcp_server.custom_route("/api/check_invariants", methods=["POST"])
@trace_span()
async def api_check_invariants(request: Request) -> JSONResponse:
    """Run memory-store consistency checks + auto-repairs (bearer-protected)."""
    return await check_invariants_handler(request)
