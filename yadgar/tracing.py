"""Distributed tracing for Yadgar — v5.7.11.

Provides:
  - setup_tracing(service_name) — creates TracerProvider, registers LogSpanProcessor, idempotent.
    Optional OTLP/HTTP exporter (Tempo) via Settings.OTLP_ENDPOINT (yaml/env/default).
  - LogSpanProcessor — on span finish, emits ONE INFO log line in I14 JSON format.
  - @trace_span(name, attributes) — decorator for sync + async functions.
  - get_current_trace_id() / get_current_span_id() — helpers for log formatter integration.
  - _parse_otlp_headers(raw) — parse comma-separated k=v header string.

Falls back gracefully (no-op) when opentelemetry deps not installed.

OTLP knobs (all optional, yaml/env/default via Settings):
  YADGAR_OTLP_ENDPOINT      — HTTP endpoint, e.g. http://tempo:4318/v1/traces.
                              Empty/unset → OTLP exporter disabled.
  YADGAR_OTLP_HEADERS       — Comma-separated k=v pairs for auth/tenant headers.
  YADGAR_OTLP_TIMEOUT_SEC   — Exporter timeout in seconds (default 10).
  YADGAR_OTLP_INSECURE      — true → plain HTTP (default). false → TLS.

W3C TraceContext propagation: use opentelemetry-instrumentation-httpx (outbound)
and opentelemetry-instrumentation-fastapi (inbound) in the service setup code.
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
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
# OTLP helpers
# ---------------------------------------------------------------------------


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse comma-separated k=v pairs into a header dict.

    Splits on commas first, then on the FIRST '=' in each pair so that values
    containing '=' (e.g. Base64 tokens) are preserved.  Whitespace around keys
    and values is stripped.  Pairs without '=' are silently skipped.

    Example:
        "x-tenant=foo,authorization=Bearer x" → {"x-tenant": "foo", "authorization": "Bearer x"}
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result


def _build_otlp_exporter():  # type: ignore[return]
    """Construct an OTLPSpanExporter from Settings (yaml/env/default), or return None.

    v5.7.11: reads from Settings (pydantic-settings) instead of os.environ directly,
    so yaml-overrides in ~/.yadgar/config.yaml are honoured.

    Returns None when OTLP_ENDPOINT is empty/unset.
    Returns None and logs WARN on configuration error (URL validation failure).
    """
    from yadgar.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    endpoint = settings.OTLP_ENDPOINT.strip()
    if not endpoint:
        return None

    # Basic URL sanity check — must start with http:// or https://
    if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
        logger.warning(
            "otlp_endpoint_invalid",
            extra={
                "event": "otlp_endpoint_invalid",
                "component": "tracing",
                "endpoint": endpoint,
                "reason": "endpoint must start with http:// or https://",
                "action": "falling back to logs-only",
            },
        )
        return None

    headers_raw = settings.OTLP_HEADERS.strip()
    headers = _parse_otlp_headers(headers_raw) if headers_raw else {}

    timeout = settings.OTLP_TIMEOUT_SEC

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=headers if headers else None,
            timeout=timeout,
        )
        logger.info(
            "otlp_exporter_init",
            extra={
                "event": "otlp_exporter_init",
                "component": "tracing",
                "endpoint": endpoint,
                "timeout_sec": timeout,
                "headers_count": len(headers),
            },
        )
        return exporter
    except Exception as exc:
        logger.warning(
            "otlp_exporter_init_failed",
            extra={
                "event": "otlp_exporter_init_failed",
                "component": "tracing",
                "endpoint": endpoint,
                "error": str(exc),
                "action": "falling back to logs-only",
            },
        )
        return None


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

        def _emit_span_log(self, span: Any) -> None:
            """Emit one I14-compliant JSON log line for the finished span."""
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

        def on_end(self, span: Any) -> None:
            """Called after span.end() completes (SDK compatibility path)."""
            # Newer OTel SDK (>= 1.31) calls _on_ending instead of on_end for
            # the primary emission path. We guard here so both paths work.
            pass

        def _on_ending(self, span: Any) -> None:
            """Called by newer OTel SDK (>= 1.31) when a span is about to end.

            This replaces on_end as the primary emission hook in recent SDK versions.
            Emits the I14 span_end log line. Falls back gracefully if the SDK calls
            on_end directly (older SDK versions).
            """
            self._emit_span_log(span)

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

        def _on_ending(self, span: Any) -> None:
            """No-op: called by newer OTel SDK (>= 1.31) when span is about to end."""
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

    If Settings.OTLP_ENDPOINT is set (via yaml/env/default), also wires a BatchSpanProcessor
    with an OTLPSpanExporter so spans ship directly to Tempo alongside the JSON log path.
    LogSpanProcessor is always registered; OTLP exporter is opt-in via env.

    Falls back silently (no-op) when opentelemetry-sdk not installed.
    """
    if not _OTEL_AVAILABLE:  # pragma: no cover
        return

    if service_name in _SETUP_DONE:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(LogSpanProcessor(service_name=service_name))

    # Optional OTLP/HTTP exporter — runs alongside LogSpanProcessor (not replacing it)
    otlp_exporter = _build_otlp_exporter()
    if otlp_exporter is not None:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    _otel_trace.set_tracer_provider(provider)
    _SETUP_DONE.add(service_name)
    logger.info(
        "tracing_init",
        extra={
            "component": "tracing",
            "service": service_name,
            "action": "setup_tracing",
            "outcome": "ok",
            "otlp_enabled": otlp_exporter is not None,
        },
    )


def shutdown_tracing(timeout_sec: float = 3.0) -> None:
    """Tear down the tracer provider with a HARD time bound.

    A dead/unreachable OTLP collector must NEVER hang daemon shutdown: the
    BatchSpanProcessor's final flush would otherwise retry exports against the
    collector until the exporter timeout, which (historically) blew past the
    systemd stop-timeout and got the container SIGKILLed (exit 137) on every
    restart. We run ``provider.shutdown()`` in a daemon thread and abandon it
    after ``timeout_sec`` — an abandoned daemon thread does not block process
    exit, so the daemon always stops promptly regardless of collector state.

    Idempotent and safe to call when tracing was never set up.
    """
    provider = _otel_trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if not callable(shutdown):
        return

    done = threading.Event()

    def _run() -> None:
        try:
            shutdown()
        except Exception:  # noqa: BLE001
            pass
        finally:
            done.set()

    threading.Thread(target=_run, name="otel-shutdown", daemon=True).start()
    if not done.wait(timeout_sec):
        logger.warning(
            "tracing_shutdown_timeout",
            extra={
                "component": "tracing",
                "action": "shutdown_tracing",
                "outcome": "abandoned",
                "timeout_sec": timeout_sec,
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
