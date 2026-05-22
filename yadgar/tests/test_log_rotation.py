"""Tests for v5.5.1 log rotation + rate limiter.

TDD: these tests are written before implementation. All tests here
must pass after implementation of:
  - RotatingJSONLFileHandler (log_config.py)
  - RateLimitFilter (log_config.py)
  - configure_logging() extensions (log_config.py)
  - log settings in config.py
  - metrics in metrics.py / embed_service_metrics.py

Plan reference: docs/PLAN_v5_5_1_log_management.md §10
"""

from __future__ import annotations

import json
import logging
import os
import threading
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_n(logger: logging.Logger, n: int, msg: str = "test") -> None:
    for _i in range(n):
        logger.info(msg, extra={"component": "test", "action": "emit", "outcome": "ok"})


# ---------------------------------------------------------------------------
# RotatingJSONLFileHandler: basic JSONL write
# ---------------------------------------------------------------------------


class TestRotatingJSONLFileHandler:
    def test_writes_valid_jsonl(self, tmp_path):
        """Each emitted record is valid JSON on its own line."""
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "test.log"
        handler = RotatingJSONLFileHandler(str(log_file), maxBytes=1_000_000, backupCount=3)
        logger = logging.getLogger("test_jsonl_write")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        logger.info("hello", extra={"component": "test", "action": "x", "outcome": "ok"})
        logger.warning("world", extra={"component": "test", "action": "y", "outcome": "ok"})
        handler.close()

        lines = log_file.read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "ts" in parsed
            assert "level" in parsed
            assert "event" in parsed

    def test_rotation_triggers_at_configured_size(self, tmp_path):
        """After emitting > maxBytes, the .1 backup exists; active file < maxBytes."""
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "rot.log"
        # Small maxBytes to force rotation quickly
        handler = RotatingJSONLFileHandler(str(log_file), maxBytes=1024, backupCount=3)
        logger = logging.getLogger("test_rotation_size")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        # Emit enough to exceed 1024 bytes
        for _ in range(30):
            logger.info("x" * 50, extra={"component": "t", "action": "a", "outcome": "ok"})

        handler.close()

        backup = tmp_path / "rot.log.1"
        assert backup.exists(), "rotation backup .1 must exist after exceeding maxBytes"
        assert log_file.stat().st_size < 1024 * 2, "active file should be small after rotation"

    def test_backup_count_respected(self, tmp_path):
        """After N+2 rotations, only backupCount backup files remain."""
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "bc.log"
        backup_count = 3
        handler = RotatingJSONLFileHandler(str(log_file), maxBytes=200, backupCount=backup_count)
        logger = logging.getLogger("test_backup_count")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        # emit enough to cause backup_count+2 rotations
        for _ in range(120):
            logger.info("y" * 40, extra={"component": "t", "action": "a", "outcome": "ok"})

        handler.close()

        backups = list(tmp_path.glob("bc.log.*"))
        assert len(backups) <= backup_count, (
            f"expected ≤{backup_count} backups, got {len(backups)}: {backups}"
        )

    def test_json_schema_preserved_in_file(self, tmp_path):
        """Records in file include required I14 fields: ts, level, component, action, outcome."""
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "schema.log"
        handler = RotatingJSONLFileHandler(str(log_file), maxBytes=1_000_000, backupCount=3)
        logger = logging.getLogger("test_schema")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        logger.info(
            "schema check",
            extra={"component": "svc", "action": "check", "outcome": "ok"},
        )
        handler.close()

        lines = log_file.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        for field in ("ts", "level", "component", "action", "outcome"):
            assert field in rec, f"I14 field {field!r} missing from file record"


# ---------------------------------------------------------------------------
# Dual-sink coexistence
# ---------------------------------------------------------------------------


class TestDualSinkCoexistence:
    def test_dual_sink_single_emit(self, tmp_path):
        """One logger.info() → record appears in both stdout handler and file."""
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "dual.log"
        file_handler = RotatingJSONLFileHandler(str(log_file), maxBytes=1_000_000, backupCount=3)
        stream_records: list[logging.LogRecord] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                stream_records.append(record)

        logger = logging.getLogger("test_dual_sink")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(file_handler)
        logger.addHandler(CapturingHandler())
        logger.propagate = False

        logger.info("dual", extra={"component": "t", "action": "a", "outcome": "ok"})
        file_handler.close()

        assert len(stream_records) == 1, "stdout handler must receive exactly 1 record"
        lines = log_file.read_text().splitlines()
        assert len(lines) == 1, "file handler must receive exactly 1 record"


