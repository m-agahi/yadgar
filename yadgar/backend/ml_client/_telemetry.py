"""Shared telemetry helpers for LocalMLClient and RemoteMLClient."""

from __future__ import annotations

import logging

from yadgar._shared.config import resolve_knob

logger = logging.getLogger(__name__)


def _rpc_span(name: str, attributes: dict | None = None):
    """Context manager: OTel span for RemoteMLClient RPC calls.

    Falls back to a no-op context when OTel is unavailable.
    """
    try:
        from opentelemetry import trace as _t  # noqa: PLC0415

        tracer = _t.get_tracer("yadgar.backend.ml_client")
        ctx = tracer.start_as_current_span(name)
        return ctx
    except Exception:
        import contextlib  # noqa: PLC0415

        return contextlib.nullcontext()


def _record_model_load(model: str, duration_seconds: float) -> None:
    """Record a cold model load: observe histogram + emit OTel span.

    model: metric/span label — "ce" or "nli".
    duration_seconds: wall-clock elapsed for the constructor call.
    cold_load is always True here (called only when the handle was None before construction).
    """
    # Histogram
    try:
        import yadgar.backend.embed_service.embed_service_metrics as _esm  # noqa: PLC0415

        _esm.model_load_duration_seconds.labels(model=model).observe(duration_seconds)
    except Exception:
        pass  # metrics not available in core container

    # OTel span
    try:
        from opentelemetry import trace as _otel  # noqa: PLC0415

        tracer = _otel.get_tracer("yadgar.backend.ml_client")
        with tracer.start_as_current_span("model.load") as span:
            span.set_attribute("model", model)
            span.set_attribute("cold_load", True)
            span.set_attribute("duration_seconds", duration_seconds)
    except Exception:
        pass  # OTel not available — no-op


def _emit_unload_telemetry(unloaded_ce: bool, unloaded_nli: bool, effective: float) -> None:
    """Emit Prometheus + OTel telemetry for an idle-eviction unload event.

    Extracted to keep LocalMLClient.unload_if_idle under the cyclo hard limit.
    """
    # Prometheus gauges + counters
    try:
        import yadgar.backend.embed_service.embed_service_metrics as _esm  # noqa: PLC0415

        if unloaded_ce:
            _esm.model_loaded.labels(model="ce").set(0)
            _esm.model_unload_total.labels(model="ce").inc()
        if unloaded_nli:
            _esm.model_loaded.labels(model="nli").set(0)
            _esm.model_unload_total.labels(model="nli").inc()
    except Exception:
        pass  # metrics not available in core container

    # OTel span — one span per call that actually evicted
    try:
        from opentelemetry import trace as _otel  # noqa: PLC0415

        tracer = _otel.get_tracer("yadgar.backend.ml_client")
        model_label = ",".join((["ce"] if unloaded_ce else []) + (["nli"] if unloaded_nli else []))
        with tracer.start_as_current_span("model.unload") as span:
            span.set_attribute("model", model_label)
            span.set_attribute("idle_seconds", float(effective))
    except Exception:
        pass  # OTel not available — no-op


def _idle_eviction_seconds() -> int:
    """Return the configured idle-eviction threshold in seconds.

    Returns 0 when YADGAR_MODEL_IDLE_EVICTION_SECONDS is unset, empty, or
    unparseable — meaning 'never evict' (safe default).
    """
    return resolve_knob("YADGAR_MODEL_IDLE_EVICTION_SECONDS", "MODEL_IDLE_EVICTION_SECONDS", int, 0)
