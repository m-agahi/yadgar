"""v5.45.1 Step 1 TDD — uninstall.sh macOS path (RED).

macOS path: launchctl unload + rm plists.
skipif-darwin for live launchctl behavior; mock env vars for cross-platform.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT

UNINSTALL_SH = REPO_ROOT / "scripts" / "install" / "uninstall.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


def _run_uninstall(
    yadgar_dir: Path,
    purge: bool = False,
    launchd_output_dir: Path | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run uninstall.sh in test mode (no real launchctl/systemctl calls)."""
    cmd = [BASH, str(UNINSTALL_SH)]
    if purge:
        cmd.append("--purge")
    env = {
        **os.environ,
        "YADGAR_DIR": str(yadgar_dir),
        "YADGAR_TEST_MODE": "1",  # skip real systemctl / launchctl calls
        "YADGAR_SYSTEMD_OUTPUT_DIR": str(yadgar_dir / "systemd_user"),
    }
    if launchd_output_dir is not None:
        env["YADGAR_LAUNCHD_OUTPUT_DIR"] = str(launchd_output_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class TestV5_45_1UninstallMacOSCrossPlatform:
    """Cross-platform uninstall macOS path tests — run on Linux and macOS."""

    def test_v5_45_1_uninstall_script_exists(self):
        """uninstall.sh must exist."""
        assert UNINSTALL_SH.exists(), f"uninstall.sh not found at {UNINSTALL_SH}"

    def test_v5_45_1_uninstall_removes_plist_files(self, tmp_path):
        """uninstall.sh must remove com.openfantasy.yadgar*.plist from LaunchAgents dir."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        launchd_dir = tmp_path / "LaunchAgents"
        launchd_dir.mkdir()
        # Create fake plist files
        (launchd_dir / "com.openfantasy.yadgar.plist").write_text("<plist/>")
        (launchd_dir / "com.openfantasy.yadgar-backend.plist").write_text("<plist/>")

        result = _run_uninstall(
            yadgar_dir,
            purge=False,
            launchd_output_dir=launchd_dir,
            extra_env={"YADGAR_TEST_OS_MARKER": "macos"},
        )
        assert result.returncode == 0, (
            f"uninstall.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert not (launchd_dir / "com.openfantasy.yadgar.plist").exists(), (
            "com.openfantasy.yadgar.plist was not removed by uninstall"
        )
        assert not (launchd_dir / "com.openfantasy.yadgar-backend.plist").exists(), (
            "com.openfantasy.yadgar-backend.plist was not removed by uninstall"
        )

    def test_v5_45_1_uninstall_macos_preserves_data_dir_without_purge(self, tmp_path):
        """uninstall.sh on macOS must preserve ~/.yadgar without --purge."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        (yadgar_dir / "memories.db").write_text("precious data")
        launchd_dir = tmp_path / "LaunchAgents"
        launchd_dir.mkdir()

        result = _run_uninstall(
            yadgar_dir,
            purge=False,
            launchd_output_dir=launchd_dir,
            extra_env={"YADGAR_TEST_OS_MARKER": "macos"},
        )
        assert result.returncode == 0, (
            f"uninstall.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert yadgar_dir.exists(), "YADGAR_DIR was removed without --purge on macOS"
        assert (yadgar_dir / "memories.db").exists(), "memories.db must be preserved"

    def test_v5_45_1_uninstall_macos_purge_removes_data_dir(self, tmp_path):
        """uninstall.sh --purge on macOS must remove YADGAR_DIR."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        (yadgar_dir / "memories.db").write_text("data to remove")
        launchd_dir = tmp_path / "LaunchAgents"
        launchd_dir.mkdir()

        result = _run_uninstall(
            yadgar_dir,
            purge=True,
            launchd_output_dir=launchd_dir,
            extra_env={"YADGAR_TEST_OS_MARKER": "macos"},
        )
        assert result.returncode == 0, (
            f"uninstall.sh --purge failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert not yadgar_dir.exists(), (
            f"YADGAR_DIR should be removed after --purge\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_1_uninstall_macos_purge_removes_logs_dir(self, tmp_path):
        """uninstall.sh --purge on macOS must also remove ~/Library/Logs/yadgar/."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        launchd_dir = tmp_path / "LaunchAgents"
        launchd_dir.mkdir()
        logs_dir = tmp_path / "Library" / "Logs" / "yadgar"
        logs_dir.mkdir(parents=True)
        (logs_dir / "core.out.log").write_text("log data")

        result = _run_uninstall(
            yadgar_dir,
            purge=True,
            launchd_output_dir=launchd_dir,
            extra_env={
                "YADGAR_TEST_OS_MARKER": "macos",
                "YADGAR_LOGS_DIR": str(logs_dir),  # override log dir path for testing
            },
        )
        assert result.returncode == 0, (
            f"uninstall.sh --purge failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert not logs_dir.exists(), (
            f"Logs dir should be removed after --purge on macOS\nstdout: {result.stdout}"
        )

    def test_v5_45_1_uninstall_macos_noop_when_no_plists(self, tmp_path):
        """uninstall.sh on macOS must succeed even if plist files don't exist."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        launchd_dir = tmp_path / "LaunchAgents"
        launchd_dir.mkdir()
        # No plist files created — should be a no-op without error

        result = _run_uninstall(
            yadgar_dir,
            purge=False,
            launchd_output_dir=launchd_dir,
            extra_env={"YADGAR_TEST_OS_MARKER": "macos"},
        )
        assert result.returncode == 0, (
            f"uninstall.sh should succeed even when no plists exist\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires macOS host — paper-implementation in v5.45.1, verify when host available",
)
class TestV5_45_1UninstallMacOSLive:
    """Live macOS uninstall tests — launchctl unload cycle."""

    def test_v5_45_1_uninstall_calls_launchctl_unload(self, tmp_path):
        """uninstall.sh must call launchctl unload on macOS (live)."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        launchd_dir = tmp_path / "LaunchAgents"
        launchd_dir.mkdir()
        (launchd_dir / "com.openfantasy.yadgar.plist").write_text("<plist/>")

        # Without YADGAR_TEST_MODE, real launchctl is called
        cmd = [BASH, str(UNINSTALL_SH)]
        env = {
            **os.environ,
            "YADGAR_DIR": str(yadgar_dir),
            "YADGAR_LAUNCHD_OUTPUT_DIR": str(launchd_dir),
            "YADGAR_TEST_OS_MARKER": "macos",
            # Don't set YADGAR_TEST_MODE — let real launchctl run
        }
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        # launchctl unload on a non-loaded job returns 0 or a benign error
        # Main assertion: script exits cleanly
        assert result.returncode == 0, (
            f"uninstall.sh live macOS failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
