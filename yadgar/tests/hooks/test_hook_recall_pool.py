"""#81 freeze fix: agent-lifecycle hook recalls run in a BOUNDED thread pool.

The freeze: subagent-start/prompt-recall hooks call retriever.recall via a 2s
wait_for; the underlying thread is uncancellable, so a slow recall runs past the
timeout. On a 1-CPU core, a burst of hooks piled up unbounded GIL-holding threads
→ event-loop starvation → /health/live freeze → P0 SIGKILL. Capping the pool
makes the cascade impossible.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def test_hook_recall_pool_is_bounded():
    from yadgar.core.server.http import _HOOK_RECALL_POOL, _HOOK_RECALL_POOL_WORKERS

    assert isinstance(_HOOK_RECALL_POOL, ThreadPoolExecutor)
    # v5.95 (#81 residual): 2 -> 1 to halve loop-CPU competition on the --cpus-1 core.
    # ADR-0077: 1 -> 2 — post-#166 the hook recall is a forwarded HTTP wait (idle
    # thread), not a GIL-holding in-core recall; pool=1 structurally starved the
    # second of every concurrent session pair (measured 32-52% timeout rate).
    assert _HOOK_RECALL_POOL_WORKERS == 2
    assert _HOOK_RECALL_POOL._max_workers == 2


def test_recall_runs_on_bounded_hook_pool_not_default_executor():
    """The recall must run on a 'hook-recall' pool thread, proving it's NOT on the
    loop and NOT on asyncio.to_thread's unbounded default executor."""
    from yadgar.core.server import http as _http

    seen: dict[str, str] = {}

    class FakeRetriever:
        def recall(self, *_a, **_k):
            seen["thread"] = threading.current_thread().name
            return ["ok"]

    res = asyncio.run(_http._recall_with_timeout(FakeRetriever(), "prompt-recall", "q"))
    assert res == ["ok"]
    assert seen["thread"].startswith("hook-recall"), seen


def test_recall_returns_none_on_timeout(monkeypatch):
    """Slow recall past the budget → wait_for returns None (handler treats as empty)."""
    from yadgar.core.server import http as _http

    class _FastTimeoutSettings:
        HOOK_RECALL_TIMEOUT_S = 0.2

    monkeypatch.setattr("yadgar._shared.config.get_settings", lambda: _FastTimeoutSettings())

    class SlowRetriever:
        def recall(self, *_a, **_k):
            time.sleep(1.0)  # exceeds the 0.2s budget
            return ["late"]

    res = asyncio.run(_http._recall_with_timeout(SlowRetriever(), "subagent-start", "q"))
    assert res is None


def test_pool_caps_concurrent_recall_threads():
    """Even with more concurrent calls than workers, at most _WORKERS recalls run
    at once — the leaked-thread cascade is bounded."""
    from yadgar.core.server import http as _http

    live = 0
    peak = 0
    lock = threading.Lock()

    class CountingRetriever:
        def recall(self, *_a, **_k):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.3)
            with lock:
                live -= 1
            return ["ok"]

    async def _fire():
        r = CountingRetriever()
        await asyncio.gather(
            *[_http._recall_with_timeout(r, "prompt-recall", "q") for _ in range(6)]
        )

    asyncio.run(_fire())
    assert peak <= _http._HOOK_RECALL_POOL_WORKERS, f"peak={peak} exceeded pool cap"


def test_concurrent_session_pair_both_complete(monkeypatch):
    """ADR-0077: with pool=2, the second of a concurrent hook pair no longer
    starves — both recalls complete within their own (relaxed) budget instead
    of the second timing out while queued behind the first."""
    from yadgar.core.server import http as _http

    class _RelaxedSettings:
        HOOK_RECALL_TIMEOUT_S = 1.0

    monkeypatch.setattr("yadgar._shared.config.get_settings", lambda: _RelaxedSettings())

    class SlowishRetriever:
        def recall(self, *_a, **_k):
            time.sleep(0.6)  # pair sums to 1.2s > budget if serialized on pool=1
            return ["ok"]

    async def _fire_pair():
        r = SlowishRetriever()
        return await asyncio.gather(
            _http._recall_with_timeout(r, "prompt-recall", "q1"),
            _http._recall_with_timeout(r, "subagent-start", "q2"),
        )

    results = asyncio.run(_fire_pair())
    assert results == [["ok"], ["ok"]], (
        f"second-of-pair starved: {results} — pool must serve both concurrently"
    )


def test_hook_pool_thread_inherits_otel_context():
    """ADR-0077 (D): _recall_with_timeout must propagate the OTel context into
    the executor thread — the forwarded hook recall previously started a NEW
    trace, orphaning it from the hook route span."""
    import pytest

    otel_trace = pytest.importorskip("opentelemetry.trace")
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    from yadgar.core.server import http as _http

    # Install a real (recording) provider for this test if none is set —
    # a NonRecordingSpan would give trace_id=0 on both sides (false pass).
    once = getattr(otel_trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(otel_trace, "_TRACER_PROVIDER"):
        otel_trace._TRACER_PROVIDER = None
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    otel_trace.set_tracer_provider(provider)
    tracer = otel_trace.get_tracer("test")

    seen: dict = {}

    class FakeRetriever:
        def recall(self, *_a, **_k):
            seen["trace_id"] = otel_trace.get_current_span().get_span_context().trace_id
            return ["ok"]

    with tracer.start_as_current_span("hook-route") as span:
        outer_trace_id = span.get_span_context().trace_id
        res = asyncio.run(_http._recall_with_timeout(FakeRetriever(), "prompt-recall", "q"))

    assert res == ["ok"]
    assert outer_trace_id != 0
    assert seen.get("trace_id") == outer_trace_id, (
        f"executor thread lost the OTel context: inner={seen.get('trace_id'):#x} "
        f"outer={outer_trace_id:#x} — forwarded hook span starts a NEW trace"
    )
