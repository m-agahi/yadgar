"""Bearer-token authentication middleware for the Yadgar MCP server.

Implements the daemon-lockdown bridge pattern:
- When YADGAR_REQUIRE_AUTH=False: middleware is a no-op.
  A WARN is logged once at startup so operators know auth is disabled.
- When YADGAR_REQUIRE_AUTH=True (default): all /api/* and /hooks/* routes
  require a valid bearer token in the Authorization header.
- /health and /metrics are always unauthenticated (exempt paths).

This is a pure ASGI middleware so it wraps any Starlette/FastAPI app.
"""

from __future__ import annotations

import hmac
import logging
import os
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths that bypass auth even when REQUIRE_AUTH=True
_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})

# Route prefixes that are protected when REQUIRE_AUTH=True
_PROTECTED_PREFIXES: tuple[str, ...] = ("/admin/", "/api/", "/hooks/", "/mcp")

# Paths gated by YADGAR_DEBUG_APIS_ENABLED in addition to bearer token.
# NOTE: /api/control/update is excluded — it has its own gate (YADGAR_UPDATE_DEBUG_APIS_ENABLED).
_DEBUG_API_PREFIXES: tuple[str, ...] = (
    "/api/control/config",
    "/api/control/action/",
    "/api/control/restart/",
)

_startup_warned = False


class BearerAuthMiddleware:
    """ASGI bearer-token middleware.

    Reads YADGAR_REQUIRE_AUTH and YADGAR_MCP_AUTH_TOKEN from environment
    on *each request* so the settings can be changed without restarting
    (useful for the final auth-on flip without downtime).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._log_startup()

    def _log_startup(self) -> None:
        global _startup_warned
        if _startup_warned:
            return
        _startup_warned = True
        require = _is_auth_required()
        if not require:
            logger.warning(
                "WARN: Yadgar auth disabled (YADGAR_REQUIRE_AUTH=0). "
                "All /api/* and /hooks/* routes are unauthenticated. "
                "Ensure YADGAR_REQUIRE_AUTH=1 and YADGAR_MCP_AUTH_TOKEN are set in production."
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Exempt paths always pass through
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # Debug-API gate: must fire before auth-required check (gate-off → 403 even with valid token,
        # and even when auth is disabled). Applies only to the new control API paths, NOT /api/control/update.
        if _is_debug_api_path(path) and not _is_debug_apis_enabled():
            response = JSONResponse(
                {"error": "debug APIs disabled"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        # Check if this path is protected
        if not _is_protected(path):
            await self.app(scope, receive, send)
            return

        # If auth disabled, pass through (log disabled only on startup)
        if not _is_auth_required():
            await self.app(scope, receive, send)
            return

        # Auth enabled — require valid bearer token
        # PR-B: time the auth check from here to decision (not exempt/disabled fast-paths).
        _auth_t0 = time.perf_counter()

        token = _extract_bearer(scope)
        expected = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

        if not expected:
            # Auth is required but no token configured — fail secure
            logger.error(
                "YADGAR_REQUIRE_AUTH=1 but YADGAR_MCP_AUTH_TOKEN is not set. "
                "All authenticated routes will return 401."
            )
            _observe_auth_duration(_auth_t0)
            response = JSONResponse(
                {"error": "Server misconfiguration: auth token not set"},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        if not hmac.compare_digest(token.encode(), expected.encode()):
            _observe_auth_duration(_auth_t0)
            response = JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="yadgar"'},
            )
            await response(scope, receive, send)
            return

        _observe_auth_duration(_auth_t0)
        await self.app(scope, receive, send)


def _is_auth_required() -> bool:
    """Return True when YADGAR_REQUIRE_AUTH is truthy (default: True)."""
    return os.environ.get("YADGAR_REQUIRE_AUTH", "1").lower() in ("1", "true", "yes")


def _is_protected(path: str) -> bool:
    """Return True when the path falls under a protected prefix."""
    for prefix in _PROTECTED_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _extract_bearer(scope: Scope) -> str:
    """Extract bearer token from Authorization header, or empty string."""
    headers = dict(scope.get("headers", []))
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _observe_auth_duration(t0: float) -> None:
    """Observe elapsed ms since t0 into yadgar_mcp_auth_check_duration_ms. Non-fatal."""
    try:
        from yadgar.metrics import yadgar_mcp_auth_check_duration_ms  # noqa: PLC0415

        elapsed_ms = (time.perf_counter() - t0) * 1000
        yadgar_mcp_auth_check_duration_ms.observe(elapsed_ms)
    except Exception:
        pass


def _is_debug_api_path(path: str) -> bool:
    """Return True when path is gated by YADGAR_DEBUG_APIS_ENABLED.

    Covers /api/control/config, /api/control/action/*, /api/control/restart/*.
    Explicitly excludes /api/control/update (governed by YADGAR_UPDATE_DEBUG_APIS_ENABLED).
    """
    for prefix in _DEBUG_API_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_debug_apis_enabled() -> bool:
    """Return True when YADGAR_DEBUG_APIS_ENABLED is truthy (on/true/1/yes, case-insensitive)."""
    return os.environ.get("YADGAR_DEBUG_APIS_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
