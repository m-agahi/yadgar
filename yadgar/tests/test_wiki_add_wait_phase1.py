"""Phase 1 tests — per-job completion future in FileQueue / QueueDrainer.

RED before Phase 1 implementation; GREEN after.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from yadgar.core.file_queue import FileQueue, QueueDrainer

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def fq(tmp_path):
    """FileQueue with isolated tmp directory."""
    return FileQueue(base_dir=tmp_path)


@pytest.fixture()
def drainer(fq):
    """QueueDrainer with a no-op storage factory and short drain interval."""
    storage_mock = MagicMock()
    storage_mock.return_value = storage_mock

    d = QueueDrainer(
        fq,
        storage_factory=storage_mock,
        drain_interval=60.0,  # won't auto-drain in tests; we call drain_now()
    )
    yield d
    # don't start the thread; each test controls drain manually


# ── FileQueue per-job tracking ────────────────────────────────────────────────


class TestFileQueueJobTracking:
    def test_enqueue_returns_job_id_string(self, fq):
        """enqueue() must return a non-empty string (job_id / UUID)."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_job_id_is_uuid_format(self, fq):
        """job_id should be a UUID4 format (8-4-4-4-12 hex)."""
        import re

        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            job_id,
        ), f"Expected UUID, got: {job_id!r}"

    def test_register_wait_returns_event(self, fq):
        """register_wait() should return a threading.Event."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        event = fq.register_wait(job_id)
        assert isinstance(event, threading.Event)

    def test_signal_complete_sets_event(self, fq):
        """signal_complete() should set the event returned by register_wait()."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        event = fq.register_wait(job_id)
        assert not event.is_set()
        fq.signal_complete(job_id)
        assert event.is_set()

    def test_signal_complete_noop_for_unknown_job(self, fq):
        """signal_complete() for an unknown job_id must not raise."""
        fq.signal_complete("nonexistent-job-id")  # should not raise

    def test_register_wait_same_job_returns_same_event(self, fq):
        """Calling register_wait() twice for same job returns same event."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        e1 = fq.register_wait(job_id)
        e2 = fq.register_wait(job_id)
        assert e1 is e2

    def test_distinct_jobs_get_distinct_events(self, fq):
        """Two different enqueue calls should get distinct events."""
        jid1 = fq.enqueue("memorize", {"content": "a", "context": "/tmp"})
        jid2 = fq.enqueue("memorize", {"content": "b", "context": "/tmp"})
        e1 = fq.register_wait(jid1)
        e2 = fq.register_wait(jid2)
        assert e1 is not e2


# ── QueueDrainer.wait_for_job ─────────────────────────────────────────────────


class TestQueueDrainerWaitForJob:
    def test_wait_for_job_triggers_drain_and_signals(self, fq, drainer, tmp_path):
        """wait_for_job() should call drain_now() and the event fires when job completes."""
        # Enqueue a job
        job_id = fq.enqueue("memorize", {"content": "test", "context": "/tmp"})

        # Patch drain_now to signal the job (simulates successful drain)
        def _fake_drain_now():
            fq.signal_complete(job_id)
            return 1

        drainer.drain_now = _fake_drain_now

        result = drainer.wait_for_job(job_id, timeout=2.0)
        assert result is True  # completed within timeout

    def test_wait_for_job_timeout_returns_false(self, fq, drainer):
        """wait_for_job() should return False if event never fires within timeout."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})

        # drain_now does NOT signal — simulates stuck queue
        drainer.drain_now = MagicMock(return_value=0)

        t0 = time.monotonic()
        result = drainer.wait_for_job(job_id, timeout=0.2)
        elapsed = time.monotonic() - t0

        assert result is False
        # Should have waited approximately the timeout duration
        assert elapsed < 1.0, "wait_for_job should not block longer than timeout"

    def test_wait_for_job_pre_completed_returns_immediately(self, fq, drainer):
        """If job was already completed before wait_for_job, it returns True immediately."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        # Signal before calling wait_for_job
        fq.register_wait(job_id)
        fq.signal_complete(job_id)

        drainer.drain_now = MagicMock(return_value=0)

        t0 = time.monotonic()
        result = drainer.wait_for_job(job_id, timeout=2.0)
        elapsed = time.monotonic() - t0

        assert result is True
        assert elapsed < 0.5, "Pre-completed job should return fast"
