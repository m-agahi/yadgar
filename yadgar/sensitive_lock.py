"""Sensitive-job lock (v5.69 P3).

A *sensitive job* — currently vacuum — holds a lock FILE under ``YADGAR_DATA_DIR``
for the duration of a data-mutating operation that must NOT be interrupted by an
external shutdown signal mid-swap.  On 2026-06-16 a vacuum was interrupted (the
canonical DB was renamed and the rebuild then failed) → 3622 memories lost.  This
lock lets the signal handler DRAIN/REFUSE an external SIGTERM while the job runs,
and enforces single-in-flight vacuum (the swap-recovery in ``yadgar.vacuum``
assumes exactly one vacuum at a time).

Design
------
* Lock file: ``<YADGAR_DATA_DIR>/sensitive-job.lock`` (the exact path BC-E3 in
  ``tests/e2e/test_vacuum_backup_safety.py`` writes — keep them in lockstep).
* Payload JSON: ``{"job": str, "pid": int, "started_at": float}``.
* Atomic write: write ``<lock>.tmp`` then ``os.replace`` onto the lock path —
  the proven pattern from ``yadgar.ops._fire_vacuum_service``.
* Stale-reaping (mandatory anti-hang): a lock is STALE when its PID is dead
  (``os.kill(pid, 0)`` raises) OR its ``started_at`` is older than the TTL
  (``SENSITIVE_LOCK_TTL_SEC``, generous — ~2x worst-case vacuum).  ``acquire``
  reaps a stale lock; ``is_held_by_live_job`` returns False for a stale lock.  A
  dead-pid lock that blocked shutdown would BE the hang bug this guards against,
  so stale-reaping is load-bearing and unit-tested.

This module is import-light (stdlib only) so the signal handler and tests can use
it without pulling in heavy engines.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import yadgar.paths as _paths
from yadgar.config import resolve_knob
from yadgar.observability.observe import observe

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "sensitive-job.lock"


def lock_path() -> Path:
    """Resolve the sensitive-job lock path under the live ``YADGAR_DATA_DIR``.

    Reads ``_paths._data_dir()`` each call (it honors the env override live), so
    tests that monkeypatch ``YADGAR_DATA_DIR`` are seen immediately.
    """
    return _paths._data_dir() / _LOCK_FILENAME


def _ttl_seconds() -> float:
    """Read SENSITIVE_LOCK_TTL_SEC live (bypasses lru_cache for testability)."""
    return float(resolve_knob("YADGAR_SENSITIVE_LOCK_TTL_SEC", "SENSITIVE_LOCK_TTL_SEC", int, 7200))


@observe(tier="hot")
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@observe(tier="stage")
def read() -> dict | None:
    """Return the lock payload dict, or None if absent/unreadable.

    A corrupt/unparseable lock returns None (callers treat that as "no live
    job" → stale → reapable).
    """
    p = lock_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError, ValueError):  # fmt: skip
        return None


@observe(tier="hot")
def _is_stale(payload: dict | None) -> bool:
    """True when the lock is reapable: absent, corrupt, dead pid, or TTL-expired."""
    if not payload:
        return True
    try:
        pid = int(payload.get("pid", 0))
        started_at = float(payload.get("started_at", 0))
    except (TypeError, ValueError):  # fmt: skip
        return True
    if not _pid_alive(pid):
        return True
    age = time.time() - started_at
    return age > _ttl_seconds()


@observe(tier="stage")
def is_held_by_live_job() -> bool:
    """True iff a NON-stale lock is currently present.

    A stale lock (dead pid OR TTL-expired OR corrupt) returns False so it can
    never block shutdown — the anti-hang invariant.
    """
    return not _is_stale(read())


@observe(tier="stage")
def _write_lock(job: str) -> None:
    p = lock_path()
    tmp = Path(str(p) + ".tmp")
    payload = json.dumps({"job": job, "pid": os.getpid(), "started_at": time.time()})
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(payload)
    os.replace(tmp, p)  # atomic


@observe(tier="boundary")
def acquire(job: str) -> bool:
    """Acquire the sensitive-job lock for *job*.

    Returns True on success (this process now owns the lock).  Returns False if a
    LIVE job already holds it (single-in-flight refusal).  A STALE lock (dead pid
    or TTL-expired) is reaped and acquisition succeeds.
    """
    payload = read()
    if not _is_stale(payload):
        logger.warning(
            "sensitive-job lock already held by live job=%s pid=%s — refusing %s",
            (payload or {}).get("job"),
            (payload or {}).get("pid"),
            job,
        )
        return False
    if payload is not None:
        logger.info(
            "reaping stale sensitive-job lock (job=%s pid=%s) before acquiring %s",
            payload.get("job"),
            payload.get("pid"),
            job,
        )
    _write_lock(job)
    return True


@observe(tier="boundary")
def release() -> None:
    """Release the lock (best-effort — never raises)."""
    try:
        lock_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("failed to release sensitive-job lock at %s: %s", lock_path(), exc)


class held:  # noqa: N801 — lowercase to read like a context-manager verb
    """Context manager wrapping acquire/release.

    ``with held(job) as ok:`` — ``ok`` is the acquire() result.  Always releases
    on exit IF this process acquired it; does not release a lock it did not take.
    """

    def __init__(self, job: str) -> None:
        self.job = job
        self.acquired = False

    def __enter__(self) -> bool:
        self.acquired = acquire(self.job)
        return self.acquired

    def __exit__(self, *exc) -> None:
        if self.acquired:
            release()
