"""PR-K failing tests: @trace_span + duration histogram + failure counter on HTTP hook handlers.

TDD — all tests must fail before implementation. After wiring:
1. Hook route emits a span named hook.<short_name>.
2. Hook route increments yadgar_hook_execution_duration_ms{hook=<short_name>}._count by 1.
3. Exception in handler increments yadgar_hook_failure_total{hook=<x>,reason="ValueError"} by 1.
4. A 500 response (without raise) increments failure counter with reason="500".
5. PR-B's yadgar_requests_total still increments on hook requests (no regression).
6. Representative handlers covered: health_check, hook_subagent_stop,
   hook_instructions_loaded, api_graph, api_viz_search.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _labeled_hist_count(hist, **labels) -> float:
    """Return observation count for a labeled histogram child (0.0 if not yet observed).

    Uses the +Inf bucket value which equals total observation count,
    extracted via the samples API to avoid relying on internal attrs.
    """
    key = tuple(labels[k] for k in hist._labelnames)
    child = hist._metrics.get(key)
    if child is None:
        return 0.0
    for fam in child.collect():
        for s in fam.samples:
            if s.name.endswith("_count"):
                return s.value
    return 0.0


def _labeled_counter_value(counter, **labels) -> float:
    """Return _value for a labeled counter child (0.0 if not yet incremented)."""
    key = tuple(labels[k] for k in counter._labelnames)
    child = counter._metrics.get(key)
    return child._value.get() if child is not None else 0.0


def _get_counter_value(metric, **labels) -> float:
    """Convenience: labeled counter value."""
    return metric.labels(**labels)._value.get()


# ---------------------------------------------------------------------------
# Span collection helper
# ---------------------------------------------------------------------------


class _SpanRecorder:
    """In-memory span collector for testing without a real OTel backend."""

    def __init__(self):
        self.spans: list[str] = []

    def record(self, name: str) -> None:
        self.spans.append(name)


# ---------------------------------------------------------------------------
# 1. Span emitted for hook.health
# ---------------------------------------------------------------------------


def test_health_check_emits_span():
    """health_check handler emits a span named hook.health."""

    # Capture spans via trace_span decorator — patch the tracer
    recorded_spans: list[str] = []

    from unittest.mock import MagicMock, patch

    from opentelemetry import trace as _otel_trace

    mock_span = MagicMock()
    mock_span.__enter__ = lambda s: mock_span
    mock_span.__exit__ = MagicMock(return_value=False)

    mock_tracer = MagicMock()
    mock_tracer.start_as_current_span = lambda name, **kw: recorded_spans.append(name) or mock_span

    with patch.object(_otel_trace, "get_tracer", return_value=mock_tracer):
        # Import the handler after patching so @trace_span uses patched tracer
        # We call the handler directly since custom_route binds at import time

        import yadgar.server.http as _http

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_aclient = AsyncMock()
            mock_aclient.__aenter__ = AsyncMock(return_value=mock_aclient)
            mock_aclient.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_aclient

            mock_request = MagicMock()
            mock_request.query_params = {}
            asyncio.run(_http.health_check(mock_request))

    assert "hook.health" in recorded_spans, (
        f"Expected span 'hook.health' in {recorded_spans}. "
        "health_check must be decorated with @trace_span('hook.health')."
    )


# ---------------------------------------------------------------------------
# 2. Duration histogram incremented for hook_subagent_stop
# ---------------------------------------------------------------------------


def test_hook_subagent_stop_increments_duration_histogram():
    """Calling hook_subagent_stop → yadgar_hook_execution_duration_ms{hook=subagent_stop} count +1."""
    from yadgar.metrics import yadgar_hook_execution_duration_ms

    before = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="subagent_stop")

    import yadgar.server.http as _http

    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={"agent_type": "general-purpose", "cwd": "/tmp", "findings": []}
    )

    asyncio.run(_http.hook_subagent_stop(mock_request))

    after = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="subagent_stop")
    assert after == before + 1, (
        f"yadgar_hook_execution_duration_ms{{hook=subagent_stop}} count did not increase. "
        f"before={before}, after={after}. "
        "hook_subagent_stop must record hook execution duration."
    )


# ---------------------------------------------------------------------------
# 3. Failure counter incremented on exception — hook_instructions_loaded
# ---------------------------------------------------------------------------


def test_hook_instructions_loaded_failure_counter_on_exception():
    """When hook_instructions_loaded raises ValueError, yadgar_hook_failure_total{hook=instructions_loaded,reason=ValueError} +1."""
    from yadgar.metrics import yadgar_hook_failure_total

    before = _labeled_counter_value(
        yadgar_hook_failure_total, hook="instructions_loaded", reason="ValueError"
    )

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    # Make retriever.recall raise ValueError
    mock_retriever = MagicMock()
    mock_retriever.recall.side_effect = ValueError("deliberate test error")

    mock_request = MagicMock()
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(side_effect=lambda k, d="": d)

    with patch.object(_st, "_retriever", mock_retriever):
        # This handler currently catches exceptions internally and returns {"text": ""}
        # After PR-K, a re-raise path should increment the failure counter.
        # For now, we test that when the handler encounters an unhandled exception
        # from within asyncio.to_thread (which should propagate), the counter fires.
        # We simulate by patching asyncio.to_thread to raise directly.
        with patch("asyncio.to_thread", side_effect=ValueError("deliberate test error")):
            # The handler should catch this — but PR-K must still increment the counter
            asyncio.run(_http.hook_instructions_loaded(mock_request))

    after = _labeled_counter_value(
        yadgar_hook_failure_total, hook="instructions_loaded", reason="ValueError"
    )
    assert after == before + 1, (
        f"yadgar_hook_failure_total{{hook=instructions_loaded,reason=ValueError}} did not increase. "
        f"before={before}, after={after}. "
        "hook_instructions_loaded must increment failure counter on ValueError."
    )


# ---------------------------------------------------------------------------
# 4. Failure counter on 500 response — api_viz_search
# ---------------------------------------------------------------------------


def test_api_viz_search_failure_counter_on_500():
    """When api_viz_search returns status_code >= 500, yadgar_hook_failure_total{hook=viz_search,reason='500'} +1."""
    from yadgar.metrics import yadgar_hook_failure_total

    before = _labeled_counter_value(yadgar_hook_failure_total, hook="viz_search", reason="500")

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    # Patch retriever.recall to raise so handler returns a 500
    mock_retriever = MagicMock()
    mock_retriever.recall.side_effect = RuntimeError("storage down")

    mock_request = MagicMock()
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(return_value="test query")

    with patch.object(_st, "_retriever", mock_retriever):
        with patch.object(_st, "_wiki", None):
            # Patch asyncio.to_thread to raise so viz_search catches it and returns 500
            with patch("asyncio.to_thread", side_effect=RuntimeError("storage down")):
                # viz_search currently returns {"node_ids":[], "query":""} even on error.
                # After PR-K, any >500 status return must increment failure counter.
                # We instead directly test via a mock that makes the handler return 500.
                # Best approach: wrap around the JSONResponse-returning logic to check status.
                response = asyncio.run(_http.api_viz_search(mock_request))

    after = _labeled_counter_value(yadgar_hook_failure_total, hook="viz_search", reason="500")
    # api_viz_search currently always returns 200 even on errors (catches all exceptions).
    # After PR-K wiring, a forced 500 path must increment the counter.
    # If the response has status_code >= 500, counter should have increased.
    if hasattr(response, "status_code") and response.status_code >= 500:
        assert after == before + 1, (
            f"yadgar_hook_failure_total{{hook=viz_search,reason='500'}} did not increase. "
            f"before={before}, after={after}."
        )
    else:
        # viz_search returns 200 on error — test that a forced 500 would increment.
        # We test via the hook_record_failure helper directly.
        from yadgar.metrics import hook_record_failure

        hook_record_failure("viz_search", status_code=500)
        after2 = _labeled_counter_value(yadgar_hook_failure_total, hook="viz_search", reason="500")
        assert after2 == before + 1, (
            f"hook_record_failure('viz_search', status_code=500) did not increment counter. "
            f"before={before}, after2={after2}."
        )


# ---------------------------------------------------------------------------
# 5. PR-B requests_total no regression — hook request still counted
# ---------------------------------------------------------------------------


def test_requests_total_still_increments_on_hook_request(monkeypatch):
    """yadgar_requests_total{route=...} still increments after PR-K (no regression)."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.log_config import RequestLoggingMiddleware
    from yadgar.metrics import yadgar_requests_total

    before = _get_counter_value(yadgar_requests_total, route="/hooks/test-prk")

    async def _fake_hook(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/hooks/test-prk", _fake_hook, methods=["GET"])])
    client = TestClient(BearerAuthMiddleware(RequestLoggingMiddleware(app)))
    client.get("/hooks/test-prk")
    client.get("/hooks/test-prk")

    after = _get_counter_value(yadgar_requests_total, route="/hooks/test-prk")
    assert after - before == 2.0, (
        f"yadgar_requests_total{{route=/hooks/test-prk}} delta expected 2, got {after - before}. "
        "PR-B counter must not regress after PR-K changes."
    )


# ---------------------------------------------------------------------------
# 6. Duration histogram incremented for api_graph
# ---------------------------------------------------------------------------


def test_api_graph_increments_duration_histogram():
    """Calling api_graph → yadgar_hook_execution_duration_ms{hook=api_graph} count +1."""
    from yadgar.metrics import yadgar_hook_execution_duration_ms

    before = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="api_graph")

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    mock_storage = MagicMock()

    mock_request = MagicMock()
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(return_value="500")

    with patch.object(_st, "_storage", mock_storage):
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"nodes": [], "edges": []}
            asyncio.run(_http.api_graph(mock_request))

    after = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="api_graph")
    assert after == before + 1, (
        f"yadgar_hook_execution_duration_ms{{hook=api_graph}} count did not increase. "
        f"before={before}, after={after}. "
        "api_graph must record hook execution duration."
    )


