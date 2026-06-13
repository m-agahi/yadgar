"""TDD tests for v5.10.4: consolidate_now() mode parameter.

Scope:
  - consolidate_now(mode='light') skips sleep cycle and anchor audit.
  - consolidate_now(mode='full') runs sleep cycle + anchor audit (if enabled).
  - consolidate_now() default arg behaves as mode='light'.
  - Invalid mode returns error dict without calling force_consolidate.
  - mode='full' sets _last_sleep_cycle timestamp; mode='light' does not.
  - Sleep cycle exception in full mode is caught; result still has status='completed'.
  - Result dict always includes 'mode' key.

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

import yadgar.server._state as _st

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_consolidation():
    """Minimal mock for _st._consolidation."""
    m = MagicMock()
    m.force_consolidate.return_value = {"memories_added": 0}
    m._last_sleep_cycle = None
    return m


@pytest.fixture()
def mock_sleep():
    """Minimal mock for _st._sleep."""
    m = MagicMock()
    m.run_sleep_cycle.return_value = {"phases_run": 6}
    return m


@pytest.fixture()
def mock_state(mock_consolidation, mock_sleep, monkeypatch):
    """Wire mock engines into _st without hitting real DB."""
    monkeypatch.setattr(_st, "_consolidation", mock_consolidation)
    monkeypatch.setattr(_st, "_sleep", mock_sleep)
    return mock_consolidation, mock_sleep


# ---------------------------------------------------------------------------
# 1. light mode skips sleep cycle
# ---------------------------------------------------------------------------


class TestLightMode:
    """mode='light' (default): force_consolidate only, no sleep cycle, no anchor audit."""

    def test_consolidate_now_light_mode_skips_sleep_cycle(self, mock_state):
        """mode='light' must NOT call run_sleep_cycle."""
        mock_cons, mock_slp = mock_state

        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")

        mock_cons.force_consolidate.assert_called_once()
        mock_slp.run_sleep_cycle.assert_not_called()
        assert "sleep_cycle" not in result

    def test_consolidate_now_light_mode_skips_anchor_audit(self, mock_state, monkeypatch, request):
        """mode='light' must NOT call _run_anchor_audit_pass even when ENABLED=True."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)  # reliably clears even if test fails

        audit_mock = MagicMock(return_value={"actions": []})
        with patch("yadgar.server.tools.audit._run_anchor_audit_pass", audit_mock):
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")

        audit_mock.assert_not_called()
        assert "anchor_audit_pass" not in result

    def test_consolidate_now_default_mode_is_light(self, mock_state):
        """Calling consolidate_now() without args behaves as mode='light'."""
        mock_cons, mock_slp = mock_state

        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now()

        mock_slp.run_sleep_cycle.assert_not_called()
        assert result.get("mode") == "light"

    def test_consolidate_now_light_does_not_update_last_sleep_cycle_timestamp(self, mock_state):
        """mode='light' must leave _last_sleep_cycle unchanged (stays None)."""
        mock_cons, mock_slp = mock_state
        mock_cons._last_sleep_cycle = None

        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            consolidate_now(mode="light")

        assert mock_cons._last_sleep_cycle is None


# ---------------------------------------------------------------------------
# 2. full mode runs sleep cycle and anchor audit
# ---------------------------------------------------------------------------


class TestFullMode:
    """mode='full': force_consolidate + sleep cycle + anchor audit (if enabled)."""

    def test_consolidate_now_full_mode_runs_sleep_cycle(self, mock_state):
        """mode='full' must call run_sleep_cycle."""
        mock_cons, mock_slp = mock_state

        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="full")

        mock_slp.run_sleep_cycle.assert_called_once()
        assert "sleep_cycle" in result

    def test_consolidate_now_full_mode_runs_anchor_audit_if_enabled(
        self, mock_state, monkeypatch, request
    ):
        """mode='full' + ENABLED=True calls _run_anchor_audit_pass."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)  # reliably clears even if test fails

        audit_mock = MagicMock(return_value={"actions": []})
        with patch("yadgar.server.tools.audit._run_anchor_audit_pass", audit_mock):
            from yadgar.server.tools.admin_other import consolidate_now

            # Also need _get_storage mock
            with patch("yadgar.server.tools.admin_other._get_storage", return_value=MagicMock()):
                result = consolidate_now(mode="full")

        audit_mock.assert_called_once()
        assert "anchor_audit_pass" in result

    def test_consolidate_now_full_mode_skips_anchor_audit_if_disabled(
        self, mock_state, monkeypatch, request
    ):
        """mode='full' + ENABLED=False skips _run_anchor_audit_pass."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "false")
        from yadgar.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)  # reliably clears even if test fails

        audit_mock = MagicMock(return_value={"actions": []})
        with patch("yadgar.server.tools.audit._run_anchor_audit_pass", audit_mock):
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="full")

        audit_mock.assert_not_called()
        assert "anchor_audit_pass" not in result

    def test_consolidate_now_full_updates_last_sleep_cycle_timestamp(self, mock_state):
        """mode='full' sets _last_sleep_cycle to approximately now."""
        mock_cons, mock_slp = mock_state
        mock_cons._last_sleep_cycle = None

        before = datetime.now(UTC)
        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            consolidate_now(mode="full")

        after = datetime.now(UTC)
        ts = mock_cons._last_sleep_cycle
        assert ts is not None, "_last_sleep_cycle must be set after mode='full'"
        assert before <= ts <= after, f"Timestamp {ts} outside [{before}, {after}]"

    def test_consolidate_now_full_sleep_cycle_exception_caught(self, mock_state):
        """If run_sleep_cycle raises, exception is caught; result still has status='completed'."""
        mock_cons, mock_slp = mock_state
        mock_slp.run_sleep_cycle.side_effect = RuntimeError("sleep exploded")

        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="full")

        assert result.get("status") == "completed"
        assert "sleep_cycle" not in result


# ---------------------------------------------------------------------------
# 3. Invalid mode
# ---------------------------------------------------------------------------


class TestInvalidMode:
    """Invalid mode values return error without calling force_consolidate."""

    def test_consolidate_now_invalid_mode_returns_error(self, mock_state):
        """mode='invalid' returns error dict without calling force_consolidate."""
        mock_cons, _ = mock_state
        from yadgar.server.tools.admin_other import consolidate_now

        result = consolidate_now(mode="invalid")

        assert result.get("status") == "error"
        assert "Invalid mode" in result.get("message", "")
        mock_cons.force_consolidate.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Result includes 'mode' field
# ---------------------------------------------------------------------------


class TestResultShape:
    """Result dict must include 'mode' key reflecting what was requested."""

    def test_consolidate_now_result_includes_mode_light(self, mock_state):
        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")
        assert result.get("mode") == "light"

    def test_consolidate_now_result_includes_mode_full(self, mock_state):
        with patch("yadgar.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="full")
        assert result.get("mode") == "full"
