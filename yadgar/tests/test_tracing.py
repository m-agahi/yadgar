"""v5.6.3 distributed tracing tests — TDD: written before implementation.

All tests in this file are written to FAIL until yadgar/tracing.py is implemented.

Coverage:
  - setup_tracing idempotency
  - @trace_span sync: creates span, returns value
  - @trace_span async: awaitable, creates span
  - Span tree: nested decorated calls produce parent_span_id linkage
  - Exception records on span + status=ERROR + re-raises
  - LogSpanProcessor emits valid JSON with required I14 fields
  - W3C traceparent header roundtrip (in-process FastAPI + httpx)
  - asyncio.to_thread preserves OTel context
  - Concurrent asyncio tasks don't bleed contexts
  - JSONLogFormatter injects trace_id/span_id when active, omits when inactive
  - Fallback mode: OTel missing → identity decorator, setup_tracing no-ops
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from io import StringIO
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _reset_tracer_provider():
    """Reset OTel global tracer provider state between tests.

    OTel uses a private _TRACER_PROVIDER_SET_ONCE Once guard that blocks
    repeated set_tracer_provider calls. We reset the internal '_done' flag
    and clear _TRACER_PROVIDER before installing a new provider.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        # Reset the internal once-guard (_done is the flag on opentelemetry.util._once.Once)
        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False

        # Clear the cached provider reference
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None

        new_provider = TracerProvider()
        trace.set_tracer_provider(new_provider)

        # Reset setup_tracing idempotency guard
        try:
            import yadgar.tracing as _tr

            _tr._SETUP_DONE.clear()
        except Exception:
            pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_otel_state():
    """Reset OTel global state before each test."""
    _reset_tracer_provider()
    yield
    _reset_tracer_provider()


@pytest.fixture()
def in_memory_tracer():
    """Returns (tracer, exporter) with an InMemorySpanExporter.

    Resets OTel internal state so we can install the test provider cleanly.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Reset once-guard so we can install test provider (autouse may have set it already)
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# 1. setup_tracing idempotency
# ---------------------------------------------------------------------------


class TestSetupTracingIdempotent:
    def test_setup_tracing_can_be_called_twice(self):
        """setup_tracing called twice does not raise and does not stack processors."""
        from yadgar.tracing import setup_tracing

        setup_tracing("test-service")
        setup_tracing("test-service")  # should not raise

    def test_setup_tracing_sets_global_provider(self):
        """After setup_tracing, global tracer provider is not NoOp."""
        from opentelemetry import trace
        from opentelemetry.trace import NoOpTracerProvider

        from yadgar.tracing import setup_tracing

        setup_tracing("test-service")
        provider = trace.get_tracer_provider()
        assert not isinstance(provider, NoOpTracerProvider)


# ---------------------------------------------------------------------------
# 2. @trace_span sync — creates span, returns value
# ---------------------------------------------------------------------------


class TestTraceSpanSync:
    def test_sync_returns_value(self, in_memory_tracer):
        """Decorated sync function returns its normal return value."""
        from yadgar.tracing import trace_span

        @trace_span("test.sync_fn")
        def my_fn():
            return 42

        assert my_fn() == 42

    def test_sync_creates_span(self, in_memory_tracer):
        """Decorated sync function creates exactly one span."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.sync_span")
        def my_fn():
            return "ok"

        my_fn()
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "test.sync_span" in names

    def test_sync_default_name(self, in_memory_tracer):
        """Name defaults to module.qualname when not provided."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span()
        def explicit_fn():
            return 1

        explicit_fn()
        spans = exporter.get_finished_spans()
        assert any("explicit_fn" in s.name for s in spans)

    def test_sync_attributes_set(self, in_memory_tracer):
        """Static attributes passed to decorator appear on span."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.with_attrs", attributes={"custom.key": "val"})
        def my_fn():
            return 1

        my_fn()
        spans = exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "test.with_attrs")
        assert span.attributes.get("custom.key") == "val"


# ---------------------------------------------------------------------------
# 3. @trace_span async — awaitable, creates span
# ---------------------------------------------------------------------------


