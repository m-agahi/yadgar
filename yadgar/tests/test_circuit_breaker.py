"""N4 circuit breaker tests — TDD: written before implementation.

Tests for _CircuitBreaker state machine and RemoteMLClient integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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

            from yadgar.ml_client import RemoteMLClient

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

            from yadgar.ml_client import RemoteMLClient

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
            from yadgar.ml_client import RemoteMLClient

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

            from yadgar.ml_client import RemoteMLClient

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

            from yadgar.ml_client import RemoteMLClient

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

            from yadgar.ml_client import RemoteMLClient

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

            from yadgar.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")

        # Open ONLY ce breaker
        client._breakers["ce"]._open(reason="test")

        assert client._breakers["ce"].is_open(), "ce should be open"
        assert not client._breakers["nli"].is_open(), "nli should NOT be open"
        assert not client._breakers["pair"].is_open(), "pair should NOT be open"
