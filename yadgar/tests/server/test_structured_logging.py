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
import os

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
        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(extra={"component": "test", "action": "run", "outcome": "ok"})
        output = fmt.format(record)
        # Must be parseable JSON
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_required_fields_present(self):
        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(extra={"component": "memorize", "action": "enqueue", "outcome": "ok"})
        parsed = json.loads(fmt.format(record))
        for field in ("ts", "level", "component", "action", "outcome"):
            assert field in parsed, f"required field {field!r} missing from JSON output"

    def test_timestamp_is_iso8601(self):
        from datetime import datetime

        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(extra={"component": "test", "action": "run", "outcome": "ok"})
        parsed = json.loads(fmt.format(record))
        ts = parsed["ts"]
        # Must parse as a valid ISO 8601 datetime with timezone
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None, "timestamp must include timezone"

    def test_level_field_is_levelname(self):
        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            level=logging.WARNING,
            extra={"component": "c", "action": "a", "outcome": "error"},
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "WARNING"

    def test_optional_latency_ms_included_when_present(self):
        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            extra={"component": "c", "action": "a", "outcome": "ok", "latency_ms": 42.5}
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["latency_ms"] == 42.5

    def test_event_field_contains_message(self):
        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            msg="drain_cycle_complete",
            extra={"component": "drainer", "action": "drain_cycle", "outcome": "ok"},
        )
        parsed = json.loads(fmt.format(record))
        assert parsed.get("event") == "drain_cycle_complete"

    def test_traceback_truncated(self):
        from yadgar._shared.observability.log_config import TRACEBACK_MAX_CHARS, JSONLogFormatter

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

    def test_truncation_keeps_the_exception_message_at_the_tail(self):
        """The DIAGNOSIS fix: an over-long traceback must not lose its last line.

        Truncation used to be ``tb_text[:TRACEBACK_MAX_CHARS]`` — head only.
        A Python traceback puts the frames first and the exception's own
        message LAST, so over the cap the one line naming the failure was the
        one line deleted. An engine-#2 migration that died on
        ``OperationalError: (1142, "CREATE command denied …")`` logged
        ``"error": "OperationalError"`` and a traceback that stopped short of
        the errno — indistinguishable from every other OperationalError.
        """
        from yadgar._shared.observability.log_config import TRACEBACK_MAX_CHARS, JSONLogFormatter

        fmt = JSONLogFormatter()
        # A deep stack of DISTINCT frames so the frames alone overflow the
        # budget, the way a SQLAlchemy/greenlet/alembic traceback does. They
        # must be distinct: Python collapses repeated identical frames into
        # "[Previous line repeated N more times]", so plain recursion stays
        # short no matter how deep it goes.
        namespace: dict = {}
        exec(  # noqa: S102 — building distinct frames is the point
            "\n".join(f"def _frame_{i}(nxt):\n    return nxt()" for i in range(80)),
            namespace,
        )

        def _boom():
            raise RuntimeError(
                '(asyncmy.errors.OperationalError) (1142, "CREATE command '
                "denied to user 'yadgar_app'@'localhost' for table "
                '`yadgar`.`task`")'
            )

        chain = _boom
        for i in range(80):
            chain = (lambda fn, nxt: lambda: fn(nxt))(namespace[f"_frame_{i}"], chain)

        try:
            chain()
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()

        record = _make_record(
            exc_info=exc_info,
            extra={"component": "c", "action": "a", "outcome": "error"},
        )
        tb = json.loads(fmt.format(record))["traceback"]
        assert len(tb) > TRACEBACK_MAX_CHARS - 100, "precondition: the traceback overflowed"
        assert "[truncated]" in tb, "precondition: it was actually truncated"
        assert "1142" in tb, "the errno must survive truncation"
        assert "CREATE command denied" in tb, "the driver's message must survive truncation"
        assert tb.startswith("Traceback"), "the head is still the head"

    def test_no_content_field_in_output(self):
        """Memory content must never appear in JSON output."""
        from yadgar._shared.observability.log_config import JSONLogFormatter

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
        from yadgar._shared.observability.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"component": "c", "action": "a", "outcome": "ok", "content": "pii"}
        )
        redactor.filter(record)
        assert not hasattr(record, "content") or getattr(record, "content", None) is None

    def test_strips_password_key(self):
        from yadgar._shared.observability.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"password": "s3cr3t", "component": "c", "action": "a", "outcome": "ok"}
        )
        redactor.filter(record)
        assert not hasattr(record, "password") or getattr(record, "password", None) is None

    def test_strips_token_key(self):
        from yadgar._shared.observability.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"token": "tok_abc", "component": "c", "action": "a", "outcome": "ok"}
        )
        redactor.filter(record)
        assert not hasattr(record, "token") or getattr(record, "token", None) is None

    def test_strips_substring_match(self):
        """api_key contains 'key' but should match 'api_key' denylist entry."""
        from yadgar._shared.observability.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(
            extra={"api_key": "ak_123", "component": "c", "action": "a", "outcome": "ok"}
        )
        redactor.filter(record)
        assert not hasattr(record, "api_key") or getattr(record, "api_key", None) is None

    def test_preserves_non_sensitive_keys(self):
        from yadgar._shared.observability.log_config import ContentRedactor

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
        from yadgar._shared.observability.log_config import ContentRedactor, JSONLogFormatter

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

        from yadgar._shared.observability.log_config import (
            JSONLogFormatter,
            RotatingJSONLFileHandler,
        )

        root = _logging.getLogger()
        # Remove both JSON stream handlers and file handlers to ensure clean state.
        # The autouse isolate_yadgar_paths fixture sets YADGAR_LOG_DIR which would
        # cause configure_logging to install a file handler; unset it for these tests.
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(h.formatter, JSONLogFormatter)
            and not isinstance(h, RotatingJSONLFileHandler)
        ]
        logger = _logging.getLogger("yadgar")
        logger.handlers.clear()
        logger.propagate = True
        # Unset log-dir env vars so configure_logging is stdout-only during these tests
        self._orig_log_dir = os.environ.pop("YADGAR_LOG_DIR", None)
        self._orig_log_file = os.environ.pop("YADGAR_LOG_FILE_PATH", None)

    def teardown_method(self):
        import logging as _logging

        from yadgar._shared.observability.log_config import (
            JSONLogFormatter,
            RotatingJSONLFileHandler,
        )

        root = _logging.getLogger()
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(h.formatter, JSONLogFormatter)
            and not isinstance(h, RotatingJSONLFileHandler)
        ]
        logger = _logging.getLogger("yadgar")
        logger.handlers.clear()
        logger.propagate = True
        # Restore log-dir env vars (monkeypatch autouse fixture owns teardown,
        # but we cleared them in setup_method so restore here to be safe)
        if self._orig_log_dir is not None:
            os.environ["YADGAR_LOG_DIR"] = self._orig_log_dir
        if self._orig_log_file is not None:
            os.environ["YADGAR_LOG_FILE_PATH"] = self._orig_log_file

    def test_json_mode_installs_json_formatter(self):
        """v5.4.3: JSONLogFormatter lives on root logger, not yadgar logger."""
        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="json")
        # Check root logger (v5.4.3 root-logger approach)
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert any(isinstance(f, JSONLogFormatter) for f in formatters)

    def test_text_mode_uses_standard_formatter(self):
        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="DEBUG", log_format="text")
        root = logging.getLogger()
        # JSONLogFormatter must NOT be present on root in text mode
        for h in root.handlers:
            assert not isinstance(h.formatter, JSONLogFormatter), (
                "text mode must not use JSONLogFormatter on root"
            )

    def test_idempotent_no_duplicate_handlers(self):
        """v5.4.3: idempotency guard is on root logger."""
        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

        configure_logging(level="INFO", log_format="json")
        configure_logging(level="INFO", log_format="json")
        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)]
        assert len(json_handlers) <= 1

    def test_redactor_installed_in_json_mode(self):
        """v5.4.3: ContentRedactor is on root handler, not yadgar logger."""
        from yadgar._shared.observability.log_config import (
            ContentRedactor,
            JSONLogFormatter,
            configure_logging,
        )

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

        from yadgar._shared.observability.log_config import (
            JSONLogFormatter,
            RotatingJSONLFileHandler,
        )

        root = logging.getLogger()
        # Remove both JSON stream and file handlers to ensure clean state.
        # The autouse isolate_yadgar_paths fixture sets YADGAR_LOG_DIR which would
        # cause configure_logging to install a file handler; unset it for these tests.
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(h.formatter, JSONLogFormatter)
            and not isinstance(h, RotatingJSONLFileHandler)
        ]
        # Reset yadgar logger
        yadgar_log = logging.getLogger("yadgar")
        yadgar_log.handlers.clear()
        yadgar_log.propagate = True
        # Unset log-dir env vars so configure_logging is stdout-only during these tests
        self._orig_log_dir = os.environ.pop("YADGAR_LOG_DIR", None)
        self._orig_log_file = os.environ.pop("YADGAR_LOG_FILE_PATH", None)

    def teardown_method(self):
        """Remove root handlers added by configure_logging to avoid polluting other tests."""
        import logging

        from yadgar._shared.observability.log_config import (
            JSONLogFormatter,
            RotatingJSONLFileHandler,
        )

        root = logging.getLogger()
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(h.formatter, JSONLogFormatter)
            and not isinstance(h, RotatingJSONLFileHandler)
        ]
        yadgar_log = logging.getLogger("yadgar")
        yadgar_log.handlers.clear()
        yadgar_log.propagate = True
        # Restore log-dir env vars
        if self._orig_log_dir is not None:
            os.environ["YADGAR_LOG_DIR"] = self._orig_log_dir
        if self._orig_log_file is not None:
            os.environ["YADGAR_LOG_FILE_PATH"] = self._orig_log_file

    def test_root_handler_installed_in_json_mode(self):
        """configure_logging(json) must attach JSONLogFormatter handler to root logger."""
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

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

        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

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

        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

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

        from yadgar._shared.observability.log_config import configure_logging

        configure_logging(log_format="json", level="DEBUG")
        yadgar_log = logging.getLogger("yadgar")
        assert yadgar_log.propagate is True, (
            "yadgar logger must propagate=True after v5.4.3 root-logger approach"
        )

    def test_human_format_no_json_on_root(self):
        """YADGAR_LOG_FORMAT=human must not install JSONLogFormatter on root."""
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="human", level="DEBUG")
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert not any(isinstance(f, JSONLogFormatter) for f in formatters), (
            "human/text mode must not install JSONLogFormatter on root logger"
        )

    def test_text_format_no_json_on_root(self):
        """YADGAR_LOG_FORMAT=text must not install JSONLogFormatter on root."""
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="text", level="DEBUG")
        root = logging.getLogger()
        formatters = [h.formatter for h in root.handlers]
        assert not any(isinstance(f, JSONLogFormatter) for f in formatters), (
            "text mode must not install JSONLogFormatter on root logger"
        )

    def test_idempotent_root_handler_no_duplicates(self):
        """Calling configure_logging twice must not add duplicate root handlers."""
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter, configure_logging

        configure_logging(log_format="json", level="INFO")
        configure_logging(log_format="json", level="INFO")
        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)]
        assert len(json_handlers) <= 1, "root must not accumulate duplicate JSON handlers"

    def test_root_redactor_installed(self):
        """Root handler must have ContentRedactor filter attached."""
        import logging

        from yadgar._shared.observability.log_config import (
            ContentRedactor,
            JSONLogFormatter,
            configure_logging,
        )

        configure_logging(log_format="json", level="DEBUG")
        root = logging.getLogger()
        json_handler = next(
            (h for h in root.handlers if isinstance(h.formatter, JSONLogFormatter)), None
        )
        assert json_handler is not None
        assert any(isinstance(f, ContentRedactor) for f in json_handler.filters), (
            "root JSON handler must have ContentRedactor filter"
        )


