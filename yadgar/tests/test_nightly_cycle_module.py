"""Tests for yadgar/scripts/nightly_cycle.py — nightly orchestration steps.

Coverage targets:
- _run_systemctl: success + failure
- _log_step / _log_start: emit log records (no crash)
- _step_stop_core: success returns 0, failure returns 10; stops ONLY core
- _step_pre_backup: success returns 0, failure returns 20; passes backend_url
- _step_vacuum: success/failure returns 0/40; backend start is a safety no-op
- _step_post_backup: failure returns 50, success returns 0; passes backend_url; NO stop
- _step_prune: success returns 0, failure returns 60
- _step_start_core: success returns 0, failure returns 70; starts ONLY core
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

import yadgar.scripts.nightly_cycle as nc

# ── _run_systemctl ─────────────────────────────────────────────────────────────


def test_run_systemctl_success(monkeypatch):
    mock_result = MagicMock(returncode=0)
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        nc._run_systemctl("start", "yadgar")
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "systemctl" in args
    assert "start" in args
    assert "yadgar" in args


def test_run_systemctl_raises_on_failure(monkeypatch):
    mock_result = MagicMock(returncode=1, stderr=b"Unit not found")
    with patch("yadgar.scripts.nightly_cycle.time.sleep"):  # no real backoff sleep
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="Unit not found"):
                nc._run_systemctl("start", "yadgar")


# ── _run_systemctl bounded retry (v5.69 P5) ────────────────────────────────────


def test_run_systemctl_retries_then_succeeds(monkeypatch):
    """A transient D-Bus failure (fail once, then succeed) is absorbed by retry."""
    results = [MagicMock(returncode=1, stderr=b"transient dbus"), MagicMock(returncode=0)]
    with patch("yadgar.scripts.nightly_cycle.time.sleep") as mock_sleep:
        with patch("subprocess.run", side_effect=results) as mock_run:
            nc._run_systemctl("stop", "yadgar-backend")  # must NOT raise
    assert mock_run.call_count == 2, "should retry once then succeed"
    assert mock_sleep.call_count == 1, "should back off once between the two attempts"


def test_run_systemctl_exhausts_bounded_retries(monkeypatch):
    """Persistent failure raises after exactly _SYSTEMCTL_RETRIES attempts (bounded)."""
    mock_result = MagicMock(returncode=1, stderr=b"still down")
    with patch("yadgar.scripts.nightly_cycle.time.sleep"):
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with pytest.raises(RuntimeError, match="after .* attempts: still down"):
                nc._run_systemctl("start", "yadgar")
    assert mock_run.call_count == nc._SYSTEMCTL_RETRIES, "retry must be bounded, not infinite"


# ── Step 1: stop ONLY core (#51 — backend stays up) ───────────────────────────


def test_step_stop_core_stops_only_core():
    """#51 / BC-D1: _step_stop_core stops ONLY core. Backend is NOT stopped."""
    with patch.object(nc, "_stop_service") as mock_stop:
        result = nc._step_stop_core()
    assert result == 0
    mock_stop.assert_called_once_with(nc._UNIT_CORE)


def test_step_stop_core_does_not_stop_backend():
    """Backend must stay up throughout the nightly (#51); step 1 must never stop it."""
    with patch.object(nc, "_stop_service") as mock_stop:
        nc._step_stop_core()
    stopped_units = [c.args[0] for c in mock_stop.call_args_list]
    assert nc._UNIT_BACKEND not in stopped_units, (
        f"step 1 must not stop the backend (BC-D1 / #51); got stops={stopped_units}"
    )


def test_step_stop_core_success():
    with patch.object(nc, "_run_systemctl"):
        result = nc._step_stop_core()
    assert result == 0


