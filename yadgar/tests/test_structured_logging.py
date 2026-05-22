"""Tests for I14 structured JSON logging contract.

All tests validate:
- JSONLogFormatter emits valid JSON with required I14 fields
- ContentRedactor strips sensitive keys from LogRecord extra
- configure_logging wires formatter + redactor correctly
- LOG_FORMAT=text falls back to standard formatting
- Traceback sanitization/truncation
"""

from __future__ import annotations

import json
import logging

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    msg: str = "test_event",
    level: int = logging.INFO,
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    """Create a LogRecord with optional extra fields and exc_info."""
    record = logging.LogRecord(
        name="yadgar.test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


# ---------------------------------------------------------------------------
# JSONLogFormatter
# ---------------------------------------------------------------------------


class TestJSONLogFormatter:
    def test_emits_valid_json(self):
        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(extra={"component": "test", "action": "run", "outcome": "ok"})
        output = fmt.format(record)
        # Must be parseable JSON
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_required_fields_present(self):
        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(extra={"component": "memorize", "action": "enqueue", "outcome": "ok"})
        parsed = json.loads(fmt.format(record))
        for field in ("ts", "level", "component", "action", "outcome"):
            assert field in parsed, f"required field {field!r} missing from JSON output"

    def test_timestamp_is_iso8601(self):
        from datetime import datetime

        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(extra={"component": "test", "action": "run", "outcome": "ok"})
        parsed = json.loads(fmt.format(record))
        ts = parsed["ts"]
        # Must parse as a valid ISO 8601 datetime with timezone
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None, "timestamp must include timezone"

    def test_level_field_is_levelname(self):
        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            level=logging.WARNING,
            extra={"component": "c", "action": "a", "outcome": "error"},
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "WARNING"

    def test_optional_latency_ms_included_when_present(self):
        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            extra={"component": "c", "action": "a", "outcome": "ok", "latency_ms": 42.5}
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["latency_ms"] == 42.5

    def test_event_field_contains_message(self):
        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            msg="drain_cycle_complete",
            extra={"component": "drainer", "action": "drain_cycle", "outcome": "ok"},
        )
        parsed = json.loads(fmt.format(record))
        assert parsed.get("event") == "drain_cycle_complete"

    def test_traceback_truncated(self):
        from yadgar.log_config import TRACEBACK_MAX_CHARS, JSONLogFormatter

        fmt = JSONLogFormatter()
        try:
            raise RuntimeError("boom " * 200)
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()

        record = _make_record(
            exc_info=exc_info,
            extra={"component": "c", "action": "a", "outcome": "error"},
        )
        parsed = json.loads(fmt.format(record))
        tb = parsed.get("traceback", "")
        assert len(tb) <= TRACEBACK_MAX_CHARS + 50  # +50 for truncation marker
        assert "RuntimeError" in tb  # type must survive

    def test_no_content_field_in_output(self):
        """Memory content must never appear in JSON output."""
        from yadgar.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            extra={
                "component": "memorize",
                "action": "store",
                "outcome": "ok",
                "content": "secret user memory",
            }
        )
        parsed = json.loads(fmt.format(record))
        assert "content" not in parsed, "content field must be redacted"


# ---------------------------------------------------------------------------
# ContentRedactor
# ---------------------------------------------------------------------------


