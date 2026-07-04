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
  YADGAR_OTLP_TIMEOUT_SEC   — Exporter timeout in seconds (default 3).
  YADGAR_OTLP_INSECURE      — reserved / no-op for the HTTP exporter. Transport
                              security is decided by the OTLP_ENDPOINT URL scheme
                              (http:// vs https://), not by this flag. Kept (not
                              removed) to avoid churning the I25 three-way config sync.

W3C TraceContext propagation: use opentelemetry-instrumentation-httpx (outbound)
and opentelemetry-instrumentation-fastapi (inbound) in the service setup code.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import logging.handlers
import queue
import threading
from typing import Any

logger = logging.getLogger("yadgar.tracing")

# Re-entry guard for LogSpanProcessor._emit_span_log. Emitting the I14 "span_end"
# log record can re-enter _emit_span_log on the SAME thread when a log handler or
# filter on the dispatch path is itself observed/traced (an @observe'd
# LogRingHandler.emit / ContentRedactor.filter opens a span whose end fires
# _emit_span_log again → logger.info("span_end") → handler → ...). That recursion
# is unbounded, GIL-bound, and signal-immune (pytest-timeout's SIGALRM never
# fires), which hung the full unit suite under -n auto. The guard suppresses a
# span_end emission that occurs synchronously inside another one — an artifact of
# the log-path being observed, never a legitimate nested span.
_span_log_reentry = threading.local()

# ---------------------------------------------------------------------------
# C2 P2 (obs-train, docs/plans/observability-health-otlp-fix.md):
# span-logging off the event-loop via QueueHandler + QueueListener
# ---------------------------------------------------------------------------
#
# LogSpanProcessor._emit_span_log calls a SYNCHRONOUS stdlib logger.info per span
# on the span-ending thread. For @trace_span-decorated async handlers (e.g. the
# /health handler) that thread is the EVENT LOOP thread. Under an OTLP retry flood
# the span-log shares the downstream logging-handler lock with the OTLP worker and
# can stall the event loop.
#
# Fix: attach a QueueHandler to the "yadgar.tracing" logger so the calling thread
# only does queue.put() (never touches the slow downstream handler's lock), and run
# a QueueListener on a background thread that forwards records to the real
# downstream handler(s). Same records, same content — just delivered off-thread.
_SPAN_LOG_QUEUE: queue.SimpleQueue[Any] | None = None
_SPAN_LOG_LISTENER: logging.handlers.QueueListener | None = None
_SPAN_LOG_HANDLER: logging.handlers.QueueHandler | None = None
_SPAN_LOG_PREV_PROPAGATE: bool | None = None
_SPAN_LOG_LOCK = threading.Lock()


def _install_span_log_queue(
    downstream_handlers: list[logging.Handler] | None = None,
) -> logging.handlers.QueueListener | None:
    """Route the 'yadgar.tracing' logger through a QueueHandler/QueueListener.

    The calling (event-loop) thread only enqueues; a background listener thread
    forwards records to ``downstream_handlers``. When not supplied, a snapshot of
    the ROOT logger's handlers is used (the daemon sink — yadgar propagates to
    root). Idempotent: repeated calls do not stack handlers or spawn extra
    listener threads. Returns the active listener (or None if no downstream sink).
    """
    global _SPAN_LOG_QUEUE, _SPAN_LOG_LISTENER, _SPAN_LOG_HANDLER, _SPAN_LOG_PREV_PROPAGATE

    with _SPAN_LOG_LOCK:
        if _SPAN_LOG_LISTENER is not None:
            return _SPAN_LOG_LISTENER  # already installed (idempotent)

        if downstream_handlers is None:
            downstream_handlers = list(logging.getLogger().handlers)
        if not downstream_handlers:
            # No sink to forward to — leave synchronous propagation in place.
            return None

        q: queue.SimpleQueue[Any] = queue.SimpleQueue()
        qhandler = logging.handlers.QueueHandler(q)
        listener = logging.handlers.QueueListener(
            q, *downstream_handlers, respect_handler_level=True
        )
        listener.start()

        tr_logger = logging.getLogger("yadgar.tracing")
        _SPAN_LOG_PREV_PROPAGATE = tr_logger.propagate
        tr_logger.addHandler(qhandler)
        # Prevent double-delivery: records go via the QueueHandler only, not also
        # up to root's handlers synchronously on the calling thread.
        tr_logger.propagate = False

        _SPAN_LOG_QUEUE = q
        _SPAN_LOG_LISTENER = listener
        _SPAN_LOG_HANDLER = qhandler
        return listener