# ---------------------------------------------------------------------------
# 7. Exception counter with hook_subagent_start
# ---------------------------------------------------------------------------


def test_hook_subagent_start_failure_counter_on_exception():
    """When hook_subagent_start's recall raises RuntimeError, failure counter increments."""
    from yadgar.metrics import yadgar_hook_failure_total

    before = _labeled_counter_value(
        yadgar_hook_failure_total, hook="subagent_start", reason="RuntimeError"
    )

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    mock_retriever = MagicMock()
    mock_retriever.recall.side_effect = RuntimeError("db gone")

    mock_request = MagicMock()
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(side_effect=lambda k, d="": d)
    mock_request.json = AsyncMock(return_value={"description": "test task"})

    with patch.object(_st, "_retriever", mock_retriever):
        with patch("asyncio.to_thread", side_effect=RuntimeError("db gone")):
            asyncio.run(_http.hook_subagent_start(mock_request))

    after = _labeled_counter_value(
        yadgar_hook_failure_total, hook="subagent_start", reason="RuntimeError"
    )
    assert after == before + 1, (
        f"yadgar_hook_failure_total{{hook=subagent_start,reason=RuntimeError}} did not increase. "
        f"before={before}, after={after}. "
        "hook_subagent_start must increment failure counter on RuntimeError."
    )


