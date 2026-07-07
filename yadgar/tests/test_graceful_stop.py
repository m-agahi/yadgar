"""Phase 6 — graceful-stop barrier tests (v5.49.0).

Tests 23-26 per PLAN_V5_49_0.md § 3.B.

No mcp / uvicorn imports — runs in the worktree Python env via:
  pytest yadgar/tests/test_graceful_stop.py --override-ini="addopts=" --noconftest -v
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

# ── Test 23 — drain_in_flight_requests waits for active requests ──────────────


def test_graceful_stop_waits_for_in_flight(tmp_path):
    """Inject 3 fake in-flight requests; drain returns True once all complete."""
    from yadgar.core.drain import _request_counter, drain_in_flight_requests

    # Reset counter to known state
    with _request_counter._lock:
        _request_counter._count = 0

    # Simulate 3 long-running requests by incrementing the counter
    _request_counter.increment()
    _request_counter.increment()
    _request_counter.increment()

    async def _run():
        # Release all 3 requests after 0.3 s
        async def _releaser():
            await asyncio.sleep(0.3)
            _request_counter.decrement()
            _request_counter.decrement()
            _request_counter.decrement()

        task = asyncio.create_task(_releaser())
        result = await drain_in_flight_requests(timeout=5.0)
        await task
        return result

    result = asyncio.run(_run())
    assert result is True, "drain_in_flight_requests should return True when all requests complete"


# ── Test 24 — drain_in_flight_requests honours timeout ───────────────────────


def test_graceful_stop_honors_timeout(tmp_path):
    """Single never-completing request; drain_in_flight_requests returns False at timeout."""
    from yadgar.core.drain import _request_counter, drain_in_flight_requests

    # Reset counter to known state
    with _request_counter._lock:
        _request_counter._count = 0

    # Simulate 1 never-completing request
    _request_counter.increment()

    try:
        t0 = time.monotonic()
        result = asyncio.run(drain_in_flight_requests(timeout=0.5))
        elapsed = time.monotonic() - t0
        assert result is False, "drain_in_flight_requests should return False on timeout"
        # Should complete within generous budget (timeout + 0.3s slack)
        assert elapsed < 1.0, f"Took {elapsed:.2f}s — expected < 1.0s"
    finally:
        # Clean up leaked counter
        _request_counter.decrement()


# ── Test 25 — flush_barrier drains queue to storage ──────────────────────────


def test_graceful_stop_flushes_queue(tmp_path):
    """Enqueue 10 items; flush_barrier returns True + all applied to storage."""
    import json as _json

    from yadgar.core.file_queue import FileQueue, QueueDrainer

    fq = FileQueue(base_dir=tmp_path)

    applied: list[dict] = []

    class _FakeStorage:
        def store(self, *a, **kw):
            pass

    # Override drainer to bypass branch validation and real DB calls
    class _TrackingDrainer(QueueDrainer):
        def _validate_branch_context(self, data: dict) -> str | None:  # type: ignore[override]
            return None  # disable branch validation for test

        def _validate_wiki_add(self, data: dict) -> str | None:  # type: ignore[override]
            return None  # disable wiki_add validation for test

        def _apply_inner(self, record: dict) -> None:  # type: ignore[override]
            applied.append(record)
            # Archive the processed file so queue depth drops
            for p in self._queue.pending():
                try:
                    d = _json.loads(p.read_text())
                    if d.get("id") == record.get("id"):
                        self._queue.archive(p)
                        break
                except Exception:
                    pass

    drainer = _TrackingDrainer(fq, lambda: _FakeStorage(), drain_interval=0.05)
    drainer.start()

    # Enqueue 10 items
    for i in range(10):
        fq.enqueue("memorize", {"content": f"item-{i}", "context": "/tmp"})

    ok = drainer.flush_barrier(timeout=10.0)
    drainer.stop()

    assert ok is True, "flush_barrier should return True when queue is drained"
    assert len(applied) == 10, f"Expected 10 applied items, got {len(applied)}"


# ── Test 26 — shutdown wires embed cache snapshot ────────────────────────────


def test_graceful_stop_snapshots_embed_cache(tmp_path, monkeypatch):
    """snapshot_embed_caches() calls save_snapshot on both CE and embed caches.

    Tests at the unit level: drain.snapshot_embed_caches() accesses embed_service
    module attributes, so we inject a fake module into sys.modules and verify
    that save_snapshot is invoked for both caches once.
    """
    import sys
    from types import ModuleType

    from yadgar.core.drain import snapshot_embed_caches

    ce_mock = MagicMock()
    embed_mock = MagicMock()

    fake_es = ModuleType("yadgar.backend.embed_service")
    fake_es._ce_cache = ce_mock  # type: ignore[attr-defined]
    fake_es._embed_cache = embed_mock  # type: ignore[attr-defined]
    fake_es._cache_snapshot_dir = lambda: str(tmp_path)  # type: ignore[attr-defined]

    # Inject fake module so drain.snapshot_embed_caches finds it via sys.modules
    original = sys.modules.get("yadgar.backend.embed_service")
    sys.modules["yadgar.backend.embed_service"] = fake_es
    try:
        snapshot_embed_caches()
    finally:
        if original is None:
            sys.modules.pop("yadgar.backend.embed_service", None)
        else:
            sys.modules["yadgar.backend.embed_service"] = original

    ce_mock.save_snapshot.assert_called_once()
    embed_mock.save_snapshot.assert_called_once()