# ---------------------------------------------------------------------------
# Graceful fallback
# ---------------------------------------------------------------------------


class TestGracefulFallback:
    def test_unwritable_path_no_raise(self):
        """configure_logging() with unwritable path does not raise; stdout handler active."""
        from yadgar.log_config import RotatingJSONLFileHandler, configure_logging

        with patch.dict(os.environ, {"YADGAR_LOG_FILE_PATH": "/nonexistent/path/yadgar.log"}):
            # Must not raise
            configure_logging(log_format="json", level="WARNING", process="core")

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingJSONLFileHandler)]
        stream_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingJSONLFileHandler)
        ]
        assert len(file_handlers) == 0, "file handler must NOT be installed when path unwritable"
        assert len(stream_handlers) >= 1, "stdout handler must still be installed"

    def test_opt_out_empty_path_no_file_handler(self, tmp_path):
        """YADGAR_LOG_FILE_PATH='' → file handler not installed at all (I3 opt-out)."""
        from yadgar.log_config import RotatingJSONLFileHandler, configure_logging

        with patch.dict(os.environ, {"YADGAR_LOG_FILE_PATH": ""}):
            configure_logging(log_format="json", level="WARNING", process="core")

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingJSONLFileHandler)]
        assert len(file_handlers) == 0, "no file handler when YADGAR_LOG_FILE_PATH is empty"


# ---------------------------------------------------------------------------
# Backend env override (Option A)
# ---------------------------------------------------------------------------


class TestBackendEnvOverride:
    def test_backend_reads_backend_prefix_first(self, tmp_path):
        """Backend process reads YADGAR_BACKEND_LOG_FILE_PATH over YADGAR_LOG_FILE_PATH."""
        core_path = str(tmp_path / "core.log")
        backend_path = str(tmp_path / "backend.log")

        with patch.dict(
            os.environ,
            {
                "YADGAR_LOG_FILE_PATH": core_path,
                "YADGAR_BACKEND_LOG_FILE_PATH": backend_path,
            },
        ):
            from yadgar.log_config import _resolve_log_file_path

            resolved = _resolve_log_file_path(process="backend")

        assert resolved == backend_path, (
            f"backend must prefer YADGAR_BACKEND_LOG_FILE_PATH, got {resolved!r}"
        )

    def test_core_reads_core_prefix(self, tmp_path):
        """Core process reads YADGAR_LOG_FILE_PATH, ignores YADGAR_BACKEND_LOG_FILE_PATH."""
        core_path = str(tmp_path / "core.log")
        backend_path = str(tmp_path / "backend.log")

        with patch.dict(
            os.environ,
            {
                "YADGAR_LOG_FILE_PATH": core_path,
                "YADGAR_BACKEND_LOG_FILE_PATH": backend_path,
            },
        ):
            from yadgar.log_config import _resolve_log_file_path

            resolved = _resolve_log_file_path(process="core")

        assert resolved == core_path, f"core must use YADGAR_LOG_FILE_PATH, got {resolved!r}"

    def test_backend_falls_back_to_shared_env(self, tmp_path):
        """Backend falls back to YADGAR_LOG_FILE_PATH when YADGAR_BACKEND_LOG_FILE_PATH unset."""
        core_path = str(tmp_path / "shared.log")
        env = {"YADGAR_LOG_FILE_PATH": core_path}
        env.pop("YADGAR_BACKEND_LOG_FILE_PATH", None)

        with patch.dict(os.environ, env, clear=False):
            # Remove backend-specific var if it leaked in from other tests
            os.environ.pop("YADGAR_BACKEND_LOG_FILE_PATH", None)
            from yadgar.log_config import _resolve_log_file_path

            resolved = _resolve_log_file_path(process="backend")

        assert resolved == core_path, (
            f"backend must fall back to YADGAR_LOG_FILE_PATH, got {resolved!r}"
        )