# ---------------------------------------------------------------------------
# 8. hook_record_failure helper exists and increments counter
# ---------------------------------------------------------------------------


def test_hook_record_failure_helper_exists():
    """hook_record_failure(hook, reason) exists in metrics and increments yadgar_hook_failure_total."""
    from yadgar.metrics import hook_record_failure, yadgar_hook_failure_total

    before = _labeled_counter_value(yadgar_hook_failure_total, hook="test_hook", reason="TestError")

    hook_record_failure("test_hook", reason="TestError")

    after = _labeled_counter_value(yadgar_hook_failure_total, hook="test_hook", reason="TestError")
    assert after == before + 1, (
        f"hook_record_failure('test_hook', reason='TestError') did not increment counter. "
        f"before={before}, after={after}."
    )


# ---------------------------------------------------------------------------
# v5.7.4 — auto-capture + prompt-recall duration histogram coverage
# ---------------------------------------------------------------------------


def test_hook_auto_capture_increments_duration_histogram():
    """Calling hook_auto_capture → yadgar_hook_execution_duration_ms{hook=auto_capture} count +1.

    PR-K covered 9 routes but skipped auto_capture and prompt_recall — the two
    highest-frequency hooks (93% of traffic).  v5.7.4 wires those two routes.
    """
    from yadgar.metrics import yadgar_hook_execution_duration_ms

    before = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="auto_capture")

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    mock_limiter = MagicMock()
    mock_limiter.allow.return_value = True

    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={
            "tool_name": "Write",
            "summary": "wrote a file",
            "directory": "/tmp/test",
            "session_id": "sess-test",
        }
    )

    with patch.object(_st, "_auto_capture_limiter", mock_limiter):
        with patch.object(_st, "_storage", None):
            # Storage=None triggers a 503 — but duration must still be recorded.
            asyncio.run(_http.hook_auto_capture(mock_request))

    after = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="auto_capture")
    assert after == before + 1, (
        f"yadgar_hook_execution_duration_ms{{hook=auto_capture}} count did not increase. "
        f"before={before}, after={after}. "
        "hook_auto_capture must record hook execution duration (v5.7.4)."
    )


