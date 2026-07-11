"""Regression: the logging subsystem must NOT amplify spans under a REAL
recording OTLP-style provider (prod span→log→span flood, v5.106).

Prod-down bug (v5.105 obs rollout): `@observe` on log-emission-path functions
(`log_config._is_sensitive`, `RotatingJSONLFileHandler.emit`,
`ContentRedactor.filter`, `RateLimitFilter.filter`, `LogRingHandler.emit`, …)
opens a span per log record. Under a RECORDING TracerProvider the span-end fires
`LogSpanProcessor._emit_span_log` → `logger.info("span_end")` → that record
re-enters the observed log path → MORE spans → unbounded per-log amplification.
The thread-local re-entry guard in `yadgar.tracing` stops infinite recursion but
NOT the per-log fan-out.

CI/e2e missed it because they run with `YADGAR_OTLP_ENDPOINT=''` → NonRecording
spans → `LogSpanProcessor` sees a non-recording span → no span_end log → no
re-entry → no flood. This test installs a REAL recording provider + a REAL
`LogSpanProcessor` + the REAL yadgar log filters/handlers, so it exercises the
exact prod path.

Acceptance gate (TDD): with the log-path `@observe` decorators PRESENT (the
pre-fix / prod state) this test FLOODS and FAILS. With the fix (decorators
removed, path glob-exempted in `.observe-allowlist.json`) span count stays
bounded and it PASSES. See docs / CHANGELOG v5.106.
"""

from __future__ import annotations

import logging

import pytest


def _reset_tracer_provider() -> None:
    """Reset OTel global tracer-provider once-guard so we can install a fresh one.

    Mirrors test_tracing.py::_reset_tracer_provider — OTel blocks repeated
    set_tracer_provider via a private Once guard; clear it and the cached
    provider, and tear down the span-log queue so records reach our handlers.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None
        trace.set_tracer_provider(TracerProvider())
        try:
            import yadgar._shared.observability.tracing as _tr

            _tr._SETUP_DONE.clear()
            _tr._stop_span_log_queue()
            logging.getLogger("yadgar.tracing").propagate = True
        except Exception:
            pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_otel():
    _reset_tracer_provider()
    yield
    _reset_tracer_provider()


@pytest.fixture()
def recording_provider_with_log_span_processor():
    """Install a REAL recording TracerProvider with:

      * a SimpleSpanProcessor(InMemorySpanExporter) to COUNT every span, and
      * a REAL LogSpanProcessor — so a span-end emits the "span_end" log record
        that (pre-fix) re-enters the observed log path and amplifies.

    Yields (tracer, exporter).

    LEAK HYGIENE (v5.106): this fixture installs a GLOBAL recording provider. If it
    does not restore the prior global provider + OTel once-guard in teardown, the
    recording provider leaks onto the xdist worker and pollutes sibling tests
    (notably test_logs_api — a recording span there emits span_end which lands in
    the /logs ring). So it is a yield-fixture that saves the prior provider/once
    state and restores it, and shuts down its own provider to drain any pending
    exports. Relying on the autouse _reset_otel reset alone was insufficient — the
    once-guard could no-op the replacement, leaving THIS recording provider live.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from yadgar._shared.observability import tracing as tr

    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    prev_once_done = getattr(once, "_done", None) if once is not None else None
    prev_provider = getattr(trace, "_TRACER_PROVIDER", None)

    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "amp-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The real span→log emitter — recording spans → span_end log lines.
    provider.add_span_processor(tr.LogSpanProcessor(service_name="amp-test"))
    trace.set_tracer_provider(provider)
    try:
        yield trace.get_tracer("amp-test"), exporter
    finally:
        # Drain/stop our provider so no pending export fires against a sibling.
        try:
            provider.shutdown()
        except Exception:
            pass
        # Restore the prior global provider + once-guard so the recording provider
        # cannot leak onto the worker and contaminate later tests.
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = prev_provider
        if once is not None and prev_once_done is not None:
            once._done = prev_once_done


def _make_observed_log_path_logger() -> logging.Logger:
    """Build a logger wired with the REAL yadgar log-path components that carry
    (or, pre-fix, carried) `@observe`: the JSON formatter, the ContentRedactor
    filter, and the RotatingJSONLFileHandler emit path — the same objects prod
    installs. Each observed method opens a span per record when decorated.
    """
    from yadgar._shared.observability.log_config import ContentRedactor, JSONLogFormatter

    lg = logging.getLogger("yadgar.tests.amp_probe")
    lg.handlers.clear()
    lg.filters.clear()
    lg.setLevel(logging.DEBUG)
    lg.propagate = False

    # A stream handler using the REAL observed JSON formatter (drives format()/
    # _is_sensitive via _append_extras) and the REAL observed ContentRedactor
    # filter (drives filter() + _is_sensitive per record).
    handler = logging.StreamHandler()
    handler.setFormatter(JSONLogFormatter())
    handler.addFilter(ContentRedactor())
    lg.addHandler(handler)
    return lg


class TestLogSpanAmplification:
    def test_log_burst_does_not_amplify_spans(self, recording_provider_with_log_span_processor):
        """Emit a burst of real log records under a recording provider + real
        LogSpanProcessor + real observed log-path components. Span count MUST
        stay bounded — the log path itself must open ~0 spans, and must NOT grow
        with the number of records emitted (no span→log→span amplification).
        """
        tracer, exporter = recording_provider_with_log_span_processor
        lg = _make_observed_log_path_logger()

        n_records = 25
        try:
            for i in range(n_records):
                # warning() is on the amplification path in the prod report.
                lg.warning("amp probe %d", i, extra={"component": "amp", "token": "sekret"})
        finally:
            # Flush the span-log queue path so any amplified span_end records land.
            try:
                import yadgar._shared.observability.tracing as _tr

                _tr._stop_span_log_queue()
            except Exception:
                pass

        spans = exporter.get_finished_spans()
        # The logging subsystem must be un-instrumentable: emitting N log records
        # must not create spans that scale with N. A tiny constant slack covers
        # any incidental span; a flood would produce >> n_records spans (each log
        # record re-entering the observed path opens more spans, whose span_end
        # logs re-enter again). Bound well below n_records.
        assert len(spans) < n_records, (
            f"span→log→span amplification: {len(spans)} spans from {n_records} log "
            f"records (log-emission path must not open per-record spans). "
            f"Span names: {sorted({s.name for s in spans})}"
        )
