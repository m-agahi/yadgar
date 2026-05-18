"""Tests for vacuum_now() MCP tool and _fire_vacuum_service helper (v4.9 item 1).

TDD: tests written first before implementation.
Run with: YADGAR_TEST=1 pytest yadgar/tests/test_vacuum_now.py -x
"""

from __future__ import annotations

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
# Tests for _fire_vacuum_service
# ---------------------------------------------------------------------------


class TestFireVacuumService:
    def test_invokes_no_block(self):
        """_fire_vacuum_service must pass --no-block to systemctl."""
        from yadgar.ops import _fire_vacuum_service

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _fire_vacuum_service()
            call_args = mock_run.call_args[0][0]
            assert "--no-block" in call_args
            assert "yadgar-vacuum.service" in call_args


# ---------------------------------------------------------------------------
# Tests for vacuum_now() happy path
# ---------------------------------------------------------------------------


class TestVacuumNowHappyPath:
    def test_started_true_with_before_bytes(self):
        """Happy path: returns started=True and before_bytes from DB size."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value=b"inactive\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = srv.vacuum_now(force=False)

        assert result["started"] is True
        assert result["before_bytes"] == 500 * 1024 * 1024
        assert result["service_unit"] == "yadgar-vacuum.service"
        assert result["skipped_reason"] is None

    def test_systemctl_called_with_no_block(self):
        """systemctl --user start --no-block must appear in the subprocess call."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value=b"inactive\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            srv.vacuum_now(force=False)

        calls = mock_run.call_args_list
        assert any(
            "--no-block" in args[0][0] and "yadgar-vacuum.service" in args[0][0] for args in calls
        )


# ---------------------------------------------------------------------------
# Tests for vacuum_now() refusals
# ---------------------------------------------------------------------------


class TestVacuumNowRefusals:
    def test_db_below_threshold_no_force(self):
        """DB < 200 MiB without force=True → skipped_reason=db_below_threshold."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=100 * 1024 * 1024)  # 100 MiB

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "db_below_threshold"

    def test_db_below_threshold_with_force_proceeds(self):
        """force=True bypasses the 200 MiB threshold check."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=100 * 1024 * 1024)  # 100 MiB

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value=b"inactive\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = srv.vacuum_now(force=True)

        assert result["started"] is True

    def test_service_already_active(self):
        """Vacuum service already active → skipped_reason=vacuum_already_running."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
            patch("subprocess.check_output", return_value=b"active\n"),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "vacuum_already_running"

    def test_service_activating(self):
        """Vacuum service activating → skipped_reason=vacuum_already_running."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
            patch("subprocess.check_output", return_value=b"activating\n"),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "vacuum_already_running"

    def test_no_service_manager(self):
        """Manual mode → requires_host_systemctl + actionable fields (V4)."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="manual"),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "requires_host_systemctl"
        # Preferred host command
        assert "host_command" in result
        assert "yadgar-vacuum.service" in result["host_command"]
        # Fallback direct invocation
        assert "fallback_host_command" in result
        assert "yadgar vacuum" in result["fallback_host_command"]
        # Detail message
        assert "detail" in result
        assert "host" in result["detail"].lower()
        # Backward compat: old shell_command field still present
        assert "shell_command" in result
        assert "yadgar vacuum" in result["shell_command"]

    def test_docker_mode_skips_without_systemctl(self):
        """Docker mode → requires_host_systemctl + actionable fields (V4)."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="docker"),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "requires_host_systemctl"
        assert "host_command" in result
        assert "yadgar-vacuum.service" in result["host_command"]
        assert "fallback_host_command" in result
        assert "detail" in result
        # Backward compat: old shell_command still present
        assert "shell_command" in result

    def test_file_not_found_on_is_active_skips(self):
        """systemctl not found during is-active → requires_host_systemctl (V4)."""
        from yadgar import server as srv

        storage = _make_storage(db_size_bytes=500 * 1024 * 1024)

        with (
            patch.object(srv, "_get_storage", return_value=storage),
            patch("yadgar.ops.detect_service_mode", return_value="systemd"),
            patch("subprocess.check_output", side_effect=FileNotFoundError("systemctl not found")),
        ):
            result = srv.vacuum_now(force=False)

        assert result["started"] is False
        assert result["skipped_reason"] == "requires_host_systemctl"
        assert "host_command" in result
        assert "yadgar-vacuum.service" in result["host_command"]
        assert "fallback_host_command" in result
        assert "detail" in result
        # Backward compat: old shell_command still present
        assert "shell_command" in result


class TestFireVacuumServiceErrors:
    def test_file_not_found_raises_runtime_error(self):
        """_fire_vacuum_service raises RuntimeError when systemctl binary missing."""
        from yadgar.ops import _fire_vacuum_service

        with patch("subprocess.run", side_effect=FileNotFoundError("systemctl not found")):
            import pytest

            with pytest.raises(RuntimeError, match="systemctl not available"):
                _fire_vacuum_service()
