"""Tests for v5.6.7 PR-C: DB-layer metrics wiring + storage search spans.

Covers:
  1. yadgar_db_query_duration_seconds._count increments M times after M _q calls.
  2. yadgar_surrealdb_query_duration_ms._count (labelled op) increments M times.
  3. yadgar_surrealdb_pool_active >= 1 after StorageEngine connects.
  4. yadgar_surrealdb_connection_pool_wait_ms has >= 1 observation after init.
  5. Span emitted for search_memories_by_content_date.
  6. Span emitted for search_memories_by_timestamp_range.
  7. Span emitted for search_memories_by_month.
  8. Span emitted for search_profiles_fts.

Pool-active semantics: SurrealDB embedded and server modes both use a singleton
connection (no real pool exposed). pool_active is set to 1 at connect and 0 at
close. pool_wait_ms gets one observation of 0.0 at init (no real acquire latency).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_hist_count_nolabel(metric) -> float:
    """Read _count from a labelless Histogram via the samples API."""
    for fam in metric.collect():
        for s in fam.samples:
            if s.name.endswith("_count") and not s.labels:
                return s.value
    return 0.0


def _get_hist_count_labeled(metric, **label_filter) -> float:
    """Read _count from a labeled Histogram matching the given label values."""
    total = 0.0
    for fam in metric.collect():
        for s in fam.samples:
            if not s.name.endswith("_count"):
                continue
            if all(s.labels.get(k) == v for k, v in label_filter.items()):
                total += s.value
    return total


def _get_gauge_value(metric) -> float:
    """Read current value from a labelless Gauge via the samples API."""
    for fam in metric.collect():
        for s in fam.samples:
            if not s.name.endswith("_total") and not s.name.endswith("_count"):
                return s.value
    return 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    """Embedded-mode StorageEngine on a temp path."""
    from yadgar.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "test_db_metrics.db"))
    yield engine
    engine.close()


@pytest.fixture
def in_memory_tracer():
    """Install an InMemorySpanExporter as the OTel provider. Returns (tracer, exporter)."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:
        pytest.skip("opentelemetry SDK not available")

    # Reset once-guard so test provider installs cleanly
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield trace.get_tracer("test"), exporter

    # Cleanup
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None


# ---------------------------------------------------------------------------
# 1. yadgar_db_query_duration_seconds increments M times after M calls
# ---------------------------------------------------------------------------


class TestDbQueryDurationSeconds:
    def test_increments_on_q_calls(self, storage):
        """After M _q calls the no-label histogram count increases by M."""
        from yadgar.metrics import yadgar_db_query_duration_seconds

        before = _get_hist_count_nolabel(yadgar_db_query_duration_seconds)

        M = 3
        for _ in range(M):
            storage._q("SELECT * FROM memory LIMIT 1")

        after = _get_hist_count_nolabel(yadgar_db_query_duration_seconds)
        assert after - before == M, f"Expected +{M} observations, got before={before} after={after}"


# ---------------------------------------------------------------------------
# 2. yadgar_surrealdb_query_duration_ms increments M times
# ---------------------------------------------------------------------------


class TestSurrealdbQueryDurationMs:
    def test_increments_on_q_calls(self, storage):
        """After M _q calls the labelled histogram count increases by M total."""
        from yadgar.metrics import yadgar_surrealdb_query_duration_ms

        # Sum count across all op labels (or read the specific op for SELECT)
        before = _get_hist_count_labeled(yadgar_surrealdb_query_duration_ms, op="SELECT")

        M = 3
        for _ in range(M):
            storage._q("SELECT * FROM memory LIMIT 1")

        after = _get_hist_count_labeled(yadgar_surrealdb_query_duration_ms, op="SELECT")
        assert after - before == M, (
            f"Expected +{M} observations on op=SELECT, got before={before} after={after}"
        )


# ---------------------------------------------------------------------------
# 3. yadgar_surrealdb_pool_active >= 1 after connect
# ---------------------------------------------------------------------------


class TestPoolActive:
    def test_pool_active_after_connect(self, storage):
        """pool_active must be >= 1 after StorageEngine initialises."""
        from yadgar.metrics import yadgar_surrealdb_pool_active

        val = _get_gauge_value(yadgar_surrealdb_pool_active)
        assert val >= 1, f"Expected pool_active >= 1 after connect, got {val}"

    def test_pool_active_zero_after_close(self, tmp_path):
        """pool_active must be 0 after explicit close()."""
        from yadgar.metrics import yadgar_surrealdb_pool_active
        from yadgar.storage import StorageEngine

        engine = StorageEngine(str(tmp_path / "close_test.db"))
        engine.close()
        val = _get_gauge_value(yadgar_surrealdb_pool_active)
        assert val == 0, f"Expected pool_active == 0 after close, got {val}"


# ---------------------------------------------------------------------------
# 4. yadgar_surrealdb_connection_pool_wait_ms has >= 1 observation after init
# ---------------------------------------------------------------------------


class TestPoolWaitMs:
    def test_has_observation_after_init(self, storage):
        """pool_wait_ms histogram must have at least one observation (value 0.0 for singleton)."""
        from yadgar.metrics import yadgar_surrealdb_connection_pool_wait_ms

        count = _get_hist_count_nolabel(yadgar_surrealdb_connection_pool_wait_ms)
        assert count >= 1, f"Expected >= 1 observation in pool_wait_ms, got {count}"


# ---------------------------------------------------------------------------
# 5-8. Span tests for storage search methods
# ---------------------------------------------------------------------------


def _make_memory_args():
    return {
        "content": "span test memory",
        "directory_context": "/tmp/span_test",
        "tags": ["span"],
    }


class TestStorageSearchSpans:
    def test_search_memories_by_content_date_span(self, storage, in_memory_tracer):
        """search_memories_by_content_date emits a span."""
        _tracer, exporter = in_memory_tracer
        exporter.clear()

        storage.search_memories_by_content_date(
            date_hints=["2026-01-01"],
            month_hints=[],
            session_hints=[],
        )
        span_names = [s.name for s in exporter.get_finished_spans()]
        assert "storage.memory.search_memories_by_content_date" in span_names, (
            f"Expected span; got {span_names}"
        )

    def test_search_memories_by_timestamp_range_span(self, storage, in_memory_tracer):
        """search_memories_by_timestamp_range emits a span."""
        _tracer, exporter = in_memory_tracer
        exporter.clear()

        storage.search_memories_by_timestamp_range(
            start_date="2026-01-01T00:00:00",
            end_date="2026-12-31T23:59:59",
        )
        span_names = [s.name for s in exporter.get_finished_spans()]
        assert "storage.memory.search_memories_by_timestamp_range" in span_names, (
            f"Expected span; got {span_names}"
        )

    def test_search_memories_by_month_span(self, storage, in_memory_tracer):
        """search_memories_by_month emits a span."""
        _tracer, exporter = in_memory_tracer
        exporter.clear()

        storage.search_memories_by_month(month_hints=["january"])
        span_names = [s.name for s in exporter.get_finished_spans()]
        assert "storage.memory.search_memories_by_month" in span_names, (
            f"Expected span; got {span_names}"
        )

    def test_search_profiles_fts_span(self, storage, in_memory_tracer):
        """search_profiles_fts emits a span."""
        _tracer, exporter = in_memory_tracer
        exporter.clear()

        storage.search_profiles_fts(query="alice")
        span_names = [s.name for s in exporter.get_finished_spans()]
        assert "storage.user.search_profiles_fts" in span_names, f"Expected span; got {span_names}"
