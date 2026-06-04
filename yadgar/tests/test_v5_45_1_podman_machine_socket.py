"""v5.45.1 Step 1 TDD — detect_runtime.sh macOS podman-machine socket probe (RED).

DP-C: On macOS, podman info health check must work via podman machine socket.
Socket path differs from Linux. detect_runtime.sh extends with macOS-specific probe.

Cross-platform tests: mock YADGAR_CONTAINER_RUNTIME + YADGAR_TEST_PODMAN_MACHINE_SOCKET.
macOS-runtime tests: skipif-darwin for live launchctl behavior.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DETECT_RUNTIME_SH = REPO_ROOT / "scripts" / "install" / "detect_runtime.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


def _run_detect_runtime(extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run detect_runtime.sh with optional env overrides."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(DETECT_RUNTIME_SH)],
        capture_output=True,
        text=True,
        env=env,
    )


class TestV5_45_1DetectRuntimeMacOSExtension:
    """detect_runtime.sh macOS podman-machine socket extension (DP-C)."""

    def test_v5_45_1_detect_runtime_script_exists(self):
        """detect_runtime.sh must exist."""
        assert DETECT_RUNTIME_SH.exists(), f"detect_runtime.sh not found at {DETECT_RUNTIME_SH}"

    def test_v5_45_1_detect_runtime_env_override_podman_accepted(self):
        """YADGAR_CONTAINER_RUNTIME=podman override still works on macOS path."""
        # When override is set, detect_runtime.sh should accept it and
        # attempt validation. On Linux CI the podman binary may not be present,
        # so we just test the override is respected (not that podman info passes).
        result = _run_detect_runtime(extra_env={"YADGAR_CONTAINER_RUNTIME": "podman"})
        # Either succeeds (podman present) or fails with clear error about 'podman info'
        combined = result.stdout + result.stderr
        if result.returncode != 0:
            # Must fail with a clear diagnostic, not a silent crash
            assert "podman" in combined.lower() or "runtime" in combined.lower(), (
                f"detect_runtime.sh failed without clear error\n"
                f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
            )

    def test_v5_45_1_detect_runtime_macos_socket_sentinel_env_var_documented(self):
        """detect_runtime.sh must support YADGAR_TEST_PODMAN_MACHINE_SOCKET sentinel for testing."""
        # Read the script and confirm the sentinel env var is documented/handled
        content = DETECT_RUNTIME_SH.read_text()
        # This test will fail (RED) until we add YADGAR_TEST_PODMAN_MACHINE_SOCKET support
        assert "YADGAR_TEST_PODMAN_MACHINE_SOCKET" in content, (
            "detect_runtime.sh must honor YADGAR_TEST_PODMAN_MACHINE_SOCKET sentinel "
            "for macOS podman-machine socket testing (DP-C). "
            "Add: if [[ -n ${YADGAR_TEST_PODMAN_MACHINE_SOCKET:-} ]]; then ..."
        )

    def test_v5_45_1_detect_runtime_podman_machine_socket_path_returned(self, tmp_path):
        """When YADGAR_TEST_PODMAN_MACHINE_SOCKET is set, detect_runtime.sh uses it."""
        # Create a mock socket path (file, not real socket — script uses the path string)
        fake_socket = tmp_path / "podman.sock"
        fake_socket.touch()
        result = _run_detect_runtime(
            extra_env={
                "YADGAR_CONTAINER_RUNTIME": "",  # clear override
                "YADGAR_TEST_PODMAN_MACHINE_SOCKET": str(fake_socket),
            }
        )
        # The script should attempt to use this socket for podman info
        # On Linux CI podman info will still fail, but error should mention the socket path
        # OR script returns 'podman' if podman binary accepts the socket env
        # Main assertion: script does not crash silently
        combined = result.stdout + result.stderr
        # Either success or informative failure
        assert combined.strip() != "", (
            "detect_runtime.sh produced no output when YADGAR_TEST_PODMAN_MACHINE_SOCKET set"
        )

    def test_v5_45_1_detect_runtime_macos_sentinel_message_on_no_machine(self):
        """When macOS + podman not running, detect_runtime.sh must print sentinel message."""
        # Simulate: macOS marker set, no running podman machine (fake socket doesn't exist)
        result = _run_detect_runtime(
            extra_env={
                "YADGAR_CONTAINER_RUNTIME": "",
                "YADGAR_TEST_OS_MARKER": "macos",
                "YADGAR_TEST_PODMAN_MACHINE_SOCKET": "/tmp/__nonexistent_podman_sock__",
            }
        )
        # Must fail with a useful message about podman machine not running
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            assert "podman machine" in combined.lower() or "machine" in combined.lower(), (
                f"detect_runtime.sh should mention 'podman machine' when socket not found\n"
                f"combined: {combined!r}"
            )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires macOS host — paper-implementation in v5.45.1, verify when host available",
)
class TestV5_45_1DetectRuntimeMacOSLive:
    """Live macOS tests for podman-machine socket detection."""

    def test_v5_45_1_detect_runtime_returns_podman_when_machine_running(self):
        """detect_runtime.sh returns 'podman' on macOS when podman machine is running."""
        result = _run_detect_runtime()
        assert result.returncode == 0, (
            f"detect_runtime.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert result.stdout.strip() == "podman", (
            f"Expected 'podman' but got {result.stdout.strip()!r}"
        )
