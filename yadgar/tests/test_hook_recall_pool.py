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
    assert _HOOK_RECALL_POOL_WORKERS == 1
    assert _HOOK_RECALL_POOL._max_workers == 1


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
