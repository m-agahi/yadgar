"""Structured logging configuration for Yadgar.

Supports two log formats:
- 'json' (default for production): I14-conformant JSON, one line per record.
- 'text' / 'human': standard Python %(asctime)s %(name)s %(levelname)s %(message)s

I14 JSON schema (see docs/ARCHITECTURE_INVARIANTS.md §I14):
    ts          — ISO 8601 timestamp with timezone
    level       — DEBUG/INFO/WARNING/ERROR/CRITICAL
    component   — module/subsystem identifier
    action      — verb describing what's happening
    outcome     — ok/error/skip/degraded
    event       — the log message string (record.msg)
    latency_ms? — optional float duration in ms
    error?      — optional error string / exception class
    traceback?  — truncated traceback (≤ TRACEBACK_MAX_CHARS chars)

ContentRedactor:
    Logging filter that strips sensitive keys from LogRecord extra fields.
    Two-tier denylist (v5.4.7):
      Exact match (case-insensitive): content, auth, token, secret, bearer.
        Only redacted if field name IS the word exactly.
      Substring match (case-insensitive): password, api_key, authorization,
        access_token, refresh_token, client_secret, private_key.
        Redacted if the field name contains any of these as a substring.
    Avoids false positives on content_type / content_length.
    Also redacts the 'content' key from any dict-valued extra field (memory dicts).

Usage:
    from yadgar.log_config import configure_logging
    configure_logging(log_format="json", level="INFO")

The JSON formatter propagates extra= fields directly into the JSON object.
Key extra fields (I14 schema, v5.4.7+):
    component   — subsystem emitting the log (e.g. "http_server")
    action      — verb describing what's happening (e.g. "request")
    outcome     — "ok" | "error" | "degraded" | "skip"
    latency_ms  — request duration in milliseconds (was duration_ms pre-v5.4.7)
    http_status — HTTP status code as string (was status pre-v5.4.7)
    request_id  — unique per-request ID
    tool_name   — HTTP method + path (e.g. "GET /health")
    trace_id    — from MCP client x-request-id header

RequestLoggingMiddleware:
    ASGI middleware that emits one structured log line per HTTP request.
    Reads x-request-id header as trace_id; generates request_id from uuid4.
    Emits via logger "yadgar.requests" at INFO level.
    v5.4.7: migrated to I14 schema (component/action/outcome/latency_ms/http_status).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Literal

# I14 — max chars for traceback in structured JSON (constant for test import)
TRACEBACK_MAX_CHARS: int = 2000

# I14 — two-tier denylist for sensitive field names (v5.4.7).
#
# Exact-match tier: redact ONLY if the field name IS the word exactly.
# Avoids false positives on content_type, content_length, auth_token_provider, etc.
_EXACT_DENYLIST: frozenset[str] = frozenset({"content", "auth", "token", "secret", "bearer"})

# Substring-match tier: redact if field name CONTAINS the substring.
# These compound patterns unambiguously indicate secrets in any context.
_SUBSTRING_DENYLIST: frozenset[str] = frozenset(
    {
        "password",
        "api_key",
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
    }
)

# Fields that are stdlib LogRecord internals — skip in I14 formatter
_I14_SKIP_FIELDS = frozenset(
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
        "levelname",
        "levelno",
        "name",
        "created",
        "asctime",
        "taskName",
    }
)

# Fields emitted explicitly by JSONLogFormatter — don't re-emit from __dict__
_I14_EXPLICIT_FIELDS = frozenset({"ts", "level", "event", "traceback", "error"})


def _is_sensitive(name: str) -> bool:
    """Return True if field name matches either denylist tier (case-insensitive).

    Tier 1 — exact: redact only if name IS the word (e.g. 'content', 'token').
    Tier 2 — substring: redact if name CONTAINS a compound secret pattern
              (e.g. 'authorization', 'api_key', 'client_secret').
    """
    lower = name.lower()
    if lower in _EXACT_DENYLIST:
        return True
    return any(s in lower for s in _SUBSTRING_DENYLIST)


def _redact_dict(d: dict) -> dict:
    """Return shallow copy of d with 'content' key removed (one-level deep)."""
    return {k: v for k, v in d.items() if k != "content"}


class ContentRedactor(logging.Filter):
    """Logging filter that strips sensitive fields from LogRecord extra attributes.

    Removes any LogRecord attribute whose name contains a denylist substring
    (case-insensitive). Also removes the 'content' key from dict-valued extra
    fields (e.g. memory dicts passed as extra={"memory": {...}}).

    Does NOT remove attributes that were set by stdlib (levelname, msg, etc.).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        sensitive_keys = [
            k
            for k in list(record.__dict__)
            if not k.startswith("_") and k not in _I14_SKIP_FIELDS and _is_sensitive(k)
        ]
        for key in sensitive_keys:
            # Replace with None rather than delattr — some formatters may iterate __dict__
            setattr(record, key, None)

        # One-level dict redaction: remove 'content' key inside dict extra values
        for k, v in list(record.__dict__.items()):
            if k.startswith("_") or k in _I14_SKIP_FIELDS:
                continue
            if isinstance(v, dict) and "content" in v:
                setattr(record, k, _redact_dict(v))

        return True  # always pass the record through