def _stop_span_log_queue() -> None:
    """Stop the span-log listener (drains the queue + joins the thread) and detach.

    Idempotent and safe to call when never installed. Restores the prior propagate
    flag on the 'yadgar.tracing' logger.
    """
    global _SPAN_LOG_QUEUE, _SPAN_LOG_LISTENER, _SPAN_LOG_HANDLER, _SPAN_LOG_PREV_PROPAGATE

    with _SPAN_LOG_LOCK:
        listener = _SPAN_LOG_LISTENER
        qhandler = _SPAN_LOG_HANDLER
        prev_propagate = _SPAN_LOG_PREV_PROPAGATE

        tr_logger = logging.getLogger("yadgar.tracing")
        if qhandler is not None:
            tr_logger.removeHandler(qhandler)
        if prev_propagate is not None:
            tr_logger.propagate = prev_propagate

        _SPAN_LOG_QUEUE = None
        _SPAN_LOG_LISTENER = None
        _SPAN_LOG_HANDLER = None
        _SPAN_LOG_PREV_PROPAGATE = None

    # stop() blocks until the queue drains + the thread joins — do it outside the
    # lock so a slow downstream handler can't deadlock concurrent install/stop.
    if listener is not None:
        listener.stop()


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
        # C2 P3: wrap in the circuit breaker so a down collector stops the
        # retry/log flood after K consecutive failures (OTLP stays enabled).
        wrapped = _CircuitBreakerSpanExporter(exporter)
        logger.info(
            "otlp_exporter_init",
            extra={
                "event": "otlp_exporter_init",
                "component": "tracing",
                "endpoint": endpoint,
                "timeout_sec": timeout,
                "headers_count": len(headers),
                "circuit_breaker": True,
                "cb_failure_threshold": _OTLP_CB_FAILURE_THRESHOLD,
                "cb_reset_sec": _OTLP_CB_RESET_SEC,
            },
        )
        return wrapped
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
# C2 P3 (obs-train, docs/plans/observability-health-otlp-fix.md):
# OTLP exporter circuit-breaker + rate-limited failure logging
# ---------------------------------------------------------------------------
#
# The OTLPSpanExporter + BatchSpanProcessor have NO app-level circuit-breaker: when
# the collector is down they retry on the SDK's ~5s cadence FOREVER and log every
# failed batch (the observed 14h flood). The breaker wraps the underlying exporter so
# that after K consecutive export() failures the circuit OPENS — export() returns
# FAILURE WITHOUT a network attempt for a backoff window, then HALF-OPENS to probe;
# a probe success CLOSES it. Failure logging is rate-limited to once per open window.
# OTLP stays ENABLED; this just stops the flood when the collector is unreachable.
_OTLP_CB_FAILURE_THRESHOLD = 5  # consecutive failures before opening the circuit
_OTLP_CB_RESET_SEC = 60.0  # open-circuit backoff window before a half-open probe

if _OTEL_AVAILABLE:
    import time as _time
    from collections.abc import Sequence

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _CircuitBreakerSpanExporter(SpanExporter):
        """Wrap a SpanExporter with a consecutive-failure circuit breaker.

        States: CLOSED (normal), OPEN (short-circuit export, no network), HALF_OPEN
        (one probe allowed after the reset window). Failure logging is rate-limited
        to once per open-circuit window so a dead collector cannot drive a log flood.
        Thread-safe: BatchSpanProcessor calls export() from its worker thread.
        """

        def __init__(
            self,
            inner: SpanExporter,
            failure_threshold: int = _OTLP_CB_FAILURE_THRESHOLD,
            reset_timeout_sec: float = _OTLP_CB_RESET_SEC,
            time_fn=None,
        ) -> None:
            self._inner = inner
            self._failure_threshold = max(1, int(failure_threshold))
            self._reset_timeout_sec = float(reset_timeout_sec)
            self._time_fn = time_fn or _time.monotonic
            self._lock = threading.Lock()
            self._consecutive_failures = 0
            self._opened_at: float | None = None  # monotonic ts when circuit opened
            self._logged_this_window = False

        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            now = self._time_fn()

            with self._lock:
                if self._opened_at is not None:
                    # Circuit is OPEN. Short-circuit unless the reset window elapsed.
                    if (now - self._opened_at) < self._reset_timeout_sec:
                        return SpanExportResult.FAILURE
                    # else: window elapsed -> fall through as a HALF_OPEN probe.

            # CLOSED or HALF_OPEN: attempt the real export (outside the lock so a slow
            # underlying export does not serialize the breaker's bookkeeping).
            try:
                result = self._inner.export(spans)
            except Exception:  # noqa: BLE001 — treat an exporter raise as a failure
                result = SpanExportResult.FAILURE

            with self._lock:
                if result == SpanExportResult.SUCCESS:
                    # Recovery: close the circuit, reset counters + log gate.
                    self._consecutive_failures = 0
                    self._opened_at = None
                    self._logged_this_window = False
                    return result

                # Failure path.
                self._consecutive_failures += 1
                if (
                    self._opened_at is None
                    and self._consecutive_failures >= self._failure_threshold
                ):
                    self._opened_at = now
                elif self._opened_at is not None:
                    # Half-open probe failed -> re-open with a fresh window.
                    self._opened_at = now
                    self._logged_this_window = False

                if self._opened_at is not None and not self._logged_this_window:
                    self._logged_this_window = True
                    logger.warning(
                        "otlp_circuit_open",
                        extra={
                            "event": "otlp_circuit_open",
                            "component": "tracing",
                            "consecutive_failures": self._consecutive_failures,
                            "reset_timeout_sec": self._reset_timeout_sec,
                            "action": (
                                "short-circuiting OTLP export until the collector "
                                "recovers (rate-limited: once per open window)"
                            ),
                        },
                    )
                return result

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return self._inner.force_flush(timeout_millis)

        def shutdown(self) -> None:
            self._inner.shutdown()


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
            # Re-entry guard: if this thread is already inside a span_end emission,
            # skip — the nested call is the log↔span recursion (see module comment
            # on _span_log_reentry), not a legitimate span. Prevents the unbounded
            # GIL-bound recursion that hung the -n auto unit suite.
            if getattr(_span_log_reentry, "active", False):
                return
            _span_log_reentry.active = True
            try:
                self._emit_span_log_inner(span)
            finally:
                _span_log_reentry.active = False

        def _emit_span_log_inner(self, span: Any) -> None:
            """Actual span_end log emission (guarded by _emit_span_log)."""
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


