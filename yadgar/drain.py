"""In-flight HTTP request counter, drain barrier, and embed cache snapshot (v5.49.0 Phase 6).

Isolated from _app.py / mcp imports so tests can import without FastMCP.

Usage (middleware):
    from yadgar.server._drain import _request_counter

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            _request_counter.increment()
            try:
                await self.app(scope, receive, send)
            finally:
                _request_counter.decrement()
        else:
            await self.app(scope, receive, send)

Usage (shutdown):
    from yadgar.server._drain import drain_in_flight_requests
    result = await drain_in_flight_requests(timeout=15.0)
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.05  # seconds between count checks


class _RequestCounter:
    """Thread-safe atomic counter + asyncio event for in-flight HTTP request tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count: int = 0

    def increment(self) -> None:
        with self._lock:
            self._count += 1

    def decrement(self) -> None:
        with self._lock:
            if self._count > 0:
                self._count -= 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


# Module-level singleton — imported by middleware and drain helper
_request_counter = _RequestCounter()


async def drain_in_flight_requests(timeout: float) -> bool:
    """Wait for active HTTP requests to complete, up to timeout seconds.

    Returns True if all requests completed before timeout, False otherwise.
    Caller is expected to have already stopped accepting new connections.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if _request_counter.count == 0:
            logger.info("drain_in_flight_requests: all requests drained")
            return True
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            logger.warning(
                "drain_in_flight_requests: timeout after %.1fs, %d requests still in-flight",
                timeout,
                _request_counter.count,
            )
            return False
        await asyncio.sleep(_POLL_INTERVAL)


def snapshot_embed_caches() -> None:
    """Trigger a final cache snapshot on the embed service (v5.49.0 Phase 6).

    Best-effort: non-fatal if the embed service is unavailable or the snapshot
    fails. Called once during daemon shutdown so in-memory CE + embed cache
    entries are persisted before process exit.

    Isolated here (not in lifecycle.py) so tests can import without FastMCP.
    lifecycle.shutdown() imports and calls this function.

    Uses module-level attribute access (via sys.modules) so tests can patch
    yadgar.backend.embed_service._ce_cache / ._embed_cache / ._cache_snapshot_dir.
    """
    import sys as _sys  # noqa: PLC0415

    try:
        import importlib as _il  # noqa: PLC0415

        _es = _sys.modules.get("yadgar.backend.embed_service") or _il.import_module(
            "yadgar.backend.embed_service"
        )
        snap_dir = _es._cache_snapshot_dir()
        _es._ce_cache.save_snapshot(snap_dir, "ce")
        _es._embed_cache.save_snapshot(snap_dir, "embed")
        logger.info("embed cache snapshot written on shutdown")
    except Exception as exc:  # noqa: BLE001
        logger.debug("embed cache snapshot skipped on shutdown: %s", exc)
