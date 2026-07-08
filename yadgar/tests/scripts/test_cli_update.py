"""v5.49.0 Phase 10 — CLI tests for `yadgar update --install`, --finalize, --rollback.

TDD: all 4 tests written RED first; implementation makes them GREEN.

Test index:
  1. test_cli_install_runs_orchestrator
  2. test_cli_finalize_marks_done_on_version_match
  3. test_cli_finalize_logs_failure_when_version_mismatch
  4. test_cli_rollback_restores_prev_image_tag
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a minimal argparse Namespace for cmd_update dispatch."""
    defaults = {
        "check": False,
        "install": False,
        "finalize": False,
        "rollback": False,
        "snapshot": None,
        "target_version": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _create_fake_snapshot(tmp_path: Path, target_version: str = "5.49.0") -> Path:
    """Create a minimal snapshot directory with target_version file."""
    snap_dir = tmp_path / "upgrade-snapshots" / "2026-06-08T12-00-00-000000Z"
    snap_dir.mkdir(parents=True)
    (snap_dir / "target_version").write_text(target_version)
    (snap_dir / "prev_image_tag").write_text("docker.io/openfantasy/yadgar:5.48.0")
    (snap_dir / "prev_cli_version").write_text("5.48.0")
    (snap_dir / "forward_log.json").write_text("[]")
    return snap_dir


# ---------------------------------------------------------------------------
# 1. --install calls orchestrator + maps exit code
# ---------------------------------------------------------------------------


def test_cli_install_runs_orchestrator(tmp_path: Path) -> None:
    """--install calls run_install once and exits 0 on DONE-like result (RE_EXECING)."""
    from yadgar.core.update.orchestrator import OrchestratorResult, OrchestratorState

    mock_result = OrchestratorResult(
        final_state=OrchestratorState.RE_EXECING,
        snapshot_path=tmp_path / "snap",
        from_version="5.48.0",
        to_version="5.49.0",
        forward_log=[],
        rollback_log=None,
        error=None,
    )

    with patch("yadgar.core.update.orchestrator.run_install", return_value=mock_result) as mock_run:
        from yadgar.core.cli.update import cmd_update

        args = _make_args(install=True)
        with patch("sys.exit") as mock_exit:
            cmd_update(args)

    mock_run.assert_called_once()
    # Exit code must be 0 for RE_EXECING
    mock_exit.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# 2. --finalize marks DONE on version match
# ---------------------------------------------------------------------------


def test_cli_finalize_marks_done_on_version_match(tmp_path: Path) -> None:
    """--finalize succeeds: forward_log gets 'done' entry, lock removed, exit 0."""
    snap_dir = _create_fake_snapshot(tmp_path, target_version="5.49.0")
    lock_path = tmp_path / "upgrade.lock"
    lock_path.write_text('{"pid": 1, "start_ts": 0}')

    # Daemon health probe returns matching version
    def _fake_urlopen(req, timeout=None):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"version": "5.49.0"}).encode()
        return resp

    from yadgar.core.cli.update import cmd_update

    args = _make_args(finalize=True, snapshot=str(snap_dir))

    with (
        patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        patch("yadgar.core.cli.update._DEFAULT_LOCK_PATH", lock_path),
        patch("sys.exit") as mock_exit,
        patch("yadgar.core.cli.update._get_cli_version", return_value="5.49.0"),
    ):
        cmd_update(args)

    # forward_log.json must have a 'done' terminal entry
    entries = json.loads((snap_dir / "forward_log.json").read_text())
    states = [e["state"] for e in entries]
    assert "done" in states, f"Expected 'done' in forward_log states, got {states}"

    # Lock must be released
    assert not lock_path.exists(), "Lock file must be removed on successful finalize"

    # Exit 0
    mock_exit.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# 3. --finalize logs failure on version mismatch
# ---------------------------------------------------------------------------


def test_cli_finalize_logs_failure_when_version_mismatch(tmp_path: Path) -> None:
    """--finalize fails: done_but_finalize_failed logged, lock kept, exit 4."""
    snap_dir = _create_fake_snapshot(tmp_path, target_version="5.49.0")
    lock_path = tmp_path / "upgrade.lock"
    lock_path.write_text('{"pid": 1, "start_ts": 0}')

    # Daemon returns old version
    def _fake_urlopen(req, timeout=None):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"version": "5.48.0"}).encode()
        return resp

    from yadgar.core.cli.update import cmd_update

    args = _make_args(finalize=True, snapshot=str(snap_dir))

    captured_stderr: list[str] = []

    def _fake_print(*a, file=None, **kw):
        if file is sys.stderr:
            captured_stderr.append(" ".join(str(x) for x in a))

    with (
        patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        patch("yadgar.core.cli.update._DEFAULT_LOCK_PATH", lock_path),
        patch("sys.exit") as mock_exit,
        patch("yadgar.core.cli.update._get_cli_version", return_value="5.49.0"),
        patch("builtins.print", side_effect=_fake_print),
    ):
        cmd_update(args)

    # forward_log must have done_but_finalize_failed
    entries = json.loads((snap_dir / "forward_log.json").read_text())
    states = [e["state"] for e in entries]
    assert "done_but_finalize_failed" in states, (
        f"Expected 'done_but_finalize_failed' in forward_log states, got {states}"
    )

    # Lock must NOT be removed
    assert lock_path.exists(), "Lock file must be kept on finalize failure"

    # Exit 4
    mock_exit.assert_called_once_with(4)

    # Stderr must mention rollback hint
    combined = " ".join(captured_stderr)
    assert "yadgar update --rollback" in combined, (
        f"Expected rollback hint in stderr, got: {captured_stderr}"
    )


# ---------------------------------------------------------------------------
# 4. --rollback restores prev_image_tag in upgrade.env
# ---------------------------------------------------------------------------


def test_cli_rollback_restores_prev_image_tag(tmp_path: Path) -> None:
    """--rollback rewrites upgrade.env to prev_image_tag + calls systemctl restart + exit 0."""
    # Create snapshot with prev_image_tag
    snap_dir = _create_fake_snapshot(tmp_path, target_version="5.49.0")

    # Current upgrade.env has new tag
    env_path = tmp_path / "upgrade.env"
    env_path.write_text("YADGAR_IMAGE_TAG=docker.io/openfantasy/yadgar:5.49.0\n")

    # Health check after restart returns prev version (healthy)
    def _fake_urlopen(req, timeout=None):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"version": "5.48.0"}).encode()
        return resp

    service_restart_mock = MagicMock()

    from yadgar.core.cli.update import cmd_update

    args = _make_args(rollback=True, snapshot=str(snap_dir))

    with (
        patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        patch("yadgar.core.cli.update._DEFAULT_UPGRADE_ENV_PATH", env_path),
        patch("yadgar.core.cli.update._DEFAULT_SNAPSHOTS_BASE_DIR", tmp_path / "upgrade-snapshots"),
        patch("subprocess.run", side_effect=service_restart_mock),
        patch("sys.exit") as mock_exit,
    ):
        cmd_update(args)

    # upgrade.env must be rewritten to prev tag
    env_content = env_path.read_text()
    assert "5.48.0" in env_content, (
        f"Expected prev tag 5.48.0 in upgrade.env after rollback, got: {env_content!r}"
    )
    assert "5.49.0" not in env_content, (
        f"Expected new tag 5.49.0 to be gone from upgrade.env after rollback, got: {env_content!r}"
    )

    # systemctl restart was called
    service_restart_mock.assert_called_once()

    # Exit 0
    mock_exit.assert_called_once_with(0)
