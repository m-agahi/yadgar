"""I33 P5 — storage read methods @observe instrumentation tests (obs wave P5).

Asserts:
  1. Storage read methods decorated with @observe have the _yadgar_observe_has_span
     sentinel attribute set.
  2. A representative read method (get_memories_by_ids_projected) emits at least
     one span with the expected name when called.

These tests are model-free and do not touch a real SurrealDB instance.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_observe_sentinel(fn) -> bool:
    """Return True if fn has the _yadgar_observe_has_span sentinel set by @observe."""
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


# ---------------------------------------------------------------------------
# Sentinel checks — verify @observe is wired on selected storage methods
# ---------------------------------------------------------------------------


class TestObserveSentinels:
    """Verify _yadgar_observe_has_span is set on instrumented storage methods."""

    def test_memory_get_by_ids_projected_has_sentinel(self):
        from yadgar._shared.storage.memory import _MemoryMixin

        assert _has_observe_sentinel(_MemoryMixin.get_memories_by_ids_projected)

    def test_memory_get_candidate_ids_has_sentinel(self):
        from yadgar._shared.storage.memory import _MemoryMixin

        assert _has_observe_sentinel(_MemoryMixin.get_candidate_memory_ids)

    def test_wiki_get_by_slug_has_sentinel(self):
        from yadgar._shared.storage.wiki import _WikiMixin

        assert _has_observe_sentinel(_WikiMixin.get_wiki_page_by_slug_and_branch)

    def test_entity_get_all_has_sentinel(self):
        from yadgar._shared.storage.entity import _EntityMixin

        assert _has_observe_sentinel(_EntityMixin.get_all_entities)

    def test_queue_upsert_file_hash_has_sentinel(self):
        from yadgar._shared.storage.queue import _QueueMixin

        assert _has_observe_sentinel(_QueueMixin.upsert_file_hash)

    def test_episode_insert_has_sentinel(self):
        from yadgar._shared.storage.episode import _EpisodeMixin

        assert _has_observe_sentinel(_EpisodeMixin.insert_episode)

    def test_narrative_insert_belief_has_sentinel(self):
        from yadgar._shared.storage.narrative import _NarrativeMixin

        assert _has_observe_sentinel(_NarrativeMixin.insert_belief)

    def test_storage_engine_close_has_sentinel(self):
        from yadgar._shared.storage import StorageEngine

        assert _has_observe_sentinel(StorageEngine.close)


# ---------------------------------------------------------------------------
# Span emission — verify span is emitted when a storage read is called
# ---------------------------------------------------------------------------


@pytest.fixture()
def in_memory_tracer():
    """(tracer, exporter) with InMemorySpanExporter; installs a clean test provider."""
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
    tracer = trace.get_tracer("test")
    return tracer, exporter


class TestStorageReadSpanEmission:
    """Verify spans are emitted for storage read methods."""

    def test_get_candidate_memory_ids_emits_span(self, in_memory_tracer):
        """get_candidate_memory_ids should emit a span named 'storage.memory._MemoryMixin.get_candidate_memory_ids'."""
        from unittest.mock import MagicMock

        from yadgar._shared.storage.memory import _MemoryMixin

        _tracer, exporter = in_memory_tracer

        # Build a minimal stub with the methods _MemoryMixin depends on
        stub = MagicMock(spec=_MemoryMixin)
        stub._q = MagicMock(return_value=[])
        stub._rows_to_dicts = MagicMock(return_value=[])

        # Call the unbound method directly on stub
        _MemoryMixin.get_candidate_memory_ids(stub)

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert any("get_candidate_memory_ids" in name for name in span_names), (
            f"Expected span for get_candidate_memory_ids, got: {span_names}"
        )

    def test_episode_insert_emits_span(self, in_memory_tracer):
        """insert_episode should emit a stage span."""
        from unittest.mock import MagicMock

        from yadgar._shared.storage.episode import _EpisodeMixin

        _tracer, exporter = in_memory_tracer

        stub = MagicMock(spec=_EpisodeMixin)
        stub._q = MagicMock(return_value=[])
        stub._next_id = MagicMock(return_value=42)
        stub._now_iso = MagicMock(return_value="2026-07-03T00:00:00")

        episode = {
            "session_id": "test-session",
            "directory": "/tmp/test",
            "raw_content": "content",
        }
        _EpisodeMixin.insert_episode(stub, episode)

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert any("insert_episode" in name for name in span_names), (
            f"Expected span for insert_episode, got: {span_names}"
        )
