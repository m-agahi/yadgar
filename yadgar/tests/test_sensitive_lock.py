"""Unit tests for the sensitive-job lock (v5.69 P3).

CI-runnable: no real surreal, no real signals, no real processes touched. The
lock is a plain JSON file under ``YADGAR_DATA_DIR`` (pointed at a tmp dir here),
so every case runs under ``-m 'not integration and not e2e'``.

DATA-SAFETY: ``YADGAR_DATA_DIR`` is monkeypatched to a per-test tmp dir; the real
store at ``~/.local/share/yadgar`` is NEVER touched.

Stale-reaping is the load-bearing safety property: a lock whose PID is dead, or
whose ``started_at`` is older than the TTL, MUST be reaped so it can never
deadlock shutdown. A dead-pid lock that blocked shutdown would BE a hang bug.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from yadgar.core import sensitive_lock


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("YADGAR_DATA_DIR", str(d))
    return d


def test_lock_path_under_data_dir(data_dir):
    assert sensitive_lock.lock_path() == data_dir / "sensitive-job.lock"


def test_acquire_release_round_trip(data_dir):
    assert sensitive_lock.read() is None
    acquired = sensitive_lock.acquire("vacuum")
    assert acquired is True
    payload = sensitive_lock.read()
    assert payload is not None
    assert payload["job"] == "vacuum"
    assert payload["pid"] == os.getpid()
    assert "started_at" in payload
    assert sensitive_lock.is_held_by_live_job() is True

    sensitive_lock.release()
    assert sensitive_lock.read() is None
    assert sensitive_lock.is_held_by_live_job() is False


def test_payload_round_trips_on_disk(data_dir):
    sensitive_lock.acquire("vacuum")
    raw = json.loads(sensitive_lock.lock_path().read_text())
    assert raw["job"] == "vacuum"
    assert raw["pid"] == os.getpid()
    assert isinstance(raw["started_at"], (int, float))


def test_second_acquire_while_held_by_live_is_refused(data_dir):
    # First acquire writes a live lock (pid = this process).
    assert sensitive_lock.acquire("vacuum") is True
    # A second acquire while a LIVE job holds it must be refused — single in-flight.
    assert sensitive_lock.acquire("vacuum") is False


def test_stale_lock_dead_pid_is_reaped_and_does_not_block(data_dir):
    """A dead-pid lock MUST be reaped: acquire succeeds, is_held_by_live_job False.

    This is the anti-hang invariant — a dead-pid lock must NEVER block shutdown.
    """
    dead_pid = _a_dead_pid()
    sensitive_lock.lock_path().write_text(
        json.dumps({"job": "vacuum", "pid": dead_pid, "started_at": time.time()})
    )
    # A dead-pid lock is NOT held by a live job (so shutdown is not blocked).
    assert sensitive_lock.is_held_by_live_job() is False
    # And a new acquire reaps it cleanly (takes over with this live pid).
    assert sensitive_lock.acquire("vacuum") is True
    assert sensitive_lock.read()["pid"] == os.getpid()


def test_ttl_expired_lock_is_reaped(data_dir, monkeypatch):
    """A lock older than SENSITIVE_LOCK_TTL_SEC is stale even if its pid is live."""
    monkeypatch.setenv("YADGAR_SENSITIVE_LOCK_TTL_SEC", "1")
    # Live pid (this process) but started_at far in the past → TTL-stale.
    sensitive_lock.lock_path().write_text(
        json.dumps({"job": "vacuum", "pid": os.getpid(), "started_at": time.time() - 10_000})
    )
    assert sensitive_lock.is_held_by_live_job() is False
    assert sensitive_lock.acquire("vacuum") is True


def test_corrupt_lock_is_treated_as_stale(data_dir):
    sensitive_lock.lock_path().write_text("{not valid json")
    assert sensitive_lock.is_held_by_live_job() is False
    assert sensitive_lock.acquire("vacuum") is True


def test_held_context_manager_releases_on_exit(data_dir):
    with sensitive_lock.held("vacuum") as ok:
        assert ok is True
        assert sensitive_lock.is_held_by_live_job() is True
    # Released on context exit.
    assert sensitive_lock.read() is None
    assert sensitive_lock.is_held_by_live_job() is False


def test_held_context_manager_releases_on_exception(data_dir):
    with pytest.raises(RuntimeError):  # noqa: PT012
        with sensitive_lock.held("vacuum"):
            raise RuntimeError("boom")
    assert sensitive_lock.read() is None


def _a_dead_pid() -> int:
    """Return a PID that is (almost certainly) not alive."""
    for pid in (999999, 998877, 987654):
        try:
            os.kill(pid, 0)
        except OSError:
            return pid  # no such process (or no perms) → treat as dead
    pytest.skip("could not find a dead pid for the test")
    return -1