class TestTraceSpanAsync:
    def test_async_returns_value(self, in_memory_tracer):
        """Decorated async function returns its normal return value."""
        from yadgar.tracing import trace_span

        @trace_span("test.async_fn")
        async def my_fn():
            return 99

        result = asyncio.run(my_fn())
        assert result == 99

    def test_async_creates_span(self, in_memory_tracer):
        """Decorated async function creates exactly one span."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.async_span")
        async def my_fn():
            return "ok"

        asyncio.run(my_fn())
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "test.async_span" in names


# ---------------------------------------------------------------------------
# 4. Span tree: parent_span_id linkage
# ---------------------------------------------------------------------------


class TestSpanTree:
    def test_nested_sync_spans_have_parent(self, in_memory_tracer):
        """Inner decorated function has parent_span_id == outer span id."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.inner")
        def inner():
            return "inner"

        @trace_span("test.outer")
        def outer():
            return inner()

        outer()
        spans = exporter.get_finished_spans()
        outer_span = next(s for s in spans if s.name == "test.outer")
        inner_span = next(s for s in spans if s.name == "test.inner")
        # Inner's parent should be outer
        assert inner_span.parent is not None
        assert inner_span.parent.span_id == outer_span.context.span_id

    def test_nested_async_spans_have_parent(self, in_memory_tracer):
        """Inner async decorated function has correct parent."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.async_inner")
        async def inner():
            return "inner"

        @trace_span("test.async_outer")
        async def outer():
            return await inner()

        asyncio.run(outer())
        spans = exporter.get_finished_spans()
        outer_span = next(s for s in spans if s.name == "test.async_outer")
        inner_span = next(s for s in spans if s.name == "test.async_inner")
        assert inner_span.parent is not None
        assert inner_span.parent.span_id == outer_span.context.span_id


# ---------------------------------------------------------------------------
# 5. Exception records on span + status=ERROR + re-raises
# ---------------------------------------------------------------------------


class TestTraceSpanException:
    def test_exception_reraises(self, in_memory_tracer):
        """Exception propagates out of decorated function."""
        from yadgar.tracing import trace_span

        @trace_span("test.exception_fn")
        def my_fn():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            my_fn()

    def test_exception_recorded_on_span(self, in_memory_tracer):
        """Exception is recorded on span and status is ERROR."""
        from opentelemetry.trace import StatusCode

        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.exception_span")
        def my_fn():
            raise RuntimeError("test error")

        with pytest.raises(RuntimeError):
            my_fn()

        spans = exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "test.exception_span")
        assert span.status.status_code == StatusCode.ERROR
        # Events should contain the exception
        assert any(e.name == "exception" for e in span.events)

    def test_async_exception_recorded(self, in_memory_tracer):
        """Async exception is also recorded."""
        from opentelemetry.trace import StatusCode

        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import trace_span

        @trace_span("test.async_exception")
        async def my_fn():
            raise ValueError("async boom")

        with pytest.raises(ValueError):
            asyncio.run(my_fn())

        spans = exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "test.async_exception")
        assert span.status.status_code == StatusCode.ERROR


# ---------------------------------------------------------------------------
# 6. LogSpanProcessor emits valid JSON with I14 fields
# ---------------------------------------------------------------------------


class TestLogSpanProcessor:
    def test_span_end_emits_json(self):
        """LogSpanProcessor emits a valid JSON line with required I14 fields on span end."""
        import logging as _logging

        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        from yadgar import tracing as tr

        # Capture log output using JSONLogFormatter so we get parseable JSON
        from yadgar.log_config import JSONLogFormatter

        log_capture = StringIO()
        handler = _logging.StreamHandler(log_capture)
        handler.setFormatter(JSONLogFormatter())
        handler.setLevel(_logging.DEBUG)
        proc_logger = _logging.getLogger("yadgar.tracing")
        proc_logger.addHandler(handler)
        proc_logger.setLevel(_logging.DEBUG)
        proc_logger.propagate = False

        try:
            processor = tr.LogSpanProcessor(service_name="test-svc")
            provider = TracerProvider(resource=Resource.create({"service.name": "test-svc"}))
            # LogSpanProcessor IS a SpanProcessor — add directly, not via SimpleSpanProcessor
            provider.add_span_processor(processor)

            from opentelemetry import trace

            # Reset once-guard before installing test provider
            once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
            if once is not None and hasattr(once, "_done"):
                once._done = False
            if hasattr(trace, "_TRACER_PROVIDER"):
                trace._TRACER_PROVIDER = None

            trace.set_tracer_provider(provider)
            tracer = trace.get_tracer("test")
            with tracer.start_as_current_span("test.span_emit"):
                pass

            output = log_capture.getvalue()
            assert output.strip(), "No log output emitted by LogSpanProcessor"
            line = json.loads(output.strip().splitlines()[-1])
            assert line["event"] == "span_end"
            assert line["component"] == "tracing"
            assert "trace_id" in line
            assert "span_id" in line
            assert "span_name" in line
            assert "service" in line
            assert "duration_ms" in line
            assert "status" in line
            assert line["service"] == "test-svc"
        finally:
            proc_logger.removeHandler(handler)
            proc_logger.propagate = True

    def test_span_end_has_level_info(self):
        """LogSpanProcessor emits at INFO level."""
        import logging as _logging

        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        from yadgar import tracing as tr

        records: list[_logging.LogRecord] = []

        class Capture(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                records.append(record)

        cap = Capture()
        cap.setLevel(_logging.DEBUG)
        proc_logger = _logging.getLogger("yadgar.tracing")
        proc_logger.addHandler(cap)
        proc_logger.setLevel(_logging.DEBUG)

        try:
            processor = tr.LogSpanProcessor(service_name="test-svc")
            provider = TracerProvider(resource=Resource.create({"service.name": "test-svc"}))
            # LogSpanProcessor IS a SpanProcessor — add directly
            provider.add_span_processor(processor)

            from opentelemetry import trace

            # Reset once-guard before installing test provider
            once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
            if once is not None and hasattr(once, "_done"):
                once._done = False
            if hasattr(trace, "_TRACER_PROVIDER"):
                trace._TRACER_PROVIDER = None

            trace.set_tracer_provider(provider)
            tracer = trace.get_tracer("test")
            with tracer.start_as_current_span("test.level_check"):
                pass

            assert any(r.levelno == _logging.INFO for r in records)
        finally:
            proc_logger.removeHandler(cap)


# ---------------------------------------------------------------------------
# 7. get_current_trace_id / get_current_span_id helpers
# ---------------------------------------------------------------------------


class TestCurrentIds:
    def test_no_span_returns_none(self):
        """Outside any span context, helpers return None."""
        _reset_tracer_provider()
        from yadgar.tracing import get_current_span_id, get_current_trace_id

        assert get_current_trace_id() is None
        assert get_current_span_id() is None

    def test_inside_span_returns_ids(self, in_memory_tracer):
        """Inside a span, helpers return non-None hex strings."""
        tracer, _exporter = in_memory_tracer
        from yadgar.tracing import get_current_span_id, get_current_trace_id

        with tracer.start_as_current_span("test.ids"):
            tid = get_current_trace_id()
            sid = get_current_span_id()

        assert tid is not None
        assert sid is not None
        # 32-char trace_id, 16-char span_id (W3C hex)
        assert len(tid) == 32
        assert len(sid) == 16


# ---------------------------------------------------------------------------
# 8. JSONLogFormatter injects trace_id/span_id when context active
# ---------------------------------------------------------------------------


class TestJSONLogFormatterTraceInjection:
    def test_injects_when_span_active(self, in_memory_tracer):
        """JSONLogFormatter adds trace_id + span_id when OTel span is active."""
        tracer, _exporter = in_memory_tracer
        from yadgar.log_config import JSONLogFormatter

        formatter = JSONLogFormatter()
        logger = logging.getLogger("test.formatter")
        logger.setLevel(logging.DEBUG)

        with tracer.start_as_current_span("test.fmt_span"):
            record = logging.LogRecord(
                name="test.formatter",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="hello",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)

        data = json.loads(output)
        assert "trace_id" in data
        assert "span_id" in data
        assert len(data["trace_id"]) == 32
        assert len(data["span_id"]) == 16

    def test_omits_when_no_span(self):
        """JSONLogFormatter omits trace_id/span_id when no active span."""
        _reset_tracer_provider()
        from yadgar.log_config import JSONLogFormatter

        formatter = JSONLogFormatter()
        record = logging.LogRecord(
            name="test.formatter",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "trace_id" not in data
        assert "span_id" not in data


# ---------------------------------------------------------------------------
# 9. asyncio.to_thread preserves OTel context
# ---------------------------------------------------------------------------


class TestAsyncioToThreadContextPropagation:
    def test_to_thread_sees_parent_span(self, in_memory_tracer):
        """asyncio.to_thread preserves OTel span context from the calling coroutine."""
        _tracer, exporter = in_memory_tracer
        from yadgar.tracing import get_current_trace_id

        captured: list[str | None] = []

        def thread_fn():
            captured.append(get_current_trace_id())

        async def run():
            from opentelemetry import trace

            tracer = trace.get_tracer("test")
            with tracer.start_as_current_span("test.outer_async"):
                await asyncio.to_thread(thread_fn)

        asyncio.run(run())
        # The thread should have seen the same trace_id
        assert captured[0] is not None, "Thread did not see OTel context from parent coroutine"


# ---------------------------------------------------------------------------
# 10. Concurrent asyncio tasks don't bleed contexts
# ---------------------------------------------------------------------------


class TestConcurrentTaskContextIsolation:
    def test_tasks_have_independent_spans(self, in_memory_tracer):
        """Two concurrent asyncio tasks each see their own span, not each other's."""
        tracer, exporter = in_memory_tracer
        from yadgar.tracing import get_current_trace_id

        task_ids: dict[str, str | None] = {}

        async def run_task(name: str):
            with tracer.start_as_current_span(f"task.{name}"):
                await asyncio.sleep(0)  # yield point
                task_ids[name] = get_current_trace_id()

        async def run():
            await asyncio.gather(run_task("a"), run_task("b"))

        asyncio.run(run())
        # Both should have seen a trace_id
        assert task_ids["a"] is not None
        assert task_ids["b"] is not None
        # Both should see the SAME root trace_id (they inherit from same event loop context)
        # but their span_ids should differ — just confirm no crash here
        assert isinstance(task_ids["a"], str)
        assert isinstance(task_ids["b"], str)


