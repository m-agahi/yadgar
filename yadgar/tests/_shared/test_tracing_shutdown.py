"""v5.50.10: OTEL shutdown must never hang the daemon (dead/unreachable collector)."""

import time

from yadgar._shared import tracing


def test_shutdown_tracing_bounded_when_provider_hangs(monkeypatch):
    """A blocking provider.shutdown() (dead OTLP collector) must not hang us."""

    class _HangingProvider:
        def shutdown(self):
            time.sleep(30)  # simulate an export flush that never returns

    monkeypatch.setattr(tracing._otel_trace, "get_tracer_provider", lambda: _HangingProvider())
    start = time.monotonic()
    tracing.shutdown_tracing(timeout_sec=0.5)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"shutdown_tracing blocked {elapsed:.1f}s — must be bounded"


def test_shutdown_tracing_noop_without_shutdown(monkeypatch):
    """Safe when the provider has no shutdown (tracing never set up)."""
    monkeypatch.setattr(tracing._otel_trace, "get_tracer_provider", lambda: object())
    tracing.shutdown_tracing(timeout_sec=0.5)  # must not raise
