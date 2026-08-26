"""Bounded wait-for-backend gate for the CORE composition root (task #0027c).

Why this exists
---------------
``StorageEngine.__init__`` calls ``_init_schema()`` inline, and ``_init_schema``
issues HTTP on its very first statement.  So a core process started while the
backend is down raises ``httpx.ConnectError`` out of the constructor, out of
``lifecycle.init_engines``, out of ``main()`` — the process exits non-zero and
``Restart=on-failure`` + ``RestartSec=5`` brings it straight back into the same
failure.  The ~6s cycle stays under systemd's default
``StartLimitBurst=5``/``StartLimitIntervalSec=10s``, so the unit never reaches
``failed`` and loops indefinitely.

This is a STARTUP-only problem: at runtime the storage request path already
handles per-call connection errors.  Nothing here changes runtime behaviour.

What it is NOT for
------------------
It is **not** the cold-boot mechanism.  ``After=yadgar-backend.service`` is (once
the backend unit is ``Type=notify``/gated).  A first-ever cold boot where the
backend loads its model can run past this budget by design; the budget exists to
turn an unbounded crashloop into a small number of legible, bounded attempts.

Placement (the load-bearing constraint)
---------------------------------------
The gate is called from ``core_init_engines`` and NOWHERE else:

* not in ``StorageEngine.__init__`` — every construction site would pay it
  (tests, ``yadgar vacuum``/``seed``, the nightly cycle, the backend's own slim
  bootstrap), turning a fast "backend is down" CLI error into a minute-long hang;
* not in ``lifecycle.init_engines`` — the BACKEND calls that function directly
  (``embed_service._ensure_recall_engines`` -> ``engine_set="slim"``), so a gate
  there would make the backend wait for its own /health during its own startup.

``yadgar/tests/core/test_core_startup_backend_ready.py`` pins both.

Readiness signal: the backend's ``/health``, which returns 200 only when
``db_ok and engine_loaded`` — exactly the precondition schema init needs.  Note
that ``daemon._embed_health_ok`` is deliberately WEAKER (it treats any HTTP
response, including a 503, as OK because it answers "is the container up"), so
it is not reused here.

Escape hatch: ``YADGAR_BACKEND_READY_WAIT_SEC=0`` disables the gate entirely and
restores the previous behaviour without a downgrade.
"""

from __future__ import annotations

import logging
import os
import time

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

__all__ = ["BackendNotReadyError", "await_backend_ready"]

# Per-probe HTTP timeout. Matches daemon.py's 1s /health probe — a readiness
# check that hangs is indistinguishable from one that failed, and the loop's own
# budget is the thing that bounds total wall time.
#
# Caveat, measured not assumed: httpx's sync timeout does NOT bound name
# resolution (getaddrinfo blocks below it). A refused connection on loopback
# returns in ~0.07s, but an UNRESOLVABLE host — the shape a stopped
# `yadgar-backend` container takes on a podman network — took ~3.5s despite
# timeout=1.0. Since the deadline is checked AFTER each probe, total wall time
# can overshoot the budget by up to one resolution. At the 60s default that is
# ~64s against the core unit's TimeoutStartSec=120, so the headroom absorbs it —
# but do not describe the budget as an exact attempt count, because it is not.
_PROBE_TIMEOUT_SEC = 1.0


class BackendNotReadyError(RuntimeError):
    """The backend did not become ready inside ``BACKEND_READY_WAIT_SEC``."""


