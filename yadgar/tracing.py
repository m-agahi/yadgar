"""Distributed tracing for Yadgar — v5.6.3.

Provides:
  - setup_tracing(service_name) — creates TracerProvider, registers LogSpanProcessor, idempotent.
  - LogSpanProcessor — on span finish, emits ONE INFO log line in I14 JSON format.
  - @trace_span(name, attributes) — decorator for sync + async functions.
  - get_current_trace_id() / get_current_span_id() — helpers for log formatter integration.

Falls back gracefully (no-op) when opentelemetry deps not installed.

W3C TraceContext propagation: use opentelemetry-instrumentation-httpx (outbound)
and opentelemetry-instrumentation-fastapi (inbound) in the service setup code.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any

logger = logging.getLogger("yadgar.tracing")

# ---------------------------------------------------------------------------
# OTel availability gate
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------

_SETUP_DONE: set[str] = set()


# ---------------------------------------------------------------------------
# LogSpanProcessor
# ---------------------------------------------------------------------------


if _OTEL_AVAILABLE:
    from opentelemetry.sdk.trace import SpanProcessor

    class LogSpanProcessor(SpanProcessor):
        """On span finish, emit one I14-compliant JSON log line via stdlib logging.

        Subclasses opentelemetry.sdk.trace.SpanProcessor.
        on_start is no-op; on_end emits I14 JSON at INFO level.
        Registered via TracerProvider.add_span_processor().
        """

        def __init__(self, service_name: str = "yadgar") -> None:
            self._service_name = service_name

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            pass  # no-op

        def on_end(self, span: Any) -> None:
            ctx = span.context
            if ctx is None:
                return  # pragma: no cover

            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")

            parent_span_id: str | None = None
            if span.parent is not None:
                parent_span_id = format(span.parent.span_id, "016x")

            # Duration in ms (OTel uses nanoseconds)
            duration_ms = 0
            if span.end_time and span.start_time:
                duration_ms = int((span.end_time - span.start_time) / 1_000_000)

            # Status
            status = "OK"
            try:
                if span.status.status_code.name == "ERROR":
                    status = "ERROR"
                elif span.status.status_code.name == "UNSET":
                    status = "OK"
                else:
                    status = span.status.status_code.name
            except Exception:  # pragma: no cover
                pass

            # Attributes dict (convert OTel BoundedAttributes to plain dict)
            attrs: dict[str, Any] = {}
            if span.attributes:
                try:
                    attrs = dict(span.attributes)
                except Exception:  # pragma: no cover
                    pass

            payload: dict[str, Any] = {
                "event": "span_end",
                "component": "tracing",
                "trace_id": trace_id,
                "span_id": span_id,
                "span_name": span.name,
                "service": self._service_name,
                "duration_ms": duration_ms,
                "status": status,
            }
            if parent_span_id is not None:
                payload["parent_span_id"] = parent_span_id
            if attrs:
                payload["attributes"] = attrs

            logger.info("span_end", extra=payload)

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

else:  # pragma: no cover

    class LogSpanProcessor:  # type: ignore[no-redef]
        """No-op fallback when OTel is not available."""

        def __init__(self, service_name: str = "yadgar") -> None:
            self._service_name = service_name

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            pass

        def on_end(self, span: Any) -> None:
            pass

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True


# ---------------------------------------------------------------------------
# setup_tracing
# ---------------------------------------------------------------------------


def setup_tracing(service_name: str) -> None:
    """Create TracerProvider with LogSpanProcessor. Idempotent per service_name.

    Falls back silently (no-op) when opentelemetry-sdk not installed.
    """
    if not _OTEL_AVAILABLE:  # pragma: no cover
        return

    if service_name in _SETUP_DONE:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(LogSpanProcessor(service_name=service_name))
    _otel_trace.set_tracer_provider(provider)
    _SETUP_DONE.add(service_name)
    logger.info(
        "tracing_init",
        extra={
            "component": "tracing",
            "service": service_name,
            "action": "setup_tracing",
            "outcome": "ok",
        },
    )


# ---------------------------------------------------------------------------
# get_current_trace_id / get_current_span_id
# ---------------------------------------------------------------------------


def get_current_trace_id() -> str | None:
    """Return current trace_id as 32-char hex string, or None if no active span."""
    if not _OTEL_AVAILABLE:  # pragma: no cover
        return None
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def get_current_span_id() -> str | None:
    """Return current span_id as 16-char hex string, or None if no active span."""
    if not _OTEL_AVAILABLE:  # pragma: no cover
        return None
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.span_id, "016x")


# ---------------------------------------------------------------------------
# @trace_span decorator
# ---------------------------------------------------------------------------


def trace_span(name: str | None = None, attributes: dict[str, Any] | None = None):
    """Decorator: wraps sync or async functions in an OTel span.

    name: span name. Defaults to f"{fn.__module__}.{fn.__qualname__}".
    attributes: static key/value attributes to set on span.

    On exception: records exception + sets status=ERROR + re-raises.
    Falls back to identity decorator when OTel not available.
    """

    def decorator(fn):
        span_name = name if name is not None else f"{fn.__module__}.{fn.__qualname__}"

        if not _OTEL_AVAILABLE:  # pragma: no cover
            return fn

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                tracer = _otel_trace.get_tracer(fn.__module__)
                with tracer.start_as_current_span(span_name) as span:
                    if attributes:
                        for k, v in attributes.items():
                            span.set_attribute(k, v)
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(_otel_trace.Status(StatusCode.ERROR, str(exc)))
                        raise

            return async_wrapper

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                tracer = _otel_trace.get_tracer(fn.__module__)
                with tracer.start_as_current_span(span_name) as span:
                    if attributes:
                        for k, v in attributes.items():
                            span.set_attribute(k, v)
                    try:
                        return fn(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(_otel_trace.Status(StatusCode.ERROR, str(exc)))
                        raise

            return sync_wrapper

    # Support both @trace_span and @trace_span() and @trace_span("name")
    # If called as @trace_span("name") or @trace_span(), name is str or None
    # If called as @trace_span(fn), fn is a callable (no parens case not supported
    # for simplicity — always use with parens)
    return decorator
