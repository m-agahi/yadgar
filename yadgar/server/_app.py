"""FastMCP application instance, _tool decorator, and middleware wrappers.

Leaf module — no imports from other yadgar.server.* modules.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from yadgar.config import get_settings

settings = get_settings()

# ── Tool profile (read at import time — decorators execute on module load) ────
# YADGAR_PROFILE=minimal  →  10 core tools only
# YADGAR_PROFILE=full     →  all tools including power tier (default)
_PROFILE = os.environ.get("YADGAR_PROFILE", "full")

mcp_server = FastMCP(
    name="yadgar",
    instructions="Persistent memory engine for Claude Code — heat decay, sleep consolidation, and surprise-gated storage.",
    host=settings.HOST,
    port=settings.PORT,
)


# ── CORS: default-deny; configurable via YADGAR_ALLOWED_ORIGINS ───────────────
def _get_allowed_origins() -> list[str]:
    """Read allowed origins from config. Default: loopback only."""
    raw = os.environ.get("YADGAR_ALLOWED_ORIGINS", "")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        f"http://127.0.0.1:{settings.PORT}",
        f"http://localhost:{settings.PORT}",
        "http://127.0.0.1:42069",
        "http://localhost:42069",
    ]


def _cors_wrapped_http_app(self):
    from starlette.middleware.cors import CORSMiddleware

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    # Stack: BearerAuth (outermost) → RequestLogging → CORS → MCP
    inner = _orig_streamable_http_app(self)
    cors_app = CORSMiddleware(
        app=inner,
        allow_origins=_get_allowed_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    logged_app = RequestLoggingMiddleware(cors_app)
    return BearerAuthMiddleware(logged_app)


def _auth_wrapped_sse_app(self, mount_path=None):
    """Wrap SSE transport with BearerAuthMiddleware + RequestLogging (C-1).

    SSE is the default transport; without this wrapper REQUIRE_AUTH=1 has
    no effect on the SSE path.
    """
    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    inner = _orig_sse_app(self, mount_path)
    logged_app = RequestLoggingMiddleware(inner)
    return BearerAuthMiddleware(logged_app)


_orig_streamable_http_app = mcp_server.streamable_http_app.__func__
mcp_server.streamable_http_app = _cors_wrapped_http_app.__get__(mcp_server, type(mcp_server))

_orig_sse_app = mcp_server.sse_app.__func__
mcp_server.sse_app = _auth_wrapped_sse_app.__get__(mcp_server, type(mcp_server))


def _tool(power: bool = False):
    """Register a function as an MCP tool.

    power=True tools are omitted when YADGAR_PROFILE=minimal.
    """

    def decorator(func):
        if power and _PROFILE == "minimal":
            return func  # skip registration; function still callable internally
        return mcp_server.tool()(func)

    return decorator
