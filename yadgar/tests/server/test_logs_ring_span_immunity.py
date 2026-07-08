"""Regression: the /logs app-log RING must be structurally immune to span_end
telemetry, even under a REAL recording OTLP provider (v5.106 gap #3).

Class of bug (3rd occurrence, see ADR-0041): span_end telemetry emitted by
``LogSpanProcessor._emit_span_log`` on the ``yadgar.tracing`` logger PROPAGATES to
root, whose handlers include ``LogRingHandler`` (the in-memory ring served by the
``/api/logs/poll`` debug API). Every finished span therefore injects an
``event=="span_end"`` record into the app-log ring — telemetry spam where app logs
should be. In prod OTLP recording is ON PERMANENTLY, so this floods the ring for
every span regardless of any test.

The earlier fixes exempted individual @observe'd functions from opening spans.
That is whack-a-mole: the ring shows span_end from MANY sources
(auth_middleware.*, config.resolve_knob, _ring_append, logs_poll_handler, …), so
exempting one more function is always one gap short. The structural fix makes the
RING itself refuse span_end telemetry regardless of how many spans exist or which
provider is active.

CI missed the whole class because the suite runs ``YADGAR_OTLP_ENDPOINT=''`` →
NonRecording spans → no span_end log → clean ring → green. This test installs a
REAL recording ``TracerProvider`` + REAL ``LogSpanProcessor`` and drives spans, so
it exercises the exact prod path.

Two acceptance gates:
  1. IMMUNITY  — after a burst of recorded spans, ``get_ring_snapshot`` contains
     ZERO ``event=="span_end"`` records (the ring holds only app logs).
  2. SINK KEPT — span_end telemetry STILL reaches its file sink
     (``RotatingJSONLFileHandler`` on root); the fix drops it from the ring only,
     it does not silently discard telemetry.
"""

from __future__ import annotations

import json
import logging

import pytest


def _reset_tracer_provider() -> None:
    """Clear OTel's set-once guard + cached provider so we can install a fresh one."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None
        trace.set_tracer_provider(TracerProvider())
        import yadgar._shared.tracing as _tr

        _tr._SETUP_DONE.clear()
        _tr._stop_span_log_queue()
        logging.getLogger("yadgar.tracing").propagate = True
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_otel():
    _reset_tracer_provider()
    yield
    _reset_tracer_provider()


@pytest.fixture()
def _tracing_logger_at_info():
    """span_end is emitted at INFO — the tracing logger must be at INFO for the
    record to fire (its default effective level is inherited WARNING otherwise).
    Restore level + propagate afterwards.
    """
    tl = logging.getLogger("yadgar.tracing")
    orig_level, orig_prop = tl.level, tl.propagate
    tl.setLevel(logging.INFO)
    tl.propagate = True
    try:
        yield tl
    finally:
        tl.setLevel(orig_level)
        tl.propagate = orig_prop


@pytest.fixture()
def _recording_provider():
    """Install a REAL recording TracerProvider with an InMemorySpanExporter (to
    count spans) + a REAL LogSpanProcessor (so span-end emits the span_end log).

    Yield-fixture (NOT return) that saves + restores the prior global provider and
    OTel once-guard in teardown — a recording provider bearing a LogSpanProcessor
    must not leak onto the xdist worker and contaminate sibling tests (the very
    leak that caused test_logs_api to flake). Belt-and-suspenders with the autouse
    _reset_otel reset.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from yadgar._shared import tracing as tr

    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    prev_once_done = getattr(once, "_done", None) if once is not None else None
    prev_provider = getattr(trace, "_TRACER_PROVIDER", None)

    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "ring-immunity"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(tr.LogSpanProcessor(service_name="ring-immunity"))
    trace.set_tracer_provider(provider)
    try:
        yield trace.get_tracer("ring-immunity"), exporter
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = prev_provider
        if once is not None and prev_once_done is not None:
            once._done = prev_once_done


def _reset_ring():
    import yadgar.core.server.routes.logs as _m

    with _m._ring_lock:
        _m._ring.clear()
        _m._ring_bytes = 0
        _m._ring_seq = 0


def test_ring_is_immune_to_span_end_under_recording_provider(
    _recording_provider, _tracing_logger_at_info
):
    """Drive a burst of RECORDED spans → span_end propagates to root → LogRingHandler.
    The ring must hold ONLY app logs: ZERO span_end telemetry records.
    """
    import yadgar.core.server.routes.logs as _m

    tracer, exporter = _recording_provider
    _m.install_ring_handler()
    _reset_ring()

    # An honest app log the ring SHOULD keep, to prove we drop only telemetry.
    app_logger = logging.getLogger("yadgar")
    orig_level = app_logger.level
    app_logger.setLevel(logging.INFO)
    root = logging.getLogger()
    orig_root_level = root.level
    root.setLevel(logging.INFO)
    try:
        app_logger.info("real app log line — keep me")

        n_spans = 20
        for i in range(n_spans):
            with tracer.start_as_current_span(f"api.logs.poll.{i}"):
                pass
    finally:
        app_logger.setLevel(orig_level)
        root.setLevel(orig_root_level)

    # Sanity: the provider actually recorded spans (else the test proves nothing).
    assert len(exporter.get_finished_spans()) >= n_spans

    entries, _next = _m.get_ring_snapshot(since_seq=0)

    span_end_entries = [
        e
        for e in entries
        if "span_end" in str(e.get("message", "")) or e.get("event") == "span_end"
    ]
    assert not span_end_entries, (
        f"app-log ring contaminated with {len(span_end_entries)} span_end telemetry "
        f"records under a recording provider (must be structurally immune). "
        f"Sample: {[e.get('message', '')[:80] for e in span_end_entries[:3]]}"
    )

    # The genuine app log must still be present — the filter drops telemetry, not app logs.
    kept = [e for e in entries if "keep me" in str(e.get("message", ""))]
    assert kept, (
        f"app log line wrongly dropped from ring; entries={[e.get('message', '')[:60] for e in entries]}"
    )


def test_span_end_still_reaches_file_sink_after_ring_filter(
    _recording_provider, _tracing_logger_at_info, tmp_path
):
    """The fix drops span_end from the RING only. span_end telemetry MUST still
    reach its intended daemon sink (the RotatingJSONLFileHandler file). Assert the
    span_end JSON line lands in the file even while the ring stays immune.
    """
    import yadgar.core.server.routes.logs as _m
    from yadgar._shared.log_config import RotatingJSONLFileHandler

    tracer, _exporter = _recording_provider
    _m.install_ring_handler()
    _reset_ring()

    log_file = tmp_path / "spans.log"
    file_handler = RotatingJSONLFileHandler(str(log_file), maxBytes=10_000_000, backupCount=1)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    orig_root_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    try:
        with tracer.start_as_current_span("api.file_sink_probe"):
            pass
    finally:
        file_handler.flush()
        file_handler.close()
        root.removeHandler(file_handler)
        root.setLevel(orig_root_level)

    # Ring stays immune...
    entries, _next = _m.get_ring_snapshot(since_seq=0)
    assert not [e for e in entries if "span_end" in str(e.get("message", ""))], (
        "ring must not contain span_end"
    )

    # ...but the file sink DID receive the span_end telemetry line.
    contents = log_file.read_text()
    span_end_lines = []
    for line in contents.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "span_end":
            span_end_lines.append(rec)
    assert span_end_lines, (
        f"span_end telemetry must still reach the file sink (not be silently dropped). "
        f"File contents: {contents[:400]!r}"
    )
