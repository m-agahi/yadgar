"""P3 (obs wave, #8) — backend tri-signal @observe coverage.

TDD: RED before the @observe decorators land on yadgar/backend/*.

Scope (backend only, 4 files):
  - embed_service.py : FastAPI /embed /health /admin/dbsize boundary spans
                       (rerank stays EXEMPT — it already emits yadgar_embed_rerank_*
                       RED + a manual backend.rerank.{mode} span).
  - ml_client.py     : LocalMLClient rerank/embed internals -> stage spans
                       (RemoteMLClient.score_* stay EXEMPT — manual _rpc_span).
  - cache.py         : snapshot I/O -> stage spans (get/put stay hot-loop exempt).

Load-bearing assertions:
  1. Route boundary spans emit AND nest (share the FastAPI server-span trace_id)
     — a span that emits as an orphan root passes a naive check but breaks the
     under-request view.  We hit the route via ASGITransport, never a direct call,
     because @observe placed ABOVE @app.post silently no-ops on real requests.
  2. Admin auth still fires after wrapping (a no-token request must still 401/500)
     — proves functools.wraps preserved the Depends() signature.
  3. LocalMLClient stage methods carry a span source (the observe sentinel).
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# In-memory OTel harness (mirrors test_stage_spans.py / test_backend_traceparent_e2e.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def span_provider():
    """Fresh TracerProvider + InMemorySpanExporter installed as the global provider."""
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
    provider = TracerProvider(resource=Resource.create({"service.name": "p3-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider, exporter


def _reload_es(monkeypatch, *, allow_root: bool = True):
    import yadgar._shared.config as cfg

    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1" if allow_root else "0")
    monkeypatch.delenv("YADGAR_DB_PATH", raising=False)
    cfg.get_settings.cache_clear()
    import yadgar.backend.embed_service.embed_service as es

    importlib.reload(es)
    return es


def _client(es, provider):
    """TestClient with the app's FastAPI instrumentation bound to our provider."""
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # Re-instrument the freshly-reloaded app against the test provider so server
    # spans (and the nested @observe boundary spans) export to our exporter.
    try:
        FastAPIInstrumentor.instrument_app(es.app, tracer_provider=provider)
    # Re-instrumenting an app that a previous test already instrumented raises
    # opentelemetry.instrumentation.dependencies errors whose classes differ by
    # instrumentation version and are not imported here.
    except Exception:
        pass
    return TestClient(es.app, raise_server_exceptions=False)


def _span_names(exporter) -> set[str]:
    return {s.name for s in exporter.get_finished_spans()}


# ---------------------------------------------------------------------------
# 1. Route boundary spans emit + nest under the FastAPI server span
# ---------------------------------------------------------------------------


def test_health_boundary_span_emits(span_provider, monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    provider, exporter = span_provider
    es = _reload_es(monkeypatch)
    client = _client(es, provider)

    resp = client.get("/health")
    # /health returns 503 when DB/model down — status is irrelevant; span must emit.
    assert resp.status_code in (200, 503)
    names = _span_names(exporter)
    # Span name is now the dynamic module.qualname (…embed_service.health); the
    # "backend.health" metric label is preserved separately via @observe(metric=).
    assert any(n.endswith(".embed_service.health") for n in names), (
        f"no backend health boundary span in {names}"
    )


def test_admin_dbsize_boundary_span_and_nesting(span_provider, monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    provider, exporter = span_provider
    es = _reload_es(monkeypatch)
    client = _client(es, provider)

    resp = client.get("/admin/dbsize")
    assert resp.status_code == 200
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    # Span name is now the dynamic module.qualname (…embed_service.admin_dbsize).
    assert any(n.endswith(".admin_dbsize") for n in names), f"no boundary span in {names}"

    # Nesting: the observe boundary span shares a trace_id with the server span.
    obs = next(s for s in spans if s.name.endswith(".admin_dbsize"))
    server = [s for s in spans if not s.name.endswith(".admin_dbsize")]
    assert server, "no FastAPI server span captured"
    assert any(s.context.trace_id == obs.context.trace_id for s in server), (
        "boundary span is an orphan root — not nested under the request span"
    )


# ---------------------------------------------------------------------------
# 2. Admin auth still fires after @observe wrapping (signature preserved)
# ---------------------------------------------------------------------------


def test_admin_auth_still_enforced_after_wrap(span_provider, monkeypatch):
    """No token + no root-escape → 401/500, proving Depends() survived the wrap.

    500 (not 503) is the unconfigured-token status since ADR-0180 / task:0090.
    """
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    provider, _exporter = span_provider
    es = _reload_es(monkeypatch, allow_root=False)
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    client = _client(es, provider)

    resp = client.get("/admin/dbsize")  # no Authorization header
    assert resp.status_code in (401, 500), (
        f"admin route not protected after @observe wrap: {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 3. rerank stays EXEMPT — no duplicate observe RED family for the endpoint
# ---------------------------------------------------------------------------


def test_rerank_not_double_instrumented(monkeypatch):
    """rerank must NOT carry an @observe wrapper (it already emits RED + a manual span)."""
    es = _reload_es(monkeypatch)
    # The route function object must not have the observe span sentinel.
    fn = es.rerank
    assert not getattr(fn, "_yadgar_observe_has_span", False), (
        "rerank was decorated with @observe — duplicate RED + double span"
    )


# ---------------------------------------------------------------------------
# 4. LocalMLClient rerank/embed internals carry a span source (stage tier)
# ---------------------------------------------------------------------------


def test_local_ml_client_stage_methods_span_sourced():
    from yadgar.backend import ml_client as mc

    for meth in (
        "score_cross_encoder",
        "score_nli",
        "_try_gte_reranker",
        "_try_st_cross_encoder",
        "_load_gte_reranker",
    ):
        fn = getattr(mc.LocalMLClient, meth)
        assert getattr(fn, "_yadgar_observe_has_span", False), (
            f"LocalMLClient.{meth} missing @observe span source"
        )


def test_remote_ml_client_score_methods_stay_exempt():
    """RemoteMLClient.score_* use a manual _rpc_span — must NOT be @observe-decorated."""
    from yadgar.backend import ml_client as mc

    for meth in ("score_cross_encoder", "score_nli", "score_pair"):
        fn = getattr(mc.RemoteMLClient, meth)
        assert not getattr(fn, "_yadgar_observe_has_span", False), (
            f"RemoteMLClient.{meth} double-instrumented (manual _rpc_span already spans)"
        )
