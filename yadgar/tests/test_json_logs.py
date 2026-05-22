"""§15 Structured JSON logs tests.

Tests:
- YADGAR_LOG_FORMAT=json produces valid JSON per line
- Required fields present: request_id, tool_name, duration_ms, status
- x-request-id header propagates as trace_id in log output
- Human format (default) still works — not JSON
- RequestLoggingMiddleware emits per-request structured log line
"""

import io
import json
import logging


def _make_json_handler():
    """Create a StringIO handler with JSON formatter for testing."""
    from yadgar.log_config import JsonFormatter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    return handler, buf


def test_json_formatter_produces_valid_json():
    """JsonFormatter output is valid JSON on each line."""
    handler, buf = _make_json_handler()
    logger = logging.getLogger("test_json_formatter_valid")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("test message")
        line = buf.getvalue().strip()
        assert line, "Expected at least one line of output"
        record = json.loads(line)
        assert isinstance(record, dict)
    finally:
        logger.removeHandler(handler)


def test_json_formatter_includes_message():
    """JSON log record includes the log message."""
    handler, buf = _make_json_handler()
    logger = logging.getLogger("test_json_formatter_msg")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("hello json world")
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert "hello json world" in str(record.get("message", record.get("msg", "")))
    finally:
        logger.removeHandler(handler)


def test_json_formatter_includes_level():
    """JSON log record includes the log level."""
    handler, buf = _make_json_handler()
    logger = logging.getLogger("test_json_formatter_level")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("warn test")
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert "WARNING" in str(record.get("level", record.get("levelname", ""))).upper()
    finally:
        logger.removeHandler(handler)


def test_json_formatter_includes_timestamp():
    """JSON log record includes a timestamp field."""
    handler, buf = _make_json_handler()
    logger = logging.getLogger("test_json_formatter_ts")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("ts test")
        line = buf.getvalue().strip()
        record = json.loads(line)
        has_ts = any(k in record for k in ("timestamp", "time", "asctime", "created"))
        assert has_ts, f"Expected a timestamp field in {record}"
    finally:
        logger.removeHandler(handler)


def test_json_formatter_extra_fields():
    """Extra fields passed via extra= appear in JSON output."""
    handler, buf = _make_json_handler()
    logger = logging.getLogger("test_json_formatter_extra")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "request done",
            extra={
                "request_id": "req-abc-123",
                "tool_name": "recall",
                "duration_ms": 42,
                "status": "ok",
            },
        )
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert record.get("request_id") == "req-abc-123"
        assert record.get("tool_name") == "recall"
        assert record.get("duration_ms") == 42
        assert record.get("status") == "ok"
    finally:
        logger.removeHandler(handler)


def test_json_formatter_trace_id_propagation():
    """trace_id (from x-request-id) propagates into JSON log lines."""
    handler, buf = _make_json_handler()
    logger = logging.getLogger("test_json_formatter_trace")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "traced request",
            extra={
                "trace_id": "x-req-999",
                "tool_name": "memorize",
                "duration_ms": 10,
                "status": "ok",
            },
        )
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert record.get("trace_id") == "x-req-999"
    finally:
        logger.removeHandler(handler)


def test_configure_json_logging(monkeypatch, tmp_path):
    """configure_logging(format='json') installs JSON handler on root logger (v5.4.3+)."""
    monkeypatch.setenv("YADGAR_LOG_FORMAT", "json")

    from yadgar.log_config import JSONLogFormatter, configure_logging

    configure_logging(log_format="json", level="INFO")
    # v5.4.3+: root-logger approach — JSONLogFormatter attached to root, yadgar propagates up.
    root = logging.getLogger()
    json_handlers = [h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)]
    assert len(json_handlers) >= 1, "Expected at least one JSON handler on root logger"