class JSONLogFormatter(logging.Formatter):
    """I14-conformant JSON log formatter.

    Emits one compact JSON line per record with fields:
        ts, level, component, action, outcome, event
    plus optional: latency_ms, error, traceback, and any non-sensitive extra fields.

    Backwards note: pre-I14 JsonFormatter emitted 'timestamp'/'logger'/'message'.
    This formatter uses the I14 schema ('ts'/'event'). See MIGRATION_NOTES.md.
    """

    def _build_base(self, record: logging.LogRecord) -> dict:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        return {
            "ts": ts,
            "level": record.levelname,
            "event": record.getMessage(),
        }

    def _append_extras(self, payload: dict, record: logging.LogRecord) -> None:
        for key, value in record.__dict__.items():
            if key.startswith("_"):
                continue
            if key in _I14_SKIP_FIELDS or key in _I14_EXPLICIT_FIELDS:
                continue
            if value is None:
                # Skip None — ContentRedactor sets sensitive keys to None
                continue
            # Last-resort guard: skip sensitive fields even if redactor not installed
            if _is_sensitive(key):
                continue
            # One-level dict redaction: strip 'content' from dict values (memory dicts)
            if isinstance(value, dict) and "content" in value:
                value = _redact_dict(value)
            payload[key] = value

    def _append_traceback(self, payload: dict, record: logging.LogRecord) -> None:
        if not record.exc_info:
            return
        tb_text = self.formatException(record.exc_info)
        if len(tb_text) > TRACEBACK_MAX_CHARS:
            tb_text = tb_text[:TRACEBACK_MAX_CHARS] + " … [truncated]"
        payload["traceback"] = tb_text
        exc_type = record.exc_info[0]
        if exc_type is not None:
            payload["error"] = exc_type.__name__

    def _append_trace_context(self, payload: dict) -> None:
        """Inject trace_id + span_id from active OTel span context (if any).

        Called on every log format — adds fields only when an OTel span is
        active so any log line can be correlated with its trace.
        """
        try:
            from yadgar.tracing import get_current_span_id, get_current_trace_id  # noqa: PLC0415

            tid = get_current_trace_id()
            sid = get_current_span_id()
            if tid is not None:
                payload["trace_id"] = tid
            if sid is not None:
                payload["span_id"] = sid
        except Exception:
            pass  # tracing not available / not yet initialized — skip silently

    def format(self, record: logging.LogRecord) -> str:
        payload = self._build_base(record)
        self._append_extras(payload, record)
        self._append_traceback(payload, record)
        self._append_trace_context(payload)
        try:
            return json.dumps(payload, default=str)
        except Exception:
            return json.dumps(
                {
                    "ts": payload.get("ts", ""),
                    "level": record.levelname,
                    "event": record.getMessage(),
                }
            )


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


