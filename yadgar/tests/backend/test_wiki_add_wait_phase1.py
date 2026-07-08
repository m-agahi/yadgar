"""Phase 1 tests — per-job terminal-state polling in FileQueue.

R3 Car 1 (write-half): the old in-process Event API (register_wait /
signal_complete / QueueDrainer.wait_for_job -> bool) was ripped. The new
cross-process contract is FileQueue.wait_for_job(job_id, timeout) -> dict,
which polls the shared archive/ (success) and dlq/ (rejection) dirs. Archiving
or DLQ'ing the queue file IS the completion signal — no threading.Event.

These tests assert that observable contract via the new poll API.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

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


def _archive_job(fq: FileQueue, job_id: str) -> Path:
    """Simulate the drainer committing a job: drop an archive file for job_id.

    Mirrors the drainer's success terminal (archive/memories/<date>/*_<job_id>.json)
    that FileQueue.wait_for_job() polls.
    """
    dest_dir = fq._memories_archive_dir()
    dest = dest_dir / f"{int(time.time() * 1000):016d}_{job_id}.json"
    dest.write_text(json.dumps({"id": job_id, "op": "memorize"}))
    return dest


def _dlq_job(fq: FileQueue, job_id: str, candidates: list | None = None) -> Path:
    """Simulate the drainer rejecting a job: drop a dlq file (+ optional sidecar).

    When *candidates* is given, writes a duplicate_detected .error.json sidecar in
    the drainer's real format ({failure_reason, failure_metadata.candidates}) so
    FileQueue._read_dlq_rejection reconstructs the rejection dict.
    """
    fname = f"{int(time.time() * 1000):016d}_{job_id}.json"
    dest = fq.dlq_dir / fname
    dest.write_text(json.dumps({"id": job_id, "op": "wiki_add"}))
    if candidates is not None:
        (fq.dlq_dir / f"{fname}.error.json").write_text(
            json.dumps(
                {
                    "failure_reason": "duplicate_detected",
                    "failure_metadata": {"candidates": candidates},
                }
            )
        )
    return dest


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

    def test_distinct_jobs_get_distinct_ids(self, fq):
        """Two different enqueue calls should get distinct job_ids."""
        jid1 = fq.enqueue("memorize", {"content": "a", "context": "/tmp"})
        jid2 = fq.enqueue("memorize", {"content": "b", "context": "/tmp"})
        assert jid1 != jid2


# ── FileQueue.wait_for_job (poll API) ─────────────────────────────────────────


class TestFileQueueWaitForJob:
    def test_wait_returns_ok_when_archived(self, fq):
        """Archiving the job file IS the success signal -> {"status": "ok"}."""
        job_id = fq.enqueue("memorize", {"content": "test", "context": "/tmp"})
        _archive_job(fq, job_id)

        outcome = fq.wait_for_job(job_id, timeout=2.0)
        assert outcome["status"] == "ok"

    def test_wait_returns_rejected_with_result_when_dlqd(self, fq):
        """A DLQ'd job surfaces the .error.json rejection payload synchronously."""
        job_id = fq.enqueue("wiki_add", {"title": "dup", "content": "x"})
        _dlq_job(fq, job_id, candidates=["yadgar-existing-page"])

        outcome = fq.wait_for_job(job_id, timeout=2.0)
        assert outcome["status"] == "rejected"
        assert outcome["result"] == {
            "stored": False,
            "reason": "duplicate_detected",
            "candidates": ["yadgar-existing-page"],
        }

    def test_wait_returns_rejected_none_when_no_sidecar(self, fq):
        """DLQ'd job without a sidecar -> rejected with result=None."""
        job_id = fq.enqueue("wiki_add", {"title": "x", "content": "y"})
        _dlq_job(fq, job_id, candidates=None)

        outcome = fq.wait_for_job(job_id, timeout=2.0)
        assert outcome["status"] == "rejected"
        assert outcome["result"] is None

    def test_wait_returns_timeout_when_never_terminal(self, fq):
        """No archive + no dlq within timeout -> {"status": "timeout"}."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})

        t0 = time.monotonic()
        outcome = fq.wait_for_job(job_id, timeout=0.2)
        elapsed = time.monotonic() - t0

        assert outcome["status"] == "timeout"
        assert elapsed < 1.0, "wait_for_job should not block much beyond timeout"

    def test_wait_pre_archived_returns_fast(self, fq):
        """If job was already archived before wait_for_job, it returns ok immediately."""
        job_id = fq.enqueue("memorize", {"content": "x", "context": "/tmp"})
        _archive_job(fq, job_id)

        t0 = time.monotonic()
        outcome = fq.wait_for_job(job_id, timeout=2.0)
        elapsed = time.monotonic() - t0

        assert outcome["status"] == "ok"
        assert elapsed < 0.5, "Pre-archived job should return fast"

    def test_drain_now_archive_signals_completion(self, fq, drainer):
        """drain_now() that archives the job makes a concurrent poll observe ok.

        Simulates the real flow: caller enqueues, drainer commits+archives, poll sees it.
        """
        job_id = fq.enqueue("memorize", {"content": "test", "context": "/tmp"})

        def _fake_drain_now():
            _archive_job(fq, job_id)
            return 1

        drainer.drain_now = _fake_drain_now
        drainer.drain_now()

        outcome = fq.wait_for_job(job_id, timeout=2.0)
        assert outcome["status"] == "ok"