# ---------------------------------------------------------------------------
# v5.4.7 — ContentRedactor denylist tighten (exact vs substring)
# ---------------------------------------------------------------------------


class TestContentRedactorDenylistV547:
    """Regression tests for false-positive header redaction (v5.4.7 fix).

    content_type / content_length must NOT be redacted.
    Exact matches (content, auth, token, secret, bearer) still redacted.
    Substring matches on compound secrets (password, api_key, etc.) still redacted.
    auth_token_provider: NOT redacted per new two-tier policy (no substring or exact match).
    """

    def _redact(self, extra: dict) -> logging.LogRecord:
        from yadgar._shared.observability.log_config import ContentRedactor

        redactor = ContentRedactor()
        record = _make_record(extra=extra)
        redactor.filter(record)
        return record

    # --- false-positive regression ---

    def test_content_type_not_redacted(self):
        """content_type must NOT be redacted — false positive from old substring match."""
        record = self._redact({"content_type": "application/json"})
        assert getattr(record, "content_type", "MISSING") == "application/json", (
            "content_type must not be redacted"
        )

    def test_content_length_not_redacted(self):
        """content_length must NOT be redacted — false positive from old substring match."""
        record = self._redact({"content_length": 1234})
        assert getattr(record, "content_length", "MISSING") == 1234, (
            "content_length must not be redacted"
        )

    # --- exact-match still redacted ---

    def test_content_exact_still_redacted(self):
        """Bare 'content' field must still be redacted (exact match)."""
        record = self._redact({"content": "pii data"})
        assert getattr(record, "content", None) is None, "content must be redacted"

    def test_auth_exact_still_redacted(self):
        """Bare 'auth' field must still be redacted (exact match)."""
        record = self._redact({"auth": "bearer tok"})
        assert getattr(record, "auth", None) is None, "auth must be redacted"

    def test_token_exact_still_redacted(self):
        """Bare 'token' field must still be redacted (exact match)."""
        record = self._redact({"token": "tok_123"})
        assert getattr(record, "token", None) is None, "token must be redacted"

    def test_secret_exact_still_redacted(self):
        """Bare 'secret' field must still be redacted (exact match)."""
        record = self._redact({"secret": "s3cr3t"})
        assert getattr(record, "secret", None) is None, "secret must be redacted"

    def test_bearer_exact_still_redacted(self):
        """Bare 'bearer' field must still be redacted (exact match)."""
        record = self._redact({"bearer": "tok_abc"})
        assert getattr(record, "bearer", None) is None, "bearer must be redacted"

    # --- substring denylist still redacted ---

    def test_authorization_header_still_redacted(self):
        """authorization (case-insensitive) must still be redacted (substring denylist)."""
        record = self._redact({"authorization": "Bearer tok"})
        assert getattr(record, "authorization", None) is None, "authorization must be redacted"

    def test_authorization_header_still_redacted_capitalized(self):
        """Authorization (capital A) must still be redacted (case-insensitive)."""
        record = self._redact({"Authorization": "Bearer tok"})
        assert getattr(record, "Authorization", None) is None, "Authorization must be redacted"

    def test_authorization_header_compound_still_redacted(self):
        """authorization_header must still be redacted (contains 'authorization' substring)."""
        record = self._redact({"authorization_header": "Bearer tok"})
        assert getattr(record, "authorization_header", None) is None, (
            "authorization_header must be redacted"
        )

    def test_password_still_redacted(self):
        """password must still be redacted (substring denylist)."""
        record = self._redact({"password": "hunter2"})
        assert getattr(record, "password", None) is None, "password must be redacted"

    def test_api_key_still_redacted(self):
        """api_key must still be redacted (substring denylist)."""
        record = self._redact({"api_key": "ak_123"})
        assert getattr(record, "api_key", None) is None, "api_key must be redacted"

    def test_api_key_foo_still_redacted(self):
        """api_key_foo must still be redacted (contains 'api_key' substring)."""
        record = self._redact({"api_key_foo": "ak_456"})
        assert getattr(record, "api_key_foo", None) is None, "api_key_foo must be redacted"

    def test_client_secret_still_redacted(self):
        """client_secret must still be redacted (contains 'client_secret' substring)."""
        record = self._redact({"client_secret": "cs_abc"})
        assert getattr(record, "client_secret", None) is None, "client_secret must be redacted"

    def test_access_token_still_redacted(self):
        """access_token must still be redacted (contains 'access_token' substring)."""
        record = self._redact({"access_token": "at_xyz"})
        assert getattr(record, "access_token", None) is None, "access_token must be redacted"

    def test_refresh_token_still_redacted(self):
        """refresh_token must still be redacted (contains 'refresh_token' substring)."""
        record = self._redact({"refresh_token": "rt_xyz"})
        assert getattr(record, "refresh_token", None) is None, "refresh_token must be redacted"

    def test_private_key_still_redacted(self):
        """private_key must still be redacted (contains 'private_key' substring)."""
        record = self._redact({"private_key": "-----BEGIN RSA PRIVATE KEY-----"})
        assert getattr(record, "private_key", None) is None, "private_key must be redacted"

    def test_auth_token_provider_not_redacted(self):
        """auth_token_provider is NOT redacted per two-tier policy.

        - Not in _EXACT_DENYLIST (not 'auth', 'token', etc. exactly)
        - Not matched by _SUBSTRING_DENYLIST ('auth'/'token' are exact-only; compound
          denylist has 'access_token'/'refresh_token' but not bare 'token' as substring)
        This is intentional: tightened policy accepts false-negatives on ambiguous
        compound names vs false-positives on header fields.
        """
        record = self._redact({"auth_token_provider": "oauth2"})
        assert getattr(record, "auth_token_provider", "MISSING") == "oauth2", (
            "auth_token_provider must NOT be redacted under two-tier policy"
        )

    def test_formatter_last_resort_guard_respects_new_denylist(self):
        """JSONLogFormatter._append_extras last-resort guard uses same _is_sensitive fn.

        content_type must pass through the formatter (not filtered out by guard).
        """
        from yadgar._shared.observability.log_config import JSONLogFormatter

        fmt = JSONLogFormatter()
        record = _make_record(
            extra={
                "component": "http_server",
                "action": "request",
                "outcome": "ok",
                "content_type": "application/json",
                "content_length": 512,
            }
        )
        parsed = json.loads(fmt.format(record))
        assert parsed.get("content_type") == "application/json", (
            "content_type must appear in JSON output"
        )
        assert parsed.get("content_length") == 512, "content_length must appear in JSON output"