def _outcome_from_status(status: str) -> str:
    """Map HTTP status string to I14 outcome value.

    Args:
        status: HTTP status code as string ("200", "404", etc.),
                or sentinel values "0" (no response) / "cancelled" (ASGI cancel).

    Returns:
        "ok"       — 2xx / 3xx
        "degraded" — ASGI scope cancelled mid-flight
        "error"    — 4xx, 5xx, "0", or any unrecognised value
    """
    if status == "cancelled":
        return "degraded"
    if status.isdigit():
        code = int(status)
        if 200 <= code <= 399:
            return "ok"
    return "error"


class RequestLoggingMiddleware:
    """ASGI middleware that emits one structured log line per HTTP request.

    Emits at INFO level on "yadgar.requests" logger.
    v5.4.7: I14-conformant schema — fields:
        component   — "http_server" (I14 required)
        action      — "request" (I14 required)
        outcome     — "ok"|"error"|"degraded" via _outcome_from_status (I14 required)
        latency_ms  — integer milliseconds (was duration_ms pre-v5.4.7)
        http_status — HTTP status code as string (was status pre-v5.4.7)
        request_id  — random UUID per request
        tool_name   — HTTP method + path (e.g. "GET /health")
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
        # Defensive init: covers BaseException paths (asyncio.CancelledError on
        # shutdown of in-flight requests) where neither try-body nor except-Exception
        # runs, leaving `status` unbound in `finally`. Pre-v5.4.5 bug.
        status = "0"

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
        except BaseException:
            status = "cancelled"
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            _request_logger.info(
                "request",
                extra={
                    "component": "http_server",
                    "action": "request",
                    "outcome": _outcome_from_status(status),
                    "latency_ms": duration_ms,
                    "http_status": status,
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "trace_id": trace_id,
                },
            )


def _configure_yadgar_logger(numeric_level: int, *, propagate: bool) -> None:
    """Set level and propagate flag on the yadgar logger; clear its own handlers.

    v5.4.3: yadgar propagates to root (propagate=True) — root handler covers output.
    """
    logger = logging.getLogger("yadgar")
    logger.setLevel(numeric_level)
    logger.handlers.clear()
    logger.propagate = propagate


def _configure_request_logger(formatter: logging.Formatter) -> None:
    """Install a dedicated always-INFO handler on yadgar.requests (v5.4.8).

    Root cause fix: CORE_LOG_LEVEL defaults to 'warn' → root handler at WARNING
    → yadgar.requests (child of yadgar) inherits WARNING → INFO records dropped.

    Solution: attach a dedicated StreamHandler at INFO on yadgar.requests with
    propagate=False so request telemetry flows regardless of root log level.
    Idempotent: no-op if a handler is already present.
    """
    req_logger = logging.getLogger("yadgar.requests")
    if req_logger.handlers:
        # Already configured — idempotency guard: update level only
        for h in req_logger.handlers:
            h.setLevel(logging.INFO)
        req_logger.setLevel(logging.INFO)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(ContentRedactor())
    handler.setLevel(logging.INFO)
    req_logger.setLevel(logging.INFO)
    req_logger.addHandler(handler)
    req_logger.propagate = False


# ---------------------------------------------------------------------------
# v5.5.1 — Rotating file handler (Sink B)
# ---------------------------------------------------------------------------

# Default log paths inside the container (both map through /data bind mount)
_DEFAULT_CORE_LOG_PATH: str = "/data/logs/yadgar.log"
_DEFAULT_BACKEND_LOG_PATH: str = "/data/logs/backend.log"

# Gauge update interval: update log-file size metric at most once per second
_LOG_SIZE_GAUGE_INTERVAL: float = 1.0


def _resolve_log_file_path(process: Literal["core", "backend"] = "core") -> str:
    """Option A env resolution: backend prefers YADGAR_BACKEND_LOG_FILE_PATH first.

    Priority (backend): YADGAR_BACKEND_LOG_FILE_PATH → YADGAR_LOG_FILE_PATH → default
    Priority (core):    YADGAR_LOG_FILE_PATH → default
    Returns empty string if operator explicitly set the resolved var to "".
    """
    if process == "backend":
        backend_specific = os.environ.get("YADGAR_BACKEND_LOG_FILE_PATH")
        if backend_specific is not None:
            return backend_specific
        shared = os.environ.get("YADGAR_LOG_FILE_PATH")
        if shared is not None:
            return shared
        return _DEFAULT_BACKEND_LOG_PATH
    else:
        shared = os.environ.get("YADGAR_LOG_FILE_PATH")
        if shared is not None:
            return shared
        return _DEFAULT_CORE_LOG_PATH


def _resolve_log_env_int(key: str, backend_key: str, default: int, process: str) -> int:
    """Option A env resolution for int settings."""
    if process == "backend":
        val = os.environ.get(backend_key)
        if val is not None:
            try:
                return int(val)
            except ValueError:
                pass
    val = os.environ.get(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return default


class RotatingJSONLFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that writes I14-conformant JSONL.

    Uses JSONLogFormatter. Hooks doRollover to update the rotation counter
    metric. Updates the file-size gauge on each emit (throttled to once/sec).

    Args:
        filename: Path to the active log file.
        maxBytes: Rotate when file exceeds this size (default 100 MB).
        backupCount: Number of backup files to keep (default 5).
        logger_name: Label used for Prometheus metrics (default "core").
        metrics_module: Module supplying yadgar_log_* metrics. Defaults to
            ``yadgar.metrics`` (core registry). Pass ``yadgar.embed_service_metrics``
            for the backend process so updates land in the isolated registry.
    """

    def __init__(
        self,
        filename: str,
        maxBytes: int = 100_000_000,
        backupCount: int = 5,
        logger_name: str = "core",
        metrics_module=None,
    ) -> None:
        super().__init__(
            filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding="utf-8",
            delay=True,
        )
        self.setFormatter(JSONLogFormatter())
        self.addFilter(ContentRedactor())
        self._logger_name = logger_name
        self._last_size_update: float = 0.0
        self._rotation_counter = None  # lazy — avoid import cycle at module load
        self._size_gauge = None
        self._metrics_module = metrics_module  # None → resolved lazily on first use

    def _ensure_metrics(self) -> None:
        if self._rotation_counter is not None:
            return
        try:
            if self._metrics_module is None:
                from yadgar import metrics as _m  # noqa: PLC0415

                self._metrics_module = _m
            _m = self._metrics_module
            self._rotation_counter = _m.yadgar_log_file_rotations_total
            self._size_gauge = _m.yadgar_log_file_size_bytes
        except Exception:
            pass

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        super().doRollover()
        self._ensure_metrics()
        if self._rotation_counter is not None:
            try:
                self._rotation_counter.labels(logger=self._logger_name).inc()
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        now = time.monotonic()
        if now - self._last_size_update >= _LOG_SIZE_GAUGE_INTERVAL:
            self._last_size_update = now
            self._ensure_metrics()
            if self._size_gauge is not None and self.baseFilename:
                try:
                    size = os.path.getsize(self.baseFilename)
                    self._size_gauge.labels(logger=self._logger_name).set(size)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# v5.5.1 — Token-bucket rate limiter