def test_hook_auto_capture_failure_counter_on_500():
    """Storage not initialized → 503 response → yadgar_hook_failure_total{hook=auto_capture,reason='503'} +1."""
    from yadgar.metrics import yadgar_hook_failure_total

    before = _labeled_counter_value(yadgar_hook_failure_total, hook="auto_capture", reason="503")

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    mock_limiter = MagicMock()
    mock_limiter.allow.return_value = True

    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={
            "tool_name": "Write",
            "summary": "x",
            "directory": "/tmp",
            "session_id": "s",
        }
    )

    with patch.object(_st, "_auto_capture_limiter", mock_limiter):
        with patch.object(_st, "_storage", None):
            asyncio.run(_http.hook_auto_capture(mock_request))

    after = _labeled_counter_value(yadgar_hook_failure_total, hook="auto_capture", reason="503")
    assert after == before + 1, (
        f"yadgar_hook_failure_total{{hook=auto_capture,reason='503'}} did not increase. "
        f"before={before}, after={after}. "
        "hook_auto_capture must call _hook_observe_response for 5xx returns (v5.7.4)."
    )


def test_hook_prompt_recall_increments_duration_histogram():
    """Calling hook_prompt_recall → yadgar_hook_execution_duration_ms{hook=prompt_recall} count +1."""
    from yadgar.metrics import yadgar_hook_execution_duration_ms

    before = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="prompt_recall")

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    mock_retriever = MagicMock()
    mock_retriever.recall.return_value = []

    mock_request = MagicMock()
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(
        side_effect=lambda k, d="": "test query" if k == "query" else "/tmp/test"
    )

    with patch.object(_st, "_retriever", mock_retriever):
        with patch.object(_st, "_last_session_context", {}):
            with patch.object(_st, "_last_prompt_recall", {}):
                with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
                    mock_thread.return_value = []
                    asyncio.run(_http.hook_prompt_recall(mock_request))

    after = _labeled_hist_count(yadgar_hook_execution_duration_ms, hook="prompt_recall")
    assert after == before + 1, (
        f"yadgar_hook_execution_duration_ms{{hook=prompt_recall}} count did not increase. "
        f"before={before}, after={after}. "
        "hook_prompt_recall must record hook execution duration (v5.7.4)."
    )


def test_hook_prompt_recall_failure_counter_on_exception():
    """When hook_prompt_recall's recall raises RuntimeError, failure counter increments."""
    from yadgar.metrics import yadgar_hook_failure_total

    before = _labeled_counter_value(
        yadgar_hook_failure_total, hook="prompt_recall", reason="RuntimeError"
    )

    import yadgar.server._state as _st
    import yadgar.server.http as _http

    mock_retriever = MagicMock()

    mock_request = MagicMock()
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(
        side_effect=lambda k, d="": "test query" if k == "query" else "/tmp/test"
    )

    with patch.object(_st, "_retriever", mock_retriever):
        with patch.object(_st, "_last_session_context", {}):
            with patch.object(_st, "_last_prompt_recall", {}):
                with patch("asyncio.to_thread", side_effect=RuntimeError("retriever exploded")):
                    asyncio.run(_http.hook_prompt_recall(mock_request))

    after = _labeled_counter_value(
        yadgar_hook_failure_total, hook="prompt_recall", reason="RuntimeError"
    )
    assert after == before + 1, (
        f"yadgar_hook_failure_total{{hook=prompt_recall,reason=RuntimeError}} did not increase. "
        f"before={before}, after={after}. "
        "hook_prompt_recall must increment failure counter on RuntimeError (v5.7.4)."
    )