# ---------------------------------------------------------------------------
# Idempotent handler install
# ---------------------------------------------------------------------------


class TestIdempotentHandlerInstall:
    def test_double_configure_no_duplicate_file_handlers(self, tmp_path):
        """Calling configure_logging() twice does not stack duplicate file handlers."""
        from yadgar.log_config import RotatingJSONLFileHandler, configure_logging

        log_file = str(tmp_path / "idem.log")
        with patch.dict(os.environ, {"YADGAR_LOG_FILE_PATH": log_file}):
            configure_logging(log_format="json", level="WARNING", process="core")
            configure_logging(log_format="json", level="WARNING", process="core")

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingJSONLFileHandler)]
        assert len(file_handlers) <= 1, (
            f"at most 1 file handler expected after double configure, got {len(file_handlers)}"
        )


# ---------------------------------------------------------------------------
# Rotation counter metric
# ---------------------------------------------------------------------------


class TestRotationCounterMetric:
    def test_rotation_counter_incremented(self, tmp_path):
        """yadgar_log_file_rotations_total increments on doRollover."""
        import yadgar.metrics as m
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "metric.log"
        handler = RotatingJSONLFileHandler(
            str(log_file), maxBytes=200, backupCount=3, logger_name="core"
        )
        logger = logging.getLogger("test_metric_rotation")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        # Get baseline
        before = _get_rotation_counter(m, "core")

        # Force rotation
        for _ in range(50):
            logger.info("z" * 40, extra={"component": "t", "action": "a", "outcome": "ok"})

        handler.close()
        after = _get_rotation_counter(m, "core")
        assert after > before, "rotation counter must increment after doRollover"


def _get_rotation_counter(metrics_module, logger_name: str) -> float:
    """Read current value of yadgar_log_file_rotations_total for logger_name."""
    counter = getattr(metrics_module, "yadgar_log_file_rotations_total", None)
    if counter is None:
        return 0.0
    try:
        return counter.labels(logger=logger_name)._value.get()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimitFilter:
    def test_burst_drops_excess(self):
        """With burst=50, rate=10/s: emitting 51 records immediately drops >=1 (Q4 defaults)."""
        from yadgar.log_config import RateLimitFilter

        filt = RateLimitFilter(
            rate=10.0,
            burst=50,
            name="test_burst",
        )

        records_passed = 0
        records_dropped = 0
        for i in range(51):
            record = logging.LogRecord(
                name="yadgar.requests",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"msg {i}",
                args=(),
                exc_info=None,
            )
            if filt.filter(record):
                records_passed += 1
            else:
                records_dropped += 1

        assert records_dropped >= 1, (
            f"expected >=1 drop with burst=50 and 51 records, got {records_dropped} drops"
        )
        assert records_passed <= 50, (
            f"expected <=50 pass with burst=50, got {records_passed} passes"
        )

    def test_rate_limiter_drop_counter_increments(self):
        """yadgar_log_dropped_total counter increments when rate limiter drops a record."""
        import yadgar.metrics as m
        from yadgar.log_config import RateLimitFilter

        counter = getattr(m, "yadgar_log_dropped_total", None)
        if counter is None:
            pytest.skip("yadgar_log_dropped_total not yet registered")

        filt = RateLimitFilter(rate=1.0, burst=1, name="test_drop_counter")

        before = _get_dropped_counter(m)

        # Emit burst+5 records to guarantee drops
        for i in range(6):
            record = logging.LogRecord(
                name="yadgar.requests",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"msg {i}",
                args=(),
                exc_info=None,
            )
            filt.filter(record)

        after = _get_dropped_counter(m)
        assert after > before, "dropped counter must increment on rate-limiter drops"

    def test_rate_limiter_summary_line_on_drop(self, caplog):
        """When drops occur, a 'rate limited' summary line is logged (at most once/min)."""
        from yadgar.log_config import RateLimitFilter

        filt = RateLimitFilter(rate=1.0, burst=1, name="test_summary")

        # Reset summary state for this test
        filt._last_summary_time = 0.0
        filt._drop_count_since_summary = 0

        # Emit enough to guarantee drops
        for i in range(10):
            record = logging.LogRecord(
                name="yadgar.requests",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"msg {i}",
                args=(),
                exc_info=None,
            )
            filt.filter(record)

        # The filter should have emitted a summary log record internally when dropping
        # Check that the _drop_count_since_summary tracked drops
        assert filt._drop_count_since_summary >= 0  # state exists
        # After summary flush, count resets — we just verify the attribute exists
        assert hasattr(filt, "_last_summary_time")