class TestContentRedactor:
    def test_strips_content_key(self):
        from yadgar.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"component": "c", "action": "a", "outcome": "ok", "content": "pii"}
        )
        redactor.filter(record)
        assert not hasattr(record, "content") or getattr(record, "content", None) is None

    def test_strips_password_key(self):
        from yadgar.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"password": "s3cr3t", "component": "c", "action": "a", "outcome": "ok"}
        )
        redactor.filter(record)
        assert not hasattr(record, "password") or getattr(record, "password", None) is None

    def test_strips_token_key(self):
        from yadgar.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"token": "tok_abc", "component": "c", "action": "a", "outcome": "ok"}
        )
        redactor.filter(record)
        assert not hasattr(record, "token") or getattr(record, "token", None) is None

    def test_strips_substring_match(self):
        """api_key contains 'key' but should match 'api_key' denylist entry."""
        from yadgar.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"api_key": "ak_123", "component": "c", "action": "a", "outcome": "ok"}
        )
        redactor.filter(record)
        assert not hasattr(record, "api_key") or getattr(record, "api_key", None) is None

    def test_preserves_non_sensitive_keys(self):
        from yadgar.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"component": "memorize", "action": "enqueue", "outcome": "ok", "latency_ms": 5.0}
        )
        redactor.filter(record)
        assert getattr(record, "component", None) == "memorize"
        assert getattr(record, "outcome", None) == "ok"
        assert getattr(record, "latency_ms", None) == 5.0

    def test_memory_dict_content_key_removed(self):
        """Memory dict passed as extra value: content key inside must be dropped."""
        from yadgar.log_config import ContentRedactor, JSONLogFormatter

        redactor = ContentRedactor()
        fmt = JSONLogFormatter()

        memory_obj = {"id": 1, "tags": ["work"], "content": "private user text"}
        record = _make_record(
            extra={
                "component": "recall",
                "action": "fetch",
                "outcome": "ok",
                "memory": memory_obj,
            }
        )
        redactor.filter(record)
        parsed = json.loads(fmt.format(record))

        mem = parsed.get("memory", {})
        assert mem.get("id") == 1, "memory.id must be preserved"
        assert "content" not in mem, "memory.content must be redacted"


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def setup_method(self):
        # Reset root and yadgar loggers before each test (v5.4.3: handler lives on root)
        import logging as _logging

        from yadgar.log_config import JSONLogFormatter

        root = _logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]
        logger = _logging.getLogger("yadgar")
        logger.handlers.clear()
        logger.propagate = True

    def teardown_method(self):
        import logging as _logging

        from yadgar.log_config import JSONLogFormatter

        root = _logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]
        logger = _logging.getLogger("yadgar")
        logger.handlers.clear()
        logger.propagate = True

    def test_json_mode_installs_json_formatter(self):
        """v5.4.3: JSONLogFormatter lives on root logger, not yadgar logger."""
        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="json")
        # Check root logger (v5.4.3 root-logger approach)
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert any(isinstance(f, JSONLogFormatter) for f in formatters)

    def test_text_mode_uses_standard_formatter(self):
        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="text")
        root = logging.getLogger()
        # JSONLogFormatter must NOT be present on root in text mode
        for h in root.handlers:
            assert not isinstance(h.formatter, JSONLogFormatter), (
                "text mode must not use JSONLogFormatter on root"
            )

    def test_idempotent_no_duplicate_handlers(self):
        """v5.4.3: idempotency guard is on root logger."""
        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="INFO", log_format="json")
        configure_logging(level="INFO", log_format="json")
        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)]
        assert len(json_handlers) <= 1

    def test_redactor_installed_in_json_mode(self):
        """v5.4.3: ContentRedactor is on root handler, not yadgar logger."""
        from yadgar.log_config import ContentRedactor, JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="json")
        root = logging.getLogger()
        json_handler = next(
            (h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)), None
        )
        assert json_handler is not None
        assert any(isinstance(f, ContentRedactor) for f in json_handler.filters)


# ---------------------------------------------------------------------------
# Framework logger coverage (v5.4.3) — I14 extended to root logger
# ---------------------------------------------------------------------------


