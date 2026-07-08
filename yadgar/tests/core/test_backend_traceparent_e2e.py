"""v5.101 P0 R1 — end-to-end traceparent propagation core → backend.

Verifies the (already-wired) propagation chain actually connects one trace:
a core-side httpx call under an active span injects a W3C `traceparent`; the
backend FastAPI app (instrumented with FastAPIInstrumentor) extracts it, so the
backend request span shares the core span's trace_id — one connected trace.

Kept minimal (no ML models, no live backend): a wsgi/asgi test transport carries
the header; both ends use InMemorySpanExporter on a single shared provider so a
shared trace_id is directly assertable.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def shared_tracer_provider():
    """Single in-memory provider shared by the 'core' httpx client + 'backend' app."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "e2e"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter


def test_traceparent_crosses_to_backend_shared_trace_id(shared_tracer_provider):
    """A core httpx call under a span → backend server span shares the trace_id."""
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    pytest.importorskip("opentelemetry.instrumentation.fastapi")

    import asyncio

    import httpx
    from fastapi import FastAPI
    from opentelemetry import trace
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    provider, exporter = shared_tracer_provider

    # ── backend side: a FastAPI app, instrumented, with a model-free route ──
    app = FastAPI()

    @app.get("/rerank-probe")
    def rerank_probe():
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    # ── core side: httpx client hitting the backend app through ASGITransport ──
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)

    async def _drive() -> int:
        transport = httpx.ASGITransport(app=app)
        tracer = trace.get_tracer("core")
        with tracer.start_as_current_span("recall") as core_span:
            core_trace_id = core_span.get_span_context().trace_id
            async with httpx.AsyncClient(transport=transport, base_url="http://backend") as client:
                resp = await client.get("/rerank-probe")
                assert resp.status_code == 200
        return core_trace_id

    try:
        core_trace_id = asyncio.run(_drive())
    finally:
        HTTPXClientInstrumentor().uninstrument()
        FastAPIInstrumentor.uninstrument_app(app)

    spans = exporter.get_finished_spans()
    # The backend FastAPI request span (route "GET /rerank-probe") must share the
    # core span's trace_id — proof the W3C traceparent crossed the wire and the
    # backend extracted it. (Span *kind* varies by OTel version; identity is by
    # the route span carrying the same trace_id as the core "recall" span.)
    backend_spans = [
        s for s in spans if s.name == "GET /rerank-probe" and " http send" not in s.name
    ]
    assert backend_spans, f"no backend request span produced: {[s.name for s in spans]}"
    assert any(s.get_span_context().trace_id == core_trace_id for s in backend_spans), (
        "backend request span did not share the core trace_id — traceparent not propagated"
    )
    # And the core span itself must exist in the same trace.
    core_spans = [s for s in spans if s.name == "recall"]
    assert core_spans and core_spans[0].get_span_context().trace_id == core_trace_id
