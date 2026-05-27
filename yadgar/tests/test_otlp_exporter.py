"""v5.7.6 — OTLP/HTTP span exporter tests. TDD: written before implementation.

Coverage:
  - When YADGAR_OTLP_ENDPOINT unset: no OTLP exporter registered; LogSpanProcessor present.
  - When YADGAR_OTLP_ENDPOINT set: OTLPSpanExporter registered + LogSpanProcessor still present.
  - YADGAR_OTLP_HEADERS parses comma-separated k=v pairs correctly.
  - Invalid endpoint URL logs WARN and falls back to logs-only (no crash).
  - YADGAR_OTLP_TIMEOUT_SEC respected (default 10).
"""

from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_tracing():
    """Reset OTel global state + yadgar.tracing idempotency guard.

    Clears the once-guard and cached provider WITHOUT installing a blank
    provider — doing so would mark _done=True again, blocking setup_tracing.
    """
    try:
        from opentelemetry import trace

        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None

        import yadgar.tracing as _tr

        _tr._SETUP_DONE.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_otel():
    _reset_tracing()
    # Clear Settings cache so monkeypatched env vars are picked up by pydantic-settings.
    try:
        from yadgar.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass
    yield
    _reset_tracing()
    try:
        from yadgar.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass


def _get_processors(provider):
    """Extract span processors from a TracerProvider (SDK internal)."""
    # SDK stores processors on the active span processor (a SynchronousMultiSpanProcessor)
    multi = getattr(provider, "_active_span_processor", None)
    if multi is None:
        return []
    # _span_processors is a tuple/list on SynchronousMultiSpanProcessor
    return list(getattr(multi, "_span_processors", []))


# ---------------------------------------------------------------------------
# 1. No endpoint set — no OTLP exporter; LogSpanProcessor present
# ---------------------------------------------------------------------------


class TestNoEndpoint:
    def test_no_otlp_when_endpoint_unset(self, monkeypatch):
        """With YADGAR_OTLP_ENDPOINT unset, no OTLPSpanExporter is registered."""
        monkeypatch.delenv("YADGAR_OTLP_ENDPOINT", raising=False)

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import setup_tracing

        setup_tracing("test-no-otlp")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)

        otlp_batch_procs = [
            p
            for p in processors
            if isinstance(p, BatchSpanProcessor)
            and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
        ]
        assert len(otlp_batch_procs) == 0, (
            f"Expected 0 OTLP BatchSpanProcessors, got {len(otlp_batch_procs)}"
        )

    def test_log_processor_present_when_no_endpoint(self, monkeypatch):
        """LogSpanProcessor is always registered regardless of OTLP endpoint."""
        monkeypatch.delenv("YADGAR_OTLP_ENDPOINT", raising=False)

        from yadgar.tracing import LogSpanProcessor, setup_tracing

        setup_tracing("test-log-always")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)

        log_procs = [p for p in processors if isinstance(p, LogSpanProcessor)]
        assert len(log_procs) == 1, (
            f"Expected exactly 1 LogSpanProcessor, got {len(log_procs)}: {processors}"
        )


# ---------------------------------------------------------------------------
# 2. Endpoint set — OTLP exporter registered + LogSpanProcessor still present
# ---------------------------------------------------------------------------


class TestWithEndpoint:
    def test_otlp_registered_when_endpoint_set(self, monkeypatch):
        """OTLPSpanExporter is registered inside a BatchSpanProcessor when endpoint is set."""
        monkeypatch.setenv("YADGAR_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
        monkeypatch.delenv("YADGAR_OTLP_HEADERS", raising=False)

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import setup_tracing

        setup_tracing("test-with-otlp")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)

        otlp_batch_procs = [
            p
            for p in processors
            if isinstance(p, BatchSpanProcessor)
            and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
        ]
        assert len(otlp_batch_procs) == 1, (
            f"Expected 1 OTLP BatchSpanProcessor, got {len(otlp_batch_procs)}: {processors}"
        )

    def test_log_processor_still_present_when_endpoint_set(self, monkeypatch):
        """LogSpanProcessor is present even when OTLP endpoint is configured."""
        monkeypatch.setenv("YADGAR_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")

        from yadgar.tracing import LogSpanProcessor, setup_tracing

        setup_tracing("test-both-procs")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)

        log_procs = [p for p in processors if isinstance(p, LogSpanProcessor)]
        assert len(log_procs) == 1, (
            f"Expected 1 LogSpanProcessor alongside OTLP, got {len(log_procs)}: {processors}"
        )

    def test_both_processors_registered(self, monkeypatch):
        """Both LogSpanProcessor and BatchSpanProcessor(OTLP) registered when endpoint set."""
        monkeypatch.setenv("YADGAR_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import LogSpanProcessor, setup_tracing

        setup_tracing("test-dual")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)

        log_procs = [p for p in processors if isinstance(p, LogSpanProcessor)]
        otlp_procs = [
            p
            for p in processors
            if isinstance(p, BatchSpanProcessor)
            and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
        ]
        assert len(log_procs) == 1
        assert len(otlp_procs) == 1


