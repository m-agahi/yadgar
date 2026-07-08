"""v5.45.1 Step 1 TDD — detect_os.sh returns 'macos' on Darwin probe (RED)."""

import os
import shutil
import subprocess

from yadgar.tests._paths import REPO_ROOT

DETECT_OS_SH = REPO_ROOT / "scripts" / "install" / "detect_os.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


def _run_detect_os(extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run detect_os.sh with optional env overrides."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(DETECT_OS_SH)],
        capture_output=True,
        text=True,
        env=env,
    )


class TestV5_45_1DetectMacOS:
    """detect_os.sh correctly identifies macOS via YADGAR_TEST_OS_MARKER=macos."""

    def test_v5_45_1_detect_os_script_exists(self):
        """detect_os.sh must exist."""
        assert DETECT_OS_SH.exists(), f"detect_os.sh not found at {DETECT_OS_SH}"

    def test_v5_45_1_detect_os_macos_marker_returns_macos(self):
        """YADGAR_TEST_OS_MARKER=macos must cause detect_os.sh to output 'macos'."""
        result = _run_detect_os(extra_env={"YADGAR_TEST_OS_MARKER": "macos"})
        assert result.returncode == 0, (
            f"detect_os.sh failed with YADGAR_TEST_OS_MARKER=macos\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert result.stdout.strip() == "macos", (
            f"Expected 'macos' but got {result.stdout.strip()!r}\nstderr: {result.stderr!r}"
        )

    def test_v5_45_1_detect_os_linux_marker_returns_linux_or_nixos(self):
        """Without the macOS marker, detect_os.sh must return linux/* on Linux host."""
        result = _run_detect_os(
            extra_env={
                "YADGAR_TEST_OS_MARKER": "",  # clear marker — rely on uname
                # Ensure nixos marker points to a non-existent path for predictability
                "YADGAR_TEST_NIXOS_MARKER": "/tmp/__no_nixos_marker_v5451__",
            }
        )
        assert result.returncode == 0, (
            f"detect_os.sh failed\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        out = result.stdout.strip()
        # On Linux CI this should return 'linux' or 'linux-other', not 'macos'
        assert out in ("linux", "linux-other", "linux-nixos"), (
            f"Unexpected detect_os output: {out!r}"
        )

    def test_v5_45_1_detect_os_macos_marker_ignored_when_empty(self):
        """Empty YADGAR_TEST_OS_MARKER must NOT trigger macOS path."""
        result = _run_detect_os(
            extra_env={
                "YADGAR_TEST_OS_MARKER": "",
                "YADGAR_TEST_NIXOS_MARKER": "/tmp/__no_nixos_marker_v5451__",
            }
        )
        assert result.returncode == 0
        assert result.stdout.strip() != "macos", (
            "Empty YADGAR_TEST_OS_MARKER should not return 'macos'"
        )