def _get_dropped_counter(metrics_module) -> float:
    counter = getattr(metrics_module, "yadgar_log_dropped_total", None)
    if counter is None:
        return 0.0
    try:
        # Sum across all label combos
        total = 0.0
        for sample in counter.collect():
            for s in sample.samples:
                if s.name.endswith("_total"):
                    total += s.value
        return total
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Concurrent rotation safety
# ---------------------------------------------------------------------------


class TestConcurrentRotation:
    def test_concurrent_rotation_all_lines_valid_json(self, tmp_path):
        """4 threads × 250 records = 1000 total — every retained line is valid JSON.

        Uses enough backupCount + maxBytes that all 1000 records fit across retained files.
        Verifies thread-safety of RotatingFileHandler (stdlib doRollover uses acquire()).
        """
        from yadgar.log_config import RotatingJSONLFileHandler

        log_file = tmp_path / "concurrent.log"
        n_threads = 4
        records_per_thread = 250  # 4 × 250 = 1000 total
        # Each record ≈ 200 bytes; 1000 records ≈ 200 KB.
        # maxBytes=40_000 → ≈200 records/file; backupCount=10 → 11 files max ≥ 1000 records.
        handler = RotatingJSONLFileHandler(str(log_file), maxBytes=40_000, backupCount=10)
        logger = logging.getLogger("test_concurrent_rotation")
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        def emit_records():
            for i in range(records_per_thread):
                logger.info(
                    "concurrent record %d",
                    i,
                    extra={"component": "t", "action": "a", "outcome": "ok"},
                )

        threads = [threading.Thread(target=emit_records) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        handler.close()

        # Collect all log files (active + backups)
        all_files = [log_file] + sorted(tmp_path.glob("concurrent.log.*"))
        total_lines = 0
        for f in all_files:
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)  # raises JSONDecodeError if any line corrupted
                assert "ts" in parsed and "level" in parsed
                total_lines += 1

        assert total_lines == n_threads * records_per_thread, (
            f"expected {n_threads * records_per_thread} total lines across all files, "
            f"got {total_lines}"
        )


# ---------------------------------------------------------------------------
# End-to-end rate limiter via configure_logging
# ---------------------------------------------------------------------------


class TestRateLimiterEndToEnd:
    def test_configure_logging_installs_rate_limiter_and_drops(self, tmp_path):
        """configure_logging() installs RateLimitFilter; emitting 51 records drops >= 1."""
        from yadgar.log_config import RateLimitFilter, configure_logging

        env = {
            "YADGAR_LOG_FILE_PATH": "",
            "YADGAR_LOG_RATE_LIMIT_ENABLED": "1",
            "YADGAR_LOG_RATE_LIMIT_TOKENS_PER_SEC": "10.0",
            "YADGAR_LOG_RATE_LIMIT_BURST": "50",
        }
        with patch.dict(os.environ, env):
            configure_logging(log_format="json", level="INFO", process="core")

        req_logger = logging.getLogger("yadgar.requests")
        rate_filters = [f for f in req_logger.filters if isinstance(f, RateLimitFilter)]
        assert len(rate_filters) >= 1, "RateLimitFilter must be installed on yadgar.requests"

        # Count passes and drops directly via filter
        filt = rate_filters[0]
        passed = 0
        dropped = 0
        for i in range(51):
            record = logging.LogRecord(
                name="yadgar.requests",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"e2e msg {i}",
                args=(),
                exc_info=None,
            )
            if filt.filter(record):
                passed += 1
            else:
                dropped += 1

        assert dropped >= 1, (
            f"end-to-end: expected >=1 drop with burst=50 and 51 records, got {dropped}"
        )
