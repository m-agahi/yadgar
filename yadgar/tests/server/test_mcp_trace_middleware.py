"""v5.7.8 — MCPTraceSpanMiddleware tests (Bug 4 residual).

Verifies that POST /mcp log lines carry trace_id even without an outer span
in the call context.  Written TDD — red before MCPTraceSpanMiddleware existed.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _reset_tracer_provider():
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None
        new_provider = TracerProvider()
        trace.set_tracer_provider(new_provider)
        try:
            import yadgar._shared.observability.tracing as _tr

            _tr._SETUP_DONE.clear()
        except (ImportError, AttributeError):  # fmt: skip
            pass
    # The outer arm guards `trace._TRACER_PROVIDER` — a private OTel attribute
    # that a version bump can rename. `set_tracer_provider` logs rather than
    # raising when a provider is already set.
    except (ImportError, AttributeError):  # fmt: skip
        pass


@pytest.fixture(autouse=True)
def reset_otel_state():
    _reset_tracer_provider()
    yield
    _reset_tracer_provider()


@pytest.fixture()
def in_memory_tracer():
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_capture_for(logger_name: str):
    """Return (StringIO, handler, logger) — call handler.close() and
    logger.removeHandler(handler) in finally."""
    from yadgar._shared.observability.log_config import JSONLogFormatter

    buf = StringIO()
    hdl = logging.StreamHandler(buf)
    hdl.setFormatter(JSONLogFormatter())
    hdl.setLevel(logging.DEBUG)
    lgr = logging.getLogger(logger_name)
    lgr.addHandler(hdl)
    lgr.setLevel(logging.DEBUG)
    lgr.propagate = False
    return buf, hdl, lgr


def _build_stacked_app():
    """Return a TestClient wrapping MCPTraceSpanMiddleware → RequestLogging → stub /mcp."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from yadgar._shared.observability.log_config import RequestLoggingMiddleware
    from yadgar.core.server._app import MCPTraceSpanMiddleware

    async def mcp_handler(request: Request):
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/mcp", mcp_handler, methods=["POST"])])
    stacked = MCPTraceSpanMiddleware(RequestLoggingMiddleware(inner))
    return TestClient(stacked, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# 18. /mcp request log lines carry trace_id (v5.7.8 — Bug 4 residual)
# ---------------------------------------------------------------------------


class TestMCPTraceSpanMiddleware:
    """MCPTraceSpanMiddleware opens a span before RequestLoggingMiddleware so
    that get_current_trace_id() returns a value when the log line is emitted.

    Regression: without the middleware, FastAPIInstrumentor's span closes before
    RequestLoggingMiddleware.finally fires — trace_id is absent.
    """

    def test_mcp_request_log_has_trace_id_without_outer_span(self, in_memory_tracer):
        """POST /mcp log line has non-empty trace_id even with no outer span."""
        buf, hdl, lgr = _log_capture_for("yadgar.requests")
        try:
            client = _build_stacked_app()
            # No surrounding start_as_current_span — this is the regression case.
            client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})

            log_out = buf.getvalue()
            assert log_out.strip(), "No log output from RequestLoggingMiddleware"

            lines = [ln for ln in log_out.strip().splitlines() if ln.strip()]
            assert lines, "No log lines found"

            last_line = json.loads(lines[-1])
            trace_id = last_line.get("trace_id", "")
            assert trace_id, f"trace_id absent/empty in /mcp log line: {last_line}"
            assert len(trace_id) == 32, f"trace_id should be 32-char hex, got: {trace_id!r}"
        finally:
            lgr.removeHandler(hdl)
            lgr.propagate = True

    def test_mcp_trace_middleware_propagates_traceparent(self, in_memory_tracer):
        """W3C traceparent header is continued — logged trace_id matches header."""
        buf, hdl, lgr = _log_capture_for("yadgar.requests")
        try:
            client = _build_stacked_app()

            known_trace_id = "0af7651916cd43dd8448eb211c80319c"
            traceparent = f"00-{known_trace_id}-b7ad6b7169203331-01"

            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                headers={"traceparent": traceparent},
            )

            log_out = buf.getvalue()
            lines = [ln for ln in log_out.strip().splitlines() if ln.strip()]
            assert lines

            last_line = json.loads(lines[-1])
            logged_trace_id = last_line.get("trace_id", "")
            assert logged_trace_id == known_trace_id, (
                f"Expected trace_id {known_trace_id!r}, got {logged_trace_id!r}"
            )
        finally:
            lgr.removeHandler(hdl)
            lgr.propagate = True
