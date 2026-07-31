"""Read-only DB inspection route — ADR-0078 sanctioned debug read path.

Endpoint:
  POST /api/debug/read_query  — forward a read-only SurrealQL query to the backend

The query executes backend-side on the VIEWER-role RO DB connection (the DB
rejects writes regardless of query text — ADR-0078). Core touches zero DB: this
route is a thin forwarder to the backend ``POST /read_query`` via
``_forward_read_query``.

Gate: bearer + ``YADGAR_DEBUG_APIS_ENABLED=on`` — enforced in BearerAuthMiddleware
via ``_DEBUG_API_PREFIXES`` (``/api/debug/read_query``) before the request reaches
this handler; the handler re-checks the flag for defence-in-depth (mirrors
``/api/logs/*`` per ADR-0013). NOT auth-only.

Registered as a side-effect import in yadgar/core/server/__init__.py.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.forward import _forward_read_query
from yadgar.core.server._app import mcp_server

logger = logging.getLogger(__name__)


class ReadQueryBody(BaseModel):
    """Request body for POST /api/debug/read_query."""

    query: str
    params: dict = {}  # noqa: RUF012 — Pydantic model field default, not a mutable class attr
    timeout_ms: int = 5000

    model_config = {"extra": "forbid"}


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
async def read_query_handler(request: Request) -> JSONResponse:
    """POST /api/debug/read_query — forward a read-only query to the backend.

    Defence-in-depth gate re-check (the middleware already gated the prefix).
    Then forward to backend ``/read_query`` — the DB's VIEWER role is the real
    safety guard.
    """
    denied = _gate_check()
    if denied is not None:
        return denied

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON body
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    try:
        body = ReadQueryBody(**payload)
    except (ValidationError, TypeError) as exc:  # fmt: skip
        return JSONResponse({"error": f"invalid request: {exc}"}, status_code=400)

    try:
        result = _forward_read_query(body.query, body.params, timeout_ms=body.timeout_ms)
    except httpx.HTTPStatusError as exc:
        # Backend rejected (write keyword / malformed query / write rejected by
        # the VIEWER role) → propagate its status + detail.
        detail: object = exc.response.text
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"error": detail}, status_code=exc.response.status_code)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    return JSONResponse(result)


@mcp_server.custom_route("/api/debug/read_query", methods=["POST"])
@trace_span()
async def read_query(request: Request) -> JSONResponse:
    """Read-only DB inspection: forward a SurrealQL query to the backend."""
    return await read_query_handler(request)