@observe(exempt="trivial env-var read plus a string join; no I/O, no error branch worth spanning")
def _backend_health_url() -> str | None:
    """The backend /health URL, or None when there is no remote backend.

    ``YADGAR_EMBED_URL`` is the base URL every core->backend forwarder already
    uses (``core/forward.py``, ``recall._forward_to_backend``, ``RemoteMLClient``).
    Unset means local/in-process engines — there is nothing to wait for.
    """
    base = os.environ.get("YADGAR_EMBED_URL", "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/health"


@observe(
    exempt=(
        "hot poll loop: one span per /health retry would emit up to a budget's worth of "
        "spans per startup, and the enclosing await_backend_ready stage span already "
        "covers the whole wait including its outcome"
    )
)
def _probe_backend_health(url: str, timeout_s: float) -> bool:
    """True only on HTTP 200 (``db_ok and engine_loaded``); never raises."""
    try:
        import httpx  # noqa: PLC0415 — lazy, keeps this module import-cheap

        resp = httpx.get(url, timeout=timeout_s)
        return resp.status_code == 200
    except Exception:
        return False


@observe(tier="stage")
def await_backend_ready(settings=None) -> bool:
    """Block until the backend's /health returns 200, or the budget expires.

    Polling cadence (task #61):
      * sleep doubles after each consecutive failure, starting at
        ``BACKEND_READY_POLL_SEC`` and capped at ``BACKEND_READY_POLL_MAX_SEC``;
      * after ``BACKEND_READY_LONG_BAKE_OUT_AFTER`` consecutive failures the
        loop enters long-bake-out: a single ``BACKEND_READY_LONG_BAKE_OUT_SEC``
        sleep with one ``long-bake-out`` log line, then re-enters the probe loop.
        The audit hook makes "backend has been down for minutes" greppable in
        ``journalctl`` instead of indistinguishable from a probe storm.

    Args:
        settings: injected for tests; defaults to ``get_settings()``.

    Returns:
        True when the backend is ready, or when the gate is skipped (no remote
        backend configured / budget disabled).

    Raises:
        BackendNotReadyError: the budget expired with the backend still not
            answering 200.  Deliberately NOT swallowed — the caller must exit
            non-zero so the unit restarts, one bounded attempt at a time.
    """
    _settings = settings if settings is not None else get_settings()
    budget_sec = float(getattr(_settings, "BACKEND_READY_WAIT_SEC", 60))
    if budget_sec <= 0:
        logger.debug("backend readiness gate disabled (BACKEND_READY_WAIT_SEC=0)")
        return True

    url = _backend_health_url()
    if url is None:
        logger.debug("backend readiness gate skipped — no YADGAR_EMBED_URL (local engines)")
        return True

    poll_sec = max(float(getattr(_settings, "BACKEND_READY_POLL_SEC", 2.0)), 0.0)
    poll_max_sec = max(float(getattr(_settings, "BACKEND_READY_POLL_MAX_SEC", 30.0)), 0.0)
    long_bake_after = max(int(getattr(_settings, "BACKEND_READY_LONG_BAKE_OUT_AFTER", 5)), 1)
    long_bake_sec = max(float(getattr(_settings, "BACKEND_READY_LONG_BAKE_OUT_SEC", 60.0)), 0.0)

    started = time.monotonic()
    deadline = started + budget_sec
    attempt = 0
    consecutive_failures = 0
    long_bake_emitted = False

    while True:
        attempt += 1
        if _probe_backend_health(url, _PROBE_TIMEOUT_SEC):
            if attempt > 1:
                logger.info(
                    "backend ready after %.1fs (%d attempts): %s",
                    time.monotonic() - started,
                    attempt,
                    url,
                )
            return True

        consecutive_failures += 1
        now = time.monotonic()
        if now >= deadline:
            break

        # Long-bake-out: once we've failed enough times in a row, switch to a
        # single long sleep (with one audit log line) and reset the per-probe
        # cadence — but NOT consecutive_failures, since the budget still has
        # the same number of probes left to spend.
        if consecutive_failures >= long_bake_after:
            if not long_bake_emitted:
                # Car-J (ledger #367): name both the configured cadence
                # (long_bake_sec, what the operator wrote) AND the actual
                # ceiling (poll_max_sec, what the loop honours) so a journal
                # reader can tell a configured 60s cadence from a 1s ceiling
                # that the loop silently imposed.
                logger.info(
                    "long-bake-out: backend %s unreachable for %.1fs after %d "
                    "consecutive failures; configured cadence=%.0fs, actual "
                    "sleep clamped to poll_max_sec=%.1fs until the %ds budget expires",
                    url,
                    now - started,
                    consecutive_failures,
                    long_bake_sec,
                    poll_max_sec,
                    int(budget_sec),
                )
                long_bake_emitted = True
            # Car-J (ledger #367): long_bake_sec is INTENTIONALLY large to keep
            # the audit hook coarse ("backend has been down for minutes"); the
            # actual time.sleep is capped at poll_max_sec so the host's CPU
            # budget is not drained by a single 60s sleep. The audit line
            # still names long_bake_sec so the journal entry is honest about
            # the configured cadence.
            sleep_for = min(long_bake_sec, poll_max_sec)
        else:
            # Exponential backoff from poll_sec → poll_max_sec, doubling each
            # failure. The cap exists so a long outage doesn't end up with
            # 30s sleeps back-to-back from attempt 1.
            #
            # The exponent is bounded before 2** to avoid overflowing the IEEE-754
            # double that backs Python float — at poll_sec=2.0 and consecutive=1024
            # the value crosses 2**1023 (the double max). 30 gives a 1e9 multiplier
            # which is well beyond any sane poll_max_sec, so the floor/cap below
            # does the rest of the work.
            shift = min(consecutive_failures - 1, 30)
            sleep_for = poll_sec * (2**shift)
            sleep_for = max(sleep_for, poll_sec)  # floor at poll_sec
            sleep_for = min(sleep_for, poll_max_sec)  # cap at poll_max_sec

        # Never sleep past the deadline — the deadline check at loop top is the
        # one that actually terminates the gate.
        sleep_for = min(sleep_for, max(deadline - now, 0.0))
        # One INFO line per attempt — a support question is answerable from
        # `journalctl` without a rebuild.
        logger.info(
            "waiting for backend %s — attempt %d, %.1fs elapsed of %.0fs budget",
            url,
            attempt,
            now - started,
            budget_sec,
        )
        time.sleep(sleep_for)

    elapsed = time.monotonic() - started
    logger.error(
        "backend not ready after %.1fs (%d attempts): %s",
        elapsed,
        attempt,
        url,
    )
    raise BackendNotReadyError(
        f"backend {url} did not return HTTP 200 within {budget_sec:.0f}s "
        f"({attempt} attempts). Is yadgar-backend running? "
        f"Set YADGAR_BACKEND_READY_WAIT_SEC=0 to disable this startup gate."
    )
