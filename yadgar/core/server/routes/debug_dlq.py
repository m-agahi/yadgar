"""Read-only DLQ inspection route — debug-gated wrapper over ``dlq_inspect``.

Endpoint:
  GET /api/debug/dlq  — list dead-letter-queue entries (filesystem, not DB)

The DLQ lives on the local filequeue (``FileQueue.dlq_dir``), NOT the database.
This route is a thin wrapper over the existing ``dlq_inspect()`` core tool
(``yadgar.core.server.tools.admin_dlq``) — it forwards/reuses that path rather
than reading the DB. ADR-0078 (DB isolation) is therefore not engaged: no DB
read happens here at all.

Gate: bearer + ``YADGAR_DEBUG_APIS_ENABLED=on`` — enforced in BearerAuthMiddleware
via ``_DEBUG_API_PREFIXES`` (``/api/debug/dlq``) before the request reaches this
handler; the handler re-checks the flag for defence-in-depth (mirrors
``/api/debug/read_query`` and ``/api/logs/*``). NOT auth-only.

Query params:
  filter — optional; one of ``all`` | ``rejections`` | ``failures`` (see
           ``dlq_inspect``). Omitted → all entries.

Registered as a side-effect import in yadgar/core/server/__init__.py.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.server._app import mcp_server

logger = logging.getLogger(__name__)


def _is_debug_apis_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_DEBUG_APIS_ENABLED",
        "DEBUG_APIS_ENABLED",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )


@observe(tier="stage")
def _gate_check() -> JSONResponse | None:
    """Return 403 JSONResponse when debug APIs disabled, else None."""
    if not _is_debug_apis_enabled():
        return JSONResponse({"error": "debug APIs disabled"}, status_code=403)
    return None


@observe(tier="boundary")
async def dlq_handler(request: Request) -> JSONResponse:
    """GET /api/debug/dlq — list DLQ entries via the ``dlq_inspect`` tool.

    Defence-in-depth gate re-check (the middleware already gated the prefix),
    then forward to the filesystem-backed ``dlq_inspect()`` — no DB access.
    """
    denied = _gate_check()
    if denied is not None:
        return denied

    # Import lazily so the gate short-circuit path stays cheap and the module
    # import graph does not pull the tool at route-registration time.
    from yadgar.core.server.tools.admin_dlq import dlq_inspect  # noqa: PLC0415

    filter_ = request.query_params.get("filter")
    try:
        entries = dlq_inspect(filter_)
    except Exception as exc:  # noqa: BLE001 — surface any inspection failure as 500
        logger.warning("dlq_inspect failed: %s", exc)
        return JSONResponse({"error": f"dlq inspection failed: {exc}"}, status_code=500)

    return JSONResponse({"entries": entries, "count": len(entries)})


@mcp_server.custom_route("/api/debug/dlq", methods=["GET"])
@trace_span()
async def dlq(request: Request) -> JSONResponse:
    """Read-only DLQ inspection: list dead-letter-queue entries."""
    return await dlq_handler(request)
