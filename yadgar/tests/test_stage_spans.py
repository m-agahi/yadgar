"""v5.100 — fine-grained OTEL stage spans across recall + write paths.

TDD: these tests are RED before the @trace_span stage decorators are added.

Goal (docs/plans / task v5.100): per-stage trace visibility in Tempo. Each
recall sub-stage (fts/vector/ppr/spreading/temporal/fusion) and each rerank
stage (heuristic/cross_encoder/nli/multi_passage/mmr/rules/metacognition) must
emit its OWN span, nested under the enclosing operation. Same for the write
path (compute_surprisal / should_store).

The nesting assertion (span shares the parent's trace_id AND references the
parent span_id) is the load-bearing one: a stage span that emits as an orphan
root passes a naive "does it emit" check but breaks the per-stage-under-recall
view that is the entire point. We assert PARENTAGE, not mere existence.

Run:
    export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1
    timeout --signal=KILL 400 python -m pytest yadgar/tests/test_stage_spans.py -q
"""

from __future__ import annotations

from collections import defaultdict
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# In-memory OTel harness (mirrors test_tracing.py::in_memory_tracer)
# ---------------------------------------------------------------------------


@pytest.fixture()
def span_exporter():
    """Install a fresh TracerProvider + InMemorySpanExporter as the global provider.

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


def _find(exporter, name):
    for s in exporter.get_finished_spans():
        if s.name == name:
            return s
    return None


def _assert_child_of(exporter, child_name: str, parent_name: str) -> None:
    """Assert child_name span is a real child of parent_name (same trace, parent ref)."""
    child = _find(exporter, child_name)
    parent = _find(exporter, parent_name)
    assert child is not None, f"missing span {child_name!r} in {_span_names(exporter)}"
    assert parent is not None, f"missing parent span {parent_name!r}"
    assert child.context.trace_id == parent.context.trace_id, (
        f"{child_name} trace_id != {parent_name} trace_id — orphaned span, "
        "will not nest under recall in Tempo."
    )
    assert child.parent is not None and child.parent.span_id == parent.context.span_id, (
        f"{child_name}.parent is not {parent_name} — not a direct child."
    )


# ---------------------------------------------------------------------------
# Recall scoring stages — one representative + full set of names
# ---------------------------------------------------------------------------


def _make_scoring_stub():
    from yadgar.retrieval.scoring import _ScoringMixin

    class _Stub(_ScoringMixin):
        def __init__(self):
            self._storage = MagicMock()
            self._settings = MagicMock()
            self._settings.QUERY_EXPANSION_ENABLED = False

        def ppr_retrieve(self, query, top_k):
            return []

        def spreading_activation(self, seeds, spread_factor=0.5, max_depth=2):
            return []

        def _comet_expand_query(self, query):
            return []

    return _Stub()


def _make_scores():
    return defaultdict(
        lambda: {"vector": 0.0, "fts": 0.0, "ppr": 0.0, "spread": 0.0, "temporal": 0.0}
    )


def test_ppr_stage_span_nested_under_parent(span_exporter):
    """_collect_ppr_scores emits retrieval.ppr nested under an outer retrieval.recall span."""
    from opentelemetry import trace

    stub = _make_scoring_stub()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("retrieval.recall"):
        stub._collect_ppr_scores("q", _make_scores(), None, 10)

    _assert_child_of(span_exporter, "retrieval.ppr", "retrieval.recall")


def test_spreading_stage_span_nested(span_exporter):
    from opentelemetry import trace

    stub = _make_scoring_stub()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("retrieval.recall"):
        stub._collect_spreading_scores(_make_scores(), None, [1, 2, 3])

    _assert_child_of(span_exporter, "retrieval.spreading", "retrieval.recall")


def test_fts_stage_span_emits(span_exporter):
    from yadgar.retrieval.scoring import FTSParams

    stub = _make_scoring_stub()
    stub._storage.search_memories_fts_scored.return_value = []
    params = FTSParams(
        query="q",
        enabled_signals=None,
        open_domain_subqueries=[],
        open_domain_mode=False,
        candidate_k=10,
        min_heat=0.0,
        branch_filter=None,
    )
    stub._collect_fts_scores(_make_scores(), params)
    assert "retrieval.fts" in _span_names(span_exporter)


def test_vector_stage_span_emits(span_exporter):
    stub = _make_scoring_stub()
    stub._embeddings = MagicMock()
    stub._embeddings.encode_query.return_value = None  # short-circuits KNN loop
    stub._collect_vector_scores("q", _make_scores(), None, [], 10, 0.0)
    assert "retrieval.vector" in _span_names(span_exporter)


def test_temporal_stage_span_emits(span_exporter):
    stub = _make_scoring_stub()
    stub._settings.TEMPORAL_RETRIEVAL_ENABLED = False
    # temporal collector may early-return on no temporal markers; span still opens
    stub._collect_temporal_scores("q", _make_scores(), 0.0, 10)
    assert "retrieval.temporal" in _span_names(span_exporter)


# ---------------------------------------------------------------------------
# Rerank stages
# ---------------------------------------------------------------------------


def test_rerank_stage_spans_nested(span_exporter):
    """A rerank stage (heuristic) emits retrieval.rerank.heuristic under retrieval.rerank."""
    from opentelemetry import trace

    from yadgar.retrieval.reranking import RerankContext, _RerankingMixin

    class _Stub(_RerankingMixin):
        def __init__(self):
            self._settings = MagicMock()
            self._settings.RERANKER_ENABLED = False  # short-circuit body, span still opens

    stub = _Stub()
    ctx = RerankContext(
        query="q",
        query_analysis={},
        query_embedding=None,
        profile={},
        profile_name="balanced",
        open_domain_mode=False,
        use_cross_encoder=False,
        max_results=5,
    )
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("retrieval.rerank"):
        stub._rerank_heuristic([], ctx)

    _assert_child_of(span_exporter, "retrieval.rerank.heuristic", "retrieval.rerank")


# ---------------------------------------------------------------------------
# Write path — compute_surprisal / should_store
# ---------------------------------------------------------------------------


def test_write_surprisal_span_emits(span_exporter):
    """WriteGate.compute_surprisal emits write.surprisal span."""
    from yadgar.predictive_coding import WriteGate

    gate = WriteGate.__new__(WriteGate)
    gate._storage = MagicMock()
    gate._embeddings = MagicMock()
    gate._settings = MagicMock()
    # Empty recent-memory set → early 0.8 return; the span still opens + closes.
    gate._storage.get_memories_for_directory.return_value = []

    result = gate.compute_surprisal("some content", "/tmp", [])
    assert isinstance(result, float)
    assert "write.surprisal" in _span_names(span_exporter)


# ---------------------------------------------------------------------------
# Drainer — one span per replayed record (drainer.apply), MUST NOT raise
# ---------------------------------------------------------------------------


def test_drainer_apply_span_emits_and_does_not_raise(span_exporter):
    """QueueDrainer._apply emits drainer.apply with op attr — and does not raise.

    Regression guard: the span helper must not double-enter the context manager
    (start_as_current_span is a single-use generator). A double __enter__ would
    raise RuntimeError on every drained record and DLQ every write.
    """
    from yadgar.file_queue.apply import _ApplyMixin

    applied = []

    class _StubDrainer(_ApplyMixin):
        def _apply_inner(self, record):
            applied.append(record)

    drainer = _StubDrainer()
    # Must not raise:
    drainer._apply({"op": "memorize", "payload": {"content": "x"}})

    assert applied == [{"op": "memorize", "payload": {"content": "x"}}]
    span = _find(span_exporter, "drainer.apply")
    assert span is not None, f"missing drainer.apply span in {_span_names(span_exporter)}"
    assert span.attributes.get("op") == "memorize"


# ---------------------------------------------------------------------------
# P2 (write + consolidation) — @observe boundary/stage spans emit (ADR-0034)
# ---------------------------------------------------------------------------


def test_drainer_apply_no_double_span_after_observe(span_exporter):
    """After P2, _apply_inner carries @observe(stage, drainer.apply_inner) but _apply
    keeps its MANUAL drainer.apply span — exactly ONE drainer.apply span, plus the
    nested drainer.apply_inner. Guards against the double-span trap (a decorated
    body-span opener would emit two drainer.apply spans).
    """
    from yadgar.file_queue.apply import _ApplyMixin

    # op="unknown" reaches the real (now @observe-decorated) _apply_inner's else
    # branch — a debug log, no tool import — so drainer.apply_inner span opens
    # while _apply keeps its manual drainer.apply span. Exactly one drainer.apply.
    drainer = _ApplyMixin()
    drainer._apply({"op": "unknown", "payload": {}})

    names = [s.name for s in span_exporter.get_finished_spans()]
    assert names.count("drainer.apply") == 1, f"expected exactly one drainer.apply, got {names}"
    _assert_child_of(span_exporter, "drainer.apply_inner", "drainer.apply")


def test_engram_allocate_boundary_span_emits(span_exporter):
    """EngramAllocator.allocate emits the engram.allocate boundary span."""
    from yadgar.engram import EngramAllocator

    alloc = EngramAllocator.__new__(EngramAllocator)
    alloc._storage = MagicMock()
    alloc._settings = MagicMock()
    alloc._num_slots = 4
    alloc._half_life = 6.0
    alloc._boost = 0.5
    alloc._storage.get_all_engram_slots.return_value = []
    alloc._storage.get_slot_occupancy.return_value = {}
    alloc._storage.get_memories_in_slot.return_value = []
    alloc._storage.get_engram_slot.return_value = None
    alloc._storage._now_iso.return_value = "2026-07-03T00:00:00+00:00"

    alloc.allocate(1)
    assert "engram.allocate" in _span_names(span_exporter)


def test_cognitive_map_record_transition_boundary_span_emits(span_exporter):
    """CognitiveMap.record_transition emits the cognitive_map.record_transition boundary span."""
    from yadgar.cognitive_map import CognitiveMap

    cm = CognitiveMap.__new__(CognitiveMap)
    cm._storage = MagicMock()
    cm._storage.get_transition.return_value = None
    cm._dirty = False

    cm.record_transition(1, 2)
    assert "cognitive_map.record_transition" in _span_names(span_exporter)


def test_kg_extract_entities_typed_boundary_span_emits(span_exporter):
    """KnowledgeGraph.extract_entities_typed emits the boundary span (write-path entity extract)."""
    from yadgar.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph.__new__(KnowledgeGraph)
    kg._storage = MagicMock()
    kg._settings = MagicMock()

    kg.extract_entities_typed("def foo(): pass", "/tmp")
    assert "knowledge_graph.extract_entities_typed" in _span_names(span_exporter)


def test_queue_enqueue_boundary_span_emits(span_exporter, tmp_path):
    """FileQueue.enqueue emits the queue.enqueue boundary span (durability write boundary)."""
    from yadgar.file_queue.queue import FileQueue

    q = FileQueue(base_dir=str(tmp_path))
    q.enqueue("memorize", {"content": "x"})
    assert "queue.enqueue" in _span_names(span_exporter)