# ---------------------------------------------------------------------------
# v5.4.7 — RequestLoggingMiddleware I14 schema + _outcome_from_status
# ---------------------------------------------------------------------------


class TestOutcomeFromStatus:
    """Unit tests for _outcome_from_status helper."""

    def test_200_ok(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("200") == "ok"

    def test_201_ok(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("201") == "ok"

    def test_301_ok(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("301") == "ok"

    def test_399_ok(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("399") == "ok"

    def test_400_error(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("400") == "error"

    def test_404_error(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("404") == "error"

    def test_422_error(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("422") == "error"

    def test_500_error(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("500") == "error"

    def test_503_error(self):
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("503") == "error"

    def test_zero_error(self):
        """Status '0' = no status — connection died before headers."""
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("0") == "error"

    def test_cancelled_degraded(self):
        """Status 'cancelled' = ASGI scope cancelled mid-flight → degraded."""
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("cancelled") == "degraded"

    def test_unknown_fallback_error(self):
        """Unknown status strings → error (safe default)."""
        from yadgar._shared.observability.log_config import _outcome_from_status

        assert _outcome_from_status("999") == "error"


class TestRequestLoggingMiddlewareI14:
    """RequestLoggingMiddleware must emit I14 schema fields."""

    def setup_method(self):
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter

        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]

    def teardown_method(self):
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter

        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]

    def _run_middleware(self, status_code: int = 200) -> dict:
        """Run middleware against a minimal ASGI scope; capture structured log output."""
        import asyncio
        import io
        import logging

        from yadgar._shared.observability.log_config import (
            ContentRedactor,
            JSONLogFormatter,
            RequestLoggingMiddleware,
        )

        # Wire a capture handler on the yadgar.requests logger
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JSONLogFormatter())
        handler.addFilter(ContentRedactor())
        req_logger = logging.getLogger("yadgar.requests")
        req_logger.addHandler(handler)
        req_logger.setLevel(logging.DEBUG)
        req_logger.propagate = False

        async def fake_app(scope, receive, send):
            await send({"type": "http.response.start", "status": status_code, "headers": []})

        async def run():
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "headers": [],
            }

            async def receive():
                return {}

            async def send(msg):
                pass

            mw = RequestLoggingMiddleware(fake_app)
            await mw(scope, receive, send)

        asyncio.run(run())
        req_logger.removeHandler(handler)
        req_logger.propagate = True

        output = stream.getvalue().strip()
        assert output, "RequestLoggingMiddleware must emit at least one log line"
        return json.loads(output.splitlines()[-1])

    def test_emits_component_field(self):
        parsed = self._run_middleware(200)
        assert parsed.get("component") == "http_server", "component must be 'http_server'"

    def test_emits_action_field(self):
        parsed = self._run_middleware(200)
        assert parsed.get("action") == "request", "action must be 'request'"

    def test_emits_outcome_ok_for_200(self):
        parsed = self._run_middleware(200)
        assert parsed.get("outcome") == "ok", "outcome must be 'ok' for 200"

    def test_emits_latency_ms_not_duration_ms(self):
        """latency_ms replaces duration_ms in I14 schema."""
        parsed = self._run_middleware(200)
        assert "latency_ms" in parsed, "latency_ms must be present"
        assert "duration_ms" not in parsed, "duration_ms must NOT appear (renamed to latency_ms)"

    def test_emits_http_status_not_bare_status(self):
        """http_status replaces bare 'status' field."""
        parsed = self._run_middleware(200)
        assert "http_status" in parsed, "http_status must be present"

    def test_emits_request_id(self):
        parsed = self._run_middleware(200)
        assert "request_id" in parsed, "request_id must be present"

    def test_emits_tool_name(self):
        parsed = self._run_middleware(200)
        assert "tool_name" in parsed, "tool_name must be present"

    def test_outcome_404_is_error(self):
        parsed = self._run_middleware(404)
        assert parsed.get("outcome") == "error", "outcome must be 'error' for 404"

    def test_outcome_500_is_error(self):
        parsed = self._run_middleware(500)
        assert parsed.get("outcome") == "error", "outcome must be 'error' for 500"


# ---------------------------------------------------------------------------
# v5.4.8 — RequestLoggingMiddleware visibility at WARNING root level
# ---------------------------------------------------------------------------


class TestRequestLogVisibilityAtWarningLevel:
    """Regression: yadgar.requests INFO must flow even when root level=WARNING.

    Root cause (v5.4.8): CORE_LOG_LEVEL defaults to 'warn' → configure_logging
    sets root+handler+yadgar logger all to WARNING → yadgar.requests inherits
    WARNING → INFO records silently dropped. Fix: dedicated always-INFO handler
    on yadgar.requests with propagate=False.
    """

    def setup_method(self):
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter

        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]
        req_logger = logging.getLogger("yadgar.requests")
        req_logger.handlers.clear()
        req_logger.propagate = True
        req_logger.setLevel(logging.NOTSET)

    def teardown_method(self):
        import logging

        from yadgar._shared.observability.log_config import JSONLogFormatter

        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not isinstance(h.formatter, JSONLogFormatter)]
        req_logger = logging.getLogger("yadgar.requests")
        req_logger.handlers.clear()
        req_logger.propagate = True
        req_logger.setLevel(logging.NOTSET)

    def test_request_info_reaches_handler_at_warning_root_level(self):
        """yadgar.requests INFO must be visible even when configure_logging level=WARNING.

        This is the production-default scenario: CORE_LOG_LEVEL=warn → level='WARNING'.
        Pre-fix: INFO dropped. Post-fix: dedicated handler on yadgar.requests passes it.
        """
        import io
        import logging

        from yadgar._shared.observability.log_config import configure_logging

        configure_logging(log_format="json", level="WARNING")

        req_logger = logging.getLogger("yadgar.requests")
        # Must have own handler (not just propagate to root at WARNING)
        assert req_logger.handlers, (
            "yadgar.requests must have dedicated handler after configure_logging(level='WARNING')"
        )

        # Capture output from the dedicated handler
        stream = io.StringIO()
        req_handler = req_logger.handlers[0]
        old_stream = req_handler.stream
        req_handler.stream = stream

        try:
            req_logger.info(
                "request",
                extra={
                    "component": "http_server",
                    "action": "request",
                    "outcome": "ok",
                    "latency_ms": 5,
                    "http_status": "200",
                    "request_id": "test-id",
                    "tool_name": "GET /health",
                    "trace_id": "",
                },
            )
            output = stream.getvalue().strip()
        finally:
            req_handler.stream = old_stream

        assert output, (
            "yadgar.requests INFO must emit log line even when root level=WARNING (v5.4.8 fix)"
        )
        parsed = json.loads(output)
        assert parsed.get("event") == "request"
        assert parsed.get("component") == "http_server"

    def test_suppression_list_does_not_include_yadgar_requests(self):
        """_suppress_noisy_framework_loggers must not raise threshold on yadgar.requests."""
        import logging

        from yadgar._shared.observability.log_config import _suppress_noisy_framework_loggers

        req_logger = logging.getLogger("yadgar.requests")
        req_logger.setLevel(logging.INFO)
        _suppress_noisy_framework_loggers()
        # Level must remain INFO — suppression list must not touch yadgar.requests
        assert req_logger.level == logging.INFO, (
            "_suppress_noisy_framework_loggers must not raise yadgar.requests above INFO"
        )

    def test_yadgar_requests_handler_not_duplicated_on_reconfigure(self):
        """Calling configure_logging twice must not stack handlers on yadgar.requests."""
        import logging

        from yadgar._shared.observability.log_config import configure_logging

        configure_logging(log_format="json", level="WARNING")
        configure_logging(log_format="json", level="WARNING")
        req_logger = logging.getLogger("yadgar.requests")
        assert len(req_logger.handlers) <= 1, (
            "yadgar.requests must not accumulate duplicate handlers on reconfigure"
        )
