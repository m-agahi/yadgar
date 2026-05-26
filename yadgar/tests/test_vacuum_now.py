"""Tests for vacuum_now() MCP tool and _fire_vacuum_service helper (v4.9 item 1).

PR-4 (v5.7.0): converted from systemctl to trigger-file pattern.

TDD: tests written first before implementation.
Run with: YADGAR_TEST=1 pytest yadgar/tests/test_vacuum_now.py -x
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helper: build a minimal storage stub
# ---------------------------------------------------------------------------


def _make_storage(db_size_bytes: int = 500 * 1024 * 1024) -> MagicMock:
    """Return a storage stub whose get_db_size() returns db_size_bytes."""
    storage = MagicMock()
    storage.get_db_size.return_value = {
        "db_size_bytes": db_size_bytes,
        "vlog_size_bytes": db_size_bytes,
        "sstables_size_bytes": 0,
        "wal_size_bytes": 0,
        "size_warning": False,
    }
    return storage


# ---------------------------------------------------------------------------
# Tests for _fire_vacuum_service (trigger-file pattern)
# ---------------------------------------------------------------------------


class TestFireVacuumService:
    def test_writes_trigger_file(self, tmp_path):
        """_fire_vacuum_service must write the trigger file at the configured path."""
        from yadgar.ops import _fire_vacuum_service

        trigger = tmp_path / "vacuum_requested"
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}):
            _fire_vacuum_service()

        assert trigger.exists(), "trigger file must exist after _fire_vacuum_service()"

    def test_trigger_file_contents_valid_json(self, tmp_path):
        """Trigger file must contain valid JSON with requested_at and source."""
        from yadgar.ops import _fire_vacuum_service

        trigger = tmp_path / "vacuum_requested"
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}):
            _fire_vacuum_service()

        data = json.loads(trigger.read_text())
        assert "requested_at" in data, "JSON must have requested_at key"
        assert "source" in data, "JSON must have source key"

    def test_write_is_atomic_no_tmp_file_after(self, tmp_path):
        """After success, no *.tmp file must remain (atomic rename)."""
        from yadgar.ops import _fire_vacuum_service

        trigger = tmp_path / "vacuum_requested"
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}):
            _fire_vacuum_service()

        tmp_file = Path(str(trigger) + ".tmp")
        assert not tmp_file.exists(), "*.tmp file must be gone after atomic rename"

    def test_returns_trigger_path(self, tmp_path):
        """_fire_vacuum_service must return the Path of the written trigger file."""
        from yadgar.ops import _fire_vacuum_service

        trigger = tmp_path / "vacuum_requested"
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}):
            result = _fire_vacuum_service()

        assert str(result) == str(trigger), "must return path of written trigger"

    def test_env_knob_override_honored(self, tmp_path):
        """YADGAR_VACUUM_TRIGGER_PATH env var must override the default path."""
        from yadgar.ops import _fire_vacuum_service

        custom_path = tmp_path / "custom" / "vac_trigger"
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(custom_path)}):
            _fire_vacuum_service()

        assert custom_path.exists()

    def test_creates_parent_dir_if_missing(self, tmp_path):
        """Parent directory must be created if it does not exist."""
        from yadgar.ops import _fire_vacuum_service

        trigger = tmp_path / "nested" / "dirs" / "vacuum_requested"
        assert not trigger.parent.exists()
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}):
            _fire_vacuum_service()

        assert trigger.exists()

    def test_io_error_raises_runtime_error(self, tmp_path):
        """I/O failure writing trigger file must raise RuntimeError."""
        import pytest

        from yadgar.ops import _fire_vacuum_service

        # Point at a path whose parent is an existing FILE (not a dir) → mkdir fails
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file")
        trigger = blocker / "vacuum_requested"  # parent is a file → cannot mkdir
        with patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}):
            with pytest.raises(RuntimeError, match="trigger"):
                _fire_vacuum_service()


# ---------------------------------------------------------------------------
# Tests for vacuum_now() happy path
# ---------------------------------------------------------------------------


class TestVacuumNowHappyPath:
    def test_started_true_with_before_bytes(self, tmp_path):
        """Happy path: returns started=True and before_bytes from DB size."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)
        trigger = tmp_path / "vacuum_requested"

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is True
        assert result["before_bytes"] == 500 * 1024 * 1024
        assert result["skipped_reason"] is None

    def test_returns_trigger_path_field(self, tmp_path):
        """Happy path: result must contain trigger_path field with the written path."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)
        trigger = tmp_path / "vacuum_requested"

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}),
        ):
            result = srv.vacuum_now(force=False)

        assert "trigger_path" in result
        assert result["trigger_path"] == str(trigger)

    def test_trigger_file_actually_written(self, tmp_path):
        """vacuum_now() happy path must produce a trigger file on disk."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)
        trigger = tmp_path / "vacuum_requested"

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}),
        ):
            srv.vacuum_now(force=False)

        assert trigger.exists()

    def test_no_service_unit_field(self, tmp_path):
        """service_unit field must no longer be present in the happy-path response."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)
        trigger = tmp_path / "vacuum_requested"

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}),
        ):
            result = srv.vacuum_now(force=False)

        assert "service_unit" not in result, "service_unit must be removed"


# ---------------------------------------------------------------------------
# Tests for vacuum_now() refusals (threshold / force)
# ---------------------------------------------------------------------------


class TestVacuumNowRefusals:
    def test_db_below_threshold_no_force(self):
        """DB < 200 MiB without force=True → skipped_reason=db_below_threshold."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=100 * 1024 * 1024)  # 100 MiB

        with patch.object(srv, "_get_storage", return_value=storage):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "db_below_threshold"

    def test_db_below_threshold_with_force_proceeds(self, tmp_path):
        """force=True bypasses the 200 MiB threshold check."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=100 * 1024 * 1024)  # 100 MiB
        trigger = tmp_path / "vacuum_requested"

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch.dict(os.environ, {"YADGAR_VACUUM_TRIGGER_PATH": str(trigger)}),
        ):
            result = srv.vacuum_now(force=True)

        assert result["started"] is True
        assert trigger.exists()
