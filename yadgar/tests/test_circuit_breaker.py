"""N4 circuit breaker tests — TDD: written before implementation.

Tests for _CircuitBreaker state machine and RemoteMLClient integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# 1. Breaker opens after failure threshold
# ---------------------------------------------------------------------------


class TestBreakerOpensAfterThreshold:
    def test_breaker_opens_after_failure_threshold(self, monkeypatch):
        """Feed 3 consecutive timeouts → ce breaker is_open() == True."""
        import httpx

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_http.post.side_effect = httpx.ReadTimeout("timed out", request=MagicMock())
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")

            # 3 failures
            for _ in range(3):
                client.score_cross_encoder("q", ["t"])

            breaker = client._breakers["ce"]
            assert breaker.is_open(), "breaker should be OPEN after 3 consecutive failures"


# ---------------------------------------------------------------------------
# 2. Open breaker returns None without HTTP call
# ---------------------------------------------------------------------------


class TestBreakerReturnsNoneWhenOpen:
    def test_breaker_returns_none_when_open(self, monkeypatch):
        """Manually open the ce breaker → score_cross_encoder returns None, no HTTP call."""
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            # Manually open breaker
            client._breakers["ce"]._open(reason="test")
            mock_http.reset_mock()

            result = client.score_cross_encoder("q", ["t"])

        assert result is None, "should return None when breaker is open"
        mock_http.post.assert_not_called()


# ---------------------------------------------------------------------------
# 3. OPEN → HALF_OPEN after cooldown
# ---------------------------------------------------------------------------


class TestBreakerHalfOpenAfterCooldown:
    def test_breaker_half_open_after_cooldown(self, monkeypatch):
        """Open breaker + simulated time past cooldown → breaker.is_half_open() == True."""
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            breaker = client._breakers["ce"]

            # Open the breaker with a fake clock that returns a time well in the past
            fake_time = 1_000_000.0
            breaker._open(reason="test", _now=fake_time)

            # Advance fake clock past duration (60s)
            later = fake_time + 61.0
            assert breaker.is_half_open(_now=later), (
                "breaker should be HALF_OPEN after cooldown expires"
            )


# ---------------------------------------------------------------------------
# 4. HALF_OPEN → CLOSED on probe success
# ---------------------------------------------------------------------------


class TestBreakerClosesOnProbeSuccess:
    def test_breaker_closes_on_probe_success(self, monkeypatch):
        """Half-open + successful HTTP call → breaker transitions to CLOSED."""
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"scores": [0.5]}
            mock_resp.raise_for_status = MagicMock()
            mock_http.post.return_value = mock_resp
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            breaker = client._breakers["ce"]

            # Put breaker in half-open state by opening it with past timestamp
            fake_open_time = 1_000_000.0
            breaker._open(reason="test", _now=fake_open_time)

            # Inject fake clock so score_cross_encoder sees the breaker as half-open
            client._fake_now = fake_open_time + 61.0
            result = client.score_cross_encoder("q", ["t"])

        assert result == [0.5], f"expected [0.5], got {result}"
        assert breaker.is_closed(), "breaker should be CLOSED after successful probe"


# ---------------------------------------------------------------------------
# 5. HALF_OPEN → OPEN on probe failure
# ---------------------------------------------------------------------------


class TestBreakerReopensOnProbeFailure:
    def test_breaker_reopens_on_probe_failure(self, monkeypatch):
        """Half-open + failed HTTP call → breaker transitions back to OPEN."""
        import httpx

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_http.post.side_effect = httpx.ReadTimeout("timeout again", request=MagicMock())
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            breaker = client._breakers["ce"]

            fake_open_time = 1_000_000.0
            breaker._open(reason="test", _now=fake_open_time)
            client._fake_now = fake_open_time + 61.0

            result = client.score_cross_encoder("q", ["t"])

        assert result is None, f"expected None from failed probe, got {result}"
        assert breaker.is_open(), "breaker should be OPEN again after failed probe"


# ---------------------------------------------------------------------------
# 6. Breaker disabled passes through
# ---------------------------------------------------------------------------


class TestBreakerEnvDisabledPassesThrough:
    def test_breaker_env_disabled_passes_through(self, monkeypatch):
        """YADGAR_CIRCUIT_BREAKER_ENABLED=0 → calls always hit HTTP, no breaker behavior."""
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "0")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"scores": [0.7]}
            mock_resp.raise_for_status = MagicMock()
            mock_http.post.return_value = mock_resp
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")

        assert not hasattr(client, "_breakers") or client._breakers is None, (
            "breaker dict should not exist or be None when disabled"
        )
        result = client.score_cross_encoder("q", ["t"])
        assert result == [0.7], f"expected [0.7] when disabled, got {result}"
        mock_http.post.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Per-endpoint isolation
# ---------------------------------------------------------------------------


class TestPerEndpointIsolation:
    def test_per_endpoint_isolation(self, monkeypatch):
        """Opening the ce breaker must NOT affect nli or pair breakers."""
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"scores": [0.5]}
            mock_resp.raise_for_status = MagicMock()
            mock_http.post.return_value = mock_resp
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")

        # Open ONLY ce breaker
        client._breakers["ce"]._open(reason="test")

        assert client._breakers["ce"].is_open(), "ce should be open"
        assert not client._breakers["nli"].is_open(), "nli should NOT be open"
        assert not client._breakers["pair"].is_open(), "pair should NOT be open"


# ---------------------------------------------------------------------------
# 8. Probe uses PROBE_TIMEOUT_SEC, not full client timeout (Fix 1a)
# ---------------------------------------------------------------------------


class TestProbeUsesShortTimeout:
    def test_probe_timeout_used_on_half_open_probe(self, monkeypatch):
        """When breaker is HALF_OPEN, score_cross_encoder must pass probe_timeout to httpx."""
        import httpx

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC", "2.0")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"scores": [0.5]}
            mock_resp.raise_for_status = MagicMock()
            mock_http.post.return_value = mock_resp
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            breaker = client._breakers["ce"]

            # Put in half-open
            fake_open_time = 1_000_000.0
            breaker._open(reason="test", _now=fake_open_time)
            client._fake_now = fake_open_time + 61.0

            client.score_cross_encoder("q", ["t"])

        call_kwargs = mock_http.post.call_args
        # probe call must pass a timeout kwarg
        assert call_kwargs is not None, "post should have been called"
        assert "timeout" in call_kwargs.kwargs, "probe must override timeout kwarg"
        probe_timeout = call_kwargs.kwargs["timeout"]
        # Should be an httpx.Timeout with read=2.0
        assert isinstance(probe_timeout, httpx.Timeout), "timeout must be httpx.Timeout"
        assert probe_timeout.read == 2.0, (
            f"probe read timeout should be 2.0, got {probe_timeout.read}"
        )

    def test_normal_call_uses_rerank_timeout(self, monkeypatch):
        """Normal (CLOSED) call must pass _rerank_timeout, NOT _probe_timeout.

        v5.6.6: all non-probe /rerank calls use RERANK_BACKEND_TIMEOUT_SEC (90s)
        so CE inference (8-46s) doesn't trip the general 5s timeout.
        """
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC", "2.0")
        monkeypatch.setenv("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "90")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"scores": [0.5]}
            mock_resp.raise_for_status = MagicMock()
            mock_http.post.return_value = mock_resp
            mock_cls.return_value = mock_http

            from yadgar.backend.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            # Breaker is CLOSED — normal call
            client.score_cross_encoder("q", ["t"])

        call_kwargs = mock_http.post.call_args
        assert call_kwargs is not None
        # v5.6.6: normal calls pass _rerank_timeout (not probe, not None)
        assert "timeout" in call_kwargs.kwargs, "normal call must pass explicit _rerank_timeout"
        import httpx

        t = call_kwargs.kwargs["timeout"]
        assert isinstance(t, httpx.Timeout), f"timeout must be httpx.Timeout, got {type(t)}"
        assert t.read == pytest.approx(90.0), (
            f"normal call rerank timeout read should be 90.0, got {t.read}"
        )
        assert t.read != pytest.approx(2.0), "normal call must NOT use probe timeout (2.0s)"


# ---------------------------------------------------------------------------
# 9. Exponential backoff on consecutive probe failures (Fix 1b)
# ---------------------------------------------------------------------------


class TestExponentialBackoffOnProbeFailure:
    def _make_client(self, monkeypatch, base_duration: float = 60.0):
        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "1")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", str(base_duration))
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC", "600")
        monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR", "2.0")
        cfg.get_settings.cache_clear()

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            from yadgar.backend.ml_client import RemoteMLClient

            return RemoteMLClient("http://localhost:8001")

    def _advance_to_half_open(self, breaker, t_open: float, duration: float) -> None:
        """Transition breaker OPEN→HALF_OPEN by simulating cooldown expiry."""
        assert breaker.is_half_open(_now=t_open + duration + 1.0), "should be half-open"

    def test_first_probe_failure_doubles_cooldown(self, monkeypatch):
        """After 1st probe failure, open_duration should double (60 → 120)."""
        client = self._make_client(monkeypatch)
        breaker = client._breakers["ce"]

        t0 = 1_000_000.0
        breaker._open(reason="initial", _now=t0)
        # Advance time past cooldown to transition to HALF_OPEN
        self._advance_to_half_open(breaker, t0, 60.0)
        breaker.record_failure(_now=t0 + 61.0)  # now half-open, probe failed

        assert breaker._open_duration_sec == 120.0, (
            f"after 1 probe failure: expected 120s, got {breaker._open_duration_sec}"
        )

    def test_second_probe_failure_quadruples_cooldown(self, monkeypatch):
        """After 2nd probe failure, cooldown should be base * factor^2 = 240."""
        client = self._make_client(monkeypatch)
        breaker = client._breakers["ce"]

        t0 = 1_000_000.0
        breaker._open(reason="initial", _now=t0)
        # First probe failure: open(60s) → half_open → fail → open(120s)
        self._advance_to_half_open(breaker, t0, 60.0)
        breaker.record_failure(_now=t0 + 61.0)
        assert breaker._open_duration_sec == 120.0

        # Second probe failure: open(120s) → half_open → fail → open(240s)
        self._advance_to_half_open(breaker, t0 + 61.0, 120.0)
        breaker.record_failure(_now=t0 + 61.0 + 121.0)

        assert breaker._open_duration_sec == 240.0, (
            f"after 2 probe failures: expected 240s, got {breaker._open_duration_sec}"
        )

    def test_backoff_capped_at_max_open_duration(self, monkeypatch):
        """Cooldown never exceeds MAX_OPEN_DURATION_SEC (600s)."""
        client = self._make_client(monkeypatch, base_duration=300.0)
        breaker = client._breakers["ce"]

        t0 = 1_000_000.0
        breaker._open(reason="initial", _now=t0)
        # One probe failure: 300 * 2 = 600 → capped at 600
        self._advance_to_half_open(breaker, t0, 300.0)
        breaker.record_failure(_now=t0 + 301.0)
        assert breaker._open_duration_sec == 600.0, (
            f"should be capped at 600, got {breaker._open_duration_sec}"
        )
        # Another probe failure: would be 1200 → still capped at 600
        self._advance_to_half_open(breaker, t0 + 301.0, 600.0)
        breaker.record_failure(_now=t0 + 301.0 + 601.0)
        assert breaker._open_duration_sec == 600.0, (
            f"should still be capped at 600, got {breaker._open_duration_sec}"
        )

    def test_success_resets_probe_failure_count(self, monkeypatch):
        """record_success resets probe failure count → next OPEN uses base duration."""
        client = self._make_client(monkeypatch)
        breaker = client._breakers["ce"]

        t0 = 1_000_000.0
        breaker._open(reason="initial", _now=t0)
        self._advance_to_half_open(breaker, t0, 60.0)
        breaker.record_failure(_now=t0 + 61.0)  # → 120s
        assert breaker._open_duration_sec == 120.0

        breaker.record_success()
        assert breaker.consecutive_probe_failures == 0, "probe failure count should reset"
        assert breaker._open_duration_sec == 60.0, "duration should reset to base after success"

        # Re-open via closed-state failure threshold → should use base 60s again
        breaker.record_failure(_now=t0 + 200.0)
        breaker.record_failure(_now=t0 + 200.0)
        breaker.record_failure(_now=t0 + 200.0)
        assert breaker._open_duration_sec == 60.0, (
            f"after reset + re-open: expected base 60s, got {breaker._open_duration_sec}"
        )


# ---------------------------------------------------------------------------
# V1b — CB-1 state gauge (v5.5.3) — TDD: written before implementation
# ---------------------------------------------------------------------------


def _make_mock_metrics():
    """Return a MagicMock that mimics yadgar.metrics gauge interface."""
    mock = MagicMock()
    # .labels(...).set(n) should work on the mock
    mock.yadgar_circuit_breaker_state.labels.return_value = MagicMock()
    return mock


class TestGaugeInitializedToClosedOnConstruct:
    def test_gauge_initialized_to_closed_on_construct(self):
        """Constructing _CircuitBreaker sets gauge to 0 (CLOSED)."""
        from yadgar.backend.ml_client import _CircuitBreaker

        mock_metrics = _make_mock_metrics()
        breaker = _CircuitBreaker(
            "/rerank/ce",
            failure_threshold=3,
            open_duration_sec=60.0,
            metrics_module=mock_metrics,
        )

        mock_metrics.yadgar_circuit_breaker_state.labels.assert_called_once_with(
            endpoint="/rerank/ce"
        )
        mock_metrics.yadgar_circuit_breaker_state.labels.return_value.set.assert_called_once_with(0)
        _ = breaker  # silence unused-var warning


class TestGaugeSetToOpenOnThresholdFailures:
    def test_gauge_set_to_open_on_threshold_failures(self):
        """After 3 consecutive failures gauge == 2 (OPEN)."""
        from yadgar.backend.ml_client import _CircuitBreaker

        mock_metrics = _make_mock_metrics()
        breaker = _CircuitBreaker(
            "/rerank/ce",
            failure_threshold=3,
            open_duration_sec=60.0,
            metrics_module=mock_metrics,
        )
        mock_metrics.reset_mock()

        for _ in range(3):
            breaker.record_failure()

        # Gauge must have been set to 2 (OPEN)
        set_calls = mock_metrics.yadgar_circuit_breaker_state.labels.return_value.set.call_args_list
        assert any(c == call(2) for c in set_calls), (
            f"Expected set(2) for OPEN state, got: {set_calls}"
        )


class TestGaugeSetToHalfOpenAfterCooldown:
    def test_gauge_set_to_half_open_after_cooldown(self):
        """After cooldown expires and is_open() called, gauge == 1 (HALF_OPEN)."""
        from yadgar.backend.ml_client import _CircuitBreaker

        mock_metrics = _make_mock_metrics()
        breaker = _CircuitBreaker(
            "/rerank/ce",
            failure_threshold=3,
            open_duration_sec=60.0,
            metrics_module=mock_metrics,
        )
        mock_metrics.reset_mock()

        t0 = 1_000_000.0
        breaker._open(reason="test", _now=t0)
        mock_metrics.reset_mock()

        # Call is_open() with time past cooldown — triggers OPEN → HALF_OPEN
        breaker.is_open(_now=t0 + 61.0)

        set_calls = mock_metrics.yadgar_circuit_breaker_state.labels.return_value.set.call_args_list
        assert any(c == call(1) for c in set_calls), (
            f"Expected set(1) for HALF_OPEN state, got: {set_calls}"
        )


class TestGaugeSetToClosedOnRecordSuccess:
    def test_gauge_set_to_closed_on_record_success(self):
        """From OPEN state, record_success() sets gauge to 0 (CLOSED)."""
        from yadgar.backend.ml_client import _CircuitBreaker

        mock_metrics = _make_mock_metrics()
        breaker = _CircuitBreaker(
            "/rerank/ce",
            failure_threshold=3,
            open_duration_sec=60.0,
            metrics_module=mock_metrics,
        )

        t0 = 1_000_000.0
        breaker._open(reason="test", _now=t0)
        mock_metrics.reset_mock()

        breaker.record_success()

        set_calls = mock_metrics.yadgar_circuit_breaker_state.labels.return_value.set.call_args_list
        assert any(c == call(0) for c in set_calls), (
            f"Expected set(0) for CLOSED state, got: {set_calls}"
        )


class TestGaugePerEndpointIndependent:
    def test_gauge_per_endpoint_independent(self):
        """Two breakers with different endpoints update gauge independently."""
        from yadgar.backend.ml_client import _CircuitBreaker

        mock_metrics = _make_mock_metrics()

        # Separate label mock per endpoint
        label_mocks: dict[str, MagicMock] = {}

        def _labels(endpoint):
            if endpoint not in label_mocks:
                label_mocks[endpoint] = MagicMock()
            return label_mocks[endpoint]

        mock_metrics.yadgar_circuit_breaker_state.labels.side_effect = _labels

        ce_breaker = _CircuitBreaker(
            "/rerank/ce",
            failure_threshold=3,
            open_duration_sec=60.0,
            metrics_module=mock_metrics,
        )
        nli_breaker = _CircuitBreaker(
            "/rerank/nli",
            failure_threshold=3,
            open_duration_sec=60.0,
            metrics_module=mock_metrics,
        )

        # Drive ce to OPEN
        for _ in range(3):
            ce_breaker.record_failure()

        # nli gauge should only have been set to 0 (CLOSED at init), never to 2 (OPEN)
        nli_set_calls = label_mocks["/rerank/nli"].set.call_args_list
        assert all(c == call(0) for c in nli_set_calls), (
            f"nli gauge should only see CLOSED(0), got: {nli_set_calls}"
        )
        # ce gauge must have been set to 2 (OPEN) at some point
        ce_set_calls = label_mocks["/rerank/ce"].set.call_args_list
        assert any(c == call(2) for c in ce_set_calls), (
            f"ce gauge should see OPEN(2), got: {ce_set_calls}"
        )
        _ = nli_breaker  # prevent unused-var lint
