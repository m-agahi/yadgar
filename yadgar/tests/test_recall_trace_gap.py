"""v5.102 — close the recall trace "gap": group the post-memory fanout work
under named spans + batch the per-memory heat/last_accessed writes.

Context (Tempo trace pinpoint, warm 3a9165975e3487f9): the ~6.2s "unaccounted"
window between ``tool.recall`` (23s) and its child ``retrieval.recall`` (16.8s)
was NEVER an un-instrumented hole. It is the SIBLING children of
``retrieval.recall`` that run AFTER it returns, inside the fan-out tool body:

    wiki.query          326ms
    rpc.rerank.ce      5445ms   ← cross-type fusion CE (the CE-correlated cost)
    tail POST/get_mem   407ms   ← _apply_recall_side_effects per-memory writes

326 + 5445 + 407 = 6179 ≈ the measured 6179.6ms tail. A naive
``tool.recall − retrieval.recall`` subtraction ignores those siblings, so the
time only LOOKED unaccounted.

This module pins two things:

1. INSTRUMENTATION (grouping): the side-effects segment now emits a
   ``recall.side_effects`` span, and the fanout fusion emits ``recall.fanout.fuse``
   — so the next trace attributes the tail under a named node instead of loose
   siblings. We assert PARENTAGE (nests under the enclosing recall op), which is
   the load-bearing property (an orphan-root span passes a naive existence check
   but breaks the under-recall view).

2. WASTE FIX (result-preserving): the per-memory heat + last_accessed writes
   (2 sequential SurrealDB round-trips × N results) collapse into ONE batched
   UPDATE. The batch must produce byte-identical heat values (min(heat+0.1, 1.0))
   — speed only, zero quality/behaviour change.

Run:
    export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1
    timeout --signal=KILL 400 python -m pytest yadgar/tests/test_recall_trace_gap.py -q
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# In-memory OTel harness (mirrors test_stage_spans.py::span_exporter)
# ---------------------------------------------------------------------------


@pytest.fixture()
def span_exporter():
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
# 1a. side-effects span nests under the enclosing recall op
# ---------------------------------------------------------------------------


def test_side_effects_span_nested_under_recall(span_exporter):
    """_apply_recall_side_effects emits recall.side_effects under an outer span."""
    from opentelemetry import trace

    import yadgar.server._state as _st
    from yadgar.server.tools.recall import _apply_recall_side_effects

    # Neutralise optional side-effect subsystems so the body runs to completion
    # without a live storage / cognitive map.
    _st._thermo = None
    _st._cognitive_map = None
    _st._buffer = None
    _st._replay = None

    storage = MagicMock()
    storage._now_iso.return_value = "2026-07-03T00:00:00Z"

    merged = [{"id": 1, "heat": 0.5, "_source": "memory"}]

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("tool.recall"):
        _apply_recall_side_effects(merged, "q", storage)

    _assert_child_of(span_exporter, "recall.side_effects", "tool.recall")


# ---------------------------------------------------------------------------
# 1b. batched write path — ONE storage call, not 2N
# ---------------------------------------------------------------------------


def test_side_effects_batches_heat_writes(span_exporter):
    """Per-memory heat + last_accessed writes collapse into one batched call.

    Pre-fix: N × update_memory_heat + N × update_memory_last_accessed.
    Post-fix: a single boost_memories_access(ids, ts) call.
    """
    import yadgar.server._state as _st
    from yadgar.server.tools.recall import _apply_recall_side_effects

    _st._thermo = None
    _st._cognitive_map = None
    _st._buffer = None
    _st._replay = None

    storage = MagicMock()
    storage._now_iso.return_value = "2026-07-03T00:00:00Z"

    merged = [
        {"id": 1, "heat": 0.5, "_source": "memory"},
        {"id": 2, "heat": 0.95, "_source": "memory"},
        {"id": 3, "heat": 0.2, "_source": "wiki"},  # wiki row → skipped
    ]

    _apply_recall_side_effects(merged, "q", storage)

    # One batched write over the memory ids (wiki excluded), not per-row calls.
    storage.boost_memories_access.assert_called_once()
    called_ids = storage.boost_memories_access.call_args.args[0]
    assert set(called_ids) == {1, 2}, called_ids
    # The legacy per-row methods must NOT be used on the hot path anymore.
    assert storage.update_memory_heat.call_count == 0
    assert storage.update_memory_last_accessed.call_count == 0


def test_side_effects_preserves_heat_values(span_exporter):
    """Batched boost must produce identical in-dict heat: min(heat+0.1, 1.0)."""
    import yadgar.server._state as _st
    from yadgar.server.tools.recall import _apply_recall_side_effects

    _st._thermo = None
    _st._cognitive_map = None
    _st._buffer = None
    _st._replay = None

    storage = MagicMock()
    storage._now_iso.return_value = "2026-07-03T00:00:00Z"

    merged = [
        {"id": 1, "heat": 0.5, "_source": "memory"},
        {"id": 2, "heat": 0.95, "_source": "memory"},  # clamps to 1.0
    ]

    _apply_recall_side_effects(merged, "q", storage)

    assert merged[0]["heat"] == pytest.approx(0.6)
    assert merged[1]["heat"] == pytest.approx(1.0)
    assert merged[0]["last_accessed"] == "2026-07-03T00:00:00Z"
    assert merged[1]["last_accessed"] == "2026-07-03T00:00:00Z"


def test_boost_memories_access_empty_is_noop(span_exporter):
    """boost_memories_access([]) must not touch the DB (guard against empty IN [])."""
    from yadgar.storage.memory import _MemoryMixin

    class _Stub(_MemoryMixin):
        def __init__(self):
            self._q = MagicMock(return_value=[])

    stub = _Stub()
    stub.boost_memories_access([], "2026-07-03T00:00:00Z")
    stub._q.assert_not_called()


# ---------------------------------------------------------------------------
# 2. fanout fusion span — the ~5.4s cross-type CE pass gets a named node
# ---------------------------------------------------------------------------


def _make_mock_retriever(memories):
    r = MagicMock()
    r.recall.return_value = memories
    return r


def _make_mock_wiki(pages):
    w = MagicMock()
    w.query.return_value = pages
    return w


def test_fanout_fuse_span_emits_on_multi_provider(span_exporter):
    """The multi-provider fusion path emits recall.fanout.fuse (both pools non-empty).

    This is the fragile span — it only fires when BOTH memory and wiki pools have
    candidates (the `else` branch that runs cross-type CE fusion). With one pool
    empty the single-provider bypass runs and NO fuse span is expected.
    """
    from opentelemetry import trace

    import yadgar.server._state as _st
    from yadgar.server.tools.recall import _fanout_recall

    mem = {
        "id": 1,
        "content": "memory content 1",
        "heat": 0.6,
        "_retrieval_score": 0.9,
        "directory_context": "/tmp/test",
        "branch": "master",
        "tags": [],
    }
    wiki = {
        "id": 100,
        "slug": "overview",
        "title": "Wiki: overview",
        "content": "wiki content for overview",
        "_retrieval_score": 0.8,
        "directory_context": "/tmp/test",
        "branch": "master",
        "_source": "wiki",
    }
    retriever = _make_mock_retriever([mem])
    wiki_store = _make_mock_wiki([wiki])

    from unittest.mock import patch

    tracer = trace.get_tracer("test")
    with (
        patch.object(_st, "_retriever", retriever),
        patch.object(_st, "_wiki", wiki_store),
        tracer.start_as_current_span("tool.recall"),
    ):
        _fanout_recall(
            query="test",
            max_results=5,
            min_heat=0.0,
            directory="/tmp/test",
            current_branch="master",
            default_branch="master",
        )

    _assert_child_of(span_exporter, "recall.fanout.fuse", "tool.recall")


def test_fanout_fuse_span_absent_on_single_provider(span_exporter):
    """Single non-empty pool takes the bypass — NO fuse span (no double-CE)."""
    from unittest.mock import patch

    from opentelemetry import trace

    import yadgar.server._state as _st
    from yadgar.server.tools.recall import _fanout_recall

    mem = {
        "id": 1,
        "content": "memory content 1",
        "heat": 0.6,
        "_retrieval_score": 0.9,
        "directory_context": "/tmp/test",
        "branch": "master",
        "tags": [],
    }
    retriever = _make_mock_retriever([mem])

    tracer = trace.get_tracer("test")
    with (
        patch.object(_st, "_retriever", retriever),
        patch.object(_st, "_wiki", None),  # wiki pool empty → bypass
        tracer.start_as_current_span("tool.recall"),
    ):
        _fanout_recall(
            query="test",
            max_results=5,
            min_heat=0.0,
            directory="/tmp/test",
            current_branch="master",
            default_branch="master",
        )

    assert "recall.fanout.fuse" not in _span_names(span_exporter)
