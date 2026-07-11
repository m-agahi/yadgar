"""Log streaming routes — v5.52.0.

Endpoints:
  GET /api/logs/_capabilities  — probe: {"sse": true, "poll": true}
  GET /api/logs/stream         — SSE of daemon log lines (text/event-stream)
  GET /api/logs/poll           — long-poll fallback: {lines:[...], next_seq:<int>}

Ring buffer: in-memory, byte-capped at LOG_RING_BUFFER_MAX_BYTES (default 1 MB).
A LogRingHandler (logging.Handler subclass) attaches to the root logger and pushes
every LogRecord into the ring. The handler is attached lazily on first import so it
is present for all routes.

Gate: all routes require YADGAR_DEBUG_APIS_ENABLED=on (enforced in
BearerAuthMiddleware via _DEBUG_API_PREFIXES before the request reaches these
handlers; handlers still verify for defence-in-depth).

Registered as a side-effect import in yadgar/server/__init__.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from threading import Lock

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.server._app import mcp_server

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ring buffer constants
# ---------------------------------------------------------------------------

LOG_RING_BUFFER_MAX_BYTES: int = 1_048_576  # 1 MB

# ---------------------------------------------------------------------------
# Log ring buffer
# ---------------------------------------------------------------------------

_ring_lock: Lock = Lock()
_ring: deque[dict] = deque()
_ring_bytes: int = 0
_ring_seq: int = 0  # monotonic sequence counter, incremented per entry


def _entry_bytes(entry: dict) -> int:
    """Rough byte estimate for a log entry dict."""
    return len(json.dumps(entry, default=str))


@observe(tier="stage", span=False)
def _ring_append(entry: dict) -> None:
    """Append an entry to the ring, evicting oldest when byte cap is exceeded.

    span=False: _ring_append is called from LogRingHandler.emit, which fires on EVERY
    log record — including logs emitted by @observe itself. A span-per-log-append creates
    a span→log→ring→span feedback loop: handler() @observe emits INFO → LogRingHandler →
    _ring_append → new span → captured by exporter → test sees 2 spans instead of 1.
    Stage metrics still emit; only the span is suppressed. LogRingHandler.emit already
    documents this class of bug (v5.106, ADR-0041).
    """
    global _ring_bytes, _ring_seq
    size = _entry_bytes(entry)
    with _ring_lock:
        _ring_seq += 1
        entry["seq"] = _ring_seq
        _ring.append(entry)
        _ring_bytes += size
        # Evict oldest entries until under cap
        while _ring_bytes > LOG_RING_BUFFER_MAX_BYTES and _ring:
            evicted = _ring.popleft()
            _ring_bytes -= _entry_bytes(evicted)


@observe(tier="stage")
def get_ring_snapshot(since_seq: int = 0) -> tuple[list[dict], int]:
    """Return (entries_since_seq, next_seq).

    Returns entries where entry['seq'] > since_seq, and the next_seq value
    a caller should use in a subsequent poll (= max seq in returned list, or
    since_seq if nothing new).
    """
    with _ring_lock:
        entries = [e for e in _ring if e.get("seq", 0) > since_seq]
        next_seq = entries[-1]["seq"] if entries else since_seq
    return entries, next_seq


# ---------------------------------------------------------------------------
# Telemetry filter — keep span_end out of the APP-log ring
# ---------------------------------------------------------------------------


class _SpanEndFilter(logging.Filter):
    """Drop OTel ``span_end`` telemetry records from the app-log ring (v5.106).

    ``LogSpanProcessor._emit_span_log`` (yadgar/tracing.py) emits one
    ``event=="span_end"`` INFO record on the ``yadgar.tracing`` logger for EVERY
    finished span. That logger propagates to root, and root's handlers include
    ``LogRingHandler`` — so under a RECORDING OTLP provider (prod: always on) the
    ring served by ``/api/logs/poll`` fills with span_end telemetry instead of
    application logs.

    This is the 3rd occurrence of the span→log→ring class (ADR-0041). The earlier
    fixes exempted individual @observe'd functions from opening spans, which is
    whack-a-mole: span_end reaches the ring from MANY span sources, so exempting
    one more function is always one gap short. Filtering the RING handler itself
    makes it structurally immune regardless of how many spans exist or which
    provider (recording vs non-recording) is active — no future @observe or new
    span source can re-contaminate it.

    Scope is deliberately narrow: only the ``span_end`` telemetry event is
    dropped. Operational tracing warnings on the same logger
    (``otlp_circuit_open``, ``tracing_init``, ``otlp_exporter_init_failed``, …)
    still reach the ring — those are genuine ops signal, span_end is not.

    span_end is NOT silently discarded: it still flows to the file/stdout/OTLP
    sinks via root's other handlers (see test_span_end_reaches_file_handler). This
    filter removes it from the ring ONLY.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # The span_end record carries event="span_end" + component="tracing" in
        # its extra fields (see LogSpanProcessor._emit_span_log_inner). Match on
        # the telemetry marker, not the logger name, so we drop exactly the record
        # that scales with span count and nothing else.
        if getattr(record, "event", None) == "span_end":
            return False
        return True


