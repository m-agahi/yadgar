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

from yadgar.observability.observe import observe
from yadgar.server._app import mcp_server
from yadgar.tracing import trace_span

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


@observe(tier="stage")
def _ring_append(entry: dict) -> None:
    """Append an entry to the ring, evicting oldest when byte cap is exceeded."""
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
# Logging handler that feeds the ring
# ---------------------------------------------------------------------------


class LogRingHandler(logging.Handler):
    """Push LogRecord dicts into the in-memory ring buffer."""

    @observe(tier="stage")
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
    from yadgar.config import resolve_knob  # noqa: PLC0415

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
@trace_span("api.logs.capabilities")
async def logs_capabilities(request: Request) -> JSONResponse:
    """Probe endpoint: advertise SSE + poll support."""
    return await logs_capabilities_handler(request)


@mcp_server.custom_route("/api/logs/poll", methods=["GET"])
@trace_span("api.logs.poll")
async def logs_poll(request: Request) -> JSONResponse:
    """Long-poll fallback: return buffered log lines since ?since=<seq>."""
    return await logs_poll_handler(request)


@mcp_server.custom_route("/api/logs/stream", methods=["GET"])
@trace_span("api.logs.stream")
async def logs_stream(request: Request):
    """SSE stream: push daemon log lines as they arrive."""
    return await logs_stream_handler(request)
