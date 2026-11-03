"""SessionStart auto-registration route — ADR-1131.

Endpoint:
  POST /hooks/project-register  — create the registry row for the minted id

The SessionStart hook runs host-side, where the backend URL is usually not
configured, so it cannot call the backend ``/admin`` route itself. It posts
the id it minted here instead; the daemon runs the same
``register_project`` the ``project_register`` MCP tool runs, which also drops
this process's registry cache so the next ``memorize`` sees the row.

Body: ``{"project_id": str, "display_name"?: str}``.

Response: always 200 with ``{"ok", "project_id", "created"[, "error"]}`` for
a well-formed body. A shape-guard rejection or backend failure is ``ok:
False`` with the reason, NOT a 4xx: the hook's HTTP helper turns any HTTP
error into ``None`` and would lose the reason it prints in the banner.
Invalid JSON is a 400.

Gate: bearer auth (the middleware guards every ``/hooks/*`` path).

Registered as a side-effect import in yadgar/core/server/__init__.py.
"""

from __future__ import annotations

import asyncio

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.server._app import mcp_server


@observe(tier="boundary", metric="core.routes.project_register")
async def project_register_handler(request: Request) -> JSONResponse:
    """Parse the body and run ``register_project`` off the event loop."""
    try:
        body = await request.json()
    except (ValueError, ClientDisconnect):  # fmt: skip
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be an object"}, status_code=400)

    from yadgar.core import project_registration

    display_name = body.get("display_name")
    result = await asyncio.to_thread(
        project_registration.register_project,
        body.get("project_id"),
        display_name if isinstance(display_name, str) else None,
    )
    return JSONResponse(result)


@mcp_server.custom_route("/hooks/project-register", methods=["POST"])
@trace_span()
async def hook_project_register(request: Request) -> JSONResponse:
    """Create the registry row for a hook-minted project id (ADR-1131)."""
    return await project_register_handler(request)