# ---------------------------------------------------------------------------
# Logging handler that feeds the ring
# ---------------------------------------------------------------------------


class LogRingHandler(logging.Handler):
    """Push LogRecord dicts into the in-memory ring buffer.

    Carries a ``_SpanEndFilter`` so OTel span_end telemetry never enters the
    app-log ring (v5.106, ADR-0041 class). The filter is attached in ``__init__``,
    so every handler instance — however constructed — is immune.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.addFilter(_SpanEndFilter())

    # NOT @observe'd: this is the log-emission sink. @observe would open a span
    # per record → span_end log (LogSpanProcessor) → that record re-enters emit()
    # → more spans → per-log amplification flood (v5.106). Allowlisted as
    # framework-instrumented in .observe-allowlist.json (logs:LogRingHandler.emit).
    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": self.format(record),
            }
            _ring_append(entry)
        except Exception:  # noqa: BLE001
            pass  # ring handler must never raise


_handler_installed: bool = False
_handler_lock: Lock = Lock()


@observe(tier="stage")
def install_ring_handler() -> None:
    """Attach LogRingHandler to root logger (idempotent)."""
    global _handler_installed
    with _handler_lock:
        if _handler_installed:
            return
        root = logging.getLogger()
        h = LogRingHandler()
        h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        root.addHandler(h)
        _handler_installed = True


# Install on module import
install_ring_handler()


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------


def _is_debug_apis_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_DEBUG_APIS_ENABLED",
        "DEBUG_APIS_ENABLED",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )


@observe(tier="stage")
def _gate_check() -> JSONResponse | None:
    """Return 403 JSONResponse when debug APIs disabled, else None."""
    if not _is_debug_apis_enabled():
        return JSONResponse({"error": "debug APIs disabled"}, status_code=403)
    return None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@observe(tier="boundary")
async def logs_capabilities_handler(request: Request) -> JSONResponse:
    """GET /api/logs/_capabilities — probe SSE + poll support."""
    denied = _gate_check()
    if denied is not None:
        return denied
    return JSONResponse({"sse": True, "poll": True})


@observe(tier="boundary")
async def logs_poll_handler(request: Request) -> JSONResponse:
    """GET /api/logs/poll?since=<seq> — return buffered lines since seq.

    Query params:
      since=<int>  — sequence number (default 0 = return everything)
    """
    denied = _gate_check()
    if denied is not None:
        return denied

    try:
        since = int(request.query_params.get("since", "0"))
    except (ValueError, TypeError):  # fmt: skip
        since = 0

    entries, next_seq = get_ring_snapshot(since_seq=since)
    return JSONResponse({"lines": entries, "next_seq": next_seq})


@observe(tier="boundary")
async def logs_stream_handler(request: Request) -> StreamingResponse:
    """GET /api/logs/stream — SSE stream of new log lines.

    Sends buffered lines immediately, then polls the ring for new entries
    every 0.5s. Terminates when the client disconnects.

    The SSE payload per event: data: <json>\n\n
    """
    denied = _gate_check()
    if denied is not None:
        # Can't return StreamingResponse with error body cleanly; return JSON
        return denied  # type: ignore[return-value]

    async def event_generator():
        # Send all buffered entries first
        entries, seq = get_ring_snapshot(since_seq=0)
        for e in entries:
            yield f"data: {json.dumps(e, default=str)}\n\n"

        # Then poll for new entries
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.5)
            new_entries, seq = get_ring_snapshot(since_seq=seq)
            for e in new_entries:
                yield f"data: {json.dumps(e, default=str)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/logs/_capabilities", methods=["GET"])
@trace_span()
async def logs_capabilities(request: Request) -> JSONResponse:
    """Probe endpoint: advertise SSE + poll support."""
    return await logs_capabilities_handler(request)


@mcp_server.custom_route("/api/logs/poll", methods=["GET"])
@trace_span()
async def logs_poll(request: Request) -> JSONResponse:
    """Long-poll fallback: return buffered log lines since ?since=<seq>."""
    return await logs_poll_handler(request)


@mcp_server.custom_route("/api/logs/stream", methods=["GET"])
@trace_span()
async def logs_stream(request: Request):
    """SSE stream: push daemon log lines as they arrive."""
    return await logs_stream_handler(request)
