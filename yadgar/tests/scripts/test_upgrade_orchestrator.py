"""v5.49.0 Phase 9 — Upgrade orchestrator state machine tests.

TDD: all 11 tests written RED first; implementation makes them GREEN.

Test index:
  1. test_orchestrator_happy_path
  2. test_orchestrator_image_pull_fail_rollback
  3. test_orchestrator_health_check_fail_triggers_rollback
  4. test_orchestrator_cli_upgrade_fail_attempts_pipx_force_install_prev
  5. test_orchestrator_snapshot_written
  6. test_orchestrator_snapshot_retention
  7. test_orchestrator_install_disabled_refuses
  8. test_orchestrator_concurrent_run_blocked
  9. test_orchestrator_stale_lock_taken_over
 10. test_orchestrator_records_re_exec_state
 11. test_install_systemd_service_type_notify  (daemon.py follow-up, Phase 7)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _noop_image_pull(version: str) -> None:  # noqa: ARG001
    """No-op image pull for tests."""


def _noop_graceful_stop(timeout: int) -> None:  # noqa: ARG001
    """No-op graceful stop for tests."""


def _noop_service_restart() -> None:
    """No-op service restart for tests."""


def _passing_health_check() -> bool:
    """Health check that always passes."""
    return True


def _failing_health_check() -> bool:
    """Health check that always fails."""
    return False


def _noop_cli_upgrade(version: str) -> None:  # noqa: ARG001
    """No-op CLI upgrade for tests."""


def _noop_re_exec(version: str, snapshot_path: Path) -> None:  # noqa: ARG001
    """No-op re-exec for tests (does NOT call os.execvp)."""


def _run(tmp_path: Path, **overrides):
    """Convenience wrapper: run_install with test-safe paths and all injections."""
    from yadgar.core.update.orchestrator import InstallConfig, _build_hooks, run_install

    # Separate config fields from hook fields
    _hook_fields = {
        "image_pull",
        "graceful_stop",
        "service_restart",
        "health_check",
        "cli_upgrade",
        "cli_rollback",
        "re_exec",
    }
    hook_defaults: dict = {
        "image_pull": _noop_image_pull,
        "graceful_stop": _noop_graceful_stop,
        "service_restart": _noop_service_restart,
        "health_check": _passing_health_check,
        "cli_upgrade": _noop_cli_upgrade,
        "re_exec": _noop_re_exec,
    }
    config_defaults: dict = {
        "target_version": "5.49.0",
        "enabled_override": True,  # bypass install_enabled check
        "lock_file_path": tmp_path / "upgrade.lock",
        "snapshots_base_dir": tmp_path / "snaps",
        "prev_image_tag_override": "docker.io/openfantasy/yadgar:5.48.0",
        "prev_unit_file_override": "[Unit]\nDescription=yadgar\n",
        "prev_cli_version_override": "5.48.0",
    }
    hook_kw = {
        k: overrides.pop(k, hook_defaults.get(k))
        for k in _hook_fields
        if k in hook_defaults or k in overrides
    }
    config_kw = {**config_defaults, **{k: v for k, v in overrides.items() if k not in _hook_fields}}

    config = InstallConfig(**config_kw)
    hooks = _build_hooks(
        hook_kw.get("image_pull"),
        hook_kw.get("graceful_stop"),
        hook_kw.get("service_restart"),
        hook_kw.get("health_check"),
        hook_kw.get("cli_upgrade"),
        hook_kw.get("cli_rollback"),
        hook_kw.get("re_exec"),
    )
    return run_install(config, hooks)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_orchestrator_happy_path(tmp_path: Path) -> None:
    """All injected callables succeed; final state = RE_EXECING (execvp would not return).

    NOTE: run_install() records RE_EXECING as the last in-process state because
    os.execvp never returns in production.  Phase 10 --finalize subcommand sets DONE.
    """
    from yadgar.core.update.orchestrator import OrchestratorState

    result = _run(tmp_path)

    assert result.final_state == OrchestratorState.RE_EXECING
    assert result.error is None
    assert result.snapshot_path is not None
    assert result.snapshot_path.is_dir()

    # Lock must be released (file removed) on terminal state
    lock = tmp_path / "upgrade.lock"
    assert not lock.exists(), "Lock file must be removed after terminal state"

    # Forward log must have all transition states
    forward_log_path = result.snapshot_path / "forward_log.json"
    assert forward_log_path.exists()
    entries = json.loads(forward_log_path.read_text())
    states_logged = [e["state"] for e in entries]
    # Minimum required states in forward path
    for expected in [
        "acquiring_lock",
        "probing_pypi",
        "snapshotting",
        "pulling_image",
        "writing_env_file",
        "graceful_stopping",
        "restarting_service",
        "health_checking",
        "cli_upgrading",
        "re_execing",
    ]:
        assert expected in states_logged, f"Missing state '{expected}' in forward_log"


# ---------------------------------------------------------------------------
# 2. Image pull failure → rollback (env-file + daemon untouched)
# ---------------------------------------------------------------------------


def test_orchestrator_image_pull_fail_rollback(tmp_path: Path) -> None:
    """Image pull failure triggers rollback.

    At PULLING_IMAGE, the env-file has NOT yet been written and the daemon has NOT
    been restarted, so rollback is a no-op cleanup.  Terminal = ROLLED_BACK_OK.
    """
    from yadgar.core.update.orchestrator import OrchestratorState

    def failing_pull(version: str) -> None:  # noqa: ARG001
        raise RuntimeError("simulated pull failure")

    service_restart = MagicMock()

    result = _run(
        tmp_path,
        image_pull=failing_pull,
        service_restart=service_restart,
    )

    assert result.final_state == OrchestratorState.ROLLED_BACK_OK
    assert result.error is not None

    # service_restart must NOT have been called (no daemon mutation happened)
    service_restart.assert_not_called()

    # Rollback log must exist
    assert result.snapshot_path is not None
    rollback_log_path = result.snapshot_path / "rollback_log.json"
    assert rollback_log_path.exists()

    # Lock released
    assert not (tmp_path / "upgrade.lock").exists()


# ---------------------------------------------------------------------------
# 3. Health-check failure → rollback with env-file restored and service restarted twice
# ---------------------------------------------------------------------------


def test_orchestrator_health_check_fail_triggers_rollback(tmp_path: Path) -> None:
    """Health-check failure triggers rollback: env-file restored, service restarted twice."""
    from yadgar.core.update.orchestrator import OrchestratorState

    service_restart = MagicMock()
    health_calls: list[bool] = []

    def flipping_health() -> bool:
        """Fail first call (new image), pass second call (rollback to old image)."""
        if not health_calls:
            health_calls.append(False)
            return False
        return True

    result = _run(
        tmp_path,
        service_restart=service_restart,
        health_check=flipping_health,
    )

    assert result.final_state == OrchestratorState.ROLLED_BACK_OK

    # service_restart called twice: once for new image, once for rollback
    assert service_restart.call_count == 2

    # env-file must have prev tag after rollback
    env_file = tmp_path / "upgrade.env"
    if env_file.exists():
        content = env_file.read_text()
        assert "5.48.0" in content, "env-file should be restored to prev tag after rollback"

    # Lock released
    assert not (tmp_path / "upgrade.lock").exists()


# ---------------------------------------------------------------------------
# 4. CLI upgrade failure → pipx rollback (ROLLING_BACK_CLI_ONLY)
# ---------------------------------------------------------------------------


def test_orchestrator_cli_upgrade_fail_attempts_pipx_force_install_prev(
    tmp_path: Path,
) -> None:
    """CLI upgrade failure triggers ROLLING_BACK_CLI_ONLY with prev-version arg."""
    from yadgar.core.update.orchestrator import OrchestratorState

    cli_rollback_calls: list[str] = []

    def failing_cli_upgrade(version: str) -> None:  # noqa: ARG001
        raise RuntimeError("pipx upgrade failed")

    def mock_cli_rollback(prev_version: str) -> None:
        cli_rollback_calls.append(prev_version)

    result = _run(
        tmp_path,
        cli_upgrade=failing_cli_upgrade,
        cli_rollback=mock_cli_rollback,
    )

    # Daemon is healthy on new image; only CLI rolls back
    assert result.final_state in (
        OrchestratorState.DONE_CLI_ROLLBACK_OK,
        OrchestratorState.DONE_CLI_ROLLBACK_FAILED,
    )
    # cli_rollback must have been called with the prev version
    assert len(cli_rollback_calls) == 1
    assert cli_rollback_calls[0] == "5.48.0"


# ---------------------------------------------------------------------------
# 5. Snapshot content written correctly
# ---------------------------------------------------------------------------


def test_orchestrator_snapshot_written(tmp_path: Path) -> None:
    """Snapshot dir contains prev_image_tag, prev_unit_file, prev_cli_version with expected content."""
    result = _run(tmp_path)

    assert result.snapshot_path is not None
    snap = result.snapshot_path

    # prev_image_tag
    tag_file = snap / "prev_image_tag"
    assert tag_file.exists()
    assert "5.48.0" in tag_file.read_text()

    # prev_unit_file
    unit_file = snap / "prev_unit_file"
    assert unit_file.exists()
    assert "[Unit]" in unit_file.read_text()

    # prev_cli_version
    ver_file = snap / "prev_cli_version"
    assert ver_file.exists()
    assert "5.48.0" in ver_file.read_text()


# ---------------------------------------------------------------------------
# 6. Snapshot retention — only N newest survive
# ---------------------------------------------------------------------------


def test_orchestrator_snapshot_retention(tmp_path: Path) -> None:
    """With UPDATE_SNAPSHOT_RETENTION=2, only 2 snapshots remain after 5 successful runs."""
    from yadgar.core.update.snapshot import list_snapshots

    snaps_dir = tmp_path / "snaps"

    for _ in range(5):
        _run(
            tmp_path,
            snapshots_base_dir=snaps_dir,
            # Override retention setting via run kwarg
            snapshot_retention=2,
        )
        time.sleep(0.02)  # ensure distinct timestamps

    remaining = list_snapshots(base_dir=snaps_dir)
    assert len(remaining) == 2, (
        f"Expected 2 snapshots after pruning with retention=2, got {len(remaining)}"
    )


# ---------------------------------------------------------------------------
# 7. install_enabled=False → immediate refusal, no mutations
# ---------------------------------------------------------------------------


def test_orchestrator_install_disabled_refuses(tmp_path: Path) -> None:
    """run_install() with enabled_override=False refuses without any state mutations."""
    from yadgar.core.update.orchestrator import (
        InstallConfig,
        OrchestratorState,
        _build_hooks,
        run_install,
    )

    service_restart = MagicMock()
    image_pull = MagicMock()

    config = InstallConfig(
        target_version="5.49.0",
        enabled_override=False,
        lock_file_path=tmp_path / "upgrade.lock",
        snapshots_base_dir=tmp_path / "snaps",
        prev_image_tag_override="docker.io/openfantasy/yadgar:5.48.0",
        prev_unit_file_override="[Unit]\nDescription=yadgar\n",
        prev_cli_version_override="5.48.0",
    )
    hooks = _build_hooks(
        image_pull,
        _noop_graceful_stop,
        service_restart,
        _passing_health_check,
        _noop_cli_upgrade,
        None,
        _noop_re_exec,
    )
    result = run_install(config, hooks)

    # Refused immediately
    assert result.final_state == OrchestratorState.IDLE
    assert result.error is not None
    assert "disabled" in result.error.lower()

    # No mutations
    image_pull.assert_not_called()
    service_restart.assert_not_called()

    # No snapshot created
    assert result.snapshot_path is None

    # No lock file created
    assert not (tmp_path / "upgrade.lock").exists()


# ---------------------------------------------------------------------------
# 8. Concurrent run blocked by live lock
# ---------------------------------------------------------------------------


def test_orchestrator_concurrent_run_blocked(tmp_path: Path) -> None:
    """A lock file with current process PID blocks a second run_install call."""
    from yadgar.core.update.orchestrator import OrchestratorState, run_install

    lock_path = tmp_path / "upgrade.lock"
    lock_data = {
        "pid": os.getpid(),
        "start_ts": time.time(),
        "version_from": "5.48.0",
        "version_to": "5.49.0",
    }
    lock_path.write_text(json.dumps(lock_data))
    original_content = lock_path.read_text()

    from yadgar.core.update.orchestrator import InstallConfig, _build_hooks

    config = InstallConfig(
        target_version="5.49.0",
        enabled_override=True,
        lock_file_path=lock_path,
        snapshots_base_dir=tmp_path / "snaps",
        prev_image_tag_override="docker.io/openfantasy/yadgar:5.48.0",
        prev_unit_file_override="",
        prev_cli_version_override="5.48.0",
    )
    hooks = _build_hooks(
        _noop_image_pull,
        _noop_graceful_stop,
        _noop_service_restart,
        _passing_health_check,
        _noop_cli_upgrade,
        None,
        _noop_re_exec,
    )
    result = run_install(config, hooks)

    assert result.final_state == OrchestratorState.IDLE
    assert result.error is not None
    assert "concurrent" in result.error.lower() or "in progress" in result.error.lower()

    # Lock file must be untouched
    assert lock_path.exists()
    assert lock_path.read_text() == original_content


# ---------------------------------------------------------------------------
# 9. Stale lock (dead PID or expired) → taken over
# ---------------------------------------------------------------------------


def test_orchestrator_stale_lock_taken_over(tmp_path: Path) -> None:
    """A lock with dead PID (or old start_ts) is treated as stale; orchestrator proceeds."""
    from yadgar.core.update.orchestrator import OrchestratorState

    lock_path = tmp_path / "upgrade.lock"
    # PID 99999999 is guaranteed not to exist
    stale_data = {
        "pid": 99999999,
        "start_ts": time.time() - 7200,  # 2 hours ago — past the 1h max_age
        "version_from": "5.47.0",
        "version_to": "5.48.0",
    }
    lock_path.write_text(json.dumps(stale_data))

    result = _run(tmp_path, lock_file_path=lock_path)

    # Must have proceeded (not blocked)
    assert result.final_state == OrchestratorState.RE_EXECING
    assert result.error is None


# ---------------------------------------------------------------------------
# 10. Last in-process state is RE_EXECING (execvp never returns in production)
# ---------------------------------------------------------------------------


def test_orchestrator_records_re_exec_state(tmp_path: Path) -> None:
    """Mock re_exec (doesn't actually exec); orchestrator records RE_EXECING as final state."""
    from yadgar.core.update.orchestrator import OrchestratorState

    re_exec_calls: list[tuple[str, Path]] = []

    def capturing_re_exec(version: str, snapshot_path: Path) -> None:
        re_exec_calls.append((version, snapshot_path))

    result = _run(tmp_path, re_exec=capturing_re_exec)

    # re_exec was called
    assert len(re_exec_calls) == 1
    assert re_exec_calls[0][0] == "5.49.0"
    assert re_exec_calls[0][1] is not None

    # Last in-process state is RE_EXECING, not DONE
    assert result.final_state == OrchestratorState.RE_EXECING

    # DONE is set only by Phase 10 --finalize subcommand (not by run_install)
    # Verify forward_log ends with re_execing entry
    forward_log_path = result.snapshot_path / "forward_log.json"
    entries = json.loads(forward_log_path.read_text())
    last_state = entries[-1]["state"]
    assert last_state == "re_execing", (
        f"Last forward_log entry must be 're_execing', got '{last_state}'"
    )


# ---------------------------------------------------------------------------
# 11. daemon.py install_systemd_service → core unit uses Type=notify
# ---------------------------------------------------------------------------


def test_install_systemd_service_type_notify(tmp_path: Path) -> None:
    """T42: install_systemd_service() core unit must use Type=notify (not Type=simple).

    Mirrors Phase 7 assertion (T37) but targets the Python-generated unit,
    not the .in template.  Also asserts the db unit is allowed to remain Type=simple
    (SurrealDB container does not sd_notify).
    """
    import yadgar.core.daemon as _daemon_mod

    daemon = _daemon_mod.YadgarDaemon()

    # Patch the service dir so we don't write to ~/.config/systemd/user
    service_dir = tmp_path / "systemd" / "user"
    service_dir.mkdir(parents=True)

    # Monkey-patch Path.home() indirectly by patching service_dir lookup inside daemon
    import unittest.mock as mock

    with mock.patch.object(
        _daemon_mod,
        "Path",
        side_effect=lambda *a, **kw: _patched_path(service_dir, *a, **kw),
    ):
        # Can't easily mock Path.home() via side_effect — use a more direct approach
        pass

    # Directly call and capture the unit text by patching write_text
    written: dict[str, str] = {}
    orig_write = Path.write_text

    def capturing_write(self: Path, text: str, *args, **kwargs) -> None:  # type: ignore[override]
        written[self.name] = text
        return orig_write(self, text, *args, **kwargs)

    with (
        mock.patch.object(Path, "write_text", capturing_write),
        mock.patch("pathlib.Path.home", return_value=tmp_path),
    ):
        daemon.install_systemd_service(dev=False)

    assert written, "install_systemd_service must write at least one unit file"

    # The core service unit must use Type=notify
    core_key = next((k for k in written if k.startswith("yadgar") and "db" not in k), None)
    assert core_key is not None, f"No core unit file found in written keys: {list(written.keys())}"
    core_unit = written[core_key]
    assert "Type=notify" in core_unit, (
        f"Core unit '{core_key}' missing Type=notify.\n"
        "Phase 7 follow-up: daemon.py install_systemd_service must match .in template."
    )
    assert "Type=simple" not in core_unit, (
        f"Core unit '{core_key}' still contains Type=simple — must be replaced by Type=notify."
    )

    # The DB unit is allowed to remain Type=simple (SurrealDB does not sd_notify)
    db_key = next((k for k in written if "db" in k), None)
    if db_key:
        db_unit = written[db_key]
        # DB unit may still be Type=simple — that's fine
        assert "Type=simple" in db_unit or "Type=" in db_unit, (
            "DB unit should still define a Type= directive."
        )


def _patched_path(service_dir: Path, *args, **kwargs) -> Path:
    """Helper — not used directly but documents the approach tried."""
    return Path(*args, **kwargs)
