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
        # Reset root yadgar logger before each test
        logger = logging.getLogger("yadgar")
        logger.handlers.clear()
        logger.propagate = True

    def test_json_mode_installs_json_formatter(self):
        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="json")
        logger = logging.getLogger("yadgar")
        formatters = [h.formatter for h in logger.handlers]
        assert any(isinstance(f, JSONLogFormatter) for f in formatters)

    def test_text_mode_uses_standard_formatter(self):
        from yadgar.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="text")
        logger = logging.getLogger("yadgar")
        # JSONLogFormatter must NOT be used in text mode
        for h in logger.handlers:
            assert not isinstance(h.formatter, JSONLogFormatter), (
                "text mode must not use JSONLogFormatter"
            )

    def test_idempotent_no_duplicate_handlers(self):
        from yadgar.log_config import configure_logging

        configure_logging(level="INFO", log_format="json")
        configure_logging(level="INFO", log_format="json")
        logger = logging.getLogger("yadgar")
        # Should not accumulate duplicate handlers on second call
        assert len(logger.handlers) <= 1

    def test_redactor_installed_in_json_mode(self):
        from yadgar.log_config import ContentRedactor, configure_logging

        configure_logging(level="DEBUG", log_format="json")
        logger = logging.getLogger("yadgar")
        # Redactor may be on handler or logger filters
        all_filters = list(logger.filters)
        for h in logger.handlers:
            all_filters.extend(h.filters)
        assert any(isinstance(f, ContentRedactor) for f in all_filters)
