"""Concurrency tests for QueueDrainer._drain_once serialization (v5.70.0).

Root cause of CI flake #53 (test_memory_behavior + test_project_brief_modes):
the background drainer thread's run() loop and a synchronous drain_now() call
both invoke _drain_once() with NO mutual exclusion. Two concurrent passes read
the same pending() file list; one thread applies+removes a file while the other
finds it already gone — "file theft". drain_now() then returns having processed
0, the test reads storage, but the other thread's write may still be in flight
under CPU starvation -> NOT-FOUND (KeyError 'id' / empty anchors). Parallel-only,
order-dependent.

Fix: a non-reentrant lock serializes _drain_once. drain_now() applies inline and
releases the lock only after the write is durable, so the data is present when
drain_now() returns. The background pass blocks on the lock instead of stealing
the file. This matches the maintainer intent already documented on flush_barrier
(which avoids _drain_once specifically "to avoid concurrent access with the
running drainer thread").

TDD: written before the lock was added; RED without it (overlap detected),
GREEN with it.
"""

from __future__ import annotations

import threading
import time

from yadgar._shared.file_queue.queue import FileQueue
from yadgar.backend.queue_drainer import QueueDrainer


def _make_drainer(tmp_path) -> QueueDrainer:
    """Build a drainer with a no-op storage factory. The thread is NOT started;
    tests drive _drain_once directly to exercise the serialization invariant."""
    fq = FileQueue(tmp_path, wiki_prefix="wiki-")
    return QueueDrainer(fq, storage_factory=lambda: None, drain_interval=999.0)


def test_drain_once_does_not_overlap_under_concurrency(tmp_path):
    """Two threads calling _drain_once concurrently must never run the pass body
    at the same time — the lock serializes them."""
    drainer = _make_drainer(tmp_path)

    overlap_detected = threading.Event()
    currently_inside = {"n": 0}
    counter_guard = threading.Lock()

    real_process = drainer._process_pending_file

    def _slow_process(path, now):
        # Mark entry; if another thread is already inside the pass body, the lock
        # is missing and we record an overlap.
        with counter_guard:
            currently_inside["n"] += 1
            if currently_inside["n"] > 1:
                overlap_detected.set()
        try:
            time.sleep(0.02)  # widen the race window
            return real_process(path, now)
        finally:
            with counter_guard:
                currently_inside["n"] -= 1

    drainer._process_pending_file = _slow_process  # type: ignore[method-assign]

    # Enqueue several ops so each pass iterates over files (giving overlap a chance).
    for i in range(5):
        drainer._queue.enqueue("memorize", {"content": f"c{i}", "tags": [], "context": "/x"})

    threads = [threading.Thread(target=drainer._drain_once) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not overlap_detected.is_set(), (
        "_drain_once ran concurrently — passes overlapped, lock missing"
    )


def test_drain_once_serializes_with_background_loop(tmp_path):
    """A running background drainer thread and a synchronous drain_now() must not
    overlap their pass bodies."""
    drainer = _make_drainer(tmp_path)

    overlap_detected = threading.Event()
    currently_inside = {"n": 0}
    counter_guard = threading.Lock()
    real_process = drainer._process_pending_file

    def _slow_process(path, now):
        with counter_guard:
            currently_inside["n"] += 1
            if currently_inside["n"] > 1:
                overlap_detected.set()
        try:
            time.sleep(0.01)
            return real_process(path, now)
        finally:
            with counter_guard:
                currently_inside["n"] -= 1

    drainer._process_pending_file = _slow_process  # type: ignore[method-assign]

    # Tight interval so the background thread spins through _drain_once repeatedly.
    drainer._drain_interval = 0.001

    for i in range(8):
        drainer._queue.enqueue("memorize", {"content": f"b{i}", "tags": [], "context": "/x"})

    drainer.start()
    try:
        for _ in range(20):
            drainer.drain_now()
            drainer._queue.enqueue("memorize", {"content": "extra", "tags": [], "context": "/x"})
            time.sleep(0.001)
    finally:
        drainer.stop()

    assert not overlap_detected.is_set(), (
        "background loop and drain_now() overlapped — _drain_once not serialized"
    )
