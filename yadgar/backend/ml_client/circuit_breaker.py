"""Per-endpoint circuit breaker for the remote ML client.

Extracted from ``ml_client.py`` (task #18 C2 internal split). ``RemoteMLClient``
constructs one ``_CircuitBreaker`` per ``/rerank`` mode (ce/nli/pair) and calls
``is_open`` / ``record_success`` / ``record_failure`` around each RPC. The class
is re-exported from the package ``__init__`` for back-compat importers/tests.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# N4 — Per-endpoint circuit breaker (v5.3.10 hotfix)
# ---------------------------------------------------------------------------

_STATE_CLOSED = "closed"
_STATE_OPEN = "open"
_STATE_HALF_OPEN = "half_open"


class _CircuitBreaker:
    """Per-endpoint open/closed/half-open state.

    States:
      CLOSED    — normal operation, calls go through
      OPEN      — fast-fail; calls return None without HTTP
      HALF_OPEN — single probe attempt; success → CLOSED, failure → OPEN

    Transitions:
      CLOSED → OPEN    when consecutive_failures >= failure_threshold
      OPEN → HALF_OPEN after open_duration_sec seconds
      HALF_OPEN → CLOSED  on probe success
      HALF_OPEN → OPEN    on probe failure (cooldown doubles up to max)

    All clock reads go through _time_fn so tests can inject a fake clock.

    v5.4.2 additions:
      - consecutive_probe_failures: tracks how many HALF_OPEN probes have failed
        without any intervening success.  Used to compute exponential backoff.
      - _base_open_duration_sec: the original config value; backoff multiplies
        the *current* _open_duration_sec, not this, so callers can read the live
        cooldown directly from _open_duration_sec.
      - _max_open_duration_sec / _backoff_factor: cap + multiplier for backoff.
    """

    def __init__(
        self,
        endpoint: str,
        failure_threshold: int,
        open_duration_sec: float,
        time_fn: Callable[[], float] | None = None,
        max_open_duration_sec: float = 600.0,
        backoff_factor: float = 2.0,
        metrics_module=None,
    ) -> None:
        self._endpoint = endpoint
        self._failure_threshold = failure_threshold
        self._base_open_duration_sec = open_duration_sec
        self._open_duration_sec = open_duration_sec
        self._max_open_duration_sec = max_open_duration_sec
        self._backoff_factor = backoff_factor
        self._time_fn: Callable[[], float] = time_fn or time.monotonic
        self._state: str = _STATE_CLOSED
        self._open_at: float = 0.0
        self.consecutive_failures: int = 0
        self.consecutive_probe_failures: int = 0
        self._metrics = metrics_module  # None → lazy-import on first use
        self._lock = threading.Lock()
        # Emit initial CLOSED state so viz sees the time-series from startup.
        self._set_gauge(0)
        # Emit initial reachable=1 — assume reachable until proven otherwise.
        self._set_reachable(1)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _set_gauge(self, value: int) -> None:
        """Update the circuit breaker state gauge. Non-fatal."""
        try:
            if self._metrics is None:
                import yadgar._shared.observability.metrics as _m  # noqa: PLC0415

                self._metrics = _m
            self._metrics.yadgar_circuit_breaker_state.labels(endpoint=self._endpoint).set(value)
        except Exception:
            pass

    def _set_reachable(self, value: int) -> None:
        """Update backend reachability gauge (1=reachable, 0=unreachable). Non-fatal."""
        try:
            if self._metrics is None:
                import yadgar._shared.observability.metrics as _m  # noqa: PLC0415

                self._metrics = _m
            self._metrics.yadgar_backend_reachable.labels(endpoint=self._endpoint).set(value)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Public state queries                                                  #
    # ------------------------------------------------------------------ #

    @observe(tier="hot")
    def is_open(self, _now: float | None = None) -> bool:
        """Return True if in OPEN state (not yet past cooldown)."""
        with self._lock:
            if self._state == _STATE_OPEN:
                now = _now if _now is not None else self._time_fn()
                if now - self._open_at >= self._open_duration_sec:
                    # Cooldown expired — move to half-open
                    self._state = _STATE_HALF_OPEN
                    self._set_gauge(1)
                    logger.info(
                        "circuit breaker %s → HALF_OPEN (cooldown expired after %.0fs)",
                        self._endpoint,
                        self._open_duration_sec,
                    )
                    return False
                return True
            return False

    def is_half_open(self, _now: float | None = None) -> bool:
        """Return True if in HALF_OPEN state (probe allowed)."""
        # Calling is_open() first handles OPEN→HALF_OPEN transition.
        self.is_open(_now=_now)
        return self._state == _STATE_HALF_OPEN

    def is_closed(self) -> bool:
        return self._state == _STATE_CLOSED

    @observe(tier="hot")
    def cooldown_remaining(self, _now: float | None = None) -> float:
        if self._state != _STATE_OPEN:
            return 0.0
        now = _now if _now is not None else self._time_fn()
        return max(0.0, self._open_duration_sec - (now - self._open_at))

    # ------------------------------------------------------------------ #
    # State mutators                                                        #
    # ------------------------------------------------------------------ #

    @observe(tier="hot")
    def record_success(self) -> None:
        with self._lock:
            if self._state in (_STATE_HALF_OPEN, _STATE_OPEN):
                logger.info(
                    "circuit breaker %s → CLOSED (probe succeeded)",
                    self._endpoint,
                )
            self._state = _STATE_CLOSED
            self._set_gauge(0)
            self._set_reachable(1)
            self.consecutive_failures = 0
            # Reset exponential backoff so next OPEN starts from base duration
            self.consecutive_probe_failures = 0
            self._open_duration_sec = self._base_open_duration_sec

    @observe(tier="hot")
    def record_failure(self, _now: float | None = None) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self._state == _STATE_HALF_OPEN:
                # Probe failed — apply exponential backoff then reopen
                self.consecutive_probe_failures += 1
                new_duration = min(
                    self._open_duration_sec * self._backoff_factor,
                    self._max_open_duration_sec,
                )
                self._open_duration_sec = new_duration
                self._open(reason="probe failed (backoff)", _now=_now)
            elif (
                self._state == _STATE_CLOSED
                and self.consecutive_failures >= self._failure_threshold
            ):
                self._open(reason=f"{self.consecutive_failures} consecutive failures", _now=_now)

    @observe(tier="hot")
    def _open(self, reason: str, _now: float | None = None) -> None:
        now = _now if _now is not None else self._time_fn()
        self._state = _STATE_OPEN
        self._open_at = now
        self._set_gauge(2)
        self._set_reachable(0)
        logger.warning(
            "breaker_open",
            extra={
                "component": "circuit_breaker",
                "action": "breaker_open",
                "outcome": "degraded",
                "endpoint": self._endpoint,
                "reason": reason,
                "cooldown_s": round(self._open_duration_sec, 1),
            },
        )