# ---------------------------------------------------------------------------
# 3. Header parsing
# ---------------------------------------------------------------------------


class TestHeaderParsing:
    def test_parse_headers_two_pairs(self):
        """Comma-separated k=v pairs parse into a dict with 2 entries."""
        from yadgar.tracing import _parse_otlp_headers

        result = _parse_otlp_headers("x-tenant=foo,authorization=Bearer x")
        assert result == {"x-tenant": "foo", "authorization": "Bearer x"}

    def test_parse_headers_single_pair(self):
        """Single k=v pair returns 1-entry dict."""
        from yadgar.tracing import _parse_otlp_headers

        result = _parse_otlp_headers("x-scope=metrics")
        assert result == {"x-scope": "metrics"}

    def test_parse_headers_empty_string(self):
        """Empty string returns empty dict."""
        from yadgar.tracing import _parse_otlp_headers

        result = _parse_otlp_headers("")
        assert result == {}

    def test_parse_headers_value_with_equals(self):
        """Value containing '=' is preserved (only split on first '=')."""
        from yadgar.tracing import _parse_otlp_headers

        result = _parse_otlp_headers("authorization=Bearer tok=en")
        assert result == {"authorization": "Bearer tok=en"}

    def test_parse_headers_whitespace_trimmed(self):
        """Leading/trailing whitespace around key and value is stripped."""
        from yadgar.tracing import _parse_otlp_headers

        result = _parse_otlp_headers(" x-key = value , x-other = thing ")
        assert result == {"x-key": "value", "x-other": "thing"}

    def test_parse_headers_malformed_pair_skipped(self):
        """Pair without '=' is silently skipped."""
        from yadgar.tracing import _parse_otlp_headers

        result = _parse_otlp_headers("good=val,badinput,other=ok")
        assert result == {"good": "val", "other": "ok"}


# ---------------------------------------------------------------------------
# 4. Invalid endpoint URL — warn + fall back to logs-only, no crash
# ---------------------------------------------------------------------------


class TestInvalidEndpoint:
    def test_invalid_endpoint_does_not_crash(self, monkeypatch, caplog):
        """Malformed endpoint URL logs WARN and setup_tracing does not raise."""
        monkeypatch.setenv("YADGAR_OTLP_ENDPOINT", "not-a-url")

        from yadgar.tracing import setup_tracing

        with caplog.at_level(logging.WARNING, logger="yadgar.tracing"):
            setup_tracing("test-invalid-url")

        # Should still have LogSpanProcessor — logs-only fallback
        from opentelemetry import trace

        from yadgar.tracing import LogSpanProcessor

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)
        log_procs = [p for p in processors if isinstance(p, LogSpanProcessor)]
        assert len(log_procs) == 1, "LogSpanProcessor missing after invalid endpoint fallback"

        # Should have emitted a warning
        warn_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("otlp" in m.lower() or "endpoint" in m.lower() for m in warn_messages), (
            f"Expected WARN about OTLP/endpoint, got: {warn_messages}"
        )


# ---------------------------------------------------------------------------
# 5. Timeout default
# ---------------------------------------------------------------------------