# ---------------------------------------------------------------------------

_RATE_LIMIT_SUMMARY_INTERVAL: float = 60.0  # emit summary at most once per minute


class RateLimitFilter(logging.Filter):
    """Token-bucket rate limiter per (logger_name, level) tuple.

    Drops records when the bucket is empty. Emits a "rate limited" summary
    to the root logger (at WARNING) at most once per minute when drops occur.
    The summary record is tagged _rate_limit_summary=True to prevent recursion.

    Gate: no-op when YADGAR_LOG_RATE_LIMIT_ENABLED=0 / "" (default ON).
    """

    def __init__(
        self,
        rate: float = 10.0,
        burst: int = 50,
        name: str = "",
        metrics_module=None,
    ) -> None:
        super().__init__(name)
        self._rate = rate
        self._burst = burst
        # Per (logger_name, levelno) → (tokens: float, last_time: float)
        self._buckets: dict[tuple[str, int], list[float]] = {}
        self._lock = __import__("threading").Lock()
        self._last_summary_time: float = 0.0
        self._drop_count_since_summary: int = 0
        self._dropped_counter = None  # lazy
        self._metrics_module = metrics_module  # None → resolved lazily on first use

    def _ensure_metrics(self) -> None:
        if self._dropped_counter is not None:
            return
        try:
            if self._metrics_module is None:
                from yadgar import metrics as _m  # noqa: PLC0415

                self._metrics_module = _m
            self._dropped_counter = getattr(self._metrics_module, "yadgar_log_dropped_total", None)
        except Exception:
            pass

    def filter(self, record: logging.LogRecord) -> bool:
        # Short-circuit for summary records to prevent recursion
        if getattr(record, "_rate_limit_summary", False):
            return True

        key = (record.name, record.levelno)
        now = time.monotonic()

        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = [float(self._burst), now]
            bucket = self._buckets[key]
            elapsed = now - bucket[1]
            bucket[0] = min(float(self._burst), bucket[0] + elapsed * self._rate)
            bucket[1] = now

            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                return True

            # Drop: update counter + track for summary
            self._drop_count_since_summary += 1
            self._ensure_metrics()
            if self._dropped_counter is not None:
                try:
                    self._dropped_counter.labels(
                        logger=record.name,
                        level=record.levelname,
                        reason="rate_limit",
                    ).inc()
                except Exception:
                    pass

            # Emit summary at most once per minute
            if now - self._last_summary_time >= _RATE_LIMIT_SUMMARY_INTERVAL:
                self._last_summary_time = now
                count = self._drop_count_since_summary
                self._drop_count_since_summary = 0
                self._emit_summary(record.name, count)

            return False

    def _emit_summary(self, logger_name: str, count: int) -> None:
        logging.getLogger(logger_name)
        summary = logging.LogRecord(
            name=logger_name,
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=f"rate limited: {count} records suppressed in the last minute",
            args=(),
            exc_info=None,
        )
        summary._rate_limit_summary = True  # type: ignore[attr-defined]
        summary.__dict__["component"] = "log_rate_limiter"
        summary.__dict__["action"] = "rate_limit_summary"
        summary.__dict__["outcome"] = "ok"
        # Emit directly to the root logger's handlers, bypassing filters
        root = logging.getLogger()
        for handler in root.handlers:
            try:
                handler.emit(summary)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# v5.5.1 — File handler install helper
