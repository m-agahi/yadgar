"""v5.6.7 PR-D: recall + wiki pipeline metrics tests — TDD, fail before implementation.

Coverage:
  1. yadgar_recall_duration_ms._count increments by M after M recall MCP tool calls (Bug A fix).
  2. yadgar_recall_stage_ms stages observed after Retriever.recall() call.
  3. retrieval.recall span emitted after Retriever.recall() call.
  4. wiki.query span emitted after WikiStore.query() call.
  5. yadgar_wiki_query_duration_ms._count increments after M wiki_query MCP tool calls.
  6. NLI-off: yadgar_recall_stage_ms{stage="nli"} has zero observations.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def _count_nolabel(metric) -> float:
    """Read _count from a labelless Histogram via samples API."""
    for fam in metric.collect():
        for s in fam.samples:
            if s.name.endswith("_count") and not s.labels:
                return s.value
    return 0.0


def _count_labeled(metric, **label_filter) -> float:
    """Read _count from a labeled Histogram matching given label values."""
    total = 0.0
    for fam in metric.collect():
        for s in fam.samples:
            if not s.name.endswith("_count"):
                continue
            if all(s.labels.get(k) == v for k, v in label_filter.items()):
                total += s.value
    return total


def _reset_otel():
    """Reset OTel global tracer provider state."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None
        new_provider = TracerProvider()
        trace.set_tracer_provider(new_provider)

        try:
            import yadgar._shared.tracing as _tr

            _tr._SETUP_DONE.clear()
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_otel_state():
    """Reset OTel global state before each test."""
    _reset_otel()
    yield
    _reset_otel()


@pytest.fixture()
def in_memory_tracer():
    """Install an InMemorySpanExporter as OTel provider. Returns (tracer, exporter)."""
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


def _make_fake_memory(mid: int = 1) -> dict:
    return {
        "id": mid,
        "content": f"memory {mid}",
        "heat": 0.5,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.5,
    }


def _make_mock_storage() -> Any:
    """Return a minimal mock StorageEngine sufficient for tool tests."""
    storage = MagicMock()
    mems = [_make_fake_memory(1)]
    storage.search_memories_fts.return_value = mems
    storage.search_vectors.return_value = []
    storage.get_memory.return_value = mems[0]
    storage._now_iso.return_value = "2026-01-01T00:00:00"
    storage.update_memory_heat.return_value = None
    storage.update_memory_last_accessed.return_value = None
    return storage


def _make_mock_retriever() -> Any:
    """Return a minimal mock Retriever."""
    retriever = MagicMock()
    retriever.recall.return_value = [_make_fake_memory(1)]
    return retriever


def _make_mock_wiki() -> Any:
    """Return a minimal mock WikiStore."""
    wiki = MagicMock()
    wiki.query.return_value = []
    return wiki


def _call_recall_mcp_tool(query: str = "test query", max_results: int = 1, min_heat: float = 0.0):
    """Call the recall MCP tool function directly with mocked dependencies."""
    import yadgar._shared.runtime.state as _st
    from yadgar.core.server.tools.recall import recall as recall_fn

    mock_retriever = _make_mock_retriever()
    mock_storage = _make_mock_storage()
    mock_wiki = _make_mock_wiki()

    with (
        patch.object(_st, "_retriever", mock_retriever),
        patch.object(_st, "_storage", mock_storage),
        patch.object(_st, "_consolidation", None),
        patch.object(_st, "_thermo", None),
        patch.object(_st, "_cognitive_map", None),
        patch.object(_st, "_buffer", None),
        patch.object(_st, "_replay", None),
        patch.object(_st, "_wiki", mock_wiki),
        patch.object(_st, "_last_recalled_ids", {}),
        patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
        patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
    ):
        return recall_fn(
            query=query, max_results=max_results, min_heat=min_heat, directory="/tmp/test"
        )


def _call_wiki_query_mcp_tool(
    query: str = "test query",
    tags=None,
    category=None,
    max_results: int = 5,
):
    """Call the wiki_query MCP tool function directly with mocked dependencies."""
    import yadgar._shared.runtime.state as _st
    from yadgar.core.server.tools.wiki import wiki_query as wiki_query_fn

    mock_wiki = _make_mock_wiki()

    with (
        patch.object(_st, "_wiki", mock_wiki),
        patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
        patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
    ):
        return wiki_query_fn(
            query=query,
            tags=tags,
            category=category,
            max_results=max_results,
            directory="/tmp/test",
        )


