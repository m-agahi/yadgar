"""FastMCP application instance, _tool decorator, and middleware wrappers.

Leaf module — no imports from other yadgar.server.* modules.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from yadgar.config import get_settings
from yadgar.tracing import setup_tracing

settings = get_settings()

# ── Bug 1 fix (v5.6.4): ensure configure_logging runs BEFORE setup_tracing ───
# configure_logging installs the rotating file handler (Sink B) on the root logger.
# setup_tracing installs LogSpanProcessor which emits via yadgar.tracing logger →
# propagates to root. If setup_tracing runs first, spans emitted before configure_logging
# has run land in a handlerless root and are silently lost.
# Solution: call configure_logging here at _app import time (same time as setup_tracing).
# Idempotent — safe to call again from __main__.py (level/format update only).
try:
    from yadgar.log_config import configure_logging as _configure_logging  # noqa: PLC0415

    _log_format = os.environ.get("YADGAR_LOG_FORMAT", "json")
    _log_level = os.environ.get(
        "YADGAR_CORE_LOG_LEVEL", os.environ.get("CORE_LOG_LEVEL", "WARNING")
    )
    _configure_logging(log_format=_log_format, level=_log_level, process="core")
except Exception:
    pass  # Non-fatal: fall back to default root handlers

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
    instructions=(
        "Yadgar holds two stores for this repo: (1) episodic + semantic memories"
        " (heat-ranked, decay-gated) and (2) a curated wiki — conventions,"
        " module purpose, past decisions, where subsystems live.\n\n"
        "READ-FIRST CONTRACT: before searching a repo for structure, conventions,"
        " decisions, or where code lives — consult the wiki index first."
        " At session start you receive a wiki catalog via project_brief; read it."
        " For named pages: wiki_list() → pick slug → wiki_read(slug)."
        " For fuzzy topic search: wiki_query() (scores ~0.34 — use for discovery,"
        " not as coordinates)."
        " For exact current code lines: grep/read the source files directly."
        " wiki_list and wiki_read are the primary read-first tools; wiki_query is"
        " the fallback for unknown-slug topic search."
    ),
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


def _instrument_starlette_app(app) -> None:
    """Apply FastAPIInstrumentor to a Starlette/FastAPI app (v5.6.4 — Bug 2 fix).

    FastAPIInstrumentor works on both FastAPI and raw Starlette apps.
    Idempotent: no-op if already instrumented or OTel not available.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        pass  # OTel not available or app already instrumented — no-op


class InFlightRequestMiddleware:
    """ASGI middleware that tracks active HTTP requests for graceful drain (v5.49.0).

    Wraps the outermost middleware layer so ALL HTTP requests (MCP, control,
    health, metrics) are counted. Decrements on scope exit (normal or error).

    Stack position: outermost — wraps BearerAuth so every request increments
    the counter before auth filtering.  This is intentional: we want the drain
    barrier to wait for all in-flight TCP flows, not just authenticated ones.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        from yadgar.drain import _request_counter  # noqa: PLC0415

        if scope["type"] == "http":
            _request_counter.increment()
            try:
                await self.app(scope, receive, send)
            finally:
                _request_counter.decrement()
        else:
            await self.app(scope, receive, send)


class MCPTraceSpanMiddleware:
    """ASGI middleware that opens an OTel span for every HTTP request.

    v5.7.8 — Bug 4 residual fix: FastMCP routes intercept requests before
    FastAPIInstrumentor's middleware attaches the context.  FastAPIInstrumentor's
    span therefore closes *before* RequestLoggingMiddleware.finally fires, leaving
    get_current_trace_id() returning None when the log line is emitted.

    This middleware sits *above* RequestLoggingMiddleware in the stack:

        BearerAuth → MCPTraceSpanMiddleware → RequestLogging → CORS → MCP

    It opens a span at request entry and closes it *after* the inner ASGI app
    (including RequestLoggingMiddleware) returns, so a valid trace_id is always
    present when the log line is formatted.

    W3C traceparent propagation: if the incoming request carries a ``traceparent``
    header the span is started as a child of that remote context; otherwise a new
    root span is created.

    No-op fallback: if OTel is unavailable the middleware passes through
    transparently, matching the guard pattern used throughout this module.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._tracer = None
        self._propagator = None
        try:
            from opentelemetry import propagate, trace  # noqa: PLC0415

            self._tracer = trace.get_tracer("yadgar.server.mcp")
            self._propagator = propagate
        except Exception:
            pass  # OTel not available — degrade gracefully

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or self._tracer is None:
            await self.app(scope, receive, send)
            return

        # Extract W3C traceparent (or other propagation headers) from ASGI scope.
        # ASGI headers are a list of (bytes, bytes) tuples; build a dict for the
        # propagator carrier interface.
        raw_headers = scope.get("headers", [])
        headers_map: dict[str, str] = {
            k.decode("latin-1"): v.decode("latin-1") for k, v in raw_headers
        }

        ctx = self._propagator.extract(headers_map)

        method = scope.get("method", "")
        path = scope.get("path", "")
        span_name = f"{method} {path}" if method else path or "mcp.http"

        with self._tracer.start_as_current_span(span_name, context=ctx):
            await self.app(scope, receive, send)


def _cors_wrapped_http_app(self):
    from starlette.middleware.cors import CORSMiddleware

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    # Stack: InFlightRequest (outermost) → BearerAuth → MCPTrace → RequestLogging → CORS → MCP
    # v5.49.0 Phase 6: InFlightRequestMiddleware wraps outermost so drain barrier
    # counts all in-flight HTTP flows before graceful shutdown.
    # v5.7.8 Bug 4 residual: MCPTraceSpanMiddleware opens a span before
    # RequestLoggingMiddleware so trace_id is present in the log line.
    inner = _orig_streamable_http_app(self)
    # v5.6.4 Bug 2: instrument the inner MCP app so HTTP requests produce server spans.
    _instrument_starlette_app(inner)
    cors_app = CORSMiddleware(
        app=inner,
        allow_origins=_get_allowed_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    logged_app = RequestLoggingMiddleware(cors_app)
    spanned_app = MCPTraceSpanMiddleware(logged_app)
    auth_app = BearerAuthMiddleware(spanned_app)
    return InFlightRequestMiddleware(auth_app)


def _auth_wrapped_sse_app(self, mount_path=None):
    """Wrap SSE transport with BearerAuthMiddleware + RequestLogging (C-1).

    SSE is the default transport; without this wrapper REQUIRE_AUTH=1 has
    no effect on the SSE path.
    """
    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware

    inner = _orig_sse_app(self, mount_path)
    # v5.6.4 Bug 2: instrument the inner SSE app for server spans.
    _instrument_starlette_app(inner)
    logged_app = RequestLoggingMiddleware(inner)
    # v5.7.8 Bug 4 residual: open a span above RequestLogging so trace_id is
    # present in the log line (same fix as the streamable-HTTP path).
    spanned_app = MCPTraceSpanMiddleware(logged_app)
    auth_app = BearerAuthMiddleware(spanned_app)
    # v5.49.0 Phase 6: InFlightRequestMiddleware outermost for drain barrier
    return InFlightRequestMiddleware(auth_app)


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
