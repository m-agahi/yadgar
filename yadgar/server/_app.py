"""FastMCP application instance, _tool decorator, and middleware wrappers.

Leaf module — no imports from other yadgar.server.* modules.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from yadgar.config import get_settings
from yadgar.tracing import setup_tracing

settings = get_settings()

# ── Distributed tracing — v5.6.3 ─────────────────────────────────────────────
# Initialise OTel TracerProvider + LogSpanProcessor early (module import time).
# HTTPXClientInstrumentor is also activated here so all httpx calls in core
# auto-inject W3C traceparent headers.
setup_tracing("yadgar-core")
try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

    HTTPXClientInstrumentor().instrument()
except Exception:
    pass  # OTel not available — no-op

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


def _patch_uvicorn_shutdown_timeout() -> None:
    """Inject timeout_graceful_shutdown into both uvicorn-backed transports.

    FastMCP builds its own uvicorn.Config without exposing a hook; we replace
    run_sse_async / run_streamable_http_async on the *instance* so the timeout
    from YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC is always forwarded.

    Called once at module import time (bottom of this file) so the patch is
    in place before lifecycle.py calls mcp_server.run(transport=...).

    I12-note: this is shutdown-path only; zero impact on normal request latency.
    I9-note: <10 LOC of new logic, all in shutdown path.
    """
    import logging as _logging

    import uvicorn as _uvicorn

    _patch_logger = _logging.getLogger(__name__)
    _timeout = settings.ASGI_SHUTDOWN_TIMEOUT_SEC

    async def _sse_async_patched(self, mount_path=None) -> None:  # pragma: no cover
        starlette_app = self.sse_app(mount_path)
        config = _uvicorn.Config(
            starlette_app,
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower(),
            timeout_graceful_shutdown=_timeout,
            timeout_keep_alive=2,
            log_config=None,
        )
        server = _uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if server.should_exit:
                _patch_logger.info(
                    "ASGI shutdown complete (timeout_graceful_shutdown=%ss)", _timeout
                )

    async def _streamable_http_async_patched(self) -> None:  # pragma: no cover
        starlette_app = self.streamable_http_app()
        config = _uvicorn.Config(
            starlette_app,
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower(),
            timeout_graceful_shutdown=_timeout,
            timeout_keep_alive=2,
            log_config=None,
        )
        server = _uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if server.should_exit:
                _patch_logger.info(
                    "ASGI shutdown complete (timeout_graceful_shutdown=%ss)", _timeout
                )

    mcp_server.run_sse_async = _sse_async_patched.__get__(mcp_server, type(mcp_server))
    mcp_server.run_streamable_http_async = _streamable_http_async_patched.__get__(
        mcp_server, type(mcp_server)
    )


_patch_uvicorn_shutdown_timeout()


def _tool(power: bool = False):
    """Register a function as an MCP tool.

    power=True tools are omitted when YADGAR_PROFILE=minimal.
    Wraps each registered tool to record estimated token output in
    yadgar_tool_token_estimate_total{tool=<name>}.
    """
    import functools
    import json

    def _estimate_tokens(result) -> int:
        """Rough token estimate: len(str(result)) / 4."""
        try:
            if isinstance(result, (str, bytes)):
                text = (
                    result if isinstance(result, str) else result.decode("utf-8", errors="replace")
                )
            else:
                text = json.dumps(result, default=str)
            return max(1, len(text) // 4)
        except Exception:
            return 0

    def decorator(func):
        if power and _PROFILE == "minimal":
            return func  # skip registration; function still callable internally

        # v5.6.3: wrap with trace_span so every tool call is traceable.
        # trace_span is applied at decoration time (before mcp_server.tool() wraps it).
        from yadgar.tracing import trace_span as _trace_span  # noqa: PLC0415

        _traced_func = _trace_span(f"tool.{func.__name__}")(func)

        @functools.wraps(func)
        def _instrumented(*args, **kwargs):
            import time as _time

            _t0 = _time.monotonic()
            _status = "ok"
            try:
                result = _traced_func(*args, **kwargs)
            except Exception:
                _status = "error"
                raise
            finally:
                try:
                    from yadgar.metrics import (  # noqa: PLC0415
                        yadgar_mcp_request_count,
                        yadgar_mcp_request_duration_ms,
                    )

                    _elapsed_ms = (_time.monotonic() - _t0) * 1000
                    yadgar_mcp_request_duration_ms.labels(tool=func.__name__).observe(_elapsed_ms)
                    yadgar_mcp_request_count.labels(tool=func.__name__, status=_status).inc()
                except Exception:
                    pass
            try:
                from yadgar.metrics import yadgar_tool_token_estimate_total  # noqa: PLC0415

                est = _estimate_tokens(result)
                yadgar_tool_token_estimate_total.labels(tool=func.__name__).inc(est)
            except Exception:
                pass
            return result

        return mcp_server.tool()(_instrumented)

    return decorator