def _make_settings_mock():
    """Return a minimal settings mock for Retriever tests."""
    from yadgar._shared.config import get_settings

    settings = get_settings()
    s = MagicMock(wraps=settings)
    s.QUERY_ROUTING_ENABLED = False
    s.QUERY_EXPANSION_ENABLED = False
    s.RERANKER_ENABLED = False
    s.HEAVY_RERANK_ENABLED = False
    s.NLI_RERANKING_ENABLED = False
    s.RETRIEVAL_PROFILE = "fast"
    s.CANDIDATE_POOL_MULTIPLIER = 2
    s.OPEN_DOMAIN_CANDIDATE_MULTIPLIER = 1.5
    s.BRANCH_BOOST_WEIGHT = 0.3
    s.POSTMORTEM_BOOST_FACTOR = 0.0
    s.POSTMORTEM_BOOST_KEYWORDS = ()
    s.ADVERSARIAL_DETECTION_ENABLED = False
    s.ADVERSARIAL_DIVERSITY_ENFORCEMENT = False
    s.COMPARISON_DUAL_SEARCH_ENABLED = False
    s.NLI_ONLY_FOR_OPEN_DOMAIN = True
    s.NLI_WEIGHT = 0.5
    s.MULTI_PASSAGE_RERANKING_ENABLED = False
    s.WRRF_VECTOR_WEIGHT = 1.0
    s.WRRF_FTS_WEIGHT = 1.0
    s.SPREADING_ACTIVATION_ENABLED = False
    s.TEMPORAL_RETRIEVAL_ENABLED = False
    s.CE_CONFIDENCE_THRESHOLD = 0.0
    s.CE_ENABLED = False
    s.OPEN_DOMAIN_FTS_BOOST = 1.6
    s.COMBMNZ_ENABLED = False
    s.FUSION_METHOD = "wrrf"
    s.FUSION_NORMALIZATION = "minmax"
    s.CONFIDENCE_GATE_ENABLED = False
    s.RERANKER_TOP_K = 5
    s.CROSS_ENCODER_TOP_K = 0
    return s


def _make_retriever_with_mocks(settings_override=None):
    """Build a Retriever with fully mocked sub-dependencies."""
    from yadgar._shared.retrieval.core import Retriever

    s = settings_override or _make_settings_mock()

    mock_storage = MagicMock()
    mock_storage.search_memories_fts.return_value = [_make_fake_memory(1)]
    mock_storage.search_memories_fts_scored.return_value = [(1, -0.5)]
    mock_storage.search_vectors.return_value = []
    mock_storage.get_memory.return_value = _make_fake_memory(1)
    mock_storage.search_profiles_fts.return_value = []
    mock_storage.search_beliefs_fts.return_value = []
    mock_storage.get_entity_by_id.return_value = None

    mock_embeddings = MagicMock()
    mock_embeddings.encode_query.return_value = [0.1] * 10

    mock_kg = MagicMock()
    mock_kg.get_related_entities.return_value = []

    return Retriever(
        storage=mock_storage,
        embeddings=mock_embeddings,
        knowledge_graph=mock_kg,
        settings=s,
    )


# ---------------------------------------------------------------------------
# 1. Bug A fix: yadgar_recall_duration_ms._count increments M times after M calls
# ---------------------------------------------------------------------------


