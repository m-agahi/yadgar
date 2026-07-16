"""finish-viz trace-replay Phase 3 — `trace_complete` SSE emit on tool completion.

When an MCP tool boundary finalizes, the tool wrapper (`_build_tool_wrappers`)
pushes a `trace_complete` event via `_push_event` so the viz "Traces" tab can
live-append the completed trace. The event carries {trace_id, tool, total_ms,
status}. The trace_id is read from the enclosing span (`get_current_trace_id`) —
an internal/test direct call with NO active trace pushes nothing.

These cover the emit layer that IS testable in-harness (event pushed / shape /
status / skip-when-no-trace). The real browser-SSE live-append is a user
smoke-check (no-browser-harness convention). The live p95/rate badges were DROPPED
(no per-stage Prometheus metrics exist) — nothing to test there.
"""

from __future__ import annotations

from collections import deque

import pytest

import yadgar._shared.runtime.state as _st
from yadgar.core.server._app import _build_tool_wrappers


@pytest.fixture
def _clean_event_queue(monkeypatch):
    """Isolate the process-global SSE ring buffer per test."""
    monkeypatch.setattr(_st, "_event_queue", deque(maxlen=500), raising=False)
    monkeypatch.setattr(_st, "_event_seq", 0, raising=False)
    return _st._event_queue


def _trace_events(queue):
    return [e for e in queue if e.get("event") == "trace_complete"]


def _wrappers(func):
    """Build the (sync, async) instrumented wrappers over a bare function."""
    return _build_tool_wrappers(func, func, lambda _r: 1)


def test_emit_on_success_with_active_trace(monkeypatch, _clean_event_queue):
    """A tool call under an active trace pushes one `trace_complete` (status=ok)."""
    import yadgar._shared.observability.tracing as _tr

    monkeypatch.setattr(_tr, "get_current_trace_id", lambda: "deadbeef" * 4)

    def my_tool(x):
        return {"ok": x}

    sync_wrapper, _async = _wrappers(my_tool)
    assert sync_wrapper(5) == {"ok": 5}

    events = _trace_events(_clean_event_queue)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "trace_complete"
    assert ev["trace_id"] == "deadbeef" * 4
    assert ev["tool"] == "my_tool"
    assert ev["status"] == "ok"
    assert isinstance(ev["total_ms"], float)
    assert ev["total_ms"] >= 0


def test_emit_on_error_sets_status_error(monkeypatch, _clean_event_queue):
    """A raising tool still pushes `trace_complete` with status=error, then re-raises."""
    import yadgar._shared.observability.tracing as _tr

    monkeypatch.setattr(_tr, "get_current_trace_id", lambda: "cafef00d" * 4)

    def boom():
        raise ValueError("nope")

    sync_wrapper, _async = _wrappers(boom)
    with pytest.raises(ValueError, match="nope"):
        sync_wrapper()

    events = _trace_events(_clean_event_queue)
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["tool"] == "boom"


def test_no_emit_without_active_trace(monkeypatch, _clean_event_queue):
    """No active trace (direct/internal call) → no `trace_complete` pushed."""
    import yadgar._shared.observability.tracing as _tr

    monkeypatch.setattr(_tr, "get_current_trace_id", lambda: None)

    def my_tool():
        return "x"

    sync_wrapper, _async = _wrappers(my_tool)
    sync_wrapper()
    assert _trace_events(_clean_event_queue) == []


def test_emit_is_best_effort_never_breaks_tool(monkeypatch, _clean_event_queue):
    """A failure inside the emit path must NOT affect the tool's return value."""
    import yadgar._shared.observability.tracing as _tr

    def _boom_trace_id():
        raise RuntimeError("tracing exploded")

    monkeypatch.setattr(_tr, "get_current_trace_id", _boom_trace_id)

    def my_tool():
        return "safe"

    sync_wrapper, _async = _wrappers(my_tool)
    assert sync_wrapper() == "safe"  # tool result unaffected
    assert _trace_events(_clean_event_queue) == []


@pytest.mark.anyio
async def test_async_wrapper_emits_on_completion(monkeypatch, _clean_event_queue):
    """The async wrapper (the FastMCP-registered path) also emits on completion."""
    import yadgar._shared.observability.tracing as _tr

    monkeypatch.setattr(_tr, "get_current_trace_id", lambda: "abcd1234" * 4)

    def my_tool(v):
        return v * 2

    _sync, async_wrapper = _wrappers(my_tool)
    result = await async_wrapper(7)
    assert result == 14
    events = _trace_events(_clean_event_queue)
    assert len(events) == 1
    assert events[0]["tool"] == "my_tool"
    assert events[0]["status"] == "ok"
