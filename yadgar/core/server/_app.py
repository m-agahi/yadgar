"""FastMCP application instance, _tool decorator, and middleware wrappers.

Leaf module — no imports from other yadgar.server.* modules.
"""

from __future__ import annotations

import logging
import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from yadgar._shared.config import get_settings, resolve_knob
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import setup_tracing, trace_span

settings = get_settings()

# ── Bug 1 fix (v5.6.4): ensure configure_logging runs BEFORE setup_tracing ───
# configure_logging installs the rotating file handler (Sink B) on the root logger.
# setup_tracing installs LogSpanProcessor which emits via yadgar.tracing logger →
# propagates to root. If setup_tracing runs first, spans emitted before configure_logging
# has run land in a handlerless root and are silently lost.
# Solution: call configure_logging here at _app import time (same time as setup_tracing).
# Idempotent — safe to call again from __main__.py (level/format update only).
try:
    from yadgar._shared.observability.log_config import (
        configure_logging as _configure_logging,  # noqa: PLC0415
    )

    _log_format = resolve_knob("YADGAR_LOG_FORMAT", "LOG_FORMAT", str, "json")
    _log_level = os.environ.get(
        "YADGAR_CORE_LOG_LEVEL", os.environ.get("CORE_LOG_LEVEL", "WARNING")
    )
    _configure_logging(log_format=_log_format, level=_log_level, process="core")
except Exception:
    pass  # Non-fatal: fall back to default root handlers

# ── Distributed tracing — v5.6.3 ─────────────────────────────────────────────
# Initialise OTel TracerProvider + LogSpanProcessor early (module import time).
# v5.101 R2: HTTPXClientInstrumentor is now activated INSIDE setup_tracing() (the
# single choke-point) so every entry mode — not just this HTTP-app path — auto-
# injects W3C traceparent on outbound httpx calls (closes the stdio/daemon hole).
setup_tracing("yadgar-core")

# ── Tool profile (read at import time — decorators execute on module load) ────
# YADGAR_PROFILE=minimal  →  10 core tools only
# YADGAR_PROFILE=full     →  all tools including power tier (default)
_PROFILE = os.environ.get("YADGAR_PROFILE", "full")

# mcp 2.0.0: FastMCP → MCPServer (moved from mcp.server.fastmcp to mcp.server).
# The constructor no longer accepts transport knobs (host/port/stateless_http/… were
# removed from the server Settings model and relocated to per-call kwargs of
# run()/streamable_http_app()/sse_app()). See _transport_runtime below.
mcp_server = MCPServer(
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
)

# ── Transport runtime knobs (mcp 2.0.0 migration) ─────────────────────────────
# mcp 2.0.0 removed host/port/stateless_http from the server Settings model and
# made them per-call kwargs of run_*_async()/streamable_http_app(). yadgar's
# middleware wrappers, the uvicorn-shutdown-timeout patch, and _startup.main()
# read/write these here instead of the (now-absent) mcp_server.settings.{host,
# port,stateless_http}. Mutated by _startup.main() before mcp_server.run().
_transport_runtime: dict[str, object] = {
    "host": settings.HOST,
    "port": settings.PORT,
    "stateless_http": False,
}

# mcp 2.0.0 auto-enables DNS-rebinding protection whenever host is a loopback
# address and no transport_security is passed — that rejects any request whose
# Host header isn't 127.0.0.1/localhost (HTTP 421), which breaks yadgar's
# container deployment (Host = container addr) and the Starlette TestClient
# (Host: testserver). yadgar guards the surface with BearerAuth + default-deny
# CORS instead, matching mcp 1.x behaviour (protection was off there), so pass an
# explicit off setting to preserve prior behaviour on both transports.
_NO_DNS_REBIND = TransportSecuritySettings(enable_dns_rebinding_protection=False)


# ── CORS: default-deny; configurable via YADGAR_ALLOWED_ORIGINS ───────────────
@observe(tier="stage")
def _get_allowed_origins() -> list[str]:
    """Read allowed origins from config. Default: loopback only."""
    raw = resolve_knob("YADGAR_ALLOWED_ORIGINS", "ALLOWED_ORIGINS", str, "")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        f"http://127.0.0.1:{settings.PORT}",
        f"http://localhost:{settings.PORT}",
        "http://127.0.0.1:42069",
        "http://localhost:42069",
    ]