class TestRecallDurationMetricBugA:
    def test_recall_duration_increments_by_m(self, recall_backend_bypass):
        """After M recall MCP tool calls, yadgar_recall_duration_ms._count must increment by M.

        Bug A: the metric was observed only inside a bare try/except at end of the
        function — an exception in the body left count=0. Fix: observation must be in
        a try/finally so it always fires.
        """
        from yadgar._shared.metrics import yadgar_recall_duration_ms

        before = _count_nolabel(yadgar_recall_duration_ms)
        M = 3
        for _ in range(M):
            _call_recall_mcp_tool()
        after = _count_nolabel(yadgar_recall_duration_ms)
        assert after - before == M, (
            f"Expected {M} new observations but got {after - before}. "
            "Bug A: yadgar_recall_duration_ms never incremented."
        )

    def test_recall_duration_increments_even_on_exception(self):
        """yadgar_recall_duration_ms must be observed even when recall() body raises.

        This is the discriminating test for Bug A: on master (before the try/finally fix),
        an exception propagated through the bare try/except at end of the happy path and
        the metric was NEVER observed. After the fix, the finally block fires regardless.
        """
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.metrics import yadgar_recall_duration_ms

        mock_storage = _make_mock_storage()

        before = _count_nolabel(yadgar_recall_duration_ms)

        # Phase 2a forward-only: recall() no longer calls _st._retriever.recall()
        # directly — it calls _forward_to_backend() first. Inject the failure at the
        # new call site so the recall() body still raises and we can prove the
        # yadgar_recall_duration_ms observation fires in the finally block regardless.
        def _boom(*_a, **_k):
            raise RuntimeError("injected error for Bug A test")

        with (
            patch.object(_st, "_storage", mock_storage),
            patch.object(_st, "_consolidation", None),
            patch.object(_st, "_thermo", None),
            patch.object(_st, "_cognitive_map", None),
            patch.object(_st, "_buffer", None),
            patch.object(_st, "_replay", None),
            patch.object(_st, "_last_recalled_ids", {}),
            patch("yadgar.core.server.tools.recall._forward_to_backend", _boom),
            patch("yadgar.core.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
            pytest.raises(RuntimeError, match="injected error for Bug A test"),
        ):
            from yadgar.core.server.tools.recall import recall as recall_fn

            recall_fn(query="test", max_results=1, directory="/tmp/test")

        after = _count_nolabel(yadgar_recall_duration_ms)
        assert after - before == 1, (
            f"yadgar_recall_duration_ms must be observed in finally block even on exception; "
            f"before={before}, after={after}. Bug A fix requires try/finally, not try/except."
        )


# ---------------------------------------------------------------------------
# 2. yadgar_recall_stage_ms stage observations (via Retriever directly)
# ---------------------------------------------------------------------------


class TestRecallStageMetrics:
    def test_embed_query_stage_observed(self):
        """yadgar_recall_stage_ms{stage='embed_query'} has at least 1 observation after recall."""
        from yadgar._shared.metrics import yadgar_recall_stage_ms

        before = _count_labeled(yadgar_recall_stage_ms, stage="embed_query")
        retriever = _make_retriever_with_mocks()
        retriever.recall("test query", max_results=1)
        after = _count_labeled(yadgar_recall_stage_ms, stage="embed_query")
        assert after > before, (
            f"Expected embed_query stage observation; before={before}, after={after}"
        )

    def test_bm25_stage_observed(self):
        """yadgar_recall_stage_ms{stage='bm25'} has at least 1 observation after recall."""
        from yadgar._shared.metrics import yadgar_recall_stage_ms

        before = _count_labeled(yadgar_recall_stage_ms, stage="bm25")
        retriever = _make_retriever_with_mocks()
        retriever.recall("test query", max_results=1)
        after = _count_labeled(yadgar_recall_stage_ms, stage="bm25")
        assert after > before, f"Expected bm25 stage observation; before={before}, after={after}"

    def test_hnsw_stage_observed(self):
        """yadgar_recall_stage_ms{stage='hnsw'} has at least 1 observation after recall."""
        from yadgar._shared.metrics import yadgar_recall_stage_ms

        before = _count_labeled(yadgar_recall_stage_ms, stage="hnsw")
        retriever = _make_retriever_with_mocks()
        retriever.recall("test query", max_results=1)
        after = _count_labeled(yadgar_recall_stage_ms, stage="hnsw")
        assert after > before, f"Expected hnsw stage observation; before={before}, after={after}"

    def test_rerank_final_stage_observed(self):
        """yadgar_recall_stage_ms{stage='rerank_final'} has at least 1 observation after recall."""
        from yadgar._shared.metrics import yadgar_recall_stage_ms

        before = _count_labeled(yadgar_recall_stage_ms, stage="rerank_final")
        retriever = _make_retriever_with_mocks()
        retriever.recall("test query", max_results=1)
        after = _count_labeled(yadgar_recall_stage_ms, stage="rerank_final")
        assert after > before, (
            f"Expected rerank_final stage observation; before={before}, after={after}"
        )

    def test_at_least_3_distinct_stages_observed(self):
        """At least 3 distinct stage names have observations after a recall call."""
        from yadgar._shared.metrics import yadgar_recall_stage_ms

        before_by_stage: dict[str, float] = {}
        for fam in yadgar_recall_stage_ms.collect():
            for s in fam.samples:
                if s.name.endswith("_count") and "stage" in s.labels:
                    before_by_stage[s.labels["stage"]] = s.value

        retriever = _make_retriever_with_mocks()
        retriever.recall("test query", max_results=1)

        observed_stages = set()
        for fam in yadgar_recall_stage_ms.collect():
            for s in fam.samples:
                if s.name.endswith("_count") and "stage" in s.labels:
                    stage = s.labels["stage"]
                    if s.value > before_by_stage.get(stage, 0.0):
                        observed_stages.add(stage)

        assert len(observed_stages) >= 3, (
            f"Expected >= 3 distinct stage observations, got {observed_stages}"
        )


# ---------------------------------------------------------------------------
# 3. retrieval.recall span emitted
# ---------------------------------------------------------------------------


class TestRecallSpanEmission:
    def test_retrieval_recall_span_emitted(self, in_memory_tracer):
        """A retrieval.recall span must be emitted when HippoRetriever.recall() runs.

        Audit's highest-leverage gap: @trace_span('retrieval.recall') was missing on recall().
        """
        _, exporter = in_memory_tracer

        retriever = _make_retriever_with_mocks()
        retriever.recall("test query", max_results=1)

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert "retrieval.recall" in span_names, (
            f"Expected 'retrieval.recall' span; found: {span_names}. "
            "Add @trace_span('retrieval.recall') to Retriever.recall() in retrieval/core.py."
        )


# ---------------------------------------------------------------------------
# 4. wiki.query span emitted
# ---------------------------------------------------------------------------


class TestWikiQuerySpanEmission:
    def test_wiki_query_span_emitted(self, in_memory_tracer):
        """A wiki.query span must be emitted when WikiStore.query() runs."""
        from yadgar._shared.wiki import WikiStore

        _, exporter = in_memory_tracer

        mock_storage = MagicMock()
        mock_storage.search_wiki_fts_scored.return_value = []
        mock_storage.search_wiki_vectors.return_value = []

        mock_embeddings = MagicMock()
        mock_embeddings.encode_query.return_value = None

        wiki = WikiStore(storage=mock_storage, embeddings=mock_embeddings)
        wiki.query("test query", max_results=3)

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert "wiki.query" in span_names, f"Expected 'wiki.query' span; found: {span_names}"


# ---------------------------------------------------------------------------
# 5. yadgar_wiki_query_duration_ms._count increments after M wiki_query calls
# ---------------------------------------------------------------------------


class TestWikiQueryDurationMetric:
    def test_wiki_query_duration_increments_by_m(self):
        """After M wiki_query MCP tool calls, yadgar_wiki_query_duration_ms._count == M."""
        from yadgar._shared.metrics import yadgar_wiki_query_duration_ms

        before = _count_nolabel(yadgar_wiki_query_duration_ms)
        M = 3
        for _ in range(M):
            _call_wiki_query_mcp_tool()
        after = _count_nolabel(yadgar_wiki_query_duration_ms)
        assert after - before == M, (
            f"Expected {M} new observations but got {after - before}. "
            "yadgar_wiki_query_duration_ms not wired to wiki_query MCP tool."
        )


# ---------------------------------------------------------------------------
# 6. NLI-off: yadgar_recall_stage_ms{stage="nli"} has zero observations
# ---------------------------------------------------------------------------


class TestNliOffNoObservation:
    def test_nli_stage_not_observed_when_disabled(self):
        """With NLI_RERANKING_ENABLED=false, nli stage must not appear in observations."""
        from yadgar._shared.metrics import yadgar_recall_stage_ms

        s = _make_settings_mock()
        s.NLI_RERANKING_ENABLED = False  # explicitly off

        nli_before = _count_labeled(yadgar_recall_stage_ms, stage="nli")

        retriever = _make_retriever_with_mocks(settings_override=s)
        retriever.recall("test query", max_results=1)

        nli_after = _count_labeled(yadgar_recall_stage_ms, stage="nli")
        assert nli_after == nli_before, (
            f"Expected no nli stage observations with NLI disabled; "
            f"before={nli_before}, after={nli_after}"
        )
