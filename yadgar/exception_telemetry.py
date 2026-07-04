"""Exception telemetry helper for PR-H: record_exception().

Increments yadgar_exception_total{location, error_type} and enriches the active
OTel span (if any) with ERROR status and recorded exception detail.

The helper MUST NEVER raise — telemetry failures must never compound a caller failure.
"""

from __future__ import annotations

from yadgar.observability.observe import observe


@observe(
    exempt="enriches the CALLER's active span — a child span would enrich the wrong span and double the span count"
)
def record_exception(location: str, exc: BaseException) -> None:
    """Increment exception counter + best-effort span enrichment.

    Args:
        location: Stable dotted-path identifier for the handler site.
                  Examples: ``ml_client.score_nli``, ``consolidation.phase_link_similar``.
                  Must be a pre-approved label value (cardinality target: ≤20 distinct values).
        exc:      The caught exception instance.

    Side effects:
        - Increments ``yadgar_exception_total{location=..., error_type=<classname>}`` by 1.
        - If an OTel span is currently recording, sets its status to ERROR and records
          the exception into it.

    Never raises. Telemetry errors are silently swallowed.
    """
    try:
        from yadgar.metrics import yadgar_exception_total  # noqa: PLC0415

        yadgar_exception_total.labels(
            location=location,
            error_type=exc.__class__.__name__,
        ).inc()
    except Exception:  # noqa: BLE001 — never let telemetry crash the caller
        pass

    # Best-effort span enrichment — if no active span, silently no-op.
    try:
        from opentelemetry import trace as _trace  # noqa: PLC0415

        span = _trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_status(_trace.Status(_trace.StatusCode.ERROR))
            span.record_exception(exc)
    except Exception:  # noqa: BLE001 — never let telemetry crash the caller
        pass
