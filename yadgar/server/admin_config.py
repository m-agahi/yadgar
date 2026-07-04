"""GET /admin/config — expose runtime configuration (v5.6.7 PR-J).

Registered at import time via @mcp_server.custom_route (side-effect import).
Imported in server/__init__.py alongside server.http.

Auth-gated: /admin/ prefix is in BearerAuthMiddleware._PROTECTED_PREFIXES.

Returns JSON:
  {
    "config": [{"name": ..., "value": ..., "source": ..., "kind": ...}, ...],
    "generated_at": "<iso8601>"
  }

Entries are sorted alphabetically by name (stable diff-ability).
Secret values (name matches /(secret|token|key|password|auth)/i, or entry.redact=True)
are replaced with "<redacted>" — name + source are still reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar.config_registry import _set_config_gauges, build_config_table
from yadgar.observability.observe import observe
from yadgar.server._app import mcp_server


async def admin_config_handler(request: Request) -> JSONResponse:
    """Return full config table as JSON. Auth enforced by BearerAuthMiddleware."""
    table = build_config_table()
    _set_config_gauges()  # keep gauges live on every GET
    return JSONResponse(
        {
            "config": table,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


@mcp_server.custom_route("/admin/config", methods=["GET"])
@observe(tier="boundary")
async def admin_config(request: Request) -> JSONResponse:
    """Expose runtime configuration knobs.

    Auth-gated via BearerAuthMiddleware (/admin/ prefix is protected).
    Returns the full config table with redacted secrets.
    """
    return await admin_config_handler(request)
