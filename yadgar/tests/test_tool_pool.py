"""v5.95.0: offload pool bounded via TOOL_POOL_WORKERS knob.

Root cause: on the --cpus 1 core, the offload pool defaulted to 8 workers.
MCP tool-call bursts (recall/wiki_query/adr_add/checkpoint) -> 8 threads
competing for 1 CPU -> event-loop starvation -> P0 health-kill (status=137).

Fix: drop default to 2 and make TOOL_POOL_WORKERS a proper I25/I32 config
knob (env > yaml > default). Sibling of HOOK_RECALL_POOL_WORKERS (#81 fix).
"""

from __future__ import annotations

import os
import threading
import time


def test_pool_default_is_two():
    """With no env override, _pool_workers() must return 2 (the new default).

    Mirrors test_hook_recall_pool.test_hook_recall_pool_is_bounded.
    """
    from yadgar.server._offload import _pool_workers, shutdown_pool

    old = os.environ.pop("YADGAR_TOOL_POOL_WORKERS", None)
    try:
        shutdown_pool()
        assert _pool_workers() == 2, f"expected default pool workers == 2, got {_pool_workers()}"
    finally:
        if old is not None:
            os.environ["YADGAR_TOOL_POOL_WORKERS"] = old
        shutdown_pool()


def test_pool_env_override():
    """YADGAR_TOOL_POOL_WORKERS=5 env override wins over Settings default.

    Env-read must be live (not lru_cache-stale) so test override works.
    """
    from yadgar.server._offload import _ensure_pool, _pool_workers, shutdown_pool

    old = os.environ.get("YADGAR_TOOL_POOL_WORKERS")
    os.environ["YADGAR_TOOL_POOL_WORKERS"] = "5"
    try:
        shutdown_pool()
        assert _pool_workers() == 5, f"expected 5 from env, got {_pool_workers()}"
        pool = _ensure_pool()
        assert pool._max_workers == 5, (
            f"_ensure_pool max_workers should be 5, got {pool._max_workers}"
        )
    finally:
        if old is None:
            os.environ.pop("YADGAR_TOOL_POOL_WORKERS", None)
        else:
            os.environ["YADGAR_TOOL_POOL_WORKERS"] = old
        shutdown_pool()


def test_pool_bounds_concurrency():
    """Under a burst of offloaded callables, peak in-flight <= pool knob.

    Mirrors test_hook_recall_pool.test_pool_caps_concurrent_recall_threads.
    Requires YADGAR_OFFLOAD_TOOLS=1 so run_offloaded dispatches to pool;
    inline mode (OFFLOAD_TOOLS=0) runs callers serially on the event loop
    and never exercises the pool cap.
    """
    import asyncio

    from yadgar.server._offload import run_offloaded, shutdown_pool

    old_offload = os.environ.get("YADGAR_OFFLOAD_TOOLS")
    old_workers = os.environ.get("YADGAR_TOOL_POOL_WORKERS")
    os.environ["YADGAR_OFFLOAD_TOOLS"] = "1"
    os.environ["YADGAR_TOOL_POOL_WORKERS"] = "2"

    live = 0
    peak = 0
    lock = threading.Lock()

    def _slow_work():
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.3)
        with lock:
            live -= 1
        return "done"

    async def _fire():
        shutdown_pool()
        await asyncio.gather(*[run_offloaded(_slow_work) for _ in range(6)])

    try:
        asyncio.run(_fire())
        assert peak <= 2, f"peak in-flight {peak} exceeded pool cap 2"
    finally:
        if old_offload is None:
            os.environ.pop("YADGAR_OFFLOAD_TOOLS", None)
        else:
            os.environ["YADGAR_OFFLOAD_TOOLS"] = old_offload
        if old_workers is None:
            os.environ.pop("YADGAR_TOOL_POOL_WORKERS", None)
        else:
            os.environ["YADGAR_TOOL_POOL_WORKERS"] = old_workers
        shutdown_pool()