class TestFrameworkLoggerCoverage:
    """Root-logger approach: configure_logging() must cover all framework namespaces."""

    def setup_method(self):
        """Reset root and yadgar loggers; remove handlers added by previous tests."""
        import logging

        root = logging.getLogger()
        # Remove any JSONLogFormatter handlers from root installed by prior test
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(
                h.formatter,
                __import__("yadgar.log_config", fromlist=["JSONLogFormatter"]).JSONLogFormatter,
            )
        ]
        # Reset yadgar logger
        yadgar_log = logging.getLogger("yadgar")
        yadgar_log.handlers.clear()
        yadgar_log.propagate = True

    def teardown_method(self):
        """Remove root handlers added by configure_logging to avoid polluting other tests."""
        import logging

        from yadgar.log_config import JSONLogFormatter

        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]
        yadgar_log = logging.getLogger("yadgar")
        yadgar_log.handlers.clear()
        yadgar_log.propagate = True

    def test_root_handler_installed_in_json_mode(self):
        """configure_logging(json) must attach JSONLogFormatter handler to root logger."""
        import logging

        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="json", level="DEBUG")
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert any(isinstance(f, JSONLogFormatter) for f in formatters), (
            "root logger must have JSONLogFormatter handler after configure_logging(json)"
        )

    def test_uvicorn_access_emits_json(self):
        """uvicorn.access child records must reach root JSON handler → produce valid JSON."""
        import io
        import logging

        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="json", level="DEBUG")

        # Capture what the root handler emits
        stream = io.StringIO()
        root = logging.getLogger()
        json_handler = next(h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter))
        old_stream = json_handler.stream
        json_handler.stream = stream

        try:
            uv_logger = logging.getLogger("uvicorn.access")
            uv_logger.propagate = True  # default — should already be True
            uv_logger.handlers = []  # no own handlers — propagate to root
            uv_logger.warning("GET /health HTTP/1.1 200")
            output = stream.getvalue().strip()
        finally:
            json_handler.stream = old_stream

        assert output, "uvicorn.access log must reach root handler"
        parsed = json.loads(output.splitlines()[-1])
        assert parsed.get("level") == "WARNING"
        assert "GET /health" in parsed.get("event", "")

    def test_fastmcp_logger_emits_json(self):
        """fastmcp.* child records must reach root JSON handler."""
        import io
        import logging

        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="json", level="DEBUG")

        stream = io.StringIO()
        root = logging.getLogger()
        json_handler = next(h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter))
        old_stream = json_handler.stream
        json_handler.stream = stream

        try:
            fm_logger = logging.getLogger("fastmcp")
            fm_logger.propagate = True
            fm_logger.handlers = []
            fm_logger.warning("Processing request of type ListResourcesRequest")
            output = stream.getvalue().strip()
        finally:
            json_handler.stream = old_stream

        assert output, "fastmcp log must reach root handler"
        parsed = json.loads(output.splitlines()[-1])
        assert "Processing request" in parsed.get("event", "")

    def test_yadgar_logger_propagates_to_root(self):
        """After configure_logging(json), yadgar logger must propagate=True to root."""
        import logging

        from yadgar.log_config import configure_logging

        configure_logging(log_format="json", level="DEBUG")
        yadgar_log = logging.getLogger("yadgar")
        assert yadgar_log.propagate is True, (
            "yadgar logger must propagate=True after v5.4.3 root-logger approach"
        )

    def test_human_format_no_json_on_root(self):
        """YADGAR_LOG_FORMAT=human must not install JSONLogFormatter on root."""
        import logging

        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="human", level="DEBUG")
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert not any(isinstance(f, JSONLogFormatter) for f in formatters), (
            "human/text mode must not install JSONLogFormatter on root logger"
        )

    def test_text_format_no_json_on_root(self):
        """YADGAR_LOG_FORMAT=text must not install JSONLogFormatter on root."""
        import logging

        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="text", level="DEBUG")
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert not any(isinstance(f, JSONLogFormatter) for f in formatters), (
            "text mode must not install JSONLogFormatter on root logger"
        )

    def test_idempotent_root_handler_no_duplicates(self):
        """Calling configure_logging twice must not add duplicate root handlers."""
        import logging

        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="json", level="INFO")
        configure_logging(log_format="json", level="INFO")
        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)]
        assert len(json_handlers) <= 1, "root must not accumulate duplicate JSON handlers"

    def test_root_redactor_installed(self):
        """Root handler must have ContentRedactor filter attached."""
        import logging

        from yadgar.log_config import ContentRedactor, JSONLogFormatter, configure_logging

        configure_logging(log_format="json", level="DEBUG")
        root = logging.getLogger()
        json_handler = next(
            (h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)), None
        )
        assert json_handler is not None
        assert any(isinstance(f, ContentRedactor) for f in json_handler.filters), (
            "root JSON handler must have ContentRedactor filter"
        )
