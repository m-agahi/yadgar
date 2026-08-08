"""T3 Car 2 — side-effect fork integration at the two call sites (RED first).

1. Backend DB half is DECOMPOSED: heat/last_accessed mutations + boosted_ids stay
   INLINE (response byte-identical); only `storage.boost_memories_access` is the
   deferrable write. Proven by _compute_db_boost returning the ids while mutating
   `merged` in place, without issuing the DB write itself.
2. Core recall() defers the session half through the fork seam.
3. SHUTDOWN WIRING: lifecycle.shutdown drains the core session fork (before the
   buffer flush), and the backend lifespan teardown awaits drain_db_tasks.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch


def test_compute_db_boost_mutates_inline_without_db_write():
    """_compute_db_boost applies heat/last_accessed in place and returns ids,
    but does NOT issue the batched DB write (that is the deferrable half)."""
    from yadgar.backend.retrieval.recall_pipeline import _compute_db_boost

    mem = {"id": 11, "heat": 0.5, "_source": "memory", "content": "x"}
    merged = [mem]
    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with patch("yadgar.backend.retrieval.recall_pipeline._st") as mock_st:
        mock_st._thermo = MagicMock()
        boosted_ids, now = _compute_db_boost(merged, storage)

    # Response-visible mutations happened inline (byte-identical payload).
    assert abs(merged[0]["heat"] - 0.6) < 1e-9
    assert merged[0]["last_accessed"] == "2026-01-01T00:00:00+00:00"
    assert boosted_ids == [11]
    assert now == "2026-01-01T00:00:00+00:00"
    # The deferrable DB write was NOT issued by the compute half.
    storage.boost_memories_access.assert_not_called()


def test_apply_db_side_effects_still_writes_when_not_forked():
    """The combined _apply_recall_db_side_effects still performs the full boost
    (compute + write) when fork is disabled — behavior preserved."""
    from yadgar.backend.retrieval.recall_pipeline import _apply_recall_db_side_effects

    mem = {"id": 9, "heat": 0.3, "_source": "memory", "content": "y"}
    merged = [mem]
    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with patch("yadgar.backend.retrieval.recall_pipeline._st") as mock_st:
        mock_st._thermo = MagicMock()
        _apply_recall_db_side_effects(merged, "q", storage)

    storage.boost_memories_access.assert_called_once()
    assert 9 in storage.boost_memories_access.call_args[0][0]
    assert abs(merged[0]["heat"] - 0.4) < 1e-9


def test_recall_defers_session_side_effects_through_fork():
    """core recall() routes the session half through the fork seam
    (_submit_session_side_effect) rather than calling it inline."""
    import sys

    import yadgar.core.server.tools  # noqa: F401

    recall_mod = sys.modules["yadgar.core.server.tools.recall"]
    fake_results = [{"id": 1, "content": "x", "heat": 0.7, "_source": "memory"}]

    captured = {}

    def _fake_submit(fn):
        # Record that the session half was routed here; run it so behavior holds.
        captured["called"] = True
        fn()

    with (
        patch.object(recall_mod, "_forward_to_backend", return_value=fake_results),
        patch.object(recall_mod, "_submit_session_side_effect", side_effect=_fake_submit),
        patch.object(recall_mod, "_apply_recall_session_side_effects") as mock_apply,
        patch.object(recall_mod, "_st") as mock_st,
    ):
        mock_st._consolidation = None
        mock_st._pool = None
        result = recall_mod.recall(query="query text", directory="/tmp", max_results=5)

    assert captured.get("called"), "recall() did not defer the session half through the fork seam"
    mock_apply.assert_called_once_with(fake_results, "query text")
    assert result == fake_results


def test_lifecycle_shutdown_drains_session_fork_before_buffer_flush():
    """lifecycle.shutdown() runs the deferred session side-effect AND drains it
    BEFORE flushing _st._buffer (so a deferred capture_action is not lost) and
    before storage.close(). This pins the shutdown wiring, not just the primitive."""
    import yadgar._shared.runtime.lifecycle as lifecycle
    import yadgar._shared.runtime.recall_side_effects_fork as fork
    import yadgar._shared.runtime.state as _st

    fork.reset_session_executor()

    events: list[str] = []
    block = threading.Event()
    started = threading.Event()

    def _deferred():
        started.set()
        block.wait(5.0)  # still running when shutdown begins
        events.append("side_effect_ran")

    class _Buffer:
        def flush(self):
            events.append("buffer_flush")

    # Reset the shutdown idempotency guard so this test drives a real teardown.
    _st._shutdown_done = False
    prev_buffer = _st._buffer
    prev_storage = _st._storage
    prev_staleness = _st._staleness
    prev_qd = _st._queue_drainer
    _st._buffer = _Buffer()
    _st._storage = None  # skip storage.close (None-guarded)
    _st._staleness = None
    _st._queue_drainer = None
    try:
        with patch.object(fork, "_sideeffect_fork_enabled", return_value=True):
            fork.submit_session_side_effect(_deferred)
            assert started.wait(2.0), "deferred side-effect never started"
            # Release the side-effect concurrently, then shut down: the drain must
            # wait for it and run it before the buffer flush.
            block.set()
            lifecycle.shutdown()
        assert "side_effect_ran" in events, "shutdown did not drain the deferred side-effect"
        assert events.index("side_effect_ran") < events.index("buffer_flush"), (
            f"session fork drained AFTER buffer flush (capture would be lost): {events}"
        )
    finally:
        _st._buffer = prev_buffer
        _st._storage = prev_storage
        _st._staleness = prev_staleness
        _st._queue_drainer = prev_qd
        _st._shutdown_done = False
        fork.reset_session_executor()


def test_backend_lifespan_teardown_awaits_db_drain():
    """The backend lifespan teardown awaits drain_db_tasks so forked heat writes
    land before the queue drainer / surreal stop (the #181 writers-stop seam)."""
    import yadgar.backend.embed_service.embed_service as svc

    drained = {"called": False}

    async def _fake_drain(timeout: float = 10.0):
        drained["called"] = True

    async def _idle_task():
        # A cancellable background coroutine standing in for the snapshot/warmup
        # tasks (the teardown cancels + awaits them).
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    async def _drive():
        # Neutralise the heavy startup + the drainer stop; we only assert the
        # teardown awaits the recall side-effect drain.
        with (
            patch.object(svc, "_start_queue_drainer"),
            patch.object(svc, "_stop_queue_drainer"),
            patch("yadgar._shared.runtime.recall_side_effects_fork.drain_db_tasks", _fake_drain),
            patch.object(svc, "_get_engine"),
            patch.object(svc, "_ce_cache", MagicMock()),
            patch.object(svc, "_embed_cache", MagicMock()),
            patch.object(svc, "_run_cache_snapshot_task", side_effect=_idle_task),
            patch.object(svc, "_run_model_warmup", side_effect=_idle_task),
        ):
            cm = svc.lifespan(svc.app)
            await cm.__aenter__()
            await cm.__aexit__(None, None, None)

    asyncio.run(_drive())
    assert drained["called"], "backend lifespan teardown did not await drain_db_tasks"
