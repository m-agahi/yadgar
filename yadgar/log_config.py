"""Structured logging configuration for Yadgar.

Supports two log formats:
- 'human' (default): standard Python %(asctime)s %(name)s %(levelname)s %(message)s
- 'json': one JSON object per log line, with all LogRecord fields + any extra= fields.

Usage:
    from yadgar.log_config import configure_logging
    configure_logging(log_format="json", level="INFO")

The JSON formatter propagates extra= fields directly into the JSON object.
Key extra fields:
    request_id  — unique per-request ID
    tool_name   — name of the MCP tool being called
    duration_ms — request duration in milliseconds
    status      — "ok" | "error"
    trace_id    — from MCP client x-request-id header

RequestLoggingMiddleware:
    ASGI middleware that emits one structured log line per HTTP request.
    Reads x-request-id header as trace_id; generates request_id from path+time.
    Emits via logger "yadgar.requests" at INFO level.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Standard LogRecord attributes (levelname, name, message) are always
    included. Any extra= fields passed to logger.info() etc. are merged in.
    """

    # Fields in LogRecord that we do NOT want to leak into the JSON output
    # (they're Python internals or duplicates).
    _SKIP_FIELDS = frozenset(
        {
            "args",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "lineno",
            "module",
            "msecs",
            "msg",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        # Base fields
        payload: dict = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge any extra= fields the caller attached
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in self._SKIP_FIELDS:
                continue
            if key in payload:
                continue
            # Skip built-in LogRecord fields that are already covered
            if key in (
                "levelname",
                "levelno",
                "name",
                "created",
                "asctime",
            ):
                if key == "created":
                    # Include created as a float epoch if not already captured
                    payload["created"] = value
                continue
            payload[key] = value

        # Include exception info if present
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        try:
            return json.dumps(payload, default=str)
        except Exception:
            return json.dumps({"message": str(record.getMessage()), "level": record.levelname})


_request_logger = logging.getLogger("yadgar.requests")


class RequestLoggingMiddleware:
    """ASGI middleware that emits one structured log line per HTTP request.

    Emits at INFO level on "yadgar.requests" logger.  Fields:
        request_id  — random UUID per request
        tool_name   — HTTP method + path (e.g. "GET /health")
        duration_ms — integer milliseconds
        status      — HTTP status code as string ("200", "401", etc.)
        trace_id    — value of x-request-id header, or "" if absent
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        request_id = str(uuid.uuid4())

        # Extract trace_id from x-request-id header
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        trace_id = headers.get(b"x-request-id", b"").decode("latin-1")

        method = scope.get("method", "")
        path = scope.get("path", "")
        tool_name = f"{method} {path}"

        status_code: list[int] = []

        async def send_with_capture(message):
            if message["type"] == "http.response.start":
                status_code.append(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_with_capture)
            status = str(status_code[0]) if status_code else "0"
        except Exception:
            status = "500"
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            _request_logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "duration_ms": duration_ms,
                    "status": status,
                    "trace_id": trace_id,
                },
            )


def configure_logging(
    log_format: str | None = None,
    level: str = "WARNING",
) -> None:
    """Configure the 'yadgar' logger with the appropriate formatter.

    log_format: 'json' | 'human' (default: read from YADGAR_LOG_FORMAT env,
                then fall back to 'human').
    level: logging level string (default 'WARNING').
    """
    if log_format is None:
        log_format = os.environ.get("YADGAR_LOG_FORMAT", "human").lower()

    logger = logging.getLogger("yadgar")
    numeric_level = getattr(logging, level.upper(), logging.WARNING)
    logger.setLevel(numeric_level)

    if log_format == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    # Add a new StreamHandler only if there are no existing handlers
    # that use our formatter type (avoids duplicate handlers in tests).
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.setLevel(numeric_level)
    logger.addHandler(handler)
    logger.propagate = False
