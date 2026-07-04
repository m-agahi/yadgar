"""POST /api/control/update — version check + upgrade-command endpoint (v5.48.0).

Auth:
  - Bearer token via BearerAuthMiddleware (automatically applied to /api/ prefix).
  - YADGAR_UPDATE_DEBUG_APIS_ENABLED=on gate (default off — power user / v5.50 UI).

Request body (optional JSON):
  {"action": "check" | "install", "install_method_override": str | null}

Response (200, action=check):
  {current_version, available_version, update_available, install_method,
   upgrade_command, release_notes_url, checked_at}

v5.48 ships CHECK-ONLY:
  action=install returns 400 — pipx upgrade kills daemon mid-call.
  Deferred to v5.49 once graceful-restart primitive exists.

Registered as a side-effect import in yadgar/server/__init__.py.
"""

from __future__ import annotations

import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar import __version__
from yadgar.config import resolve_knob
from yadgar.observability.observe import observe
from yadgar.server._app import mcp_server
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)


def _is_debug_apis_enabled() -> bool:
    """Return True when YADGAR_UPDATE_DEBUG_APIS_ENABLED=on (case-insensitive)."""
    return (
        resolve_knob(
            "YADGAR_UPDATE_DEBUG_APIS_ENABLED", "UPDATE_DEBUG_APIS_ENABLED", str, "off"
        ).lower()
        == "on"
    )


@observe(tier="boundary")
async def control_update_handler(request: Request) -> JSONResponse:
    """Handle POST /api/control/update.

    Auth-gated via BearerAuthMiddleware (/api/ prefix is protected).
    Also gated on YADGAR_UPDATE_DEBUG_APIS_ENABLED=on.
    """
    # Debug-APIs gate
    if not _is_debug_apis_enabled():
        return JSONResponse(
            {"error": ("Update API disabled. Set YADGAR_UPDATE_DEBUG_APIS_ENABLED=on to enable.")},
            status_code=403,
        )

    # Parse optional request body
    try:
        body = await request.json()
    except Exception:
        body = {}

    action = body.get("action", "check") if isinstance(body, dict) else "check"
    install_method_override = (
        body.get("install_method_override") if isinstance(body, dict) else None
    )

    # v5.48 CHECK-ONLY: action=install rejected regardless of can_self_install
    if action == "install":
        return JSONResponse(
            {
                "error": (
                    "action=install is not available in v5.48 (deferred to v5.49). "
                    "Run the upgrade command manually. "
                    "See upgrade_command in a check response for the correct incantation."
                )
            },
            status_code=400,
        )

    if action != "check":
        return JSONResponse(
            {"error": f"Unknown action: {action!r}. Supported: 'check'."},
            status_code=400,
        )

    # Detect install method
    from yadgar.update import install_methods  # noqa: PLC0415

    method = (
        install_method_override
        if install_method_override
        else install_methods.detect_install_method()
    )
    cmd = install_methods.upgrade_command(method)

    # Probe PyPI
    from yadgar.update.check import probe_latest_version  # noqa: PLC0415

    try:
        from yadgar.config import get_settings  # noqa: PLC0415

        _settings = get_settings()
        result = probe_latest_version(
            url=_settings.UPDATE_PYPI_URL,
            timeout=_settings.UPDATE_CHECK_TIMEOUT_SECONDS,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("PyPI probe failed (network): %s", exc)
        return JSONResponse(
            {"error": f"PyPI unreachable: {exc}"},
            status_code=503,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("PyPI probe failed (HTTP %s): %s", exc.response.status_code, exc)
        return JSONResponse(
            {"error": f"PyPI returned {exc.response.status_code}"},
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PyPI probe failed (unexpected): %s", exc)
        return JSONResponse(
            {"error": f"Version check failed: {exc}"},
            status_code=503,
        )

    update_available = result.available_version != __version__
    release_notes_url = f"https://pypi.org/project/yadgar/{result.available_version}/"

    return JSONResponse(
        {
            "current_version": __version__,
            "available_version": result.available_version,
            "update_available": update_available,
            "install_method": method,
            "upgrade_command": cmd,
            "release_notes_url": release_notes_url,
            "checked_at": result.checked_at,
        }
    )


@mcp_server.custom_route("/api/control/update", methods=["POST"])
@trace_span("api.control.update")
async def control_update(request: Request) -> JSONResponse:
    """Version check endpoint for Control-tab integration (v5.50) and CLI power users.

    Auth-gated via BearerAuthMiddleware (/api/ prefix is protected).
    Also requires YADGAR_UPDATE_DEBUG_APIS_ENABLED=on.

    POST body: {"action": "check"} (default) — install deferred to v5.49.
    """
    return await control_update_handler(request)
