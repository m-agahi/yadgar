"""P4 (I33, ADR-0034) — @observe span emission for server/tools helpers.

TDD: these tests are RED before the P4 @observe(tier=...) decorators are added to
the private helper functions under ``yadgar/server/tools/*.py``. The MCP tool
entrypoints themselves (``@_tool``) already carry a ``tool.<name>`` span and are
NOT re-instrumented (double-span trap); only their sub-helpers gain tri-signal
coverage in this wave.

MODEL-FREE by construction: the helpers exercised here are pure compute / render
functions (no embedding model, no CE, no DB round-trip), so the suite runs in CI
without loading GTE/ModernBERT. Do NOT add cases that import the recall provider
chain (``fuse_candidates`` et al.) — that risks pulling an ML model at import.

The assertion is span-name emission under a clean test tracer (mirrors
``test_observe_decorator.py`` / ``test_stage_spans.py``). We assert the helper
opens its OWN named span, proving the decorator is present and firing.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def span_exporter():
    """InMemorySpanExporter wired to a clean test TracerProvider.

    Returns the exporter; call get_finished_spans() after the code under test.
    """
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
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


def _span_names(exporter) -> set[str]:
    return {s.name for s in exporter.get_finished_spans()}


def test_compute_valid_until_emits_span(span_exporter):
    """memorize._compute_valid_until opens a hot span (pure ttl compute)."""
    from yadgar.config import get_settings
    from yadgar.server.tools.memorize import _compute_valid_until

    _compute_valid_until("semantic_immortal", None, None, get_settings())
    assert "tools.memorize._compute_valid_until" in _span_names(span_exporter), _span_names(
        span_exporter
    )


def test_slug_prefix_emits_span(span_exporter):
    """project._slug_prefix opens a hot span (pure string derive)."""
    from yadgar.server.tools.project import _slug_prefix

    _slug_prefix("some-wiki-slug-here")
    assert "tools.project._slug_prefix" in _span_names(span_exporter), _span_names(span_exporter)


def test_omit_sentinel_emits_span(span_exporter):
    """project._omit_sentinel opens a hot span (pure conditional dict set)."""
    from yadgar.server.tools.project import _omit_sentinel

    _omit_sentinel({}, "k", "v", None)
    assert "tools.project._omit_sentinel" in _span_names(span_exporter), _span_names(span_exporter)


def test_cosine_similarity_emits_span(span_exporter):
    """project._cosine_similarity opens a hot span (pure math)."""
    from yadgar.server.tools.project import _cosine_similarity

    _cosine_similarity([1.0, 0.0, 1.0], [1.0, 1.0, 0.0])
    assert "tools.project._cosine_similarity" in _span_names(span_exporter), _span_names(
        span_exporter
    )
