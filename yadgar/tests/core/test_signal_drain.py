"""Unit tests for the sensitive-job signal drain in _signal_handler (v5.69 P3).

CI-runnable: no real signals delivered, no real processes touched, no surreal.
The handler is called directly with a synthetic signum; ``lifecycle.shutdown`` is
patched to a recorder; the lock is a JSON file under a tmp ``YADGAR_DATA_DIR``.

The four branches under test:
  1. no lock                              → immediate shutdown (unchanged pre-P3).
  2. stale lock (dead pid)                → immediate shutdown (NEVER blocks).
  3. live lock, pid == os.getpid()        → drain; on timeout REFUSE (no shutdown).
  4. live lock, pid != os.getpid() (live) → proceed (vacuum-initiated stop).

Branch 3 is the anti-data-loss invariant (BC-E3 at unit level); branch 2 is the
anti-hang invariant (a dead-pid lock must never deadlock shutdown).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time

import pytest

# R2a Car D2: _signal_handler + shutdown moved to yadgar.core.lifecycle.
from yadgar.core import lifecycle


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("YADGAR_DATA_DIR", str(d))
    return d


@pytest.fixture()
def shutdown_recorder(monkeypatch):
    called = {"v": False}

    def _record(*a, **kw):
        called["v"] = True

    monkeypatch.setattr(lifecycle, "shutdown", _record)
    return called


def _write_lock(data_dir, *, pid, started_at=None):
    payload = {"job": "vacuum", "pid": pid, "started_at": started_at or time.time()}
    (data_dir / "sensitive-job.lock").write_text(json.dumps(payload))


def _call_handler():
    with contextlib.suppress(SystemExit):
        lifecycle._signal_handler(signal.SIGTERM, None)


def test_no_lock_shuts_down_immediately(data_dir, shutdown_recorder):
    """No sensitive job → behave exactly as before P3 (immediate shutdown)."""
    start = time.monotonic()
    _call_handler()
    elapsed = time.monotonic() - start
    assert shutdown_recorder["v"] is True
    # No drain hang — returns essentially immediately.
    assert elapsed < 1.0, f"no-lock shutdown should not drain/hang (took {elapsed:.2f}s)"


def test_stale_dead_pid_lock_shuts_down_immediately(data_dir, shutdown_recorder):
    """A dead-pid lock is STALE → must NOT block shutdown (anti-hang)."""
    _write_lock(data_dir, pid=_a_dead_pid())
    start = time.monotonic()
    _call_handler()
    elapsed = time.monotonic() - start
    assert shutdown_recorder["v"] is True, (
        "a dead-pid (stale) lock must NOT block shutdown — that would be a hang bug"
    )
    assert elapsed < 1.0, f"stale-lock shutdown should not drain (took {elapsed:.2f}s)"


def test_in_process_live_lock_refuses_shutdown_on_timeout(data_dir, shutdown_recorder, monkeypatch):
    """Live lock owned by THIS process → drain; on timeout REFUSE (no shutdown).

    BC-E3 at unit level: an external signal must not interrupt an in-process
    sensitive job mid-swap.  The lock is never released, so the bounded drain
    times out and the handler returns WITHOUT calling shutdown.
    """
    monkeypatch.setenv("YADGAR_SENSITIVE_DRAIN_TIMEOUT_SEC", "0.2")
    _write_lock(data_dir, pid=os.getpid())
    _call_handler()
    assert shutdown_recorder["v"] is False, (
        "an external signal while an IN-PROCESS sensitive job holds the lock must "
        "drain/refuse — never call shutdown() mid-swap"
    )


def test_in_process_live_lock_shuts_down_when_lock_clears(data_dir, shutdown_recorder, monkeypatch):
    """If the in-process job releases the lock during the drain, shutdown proceeds."""
    monkeypatch.setenv("YADGAR_SENSITIVE_DRAIN_TIMEOUT_SEC", "5.0")
    _write_lock(data_dir, pid=os.getpid())

    # Release the lock shortly after the handler starts draining.
    import threading

    def _release_soon():
        time.sleep(0.2)
        (data_dir / "sensitive-job.lock").unlink(missing_ok=True)

    threading.Thread(target=_release_soon, daemon=True).start()
    start = time.monotonic()
    _call_handler()
    elapsed = time.monotonic() - start
    assert shutdown_recorder["v"] is True, "shutdown must proceed once the lock clears"
    assert elapsed < 4.0, "should not wait the full timeout once the lock is released"


def test_separate_process_live_lock_proceeds(data_dir, shutdown_recorder):
    """Live lock owned by a DIFFERENT live process → proceed (vacuum-initiated stop).

    The vacuum runs as a separate process and stops core via ServiceController;
    that SIGTERM must be allowed through, else the vacuum's own stop deadlocks.
    We use this process's parent pid as a stand-in for a different live pid.
    """
    other_pid = os.getppid()  # a real, live pid that is NOT os.getpid()
    assert other_pid != os.getpid()
    _write_lock(data_dir, pid=other_pid)
    _call_handler()
    assert shutdown_recorder["v"] is True, (
        "a separate-process sensitive job (vacuum stopping core) must NOT block "
        "shutdown — that would deadlock the vacuum's own teardown"
    )


def _a_dead_pid() -> int:
    for pid in (999999, 998877, 987654):
        try:
            os.kill(pid, 0)
        except OSError:
            return pid
    pytest.skip("could not find a dead pid for the test")
    return -1