class TestOtlpTimeout:
    def test_default_timeout_is_10(self, monkeypatch):
        """YADGAR_OTLP_TIMEOUT_SEC defaults to 10."""
        monkeypatch.setenv("YADGAR_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
        monkeypatch.delenv("YADGAR_OTLP_TIMEOUT_SEC", raising=False)

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import setup_tracing

        setup_tracing("test-timeout")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)
        otlp_proc = next(
            (
                p
                for p in processors
                if isinstance(p, BatchSpanProcessor)
                and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
            ),
            None,
        )
        assert otlp_proc is not None
        exporter = otlp_proc.span_exporter
        # OTLPSpanExporter stores timeout_sec as _timeout
        assert exporter._timeout == 10

    def test_custom_timeout(self, monkeypatch):
        """YADGAR_OTLP_TIMEOUT_SEC=30 configures exporter with 30s timeout."""
        monkeypatch.setenv("YADGAR_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
        monkeypatch.setenv("YADGAR_OTLP_TIMEOUT_SEC", "30")

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import setup_tracing

        setup_tracing("test-custom-timeout")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)
        otlp_proc = next(
            (
                p
                for p in processors
                if isinstance(p, BatchSpanProcessor)
                and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
            ),
            None,
        )
        assert otlp_proc is not None
        exporter = otlp_proc.span_exporter
        assert exporter._timeout == 30


# ---------------------------------------------------------------------------
# 6. YAML override path (v5.7.11) — Settings-backed knobs honour config.yaml
# ---------------------------------------------------------------------------


class TestYamlOverride:
    """Verify yaml config file overrides work for OTLP Settings fields."""

    def test_yaml_endpoint_override(self, monkeypatch, tmp_path):
        """OTLP_ENDPOINT from yaml (not env) enables OTLP exporter."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("otlp_endpoint: http://yaml-tempo:4318/v1/traces\n")

        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))
        monkeypatch.delenv("YADGAR_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("YADGAR_OTLP_HEADERS", raising=False)
        monkeypatch.delenv("YADGAR_OTLP_TIMEOUT_SEC", raising=False)
        monkeypatch.delenv("YADGAR_OTLP_INSECURE", raising=False)

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import setup_tracing

        setup_tracing("test-yaml-endpoint")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)
        otlp_procs = [
            p
            for p in processors
            if isinstance(p, BatchSpanProcessor)
            and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
        ]
        assert len(otlp_procs) == 1, (
            f"Expected 1 OTLP exporter via yaml override, got {len(otlp_procs)}"
        )

    def test_yaml_timeout_override(self, monkeypatch, tmp_path):
        """OTLP_TIMEOUT_SEC from yaml (not env) is respected."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "otlp_endpoint: http://yaml-tempo:4318/v1/traces\notlp_timeout_sec: 25\n"
        )

        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))
        monkeypatch.delenv("YADGAR_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("YADGAR_OTLP_TIMEOUT_SEC", raising=False)

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from yadgar.tracing import setup_tracing

        setup_tracing("test-yaml-timeout")

        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        processors = _get_processors(provider)
        otlp_proc = next(
            (
                p
                for p in processors
                if isinstance(p, BatchSpanProcessor)
                and isinstance(getattr(p, "span_exporter", None), OTLPSpanExporter)
            ),
            None,
        )
        assert otlp_proc is not None
        assert otlp_proc.span_exporter._timeout == 25, (
            f"Expected timeout=25 from yaml, got {otlp_proc.span_exporter._timeout}"
        )

    def test_yaml_headers_override(self, monkeypatch, tmp_path):
        """OTLP_HEADERS from yaml (not env) are passed to exporter."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "otlp_endpoint: http://yaml-tempo:4318/v1/traces\notlp_headers: x-tenant=yaml-org\n"
        )

        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))
        monkeypatch.delenv("YADGAR_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("YADGAR_OTLP_HEADERS", raising=False)

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        from yadgar.tracing import _build_otlp_exporter

        exporter = _build_otlp_exporter()
        assert exporter is not None, "Expected exporter from yaml override"
        # OTLPSpanExporter stores parsed headers in _headers
        headers = getattr(exporter, "_headers", None)
        # headers may be a dict or list of tuples depending on OTel version
        if isinstance(headers, dict):
            assert "x-tenant" in headers, f"Expected x-tenant header, got {headers}"
        else:
            header_keys = [k for k, _ in (headers or [])]
            assert "x-tenant" in header_keys, f"Expected x-tenant header, got {headers}"