@observe(tier="stage")
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
        from yadgar.core.daemon.drain import _request_counter  # noqa: PLC0415

        if scope["type"] == "http":
            _request_counter.increment()
            try:
                await self.app(scope, receive, send)
            finally:
                _request_counter.decrement()
        else:
            await self.app(scope, receive, send)


class SessionBindMiddleware:
    """ASGI middleware that wires Mcp-Session-Id → project_id (Car B §3.3).

    Sits below BearerAuth and above the MCP transport. On every HTTP
    request:

    1. Reads the ``Mcp-Session-Id`` header (set by the MCP transport on
       the response, present on the request from the SECOND call onward —
       the first call mints it inside the SDK).
    2. Looks up the in-process ``sid -> project_id`` binding registry
       populated by ``/session_bind`` (the route registers the binding
       on a successful nonce consume; this middleware reads it).
    3. Stamps the per-request ContextVar with the looked-up project_id
       and resets it in ``finally`` so a project_id cannot leak into
       the next request on the same worker.

    Stdout / stateless_http: no ``Mcp-Session-Id`` header → ContextVar
    stays unbound → ``resolve_effective_project`` falls through to its
    existing ``project > session_project > raise`` chain. This is the
    documented stdio path; no behaviour change.

    Why NOT consume the nonce here: the nonce lives in the
    ``/session_bind`` route's pool; the /session_bind POST is the only
    caller that should pop it. This middleware's job is the steady-state
    look-up, not the bootstrap.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Decode headers once
        try:
            raw_headers = scope.get("headers", []) or []
            _hdrs: dict[str, str] = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers
            }
        except Exception:  # noqa: BLE001
            _hdrs = {}
        _sid = _hdrs.get("mcp-session-id") or None
        _project_id: str | None = None
        if _sid:
            try:
                from yadgar.core.server.http import lookup_session_binding  # noqa: PLC0415

                _project_id = lookup_session_binding(_sid)
            except Exception:  # noqa: BLE001
                _project_id = None
        if not _project_id:
            # Static per-client header — the path that actually works here.
            # The daemon runs stateless_http (see _startup.py), so there is no
            # Mcp-Session-Id and the nonce binding above can never resolve.
            # ``.claude.json``'s mcpServers entry supports static headers, so a
            # client installed for one project carries its identity on every
            # request: no sticky session state for an instance to forget to
            # switch back, and nothing the model can rewrite mid-session.
            _project_id = (_hdrs.get("x-yadgar-project-id") or "").strip() or None
        from yadgar._shared.runtime.session_project import (  # noqa: PLC0415
            reset_current_session_project,
            set_current_session_project,
        )

        _token = set_current_session_project(_project_id)
        try:
            await self.app(scope, receive, send)
        finally:
            try:
                reset_current_session_project(_token)
            except Exception:  # noqa: BLE001
                # Stale token (middleware called reset twice): never let
                # cleanup raise into the request path.
                pass


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
        except ImportError:
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


@observe(tier="stage")
def _cors_wrapped_http_app(self):
    from starlette.middleware.cors import CORSMiddleware

    from yadgar._shared.observability.log_config import RequestLoggingMiddleware
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    # Stack: InFlightRequest (outermost) → BearerAuth → MCPTrace → RequestLogging → CORS → MCP
    # v5.49.0 Phase 6: InFlightRequestMiddleware wraps outermost so drain barrier
    # counts all in-flight HTTP flows before graceful shutdown.
    # v5.7.8 Bug 4 residual: MCPTraceSpanMiddleware opens a span before
    # RequestLoggingMiddleware so trace_id is present in the log line.
    # mcp 2.0.0: stateless_http + transport_security are call-time kwargs now
    # (were mcp_server.settings.* in 1.x). stateless_http is set by _startup.main()
    # for streamable-http transport; default False keeps test-time app builds
    # session-based, exactly as before.
    inner = _orig_streamable_http_app(
        self,
        stateless_http=bool(_transport_runtime["stateless_http"]),
        transport_security=_NO_DNS_REBIND,
    )
    # v5.6.4 Bug 2: instrument the inner MCP app so HTTP requests produce server spans.
    _instrument_starlette_app(inner)
    cors_app = CORSMiddleware(
        app=inner,
        allow_origins=_get_allowed_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        # Car B (0047 §3.3): expose the Mcp-Session-Id and the X-Yadgar-Project-Id
        # to the ASGI middleware layer that stamps the per-request ContextVar.
        # CORS preflight responses will then echo the allowed set, so the
        # browser-side client (the /session_bind fetch) can read both headers.
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Mcp-Session-Id",
            "X-Yadgar-Project-Id",
        ],
    )
    logged_app = RequestLoggingMiddleware(cors_app)
    spanned_app = MCPTraceSpanMiddleware(logged_app)
    auth_app = BearerAuthMiddleware(spanned_app)
    session_bind_app = SessionBindMiddleware(auth_app)
    return InFlightRequestMiddleware(session_bind_app)


@observe(tier="stage")
def _auth_wrapped_sse_app(self, mount_path=None):
    """Wrap SSE transport with BearerAuthMiddleware + RequestLogging (C-1).

    SSE is the default transport; without this wrapper REQUIRE_AUTH=1 has
    no effect on the SSE path.
    """
    from yadgar._shared.observability.log_config import RequestLoggingMiddleware
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    # mcp 2.0.0: sse_app() is keyword-only and dropped the positional mount_path
    # arg. mount_path was always None in practice; pass explicit transport_security
    # to keep DNS-rebinding protection off (see _NO_DNS_REBIND).
    inner = _orig_sse_app(self, transport_security=_NO_DNS_REBIND)
    # v5.6.4 Bug 2: instrument the inner SSE app for server spans.
    _instrument_starlette_app(inner)
    logged_app = RequestLoggingMiddleware(inner)
    # v5.7.8 Bug 4 residual: open a span above RequestLogging so trace_id is
    # present in the log line (same fix as the streamable-HTTP path).
    spanned_app = MCPTraceSpanMiddleware(logged_app)
    auth_app = BearerAuthMiddleware(spanned_app)
    # Car B §3.3: wire the session-bind middleware so SSE-side Mcp-Session-Id
    # is also bound to the project_id minted by /session_bind.
    session_bind_app = SessionBindMiddleware(auth_app)
    # v5.49.0 Phase 6: InFlightRequestMiddleware outermost for drain barrier
    return InFlightRequestMiddleware(session_bind_app)


_orig_streamable_http_app = mcp_server.streamable_http_app.__func__
mcp_server.streamable_http_app = _cors_wrapped_http_app.__get__(mcp_server, type(mcp_server))

_orig_sse_app = mcp_server.sse_app.__func__
mcp_server.sse_app = _auth_wrapped_sse_app.__get__(mcp_server, type(mcp_server))


@observe(tier="stage")
def _start_loop_lag_monitor_on_live_loop():
    """Start the #80 event-loop lag monitor on the running uvicorn loop.

    Returns the asyncio.Task handle (or None if metrics unavailable). The monitor
    schedules a probe every ~0.5s on THIS loop; a freeze that starves /health
    (the #80 RCA) also blocks the probe, so the next probe records a large lag
    into yadgar_event_loop_lag_seconds — making the freeze diagnosable. Never
    raises: telemetry must not block daemon startup.
    """
    try:
        import asyncio as _asyncio  # noqa: PLC0415

        from yadgar._shared.observability.metrics import start_loop_lag_monitor  # noqa: PLC0415

        return start_loop_lag_monitor(_asyncio.get_running_loop())
    except Exception:  # noqa: BLE001
        return None


@observe(tier="stage")
async def _stop_loop_lag_monitor_safe(task) -> None:
    """Cancel the lag monitor task on shutdown. Never raises."""
    try:
        from yadgar._shared.observability.metrics import stop_loop_lag_monitor  # noqa: PLC0415

        await stop_loop_lag_monitor(task)
    except Exception:  # noqa: BLE001
        pass


@observe(tier="stage")
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
            host=_transport_runtime["host"],
            port=_transport_runtime["port"],
            log_level=self.settings.log_level.lower(),
            timeout_graceful_shutdown=_timeout,
            timeout_keep_alive=2,
            log_config=None,
        )
        server = _uvicorn.Server(config)
        _lag_task = _start_loop_lag_monitor_on_live_loop()
        try:
            await server.serve()
        finally:
            await _stop_loop_lag_monitor_safe(_lag_task)
            if server.should_exit:
                _patch_logger.info(
                    "ASGI shutdown complete (timeout_graceful_shutdown=%ss)", _timeout
                )

    async def _streamable_http_async_patched(self) -> None:  # pragma: no cover
        starlette_app = self.streamable_http_app()
        config = _uvicorn.Config(
            starlette_app,
            host=_transport_runtime["host"],
            port=_transport_runtime["port"],
            log_level=self.settings.log_level.lower(),
            timeout_graceful_shutdown=_timeout,
            timeout_keep_alive=2,
            log_config=None,
        )
        server = _uvicorn.Server(config)
        _lag_task = _start_loop_lag_monitor_on_live_loop()
        try:
            await server.serve()
        finally:
            await _stop_loop_lag_monitor_safe(_lag_task)
            if server.should_exit:
                _patch_logger.info(
                    "ASGI shutdown complete (timeout_graceful_shutdown=%ss)", _timeout
                )

    mcp_server.run_sse_async = _sse_async_patched.__get__(mcp_server, type(mcp_server))
    mcp_server.run_streamable_http_async = _streamable_http_async_patched.__get__(
        mcp_server, type(mcp_server)
    )


_patch_uvicorn_shutdown_timeout()


@observe(tier="stage")
def _tool(power: bool = False, always_load: bool = False):
    """Register a function as an MCP tool.

    power=True tools are omitted when YADGAR_PROFILE=minimal.
    always_load=True emits ``meta={"anthropic/alwaysLoad": True}`` on the
    registered FastMCP Tool so Claude Code never auto-defers these tools to
    the deferred-tool list (ADR-0047, #45).
    Wraps each registered tool to record estimated token output in
    yadgar_tool_token_estimate_total{tool=<name>}.
    """
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
        except (TypeError, ValueError):  # fmt: skip
            return 0

    def decorator(func):
        if power and _PROFILE == "minimal":
            return func  # skip registration; function still callable internally

        # v5.6.3: wrap with trace_span so every tool call is traceable.
        # trace_span is applied at decoration time (before mcp_server.tool() wraps it).
        from yadgar._shared.observability.tracing import trace_span as _trace_span  # noqa: PLC0415

        _traced_func = _trace_span(f"tool.{func.__name__}")(func)

        # Fix A (daemon-offload-A): sync-only guard. Every @_tool() target today is
        # a sync `def`; an async body would break run_in_executor(coroutine_fn).
        import inspect as _inspect  # noqa: PLC0415

        if _inspect.iscoroutinefunction(func):  # pragma: no cover — none exist today
            raise TypeError(
                f"@_tool() expects a sync def body; {func.__name__} is async. "
                "Async tool bodies are not supported by the offload wrapper."
            )

        sync_wrapper, async_wrapper = _build_tool_wrappers(func, _traced_func, _estimate_tokens)
        # ASYNC wrapper → FastMCP (offload-friendly `await fn` branch).
        # ADR-0047: always_load=True tools carry anthropic/alwaysLoad in their meta
        # so Claude Code never defers them to the lazy-tool list.
        _meta_kwargs = {"meta": {"anthropic/alwaysLoad": True}} if always_load else {}
        mcp_server.tool(**_meta_kwargs)(async_wrapper)
        # SYNC wrapper → the module-level name (direct-call contract: internal/test
        # callers run inline exactly as pre-Fix-A).
        return sync_wrapper

    return decorator


def _push_trace_complete_event(tool_name: str, t0: float, status: str) -> None:
    """Push a `trace_complete` SSE event when a tool trace finalizes (Phase 3).

    finish-viz trace-replay Phase 3: the viz "Traces" tab live-appends the
    completed trace. Called from the tool-boundary ``finally`` (both sync + async
    wrappers, same site as ``_emit_metrics``) via the F2 SSE relay path
    (``_push_event`` → backend ``_op_events`` → core ``_poll_backend_events`` →
    browser). The trace_id is read from the still-active enclosing span (all spans
    in a trace share one trace_id) — an internal/test direct call with no active
    span yields no trace_id, so the emit is skipped (correct: only real MCP tool
    traces reach Tempo and the Traces tab). Best-effort: never raises, never
    blocks. Module-level (not a wrapper closure) to keep ``_build_tool_wrappers``
    under the I13 cyclomatic cap.
    """
    import time as _time  # noqa: PLC0415

    try:
        from yadgar._shared.observability.tracing import (  # noqa: PLC0415
            get_current_trace_id,
        )

        trace_id = get_current_trace_id()
        if not trace_id:
            return  # no active trace (direct/internal call) → nothing to append
        from yadgar._shared.server_helpers import _push_event  # noqa: PLC0415

        _push_event(
            {
                "event": "trace_complete",
                "trace_id": trace_id,
                "tool": tool_name,
                "total_ms": round((_time.monotonic() - t0) * 1000, 1),
                "status": status,
            }
        )
    except Exception:  # noqa: BLE001 — SSE emit is best-effort, must not affect the tool
        pass


@observe(tier="stage")
def _build_tool_wrappers(func, traced_func, estimate_tokens):  # noqa: C901 - cohesive: per-tool discriminator (sync/async, needs_session_bind, forwards_to_backend) + wrapper emission; the 3 discriminator branches Car B added are irreducible without splitting wrappers, which belongs to wave-N (see .complexity-allowlist.json entry)
    """Build the (sync, async) instrumented wrappers for a tool (Fix A).

    The sync wrapper preserves the pre-Fix-A direct-call contract (run inline,
    return a result). The async wrapper is registered with FastMCP and dispatches
    the body off the asyncio loop via run_offloaded (kill-switch
    YADGAR_OFFLOAD_TOOLS, default OFF → inline). Both share _emit_metrics.
    """
    import functools  # noqa: PLC0415
    import inspect as _inspect_sig  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    # Whether this tool declares its OWN ``context`` parameter. Computed once
    # at decoration time so the per-call path stays a boolean test. See the
    # pop-site in ``_instrumented_async`` for why it matters.
    try:
        _declares_context = "context" in _inspect_sig.signature(func).parameters
    except (TypeError, ValueError):  # fmt: skip  # pragma: no cover — builtins/C funcs
        _declares_context = False

    _maint_logger = logging.getLogger(__name__)

    def _emit_metrics(_t0: float, _status: str, result) -> None:
        try:
            from yadgar._shared.observability.metrics import (  # noqa: PLC0415
                yadgar_mcp_request_count,
                yadgar_mcp_request_duration_ms,
            )

            _elapsed_ms = (_time.monotonic() - _t0) * 1000
            yadgar_mcp_request_duration_ms.labels(tool=func.__name__).observe(_elapsed_ms)
            yadgar_mcp_request_count.labels(tool=func.__name__, status=_status).inc()
        except (ImportError, ValueError):  # fmt: skip
            pass
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_tool_token_estimate_total,  # noqa: PLC0415
            )

            yadgar_tool_token_estimate_total.labels(tool=func.__name__).inc(estimate_tokens(result))
        except (ImportError, ValueError):  # fmt: skip
            pass

    def _maintenance():
        """Short-circuit every MCP tool while a maintenance window is engaged.

        task:0111 / ADR-0188 — the message no longer says "nightly": the core now
        STAYS UP across a vacuum (only the backend is stopped), so a CLI- or
        timer-triggered vacuum can engage this gate too, and a tool call landing
        in the backend-down window should read as "maintenance", not as a raw
        ``httpx.ConnectError`` from the core→backend forward.

        COVERAGE CAVEAT: this wrapper is the MCP tool path only.  The HTTP viz
        endpoints are not behind ``_instrumented``, so they are NOT gated —
        they degrade visibly on their own.  Gating them is deliberately out of
        scope (plan 0111 §9).

        task:0113 — TTL self-heal.  ``cmd_vacuum_impl`` releases the gate in a
        ``finally``, which covers returns, exceptions and ``sys.exit`` but NOT
        SIGKILL / OOM-kill / power loss.  With 0111 the core no longer restarts
        during a vacuum, so a clear-on-start reset would never fire — an expired
        deadline is the only backstop that does.  Expiry is LOUD: it means a
        vacuum died without cleanup.

        Car 1 (2026-08-20 train) — the payload itself moved to
        ``yadgar._shared.runtime.maintenance`` so ``/health`` can reuse it and so
        it can be TESTED by import rather than by grepping this file's source.
        """
        import yadgar._shared.runtime.state as _st_ref  # noqa: PLC0415 — read live attr
        from yadgar._shared.runtime.maintenance import (  # noqa: PLC0415
            build_maintenance_envelope,
            maintenance_expired,
            reset_maintenance_state,
        )

        if not _st_ref._maintenance_mode:
            return None
        if maintenance_expired():
            _deadline = _st_ref._maintenance_deadline
            _held = _time.monotonic() - (_st_ref._maintenance_entered_at or _deadline)
            # Clears operation/phase too — a stale label surviving the self-heal
            # would mislabel the NEXT window's envelope.
            reset_maintenance_state()
            _maint_logger.warning(
                "maintenance TTL expired after %.0fs — clearing the write-gate. "
                "The job that engaged it (vacuum or nightly) did not release it: "
                "it was almost certainly SIGKILLed. Check the vacuum unit.",
                _held,
                extra={"component": "app", "action": "maintenance_ttl_expired"},
            )
            return None
        return build_maintenance_envelope()

    @functools.wraps(func)
    def _instrumented(*args, **kwargs):
        _maint = _maintenance()
        if _maint is not None:
            return _maint
        _t0 = _time.monotonic()
        _status = "ok"
        result = None
        try:
            result = traced_func(*args, **kwargs)
            return result
        except Exception:
            _status = "error"
            raise
        finally:
            _emit_metrics(_t0, _status, result)
            _push_trace_complete_event(func.__name__, _t0, _status)

    @functools.wraps(func)
    async def _instrumented_async(*args, **kwargs):
        _maint = _maintenance()
        if _maint is not None:
            # RAISE on the MCP-registered path, where the SYNC wrapper RETURNS
            # (Car 1, 2026-08-20 train): the SDK validates a returned value
            # against a model derived from this tool's RETURN ANNOTATION, and no
            # single value satisfies every annotation in the registry. Full
            # argument in ``MaintenanceGateError``'s docstring.
            from yadgar._shared.runtime.maintenance import (  # noqa: PLC0415
                MaintenanceGateError,
            )

            raise MaintenanceGateError(_maint)
        # ── Car B (0047 §3.3): extract the Mcp-Session-Id binding BEFORE
        # run_offloaded forwards **kwargs. The offload call carries
        # **kwargs into the executor thread; if we left ``ctx`` inside
        # kwargs, the executor would re-receive it and the binding would
        # never be visible to ``resolve_effective_project`` tier 2.
        ctx = kwargs.pop("ctx", None)
        if ctx is None and not _declares_context:
            # mcp 2.0.0 passes the Context object under the kwarg name
            # ``context`` (per server/fastmcp/server.py:96); older SDK
            # shapes used ``ctx``. The plan §3.3 spec uses ``ctx``; we
            # accept both to stay forward/back-compat.
            #
            # ...but ONLY when the tool does not declare its own ``context``
            # parameter. Three do — ``adr_add`` (the ADR background),
            # ``anchor`` (the anchor's context) and ``memorize`` (the
            # staleness-detection path) — and an unconditional pop ate the
            # caller's value: the two REQUIRED ones raised
            # ``missing 1 required positional argument: 'context'`` and the
            # optional one dropped the value with no signal at all. The
            # wrapped signature is the authority on who owns the name.
            ctx = kwargs.pop("context", None)
        _bound_project_id = _extract_session_project(ctx) if ctx is not None else None
        # Stamp the per-request ContextVar so resolve_effective_project tier 2
        # sees it from anywhere in the call chain. Reset in the finally.
        from yadgar._shared.runtime.session_project import (  # noqa: PLC0415
            reset_current_session_project,
            set_current_session_project,
        )

        _ctx_token = set_current_session_project(_bound_project_id)
        from yadgar._shared.runtime.offload import run_offloaded  # noqa: PLC0415

        _t0 = _time.monotonic()
        _status = "ok"
        result = None
        try:
            result = await run_offloaded(traced_func, *args, **kwargs)
            return result
        except TimeoutError:
            # Wedged op past the per-tool timeout (asyncio.TimeoutError IS
            # TimeoutError in py3.11+): free the loop, return a structured error
            # (NOT a 500). The worker keeps its slot until it self-releases — O2
            # saturation + P0 health-kill cover the residual.
            _status = "timeout"
            result = {
                "error": "timeout",
                "message": f"tool {func.__name__} exceeded the offload timeout",
            }
            return result
        except Exception:
            _status = "error"
            raise
        finally:
            _emit_metrics(_t0, _status, result)
            _push_trace_complete_event(func.__name__, _t0, _status)
            reset_current_session_project(_ctx_token)

    # Car B §3.3: pin the parameter annotation so static type checkers (mypy)
    # see ``ctx: Context`` on the wrapper and don't flag the kwargs.pop
    # above as an untyped access. The annotation is set AFTER the def to
    # avoid mutating the function source at decoration time.
    try:
        from mcp.server.fastmcp.server import Context as _CtxT  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — mcp 2.0.0 guarantees this import succeeds
        _CtxT = None  # type: ignore[assignment]
    if _CtxT is not None:
        _instrumented_async.__annotations__["ctx"] = _CtxT
        _instrumented_async.__annotations__["context"] = _CtxT

    return _instrumented, _instrumented_async


@trace_span()
def _extract_session_project(ctx) -> str | None:
    """Read the bound project_id from the FastMCP ``Context`` (Car B §3.3).

    The MCP SDK 2.0.0 path to the Mcp-Session-Id is via
    ``ctx.request_context.request.headers`` (the ASGI/Starlette request
    object exposes the raw ``Mcp-Session-Id`` header set by
    ``StreamableHTTPServerTransport``). For stdio and stateless_http
    transports there is no session id — return ``None`` and let the
    caller fall back to the explicit ``project=`` param.

    The actual binding ``session_id -> project_id`` is the nonce pool
    populated by ``/session_bind``; this helper just looks up the
    header. The wrapper that PRECEDES this (the ASGI middleware) is
    responsible for actually consuming the nonce and storing the
    binding; the helper here is the read-only accessor for the tool
    wrapper path that the plan §3.3 spec names.
    """
    try:
        _rc = getattr(ctx, "request_context", None) or getattr(ctx, "_request_context", None)
        if _rc is None:
            return None
        _req = getattr(_rc, "request", None)
        if _req is None:
            return None
        _headers = getattr(_req, "headers", None)
        if _headers is None:
            return None
        # NO early-out on a missing Mcp-Session-Id. The daemon runs
        # ``stateless_http=True`` for streamable-http (_startup.py — chosen so
        # daemon restarts are transparent to the client), and stateless mode
        # issues no session id at all; verified live 2026-08-15, an
        # ``initialize`` against the running daemon returns no such header.
        # Bailing here meant the ``X-Yadgar-Project-Id`` read below was
        # unreachable in the ONLY transport the daemon actually runs, so tier 2
        # never resolved and every write without an explicit ``project=``
        # raised ``unresolved_project``. A session id is the wrong
        # precondition for a value that travels in its own header.
    except Exception:  # noqa: BLE001 — defensive; never raise into the tool path
        return None
    # Look up the binding from the nonce pool's per-session project
    # registry. The pool itself only stores outstanding nonces; the
    # live ``sid -> project_id`` table lives on the transport process.
    # In this code base the binding is read by the ASGI middleware
    # (which sees the transport directly); the wrapper's job is just
    # to surface the same value through the ContextVar. We re-read
    # the request's ``X-Yadgar-Project-Id`` header (set by the
    # middleware) for a single-source-of-truth.
    try:
        _proj_header = _req.headers.get("x-yadgar-project-id") or _req.headers.get(
            "X-Yadgar-Project-Id"
        )
    except Exception:  # noqa: BLE001
        _proj_header = None
    return _proj_header or None