# ---------------------------------------------------------------------------


def _resolve_metrics_module(process: str):
    """Return the metrics module for the given process kind.

    Backend uses the isolated embed_service_metrics registry so its log_*
    metric updates land in the registry exposed by backend's /metrics endpoint,
    not in core's registry. Core (default) uses yadgar.metrics.
    """
    try:
        if process == "backend":
            from yadgar import embed_service_metrics as _esm  # noqa: PLC0415

            return _esm
        from yadgar import metrics as _m  # noqa: PLC0415

        return _m
    except Exception:
        return None


def _install_file_handler(
    formatter: logging.Formatter,
    process: Literal["core", "backend"],
) -> RotatingJSONLFileHandler | None:
    """Resolve path and install RotatingJSONLFileHandler on root logger.

    Returns the handler if installed, None if skipped.
    Skips gracefully when path is empty (I3 opt-out) or dir unwritable.
    Idempotent: no-op if a RotatingJSONLFileHandler is already on root.
    """
    root = logging.getLogger()
    if any(isinstance(h, RotatingJSONLFileHandler) for h in root.handlers):
        return None  # already installed

    log_path = _resolve_log_file_path(process)
    if not log_path:
        return None  # explicit opt-out

    log_dir = os.path.dirname(log_path)
    if log_dir and not os.path.isdir(log_dir):
        logging.getLogger().warning(
            "log file handler skipped: directory not found or not writable: %s", log_dir
        )
        return None
    if log_dir and not os.access(log_dir, os.W_OK):
        logging.getLogger().warning("log file handler skipped: directory not writable: %s", log_dir)
        return None

    max_bytes = _resolve_log_env_int(
        "YADGAR_LOG_FILE_MAX_BYTES",
        "YADGAR_BACKEND_LOG_FILE_MAX_BYTES",
        100_000_000,
        process,
    )
    backup_count = _resolve_log_env_int(
        "YADGAR_LOG_FILE_BACKUP_COUNT",
        "YADGAR_BACKEND_LOG_FILE_BACKUP_COUNT",
        5,
        process,
    )

    metrics_mod = _resolve_metrics_module(process)
    handler = RotatingJSONLFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        logger_name=process,
        metrics_module=metrics_mod,
    )
    root.addHandler(handler)
    return handler


