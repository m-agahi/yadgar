"""Tests for yadgar/scripts/nightly_cycle.py — nightly orchestration steps.

Coverage targets:
- _run_systemctl: success + failure
- _log_step / _log_start: emit log records (no crash)
- _step_stop_core: success returns 0, failure returns 10
- _step_pre_backup: success returns 0, failure returns 20
- _step_vacuum: success/failure returns 0/40
- _step_post_backup: stop fails returns 50, backup fails returns 50, success returns 0
- _step_prune: success returns 0, failure returns 60
- _step_start_core: success returns 0, failure returns 70
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


# ── stop/start BOTH units (v5.69 P5) ───────────────────────────────────────────


def test_stop_both_units_stops_core_then_backend():
    """P5: stop core first, THEN backend — releases the surrealkv lock."""
    with patch.object(nc, "_stop_service") as mock_stop:
        nc._stop_both_units()
    assert [c.args[0] for c in mock_stop.call_args_list] == [nc._UNIT_CORE, nc._UNIT_BACKEND]


def test_start_both_units_starts_backend_then_core():
    """P5: start backend first, THEN core (After/Wants dependency order)."""
    with patch.object(nc, "_start_service") as mock_start:
        nc._start_both_units()
    assert [c.args[0] for c in mock_start.call_args_list] == [nc._UNIT_BACKEND, nc._UNIT_CORE]


def test_step_stop_core_stops_both_units():
    """P5: _step_stop_core stops BOTH units (not just core)."""
    with patch.object(nc, "_stop_service") as mock_stop:
        result = nc._step_stop_core()
    assert result == 0
    stopped = {c.args[0] for c in mock_stop.call_args_list}
    assert stopped == {nc._UNIT_CORE, nc._UNIT_BACKEND}


def test_step_vacuum_restarts_backend_before_vacuum():
    """P5: _step_vacuum starts the backend (restart after both-units stop) before
    invoking cmd_vacuum_impl, which requires the backend reachable."""
    with patch.object(nc, "_start_service") as mock_start:
        with patch("yadgar.scripts.nightly_cycle.cmd_vacuum_impl", return_value=0):
            result = nc._step_vacuum("/tmp/db", "http://backend:8001", None)
    assert result == 0
    mock_start.assert_called_once_with(nc._UNIT_BACKEND)


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


# ── _step_stop_core ───────────────────────────────────────────────────────────


def test_step_stop_core_success():
    with patch.object(nc, "_run_systemctl"):
        result = nc._step_stop_core()
    assert result == 0


def test_step_stop_core_failure():
    with patch.object(nc, "_run_systemctl", side_effect=RuntimeError("fail")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_stop_core()
    assert result == 10


# ── _step_pre_backup ──────────────────────────────────────────────────────────


def test_step_pre_backup_success(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot") as mock_snap:
        result = nc._step_pre_backup(db_path, snapshot_dir)
    assert result == 0
    mock_snap.assert_called_once_with(db_path, snapshot_dir=snapshot_dir, label="nightly-pre")


def test_step_pre_backup_failure(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch("yadgar.scripts.nightly_cycle.create_snapshot", side_effect=OSError("disk full")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_pre_backup(db_path, snapshot_dir)
    assert result == 20


# ── _step_vacuum ──────────────────────────────────────────────────────────────


def test_step_vacuum_success(tmp_path):
    # v5.69 P5: _step_vacuum restarts the backend via systemctl before vacuuming;
    # stub _run_systemctl so the unit test exercises vacuum LOGIC, not the host
    # service boundary (systemctl is absent in CI containers).
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
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch.object(nc, "_run_systemctl"):
        with patch("yadgar.scripts.nightly_cycle.create_snapshot") as mock_snap:
            result = nc._step_post_backup(db_path, snapshot_dir)
    assert result == 0
    mock_snap.assert_called_once_with(db_path, snapshot_dir=snapshot_dir, label="nightly-post")


def test_step_post_backup_stop_fails_returns_50(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch.object(nc, "_run_systemctl", side_effect=RuntimeError("stop failed")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_post_backup(db_path, snapshot_dir)
    assert result == 50


def test_step_post_backup_snapshot_fails_returns_50(tmp_path):
    db_path = tmp_path / "db"
    snapshot_dir = tmp_path / "snapshots"
    with patch.object(nc, "_run_systemctl"):
        with patch("yadgar.scripts.nightly_cycle.create_snapshot", side_effect=OSError("fail")):
            with patch("yadgar.scripts.nightly_cycle.record_exception"):
                result = nc._step_post_backup(db_path, snapshot_dir)
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


# ── _step_start_core ──────────────────────────────────────────────────────────


def test_step_start_core_success():
    with patch.object(nc, "_run_systemctl"):
        result = nc._step_start_core()
    assert result == 0


def test_step_start_core_failure_returns_70():
    with patch.object(nc, "_run_systemctl", side_effect=RuntimeError("fail")):
        with patch("yadgar.scripts.nightly_cycle.record_exception"):
            result = nc._step_start_core()
    assert result == 70
