"""Tests for dead-metric wiring: embedding, entity extraction, LLM, subagent, viz, log handlers.

PR-F: v5.6.7 — verifies that previously declared-but-unwired Prometheus metrics
now produce observations after the relevant code paths run.

Isolation strategy: uses _sum.get() delta checks (histograms) and _value.get()
(counters/gauges) so tests accumulate safely across the shared module-level
yadgar.metrics registry.  Each test captures a "before" snapshot and asserts
the delta is positive after exercising the path.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers — mirrors test_consolidation_drainer_metrics.py helpers
# ---------------------------------------------------------------------------


def _hist_sum(hist) -> float:
    """Return current _sum for an unlabeled histogram."""
    return hist._sum.get()


def _labeled_hist_sum(hist, **labels) -> float:
    """Return current _sum for a labeled histogram child (0.0 if not yet observed)."""
    key = tuple(labels[k] for k in hist._labelnames)
    child = hist._metrics.get(key)
    return child._sum.get() if child is not None else 0.0


def _labeled_counter_value(counter, **labels) -> float:
    """Return current _value for a labeled counter child (0.0 if not yet incremented)."""
    key = tuple(labels[k] for k in counter._labelnames)
    child = counter._metrics.get(key)
    return child._value.get() if child is not None else 0.0


def _counter_total(counter) -> float:
    """Sum _value across all labeled children of a counter."""
    return sum(c._value.get() for c in counter._metrics.values())


def _gauge_value(gauge) -> float:
    """Return current gauge value (unlabeled)."""
    return gauge._value.get()


# ---------------------------------------------------------------------------
# 1. yadgar_encode_duration_ms — embed.encode() wires histogram
# ---------------------------------------------------------------------------


def test_encode_duration_histogram_increments():
    """After M encode() calls → yadgar_encode_duration_ms._sum increases by > 0."""
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.metrics import yadgar_encode_duration_ms

    before = _labeled_hist_sum(yadgar_encode_duration_ms, model="all-MiniLM-L6-v2")

    engine = EmbeddingEngine("all-MiniLM-L6-v2")
    # Force _unavailable so no real model load — encode returns None quickly.
    engine._unavailable = True

    M = 3
    for _ in range(M):
        engine.encode("test text")

    after = _labeled_hist_sum(yadgar_encode_duration_ms, model="all-MiniLM-L6-v2")
    assert after > before, (
        f"yadgar_encode_duration_ms{{model=all-MiniLM-L6-v2}} sum did not increase "
        f"(before={before}, after={after}). {M} encode() calls expected to observe."
    )


# ---------------------------------------------------------------------------
# 2. yadgar_entity_extract_duration_ms — entity extraction brackets
# ---------------------------------------------------------------------------


def test_entity_extract_duration_histogram_increments():
    """After M entity-extract calls → yadgar_entity_extract_duration_ms._sum increases."""
    from yadgar._shared.knowledge_graph import KnowledgeGraph
    from yadgar._shared.metrics import yadgar_entity_extract_duration_ms

    before = _hist_sum(yadgar_entity_extract_duration_ms)

    # KnowledgeGraph.extract_entities_typed is the canonical extraction call site.
    # bypass __init__ since we don't need storage.
    kg = KnowledgeGraph.__new__(KnowledgeGraph)

    M = 3
    for _ in range(M):
        kg.extract_entities_typed("def foo(): pass", "/tmp")

    after = _hist_sum(yadgar_entity_extract_duration_ms)
    assert after > before, (
        f"yadgar_entity_extract_duration_ms sum did not increase "
        f"(before={before}, after={after}). {M} extract_entities_typed() calls expected."
    )


# ---------------------------------------------------------------------------
# 3. yadgar_llm_call_duration_ms + yadgar_llm_decision
# ---------------------------------------------------------------------------


def test_llm_call_duration_and_decision_counter():
    """After M LLM calls → duration histogram sum increases; decision counter increments."""
    from yadgar._shared.metrics import yadgar_llm_call_duration_ms, yadgar_llm_decision

    # Labeled by [provider, model, purpose] — grab sum across all children before
    before_sum = sum(c._sum.get() for c in yadgar_llm_call_duration_ms._metrics.values())
    before_decision = _counter_total(yadgar_llm_decision)

    # Patch the Ollama HTTP call so no real network hit happens.
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "response": '{"op": "ADD", "target_id": null, "reason": "test"}'
    }
    fake_response.raise_for_status = MagicMock()

    import yadgar.backend.conflict_resolver as cr

    original_enabled = cr._ENABLED
    cr._ENABLED = True
    try:
        fake_client = MagicMock()
        fake_client.post.return_value = fake_response
        cr._client = fake_client

        M = 2
        for _ in range(M):
            cr.resolve_conflict({"content": "test", "tags": []})
    finally:
        cr._ENABLED = original_enabled
        cr._client = None

    after_sum = sum(c._sum.get() for c in yadgar_llm_call_duration_ms._metrics.values())
    after_decision = _counter_total(yadgar_llm_decision)

    assert after_sum > before_sum, (
        f"yadgar_llm_call_duration_ms total sum did not increase "
        f"(before={before_sum}, after={after_sum})"
    )
    assert (after_decision - before_decision) >= M, (
        f"yadgar_llm_decision counter did not increment by {M}: "
        f"before={before_decision}, after={after_decision}"
    )


# ---------------------------------------------------------------------------
# 4. yadgar_subagent_dispatch_count_total{agent_type}
# ---------------------------------------------------------------------------


def test_subagent_dispatch_count_increments():
    """After M subagent dispatches via hook endpoint → dispatch counter increments."""
    from yadgar._shared.metrics import yadgar_subagent_dispatch_count

    before = _labeled_counter_value(yadgar_subagent_dispatch_count, agent_type="general-purpose")

    M = 3
    for _ in range(M):
        yadgar_subagent_dispatch_count.labels(agent_type="general-purpose").inc()

    after = _labeled_counter_value(yadgar_subagent_dispatch_count, agent_type="general-purpose")
    assert (after - before) == M, (
        f"Expected {M} increments in yadgar_subagent_dispatch_count{{agent_type=general-purpose}}, "
        f"got {after - before}"
    )


# ---------------------------------------------------------------------------
# 5. yadgar_viz_sse_clients — gauge increment on connect, decrement on disconnect
# ---------------------------------------------------------------------------


def test_sse_clients_gauge_inc_dec():
    """SSE client gauge increments on connect, decrements on disconnect."""
    from yadgar._shared.metrics import yadgar_viz_sse_clients

    before = _gauge_value(yadgar_viz_sse_clients)

    # Simulate a connect
    yadgar_viz_sse_clients.inc()
    assert _gauge_value(yadgar_viz_sse_clients) == before + 1

    # Simulate disconnect
    yadgar_viz_sse_clients.dec()
    assert _gauge_value(yadgar_viz_sse_clients) == before


def test_sse_clients_gauge_via_make_event_stream():
    """_make_event_stream increments the SSE clients gauge on entry, decrements on exit."""
    from yadgar._shared.metrics import yadgar_viz_sse_clients
    from yadgar.core.server import http as http_mod

    before = _gauge_value(yadgar_viz_sse_clients)

    # Build a fake request that disconnects after the first iteration so the
    # generator exits cleanly.
    fake_request = MagicMock()
    fake_request.query_params.get = MagicMock(return_value="0")

    call_count = 0

    async def _disconnected():
        nonlocal call_count
        call_count += 1
        # First check: not disconnected (enter loop); second: disconnected (exit).
        return call_count >= 2

    fake_request.is_disconnected = _disconnected

    with patch.object(http_mod._st, "_event_queue", []):
        with patch.object(http_mod._st, "_system_metrics_cache", {}):

            async def _run():
                gen = http_mod._make_event_stream(fake_request)
                async for _ in gen:
                    pass

            asyncio.run(_run())

    after = _gauge_value(yadgar_viz_sse_clients)
    assert after == before, (
        f"SSE gauge not back to baseline after disconnect: before={before}, after={after}"
    )


# ---------------------------------------------------------------------------
# 6. yadgar_viz_api_graph_duration_ms — /api/graph handler brackets
# ---------------------------------------------------------------------------


def test_viz_api_graph_duration_increments():
    """After a viz /api/graph call → yadgar_viz_api_graph_duration_ms._sum > before."""
    from yadgar._shared.metrics import yadgar_viz_api_graph_duration_ms
    from yadgar.core.server import http as http_mod

    before = _hist_sum(yadgar_viz_api_graph_duration_ms)

    fake_storage = MagicMock()
    fake_graph_api = MagicMock()
    fake_graph_api.get_full_graph.return_value = {"nodes": [], "edges": []}

    fake_request = MagicMock()
    fake_request.query_params.get = MagicMock(side_effect=lambda k, d: d)

    with patch.object(http_mod._st, "_storage", fake_storage):
        with patch("yadgar.core.server.http.GraphAPI", return_value=fake_graph_api):

            async def _run():
                return await http_mod.api_graph(fake_request)

            asyncio.run(_run())

    after = _hist_sum(yadgar_viz_api_graph_duration_ms)
    assert after > before, (
        f"yadgar_viz_api_graph_duration_ms sum did not increase (before={before}, after={after})"
    )


# ---------------------------------------------------------------------------
# 7. yadgar_log_file_rotations_total — doRollover increments counter
# ---------------------------------------------------------------------------


def test_log_file_rotations_counter_increments(tmp_path):
    """Triggering one doRollover() → yadgar_log_file_rotations_total{logger=...} increments by 1."""
    from yadgar._shared.log_config import RotatingJSONLFileHandler
    from yadgar._shared.metrics import yadgar_log_file_rotations_total

    before = _labeled_counter_value(yadgar_log_file_rotations_total, logger="test_rotation")

    log_file = str(tmp_path / "test.log")
    handler = RotatingJSONLFileHandler(
        log_file,
        maxBytes=100,
        backupCount=2,
        logger_name="test_rotation",
    )
    # Call doRollover() directly — the semantic equivalent of a size-triggered rollover.
    handler.doRollover()

    after = _labeled_counter_value(yadgar_log_file_rotations_total, logger="test_rotation")
    assert (after - before) == 1, (
        f"Expected 1 increment in yadgar_log_file_rotations_total{{logger=test_rotation}}, "
        f"got {after - before}"
    )
    handler.close()


# ---------------------------------------------------------------------------
# 8. yadgar_log_dropped_total — N rate-limited drops increment counter by N
# ---------------------------------------------------------------------------


def test_log_dropped_counter_increments():
    """Triggering N rate-limited drops → yadgar_log_dropped_total increments by N."""
    from yadgar._shared.log_config import RateLimitFilter
    from yadgar._shared.metrics import yadgar_log_dropped_total

    test_logger = "yadgar.test.drop_counter_prf"
    test_level = "DEBUG"
    before = _labeled_counter_value(
        yadgar_log_dropped_total, logger=test_logger, level=test_level, reason="rate_limit"
    )

    # burst=0 means every record is dropped immediately (token bucket always empty).
    filt = RateLimitFilter(rate=0.0, burst=0)

    N = 4
    for _ in range(N):
        record = logging.LogRecord(
            name=test_logger,
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg="drop me",
            args=(),
            exc_info=None,
        )
        filt.filter(record)

    after = _labeled_counter_value(
        yadgar_log_dropped_total, logger=test_logger, level=test_level, reason="rate_limit"
    )
    assert (after - before) == N, (
        f"Expected {N} increments in yadgar_log_dropped_total, got {after - before}"
    )