def _instrument_httpx() -> None:
    """Activate HTTPXClientInstrumentor once (idempotent, guarded, no-op on absence).

    v5.101 R2: the single choke-point for httpx traceparent injection. Called from
    setup_tracing() so every entry mode (HTTP app, stdio, daemon CLI, backend) that
    initialises tracing also instruments outbound httpx — closing the stdio/daemon
    hole where backend HTTP rooted a disconnected trace.
    """
    try:
        from opentelemetry.instrumentation.httpx import (  # noqa: PLC0415
            HTTPXClientInstrumentor,
        )

        instr = HTTPXClientInstrumentor()
        if not instr.is_instrumented_by_opentelemetry:
            instr.instrument()
    except Exception:
        pass  # OTel/httpx instrumentation not available — no-op (I3)


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

    # v5.101 R2: hoist httpx instrumentation into the single tracing choke-point
    # so stdio/daemon-mode entry paths (which never import server/_app.py) still
    # auto-inject W3C traceparent on outbound httpx calls — no disconnected traces.
    _instrument_httpx()

    # C2 P2: route per-span logging off the calling (event-loop) thread.
    _install_span_log_queue()

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
    # C2 P2: stop the off-thread span-log listener first (drains + joins).
    _stop_span_log_queue()

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
                # I3 graceful degradation: a degraded/swapped provider whose
                # get_tracer raises (e.g. the hanging stub installed during
                # shutdown_tracing, reached when a log record fires mid-teardown
                # through an observed log-path filter) must NOT break the wrapped
                # fn — run it without a span. Guard get_tracer ONLY; the fn's own
                # exceptions still propagate + record via the span path below.
                try:
                    tracer = _otel_trace.get_tracer(fn.__module__)
                except Exception:
                    return await fn(*args, **kwargs)
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

            # Sentinel: marks this fn as already span-sourced so @observe (I33)
            # runs in metric+log-only mode and never opens a second span.
            async_wrapper._yadgar_observe_has_span = True  # type: ignore[attr-defined]
            return async_wrapper

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                # I3 graceful degradation — see async_wrapper. Guard get_tracer
                # ONLY; the wrapped fn's own exceptions still propagate + record.
                try:
                    tracer = _otel_trace.get_tracer(fn.__module__)
                except Exception:
                    return fn(*args, **kwargs)
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

            sync_wrapper._yadgar_observe_has_span = True  # type: ignore[attr-defined]
            return sync_wrapper

    # Support both @trace_span and @trace_span() and @trace_span("name")
    # If called as @trace_span("name") or @trace_span(), name is str or None
    # If called as @trace_span(fn), fn is a callable (no parens case not supported
    # for simplicity — always use with parens)
    return decorator


# ---------------------------------------------------------------------------
# span() — inline sub-stage context manager (v5.100)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def span(name: str, **attributes: Any):
    """Inline OTel child span for STAGE-level sub-spans (v5.100).

    Use for fine-grained per-stage attribution *inside* an already-spanned
    function (e.g. embed / knn / fusion inside ``retrieval.recall``), where a
    whole-function ``@trace_span`` decorator does not fit. Nests under the
    current span automatically (``start_as_current_span``), so Tempo shows the
    stage as a child of the enclosing operation — *provided the caller runs on
    the same thread/asyncio-task* (OTel context does not cross a raw executor
    boundary).

    HARD CONSTRAINT (no-slowness): span at STAGE granularity, NEVER per loop
    item. Record loop sizes as attributes (``candidates=n``), not child spans.
    Keep attribute values small (ints / short strings) — never embeddings or
    large lists. Export is async (BatchSpanProcessor, off the event loop), so
    this adds no blocking I/O.

    On exception: records it + sets status=ERROR + re-raises (matches
    ``@trace_span``). No-ops to ``nullcontext`` when OTel is unavailable.

    Example::

        with span("retrieval.embed_query", query_len=len(q)):
            emb = embed(q)
    """
    if not _OTEL_AVAILABLE:  # pragma: no cover
        yield None
        return

    tracer = _otel_trace.get_tracer("yadgar")
    with tracer.start_as_current_span(name) as sp:
        for k, v in attributes.items():
            if v is not None:
                sp.set_attribute(k, v)
        try:
            yield sp
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(_otel_trace.Status(StatusCode.ERROR, str(exc)))
            raise