def _install_rate_limiter(process: Literal["core", "backend"]) -> None:
    """Attach a shared RateLimitFilter to high-volume loggers when env-gated ON.

    Gate: YADGAR_LOG_RATE_LIMIT_ENABLED must not be "0" or "" (default ON).
    Idempotent: no-op if a RateLimitFilter is already on each target logger.
    Targets: yadgar.requests, yadgar.circuit_breaker
    """
    enabled_raw = os.environ.get("YADGAR_LOG_RATE_LIMIT_ENABLED", "1")
    if not enabled_raw or enabled_raw == "0":
        return

    rate_raw = os.environ.get("YADGAR_LOG_RATE_LIMIT_TOKENS_PER_SEC")
    if process == "backend":
        rate_raw = os.environ.get("YADGAR_BACKEND_LOG_RATE_LIMIT_TOKENS_PER_SEC", rate_raw)
    try:
        rate = float(rate_raw) if rate_raw is not None else 10.0
    except ValueError:
        rate = 10.0

    burst_raw = os.environ.get("YADGAR_LOG_RATE_LIMIT_BURST")
    if process == "backend":
        burst_raw = os.environ.get("YADGAR_BACKEND_LOG_RATE_LIMIT_BURST", burst_raw)
    try:
        burst = int(burst_raw) if burst_raw is not None else 50
    except ValueError:
        burst = 50

    metrics_mod = _resolve_metrics_module(process)
    filt = RateLimitFilter(rate=rate, burst=burst, metrics_module=metrics_mod)
    targets = ("yadgar.requests", "yadgar.circuit_breaker")
    for name in targets:
        lg = logging.getLogger(name)
        if not any(isinstance(f, RateLimitFilter) for f in lg.filters):
            lg.addFilter(filt)


def _suppress_noisy_framework_loggers() -> None:
    """Raise threshold on chatty framework namespaces to WARNING.

    These namespaces still propagate to root (JSON output intact).
    Only INFO/DEBUG chatter is suppressed to reduce noise.
    Covered: uvicorn.access (per-request lines), httpx/httpcore (outbound HTTP
    debug), asyncio (event-loop internals).
    """
    noisy = (
        "uvicorn.access",
        "httpx",
        "httpcore",
        "asyncio",
    )
    for name in noisy:
        ns_logger = logging.getLogger(name)
        if ns_logger.level == logging.NOTSET or ns_logger.level < logging.WARNING:
            ns_logger.setLevel(logging.WARNING)