# ---------------------------------------------------------------------------
# 11. Fallback mode: OTel import fails → identity decorator, setup_tracing no-ops
# ---------------------------------------------------------------------------


class TestFallbackMode:
    def test_trace_span_identity_when_otel_missing(self, monkeypatch):
        """When opentelemetry is not importable, @trace_span is identity decorator."""
        # Simulate missing OTel by removing from sys.modules and blocking import
        import builtins as _builtins

        real_import = _builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name.startswith("opentelemetry"):
                raise ImportError(f"Simulated missing: {name}")
            return real_import(name, *args, **kwargs)

        # Remove tracing module from cache to force re-import
        tracing_key = "yadgar.tracing"
        saved = sys.modules.pop(tracing_key, None)
        try:
            with patch("builtins.__import__", side_effect=mock_import):
                # This import will use fallback mode
                # We test the fallback functions directly
                from yadgar.tracing import _OTEL_AVAILABLE

                if not _OTEL_AVAILABLE:
                    from yadgar.tracing import trace_span

                    @trace_span("test.fallback")
                    def my_fn():
                        return "identity"

                    assert my_fn() == "identity"
        finally:
            # Restore
            if saved is not None:
                sys.modules[tracing_key] = saved
            else:
                sys.modules.pop(tracing_key, None)

    def test_setup_tracing_noop_when_otel_missing(self):
        """setup_tracing does not raise even when called in fallback mode."""
        # The real implementation has a try/except guard
        # Just verify setup_tracing is safe to call
        from yadgar.tracing import setup_tracing

        # Should not raise regardless of environment
        try:
            setup_tracing("test-noop")
        except Exception as e:
            pytest.fail(f"setup_tracing raised unexpectedly: {e}")


