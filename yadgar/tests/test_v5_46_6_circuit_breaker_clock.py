"""v5.46.6 — B14: circuit breaker clock injection guard.

Meta-test verifying that every _CircuitBreaker constructed inside
RemoteMLClient uses the client's own _now() as its time source, not
the global time.monotonic().

Without this fix the breaker's internal clock (real monotonic ≈1.1M s)
diverges from the test's injected fake clock (≈1.0M+N s), causing
OPEN→HALF_OPEN transitions to fire at wrong times.
"""

from __future__ import annotations

import pytest

from yadgar.config import Settings


@pytest.fixture
def ml_client(monkeypatch):
    from yadgar.backend.ml_client import RemoteMLClient

    # RemoteMLClient reads settings from get_settings() — inject via env vars.
    monkeypatch.setenv("CIRCUIT_BREAKER_ENABLED", "true")
    return RemoteMLClient(base_url="http://localhost:9999")


class TestCircuitBreakerClockInjection:
    """Every breaker in RemoteMLClient must use self._now as time source."""

    def _assert_bound_to_client(self, breaker, client, mode: str) -> None:
        """Verify breaker._time_fn is the client's _now bound method.

        Python bound methods are not singleton objects — `client._now is client._now`
        is False. Compare __func__ (same underlying function) and __self__ (same instance).
        """
        assert breaker._time_fn.__func__ is client._now.__func__, (
            f"{mode} breaker._time_fn.__func__ must be RemoteMLClient._now"
        )
        assert breaker._time_fn.__self__ is client, (
            f"{mode} breaker._time_fn must be bound to the client instance"
        )

    def test_ce_breaker_uses_client_now(self, ml_client):
        breaker = ml_client._breakers["ce"]  # type: ignore[index]
        self._assert_bound_to_client(breaker, ml_client, "ce")

    def test_nli_breaker_uses_client_now(self, ml_client):
        breaker = ml_client._breakers["nli"]  # type: ignore[index]
        self._assert_bound_to_client(breaker, ml_client, "nli")

    def test_pair_breaker_uses_client_now(self, ml_client):
        breaker = ml_client._breakers["pair"]  # type: ignore[index]
        self._assert_bound_to_client(breaker, ml_client, "pair")

    def test_fake_clock_injection_controls_half_open(self, ml_client):
        """Injected clock controls OPEN→HALF_OPEN transition (regression guard for B14)."""
        import time

        breaker = ml_client._breakers["ce"]  # type: ignore[index]

        # Record enough failures to open the breaker.
        base = time.monotonic()
        ml_client._fake_now = base  # type: ignore[attr-defined]
        threshold = int(Settings().CIRCUIT_BREAKER_FAILURE_THRESHOLD)
        for _ in range(threshold):
            breaker.record_failure(_now=ml_client._now())

        assert breaker.is_open(_now=ml_client._now()), "breaker should be OPEN"
        assert not breaker.is_half_open(_now=ml_client._now()), (
            "should not be HALF_OPEN yet — no time has passed"
        )

        # Advance injected clock past open duration.
        open_sec = float(Settings().CIRCUIT_BREAKER_OPEN_DURATION_SEC)
        ml_client._fake_now = base + open_sec + 1.0  # type: ignore[attr-defined]

        assert breaker.is_half_open(_now=ml_client._now()), (
            "should be HALF_OPEN after injected clock advances past open duration"
        )
