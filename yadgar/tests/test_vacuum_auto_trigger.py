"""Tests for threshold auto-trigger vacuum in ConsolidationScheduler (v4.9 item 2).

TDD: tests written first before implementation.
Run with: YADGAR_TEST=1 pytest yadgar/tests/test_vacuum_auto_trigger.py -x
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from yadgar._shared.config import Settings
from yadgar.core import consolidation as consolidation_mod
from yadgar.core.consolidation import ConsolidationScheduler, _in_window

# ---------------------------------------------------------------------------
# _in_window unit tests
# ---------------------------------------------------------------------------


class TestInWindow:
    def test_before_window_returns_false(self):
        """18:59 local is outside the default 19:00–23:00 window."""
        # Use a fixed UTC time that maps to 18:59 in any offset-0 environment.
        # _in_window works in local time, so we build a naive local datetime.
        now = datetime(2026, 5, 14, 18, 59, 0)
        assert _in_window(now, "19:00", "23:00") is False

    def test_at_window_start_returns_true(self):
        """19:00 exactly is inside the window."""
        now = datetime(2026, 5, 14, 19, 0, 0)
        assert _in_window(now, "19:00", "23:00") is True

    def test_inside_window_returns_true(self):
        """21:30 is inside the 19:00–23:00 window."""
        now = datetime(2026, 5, 14, 21, 30, 0)
        assert _in_window(now, "19:00", "23:00") is True

    def test_at_window_end_returns_false(self):
        """23:00 is the exclusive end of the window."""
        now = datetime(2026, 5, 14, 23, 0, 0)
        assert _in_window(now, "19:00", "23:00") is False

    def test_after_window_returns_false(self):
        """23:01 is outside the window."""
        now = datetime(2026, 5, 14, 23, 1, 0)
        assert _in_window(now, "19:00", "23:00") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(
    db_size_bytes: int = 3 * 1024 * 1024 * 1024,  # 3 GiB — above threshold
    vacuum_auto_enabled: bool = True,
    vacuum_auto_threshold_bytes: int = 2 * 1024 * 1024 * 1024,  # 2 GiB
    vacuum_auto_window_start: str = "19:00",
    vacuum_auto_window_end: str = "23:00",
) -> tuple[ConsolidationScheduler, MagicMock]:
    """Return (scheduler, storage_mock) with settings pre-configured."""
    storage = MagicMock()
    storage.get_db_size.return_value = {
        "db_size_bytes": db_size_bytes,
        "vlog_size_bytes": db_size_bytes,
        "sstables_size_bytes": 0,
        "wal_size_bytes": 0,
        "size_warning": False,
    }
    storage.get_max_episode_id.return_value = 0
    storage.get_episodes_since.return_value = []
    storage.insert_consolidation_log.return_value = None
    storage.prune_old_rows.return_value = 0
    storage.prune_old_episodes.return_value = 0
    storage._q.return_value = []

    settings = Settings(
        DB_PATH="/tmp/test.db",
        DAEMON_CHECK_INTERVAL=30,
        VACUUM_AUTO_ENABLED=vacuum_auto_enabled,
        VACUUM_AUTO_THRESHOLD_BYTES=vacuum_auto_threshold_bytes,
        VACUUM_AUTO_WINDOW_START=vacuum_auto_window_start,
        VACUUM_AUTO_WINDOW_END=vacuum_auto_window_end,
    )

    embeddings = MagicMock()
    embeddings.encode.return_value = [[0.0] * 384]

    scheduler = ConsolidationScheduler.__new__(ConsolidationScheduler)
    scheduler._storage = storage
    scheduler._embeddings = embeddings
    scheduler._settings = settings
    scheduler._thermo = MagicMock()
    scheduler._graph = MagicMock()
    scheduler._curator = MagicMock()
    scheduler._curator.memify_cycle.return_value = {}
    scheduler._sleep_engine = MagicMock()
    scheduler._cls = MagicMock()
    scheduler._cls.consolidation_cycle.return_value = {}
    scheduler._last_sleep_cycle = None
    scheduler._last_consolidation_date = None
    scheduler._last_cycle_completed_at = datetime.fromtimestamp(0, UTC)
    scheduler.last_activity = datetime.now(UTC)
    scheduler.is_running = False
    scheduler._thread = None
    scheduler._stop_event = MagicMock()
    scheduler._last_consolidated_episode_id = 0
    scheduler._causal_discovery = None
    scheduler._pool = None
    scheduler._events_since_last_discovery = 0
    scheduler._last_vacuum_at = None

    return scheduler, storage


# ---------------------------------------------------------------------------
# Auto-trigger window tests
# ---------------------------------------------------------------------------


class TestAutoTriggerWindow:
    def test_outside_window_does_not_fire(self):
        """DB over threshold but at 18:59 → no fire."""
        scheduler, storage = _make_scheduler()

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            # Inject a time outside the window (18:59 local)
            outside_time = datetime(2026, 5, 14, 18, 59, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=outside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_not_called()

    def test_inside_window_fires(self):
        """DB over threshold at 19:01 → fires."""
        scheduler, storage = _make_scheduler()

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 19, 1, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_called_once()

    def test_below_threshold_does_not_fire(self):
        """DB at 1 GiB (below 2 GiB threshold) → no fire even inside window."""
        scheduler, storage = _make_scheduler(db_size_bytes=1 * 1024 * 1024 * 1024)

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_not_called()

    def test_disabled_does_not_fire(self):
        """VACUUM_AUTO_ENABLED=False → no fire even over threshold in window."""
        scheduler, storage = _make_scheduler(vacuum_auto_enabled=False)

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_not_called()


# ---------------------------------------------------------------------------
# Cooldown tests
# ---------------------------------------------------------------------------


class TestAutoTriggerCooldown:
    def test_cooldown_skips_when_recent(self):
        """last_vacuum_at set 1 hour ago → auto-trigger skips."""
        scheduler, storage = _make_scheduler()
        # Set last vacuum to 1 hour ago
        scheduler._last_vacuum_at = datetime.now(UTC) - timedelta(hours=1)

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_not_called()

    def test_cooldown_fires_after_6_hours(self):
        """last_vacuum_at 7 hours ago → cooldown expired, fires."""
        scheduler, storage = _make_scheduler()
        scheduler._last_vacuum_at = datetime.now(UTC) - timedelta(hours=7)

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_called_once()

    def test_fire_updates_last_vacuum_at(self):
        """After firing, last_vacuum_at is set on the scheduler."""
        scheduler, storage = _make_scheduler()
        scheduler._last_vacuum_at = None  # Never fired

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service"),
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        assert scheduler._last_vacuum_at is not None


# ---------------------------------------------------------------------------
# Fix 3: HH:MM validator tests
# ---------------------------------------------------------------------------


class TestHHMMValidator:
    def test_valid_start_accepted(self):
        """Valid HH:MM values must not raise."""
        s = Settings(VACUUM_AUTO_WINDOW_START="02:30", VACUUM_AUTO_WINDOW_END="06:00")
        assert s.VACUUM_AUTO_WINDOW_START == "02:30"

    def test_invalid_start_raises(self):
        """Invalid format must raise ValidationError at construction."""
        with pytest.raises(ValidationError, match="HH:MM"):
            Settings(VACUUM_AUTO_WINDOW_START="25:00", VACUUM_AUTO_WINDOW_END="23:00")

    def test_invalid_end_raises(self):
        """Invalid end format must raise ValidationError at construction."""
        with pytest.raises(ValidationError, match="HH:MM"):
            Settings(VACUUM_AUTO_WINDOW_START="19:00", VACUUM_AUTO_WINDOW_END="23:99")

    def test_missing_colon_raises(self):
        """No-colon garbage must raise ValidationError."""
        with pytest.raises(ValidationError):
            Settings(VACUUM_AUTO_WINDOW_START="1900", VACUUM_AUTO_WINDOW_END="23:00")


# ---------------------------------------------------------------------------
# Fix 4: Cross-midnight window tests
# ---------------------------------------------------------------------------


class TestInWindowCrossMidnight:
    def test_wrap_inside_before_midnight(self):
        """23:30 is inside 23:00–02:00 window."""
        now = datetime(2026, 5, 14, 23, 30, 0)
        assert _in_window(now, "23:00", "02:00") is True

    def test_wrap_inside_after_midnight(self):
        """01:30 is inside 23:00–02:00 window."""
        now = datetime(2026, 5, 14, 1, 30, 0)
        assert _in_window(now, "23:00", "02:00") is True

    def test_wrap_outside_midday(self):
        """12:00 is outside 23:00–02:00 window."""
        now = datetime(2026, 5, 14, 12, 0, 0)
        assert _in_window(now, "23:00", "02:00") is False

    def test_wrap_at_end_exclusive(self):
        """02:00 exactly is exclusive end of 23:00–02:00 window."""
        now = datetime(2026, 5, 14, 2, 0, 0)
        assert _in_window(now, "23:00", "02:00") is False

    def test_equal_start_end_false(self):
        """Equal start == end → always False (zero-length window)."""
        now = datetime(2026, 5, 14, 12, 0, 0)
        assert _in_window(now, "12:00", "12:00") is False


# ---------------------------------------------------------------------------
# Fix 5: is-active pre-check + cooldown stamp only on success
# ---------------------------------------------------------------------------


class TestIsActivePreCheck:
    def test_inactive_fires_and_updates_cooldown(self):
        """If unit is inactive (CalledProcessError from is-active), fire and set cooldown."""
        scheduler, storage = _make_scheduler()
        assert scheduler._last_vacuum_at is None

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
            patch(
                "subprocess.check_output",
                side_effect=subprocess.CalledProcessError(3, "systemctl"),
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_called_once()
        assert scheduler._last_vacuum_at is not None


# ---------------------------------------------------------------------------
# v5.7.1: container-safe trigger — no systemctl required
# ---------------------------------------------------------------------------


class TestContainerSafeAutoTrigger:
    def test_auto_trigger_fires_when_systemctl_missing_filenotfound(self):
        """FileNotFoundError from systemctl must NOT block _fire_vacuum_service.

        Containerized deploys have no systemctl on PATH.  The pre-check
        (removed in v5.7.1) raised FNFE and returned early, silently
        suppressing the threshold backstop.  After the fix, _fire_vacuum_service
        is called unconditionally once all other guards (cooldown, window,
        threshold) pass.
        """
        scheduler, storage = _make_scheduler()
        assert scheduler._last_vacuum_at is None

        with (
            patch.object(consolidation_mod, "_fire_vacuum_service") as mock_fire,
            patch(
                "yadgar.core.server._run_check_invariants",
                return_value={"ok": True, "violations": [], "fixed": [], "counts": {}},
            ),
        ):
            inside_time = datetime(2026, 5, 14, 21, 0, 0)
            with patch.object(consolidation_mod, "_now_local", return_value=inside_time):
                scheduler._consolidation_cycle()

        mock_fire.assert_called_once()
        assert scheduler._last_vacuum_at is not None
