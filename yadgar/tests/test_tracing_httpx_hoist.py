"""v5.101 P0 R2 — HTTPXClientInstrumentor hoisted into setup_tracing().

The stdio/daemon-mode hole: entry paths that never import server/_app.py never
called HTTPXClientInstrumentor().instrument(), so backend HTTP from those paths
rooted a disconnected trace. Fix: setup_tracing() is the single choke-point — it
instruments httpx itself, so every entry mode that sets up tracing gets it.
"""

from __future__ import annotations

import pytest


def test_setup_tracing_instruments_httpx():
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    import yadgar.tracing as tracing

    # Ensure a clean starting point.
    HTTPXClientInstrumentor().uninstrument()
    assert not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry

    # setup_tracing must instrument httpx as part of the shared startup path.
    # (idempotent per service_name — force a fresh service to run the body)
    tracing._SETUP_DONE.discard("test-httpx-hoist")
    try:
        tracing.setup_tracing("test-httpx-hoist")
        assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry, (
            "setup_tracing() did not instrument httpx — stdio/daemon-mode hole open"
        )
    finally:
        HTTPXClientInstrumentor().uninstrument()


def test_setup_tracing_httpx_instrument_idempotent():
    """Calling setup_tracing twice must not raise despite httpx already instrumented."""
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    import yadgar.tracing as tracing

    tracing._SETUP_DONE.discard("test-httpx-idem-a")
    tracing._SETUP_DONE.discard("test-httpx-idem-b")
    try:
        tracing.setup_tracing("test-httpx-idem-a")
        # second, distinct service — body runs again; httpx already instrumented
        tracing.setup_tracing("test-httpx-idem-b")  # must not raise
    finally:
        HTTPXClientInstrumentor().uninstrument()