def configure_logging(
    log_format: str | None = None,
    level: str = "WARNING",
    process: Literal["core", "backend"] = "core",
) -> None:
    """Configure structured logging for yadgar and all framework loggers.

    v5.4.3: root-logger approach — attaches handler to root so uvicorn, mcp,
    fastmcp, httpx, and any other framework loggers emit JSON automatically.
    The yadgar logger propagates to root (propagate=True).

    v5.5.1: dual-sink — also installs RotatingJSONLFileHandler (Sink B) on root
    when path is resolvable and dir is writable. Graceful fallback to stdout-only
    if dir missing/unwritable (I3/I7). Rate limiter installed when
    YADGAR_LOG_RATE_LIMIT_ENABLED is not explicitly "0" or "" (default ON).

    log_format: 'json' | 'text' | 'human' (default: read from YADGAR_LOG_FORMAT
                env, then fall back to 'json').
    level: logging level string (default 'WARNING').
    process: 'core' | 'backend' — selects env-var preference for Option A resolution.

    Idempotent: calling twice with the same format does not add duplicate handlers.

    Framework namespaces covered (all propagate to root by default):
        uvicorn, uvicorn.access, uvicorn.error, mcp, fastmcp, httpx, starlette
    """
    if log_format is None:
        log_format = os.environ.get("YADGAR_LOG_FORMAT", "json").lower()

    numeric_level = getattr(logging, level.upper(), logging.WARNING)
    use_json = log_format == "json"

    root = logging.getLogger()

    # Idempotency guard on root: if a StreamHandler (non-file) with the target
    # formatter type already exists, update levels and skip re-adding stdout sink.
    # Still attempt to add file handler if not yet present.
    stdout_already_configured = False
    for existing in root.handlers:
        if isinstance(existing, RotatingJSONLFileHandler):
            continue  # file handler — ignore for stdout idempotency check
        if use_json and isinstance(existing.formatter, JSONLogFormatter):
            existing.setLevel(numeric_level)
            stdout_already_configured = True
            break
        if not use_json and not isinstance(existing.formatter, JSONLogFormatter):
            existing.setLevel(numeric_level)
            stdout_already_configured = True
            break

    if stdout_already_configured:
        root.setLevel(numeric_level)
        _configure_yadgar_logger(numeric_level, propagate=True)
        # Still attempt file handler install (idempotent internally)
        for h in root.handlers:
            if isinstance(h.formatter, JSONLogFormatter) and not isinstance(
                h, RotatingJSONLFileHandler
            ):
                _configure_request_logger(h.formatter)
                break
        _install_file_handler(None, process)  # type: ignore[arg-type]
        return

    # Remove pre-installed handlers on root (stdlib default, uvicorn default)
    # before installing ours to avoid duplicate/mixed output.
    # Preserve existing RotatingJSONLFileHandler if already installed.
    existing_file_handlers = [h for h in root.handlers if isinstance(h, RotatingJSONLFileHandler)]
    root.handlers.clear()
    for fh in existing_file_handlers:
        root.addHandler(fh)
    root.setLevel(numeric_level)

    if use_json:
        formatter: logging.Formatter = JSONLogFormatter()
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.setLevel(numeric_level)
        handler.addFilter(ContentRedactor())
        root.addHandler(handler)
    else:
        formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.setLevel(numeric_level)
        root.addHandler(handler)

    # yadgar logger: propagate=True → records flow to root handler above.
    _configure_yadgar_logger(numeric_level, propagate=True)

    # v5.4.8: dedicated always-INFO handler on yadgar.requests — survives WARNING root.
    _configure_request_logger(formatter)

    # Suppress DEBUG/INFO chatter from high-volume framework namespaces.
    _suppress_noisy_framework_loggers()

    # v5.5.1: Sink B — rotating file handler (graceful fallback if unwritable)
    _install_file_handler(formatter, process)

    # v5.5.1: rate limiter — attach to high-volume loggers when env-gated ON
    _install_rate_limiter(process)
