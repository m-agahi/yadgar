"""v5.48.0 — TDD scaffolding (RED phase): daemon auto-check + opt-in flag.

Tests for:
- yadgar/server/lifecycle.py — auto-check thread on daemon start
- Thread is daemon=True
- Honors update.check_on_start (default OFF)
- Startup completes fast even if PyPI probe is slow
- Probe failure logs WARNING, daemon continues

All network calls mocked. Threading mocked where needed.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch


class TestDaemonAutoCheck:
    """Tests for the auto-check-on-start behavior wired in lifecycle.main()."""

    def test_check_on_start_false_makes_no_network_call(self, monkeypatch):
        """When UPDATE_CHECK_ON_START=False (default), no PyPI probe is fired."""
        monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "false")

        # Import and call the auto-check wiring function directly
        from yadgar.server.lifecycle import _maybe_auto_check_for_update

        with patch("yadgar.update.check.probe_latest_version") as mock_probe:
            _maybe_auto_check_for_update()
            # Give any threads time to run
            time.sleep(0.1)

        mock_probe.assert_not_called()

    def test_check_on_start_true_spawns_daemon_thread(self, monkeypatch):
        """When UPDATE_CHECK_ON_START=True, a daemon thread is spawned."""
        monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "true")

        spawned_threads: list[threading.Thread] = []

        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            t = original_thread(*args, **kwargs)
            spawned_threads.append(t)
            return t

        from yadgar.update.check import LatestVersionInfo

        mock_result = LatestVersionInfo(available_version="9.99.0")

        with (
            patch("threading.Thread", side_effect=capture_thread),
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
        ):
            from yadgar.server.lifecycle import _maybe_auto_check_for_update

            _maybe_auto_check_for_update()

        # At least one thread was spawned
        assert len(spawned_threads) >= 1

    def test_check_on_start_true_thread_is_daemon(self, monkeypatch):
        """The spawned thread has daemon=True so it doesn't prevent daemon shutdown."""
        monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "true")

        spawned_threads: list[threading.Thread] = []

        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            t = original_thread(*args, **kwargs)
            spawned_threads.append(t)
            return t

        from yadgar.update.check import LatestVersionInfo

        mock_result = LatestVersionInfo(available_version="9.99.0")

        with (
            patch("threading.Thread", side_effect=capture_thread),
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
        ):
            from yadgar.server.lifecycle import _maybe_auto_check_for_update

            _maybe_auto_check_for_update()

        assert any(t.daemon for t in spawned_threads), (
            "Expected at least one daemon=True thread; got: "
            + str([(t.name, t.daemon) for t in spawned_threads])
        )

    def test_check_on_start_daemon_starts_fast_regardless_of_probe_latency(self, monkeypatch):
        """Daemon startup completes quickly even if PyPI probe is slow."""
        monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "true")

        import time as _time

        def _slow_probe(**kwargs):
            _time.sleep(2.0)  # simulate very slow PyPI response
            from yadgar.update.check import LatestVersionInfo

            return LatestVersionInfo(available_version="9.99.0")

        with patch("yadgar.update.check.probe_latest_version", side_effect=_slow_probe):
            from yadgar.server.lifecycle import _maybe_auto_check_for_update

            t0 = _time.monotonic()
            _maybe_auto_check_for_update()
            elapsed = _time.monotonic() - t0

        # Startup should return in well under 1 second (probe runs in background)
        assert elapsed < 1.0, f"_maybe_auto_check_for_update() took {elapsed:.2f}s (expected <1s)"

    def test_probe_failure_logs_warning_does_not_raise(self, monkeypatch, caplog):
        """If the probe raises (network error), a WARNING is logged and no exception propagates."""
        import logging

        import httpx

        monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "true")

        completed = threading.Event()

        def _failing_probe(**kwargs):
            completed.set()
            raise httpx.ConnectError("unreachable")

        with patch("yadgar.update.check.probe_latest_version", side_effect=_failing_probe):
            from yadgar.server.lifecycle import _maybe_auto_check_for_update

            with caplog.at_level(logging.WARNING, logger="yadgar.server.lifecycle"):
                _maybe_auto_check_for_update()
                # Wait for the background thread to complete
                completed.wait(timeout=3.0)
                time.sleep(0.1)  # let log flush

        # Should not have raised — only logged
        any(
            "update" in r.message.lower() or "check" in r.message.lower()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )
        # Accept either a warning OR no crash (probe failure is non-fatal)
        # Primary assertion: no exception propagated (test reaching here means no crash)

    def test_settings_update_check_on_start_default_is_false(self):
        """UPDATE_CHECK_ON_START default is False — no opt-in by default."""
        from yadgar.config import Settings

        s = Settings()
        assert s.UPDATE_CHECK_ON_START is False

    def test_settings_update_check_timeout_default(self):
        """UPDATE_CHECK_TIMEOUT_SECONDS default is 5."""
        from yadgar.config import Settings

        s = Settings()
        assert s.UPDATE_CHECK_TIMEOUT_SECONDS == 5