def test_step_stop_core_failure():
    with patch.object(nc, "_run_systemctl", side_effect=RuntimeError("fail")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_stop_core()
    assert result == 10


# ── Step 7: start ONLY core (#51 — backend was never stopped) ─────────────────


def test_step_start_core_starts_only_core():
    """#51 / BC-D1: _step_start_core starts ONLY core. Backend was never stopped."""
    with patch.object(nc, "_start_service") as mock_start:
        result = nc._step_start_core()
    assert result == 0
    mock_start.assert_called_once_with(nc._UNIT_CORE)


def test_step_start_core_does_not_start_backend():
    """Backend stayed up the whole cycle; step 7 must not restart it redundantly."""
    with patch.object(nc, "_start_service") as mock_start:
        nc._step_start_core()
    started_units = [c.args[0] for c in mock_start.call_args_list]
    assert nc._UNIT_BACKEND not in started_units, (
        f"step 7 must not start backend (it was never stopped); got starts={started_units}"
    )


def test_step_start_core_success():
    with patch.object(nc, "_run_systemctl"):
        result = nc._step_start_core()
    assert result == 0


def test_step_start_core_failure_returns_70():
    with patch.object(nc, "_run_systemctl", side_effect=RuntimeError("fail")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_start_core()
    assert result == 70


# ── _log_step + _log_start ────────────────────────────────────────────────────


def test_log_step_emits_info(caplog):
    with caplog.at_level(logging.INFO, logger="yadgar.nightly_cycle"):
        nc._log_step("test_step", "ok", 42.0)
    assert any("ok" in r.message for r in caplog.records)


def test_log_step_with_extra_kwargs(caplog):
    with caplog.at_level(logging.INFO, logger="yadgar.nightly_cycle"):
        nc._log_step("backup", "ok", 100.0, removed=5)
    assert any("backup" in r.message for r in caplog.records)


def test_log_start_emits_info(caplog):
    with caplog.at_level(logging.INFO, logger="yadgar.nightly_cycle"):
        nc._log_start("stop_core")
    assert any("start" in r.message or "stop_core" in r.message for r in caplog.records)


# ── _step_pre_backup ──────────────────────────────────────────────────────────


def test_step_pre_backup_success(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    backend_url = "http://backend:8001"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot") as mock_snap:
        result = nc._step_pre_backup(db_path, snapshot_dir, backend_url)
    assert result == 0
    mock_snap.assert_called_once_with(
        db_path, snapshot_dir=snapshot_dir, label="nightly-pre", backend_url=backend_url
    )


def test_step_pre_backup_passes_backend_url(tmp_path):
    """#51: pre-backup must use HTTP export (backend_url kwarg) not copytree."""
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    backend_url = "http://backend:8001"
    captured = {}
    with patch(
        "yadgar.scripts.nightly_cycle.create_snapshot",
        side_effect=lambda *a, **kw: captured.update(kw) or None,
    ):
        nc._step_pre_backup(db_path, snapshot_dir, backend_url)
    assert captured.get("backend_url") == backend_url, (
        f"_step_pre_backup must forward backend_url to create_snapshot; got {captured}"
    )


def test_step_pre_backup_failure(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot", side_effect=OSError("disk full")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_pre_backup(db_path, snapshot_dir, "http://backend:8001")
    assert result == 20


# ── _step_vacuum ──────────────────────────────────────────────────────────────


def test_step_vacuum_starts_backend_as_safety_noop():
    """#51: _step_vacuum still calls _start_service(backend) as a safety no-op
    (backend is already up; starting an active unit is harmless). This preserves
    the fallback in case a prior step left the backend down unexpectedly."""
    with patch.object(nc, "_start_service") as mock_start:
        with patch("yadgar.scripts.nightly_cycle.cmd_vacuum_impl", return_value=0):
            result = nc._step_vacuum("/tmp/db", "http://backend:8001", None)
    assert result == 0
    mock_start.assert_called_once_with(nc._UNIT_BACKEND)


def test_step_vacuum_success(tmp_path):
    with patch.object(nc, "_run_systemctl"):
        with patch("yadgar.scripts.nightly_cycle.cmd_vacuum_impl", return_value=0):
            result = nc._step_vacuum(tmp_path / "db", "http://backend:8001", None)
    assert result == 0


def test_step_vacuum_degraded_returns_zero(tmp_path):
    # Exit code 2 from vacuum = degraded but not failure
    with patch.object(nc, "_run_systemctl"):
        with patch("yadgar.scripts.nightly_cycle.cmd_vacuum_impl", return_value=2):
            result = nc._step_vacuum(tmp_path / "db", "http://backend:8001", None)
    assert result == 0


def test_step_vacuum_unexpected_exit_code_returns_40(tmp_path):
    with patch.object(nc, "_run_systemctl"):
        with patch("yadgar.scripts.nightly_cycle.cmd_vacuum_impl", return_value=99):
            with patch("yadgar.scripts.nightly_cycle.record_exception"):
                result = nc._step_vacuum(tmp_path / "db", "http://backend:8001", None)
    assert result == 40


def test_step_vacuum_exception_returns_40(tmp_path):
    with patch.object(nc, "_run_systemctl"):
        with patch("yadgar.scripts.nightly_cycle.cmd_vacuum_impl", side_effect=RuntimeError("err")):
            with patch("yadgar.scripts.nightly_cycle.record_exception"):
                result = nc._step_vacuum(tmp_path / "db", "http://backend:8001", None)
    assert result == 40


# ── _step_post_backup ─────────────────────────────────────────────────────────


def test_step_post_backup_success(tmp_path):
    """#51: post-backup succeeds via HTTP export; no stop of any unit."""
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    backend_url = "http://backend:8001"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot") as mock_snap:
        result = nc._step_post_backup(db_path, snapshot_dir, backend_url)
    assert result == 0
    mock_snap.assert_called_once_with(
        db_path, snapshot_dir=snapshot_dir, label="nightly-post", backend_url=backend_url
    )


def test_step_post_backup_passes_backend_url(tmp_path):
    """#51: post-backup must use HTTP export (backend_url kwarg) not copytree."""
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    backend_url = "http://backend:8001"
    captured = {}
    with patch(
        "yadgar.scripts.nightly_cycle.create_snapshot",
        side_effect=lambda *a, **kw: captured.update(kw) or None,
    ):
        nc._step_post_backup(db_path, snapshot_dir, backend_url)
    assert captured.get("backend_url") == backend_url, (
        f"_step_post_backup must forward backend_url to create_snapshot; got {captured}"
    )


def test_step_post_backup_does_not_stop_any_unit(tmp_path):
    """#51: post-backup no longer stops any unit (backend stays up; HTTP export is consistent)."""
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot"):
        with patch.object(nc, "_stop_service") as mock_stop:
            nc._step_post_backup(db_path, snapshot_dir, "http://backend:8001")
    assert mock_stop.call_count == 0, (
        f"_step_post_backup must not stop any unit in HTTP mode; got {mock_stop.call_args_list}"
    )


def test_step_post_backup_snapshot_fails_returns_50(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot", side_effect=OSError("fail")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_post_backup(db_path, snapshot_dir, "http://backend:8001")
    assert result == 50


# ── _step_prune ───────────────────────────────────────────────────────────────


def test_step_prune_success(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.prune_snapshots", return_value=[]) as mock_prune:
        result = nc._step_prune(snapshot_dir, retention=3)
    assert result == 0
    mock_prune.assert_called_once()


def test_step_prune_failure_returns_60(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.prune_snapshots", side_effect=OSError("fail")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_prune(snapshot_dir, retention=3)
    assert result == 60


def test_step_prune_passes_retention(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    captured = []
    with patch(
        "yadgar.scripts.nightly_cycle.prune_snapshots",
        side_effect=lambda *a, **kw: captured.append(kw) or [],
    ):
        nc._step_prune(snapshot_dir, retention=7)
    assert captured[0]["retention"] == 7