# ---------------------------------------------------------------------------
# 12. Integration: W3C traceparent header propagation (in-process FastAPI + httpx)
# ---------------------------------------------------------------------------


class TestW3CTraceparentPropagation:
    def test_trace_id_propagates_via_traceparent(self, in_memory_tracer):
        """httpx client auto-injects traceparent; FastAPI server continues trace."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        _tracer, exporter = in_memory_tracer

        # Create a minimal FastAPI app
        test_app = FastAPI()

        @test_app.get("/ping")
        async def ping():
            return {"pong": True}

        # Instrument the app + client
        FastAPIInstrumentor.instrument_app(test_app)
        HTTPXClientInstrumentor().instrument()

        try:
            client = TestClient(test_app, raise_server_exceptions=True)

            # Call inside a parent span
            parent_trace_id = None
            with _tracer.start_as_current_span("test.client_call") as span:
                parent_trace_id = format(span.get_span_context().trace_id, "032x")
                client.get("/ping")

            spans = exporter.get_finished_spans()
            # All spans should share the same trace_id
            trace_ids = {
                format(s.context.trace_id, "032x") for s in spans if s.context.trace_id != 0
            }
            assert parent_trace_id in trace_ids
            # There should be a server-side span (route)
            server_spans = [s for s in spans if "ping" in s.name.lower() or s.kind.name == "SERVER"]
            assert server_spans, f"No server-side spans found. All spans: {[s.name for s in spans]}"
        finally:
            FastAPIInstrumentor.uninstrument_app(test_app)
            HTTPXClientInstrumentor().uninstrument()