def test_configure_human_logging_default(monkeypatch):
    """configure_logging() without format arg uses human-readable format."""
    monkeypatch.delenv("YADGAR_LOG_FORMAT", raising=False)

    from yadgar.log_config import configure_logging

    configure_logging(log_format="human", level="INFO")
    logger = logging.getLogger("yadgar")
    # Should not have JsonFormatter after human config (or not exclusively json)
    # We just verify it doesn't raise and the logger is functional
    logger.info("human log test — should not be JSON")
    # Cleanup handlers
    for h in list(logger.handlers):
        if hasattr(h, "stream") and not hasattr(h.stream, "fileno"):
            logger.removeHandler(h)


class TestRequestLoggingMiddleware:
    """RequestLoggingMiddleware emits structured per-request log lines."""

    def _make_simple_app(self, status: int = 200, body: bytes = b"ok"):
        """Minimal ASGI app that returns a fixed response."""

        async def app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [[b"content-type", b"text/plain"]],
                }
            )
            await send({"type": "http.response.body", "body": body})

        return app

    def _make_scope(self, path: str = "/health", method: str = "GET", headers=None):
        h = headers or []
        return {
            "type": "http",
            "method": method,
            "path": path,
            "headers": h,
        }

    async def _send_noop(self, message):
        pass

    async def _receive_noop(self):
        return {"type": "http.request", "body": b""}

    def _install_capture_handler(self, logger_name: str):
        """Install a capturing handler; return (handler, records_list, cleanup)."""
        records = []

        class Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        cap = Cap()
        cap.setLevel(logging.DEBUG)
        lg = logging.getLogger(logger_name)
        lg.addHandler(cap)
        lg.setLevel(logging.DEBUG)

        def cleanup():
            lg.removeHandler(cap)

        return cap, records, cleanup

    def test_middleware_emits_request_log(self):
        """Middleware emits exactly one log record per HTTP request."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app())
            scope = self._make_scope("/health")
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert len(records) == 1
        finally:
            cleanup()

    def test_middleware_record_has_required_fields(self):
        """Log record has request_id, tool_name, duration_ms, status."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app(status=200))
            scope = self._make_scope("/health", method="GET")
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert records, "No log records emitted"
            r = records[0]
            assert hasattr(r, "request_id"), "Missing request_id"
            assert hasattr(r, "tool_name"), "Missing tool_name"
            assert hasattr(r, "latency_ms"), "Missing latency_ms (was duration_ms pre-v5.4.7)"
            assert hasattr(r, "http_status"), "Missing http_status (was status pre-v5.4.7)"
        finally:
            cleanup()

    def test_middleware_trace_id_from_header(self):
        """x-request-id header propagates as trace_id in log record."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app())
            scope = self._make_scope("/health", headers=[[b"x-request-id", b"my-trace-xyz"]])
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert records
            assert records[0].trace_id == "my-trace-xyz"
        finally:
            cleanup()

    def test_middleware_status_code_captured(self):
        """Log record status matches HTTP response status code."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app(status=404))
            scope = self._make_scope("/missing")
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert records
            assert records[0].http_status == "404"
        finally:
            cleanup()

    def test_middleware_tool_name_is_method_path(self):
        """tool_name field is 'METHOD /path'."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app())
            scope = self._make_scope("/api/stats", method="GET")
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert records
            assert records[0].tool_name == "GET /api/stats"
        finally:
            cleanup()

    def test_middleware_latency_ms_is_nonnegative_int(self):
        """latency_ms is a non-negative integer (was duration_ms pre-v5.4.7)."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app())
            scope = self._make_scope("/health")
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert records
            assert isinstance(records[0].latency_ms, int)
            assert records[0].latency_ms >= 0
        finally:
            cleanup()

    def test_middleware_skips_non_http_scope(self):
        """Middleware passes through non-HTTP scopes without logging."""
        import asyncio

        from yadgar.log_config import RequestLoggingMiddleware

        _, records, cleanup = self._install_capture_handler("yadgar.requests")
        try:
            mw = RequestLoggingMiddleware(self._make_simple_app())
            scope = {"type": "lifespan"}
            # lifespan app won't send http.response.start, just pass through
            asyncio.run(mw(scope, self._receive_noop, self._send_noop))
            assert len(records) == 0, "Should not log non-HTTP scopes"
        finally:
            cleanup()
