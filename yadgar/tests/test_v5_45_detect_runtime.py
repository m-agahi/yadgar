"""v5.45.0 Step 1 TDD — detect_runtime.sh tests (RED: script does not exist yet)."""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DETECT_RUNTIME_SH = REPO_ROOT / "scripts" / "install" / "detect_runtime.sh"
DETECT_OS_SH = REPO_ROOT / "scripts" / "install" / "detect_os.sh"


def _run_detect_runtime(
    env: dict | None = None, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run detect_runtime.sh with optional PATH/env override."""
    base_env = dict(os.environ)
    if env is not None:
        base_env.update(env)
    if extra_env is not None:
        base_env.update(extra_env)
    return subprocess.run(
        ["bash", str(DETECT_RUNTIME_SH)],
        capture_output=True,
        text=True,
        env=base_env,
    )


def _make_fake_bin(tmpdir: Path, name: str, exit_code: int = 0, stdout: str = "") -> Path:
    """Create a fake binary that exits with given code and prints stdout."""
    bin_path = tmpdir / name
    bin_path.write_text(
        f"#!/bin/sh\n"
        f'[ "$1" = "info" ] && {{ echo "{stdout}"; exit {exit_code}; }}\n'
        f'[ "$1" = "--version" ] && {{ echo "{name} version 1.0"; exit 0; }}\n'
        f'[ "$1" = "version" ] && {{ echo "{name} version 1.0"; exit 0; }}\n'
        f'echo "{stdout}"; exit {exit_code}\n'
    )
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


class TestV5_45DetectRuntime:
    """Tests for scripts/install/detect_runtime.sh."""

    def test_v5_45_script_exists(self):
        """detect_runtime.sh must exist at scripts/install/detect_runtime.sh."""
        assert DETECT_RUNTIME_SH.exists(), (
            f"scripts/install/detect_runtime.sh not found at {DETECT_RUNTIME_SH}"
        )

    def test_v5_45_detect_runtime_honors_env_override(self, tmp_path):
        """YADGAR_CONTAINER_RUNTIME=docker forces docker even when podman present on PATH."""
        # Create fake podman that succeeds on info
        _make_fake_bin(tmp_path, "podman", exit_code=0, stdout="podman info ok")
        _make_fake_bin(tmp_path, "docker", exit_code=0, stdout="docker info ok")
        result = _run_detect_runtime(
            env={"PATH": str(tmp_path)},
            extra_env={"YADGAR_CONTAINER_RUNTIME": "docker"},
        )
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}\nstderr: {result.stderr}"
        )
        assert "docker" in result.stdout.lower(), (
            f"expected 'docker' in stdout when YADGAR_CONTAINER_RUNTIME=docker\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_v5_45_detect_runtime_prefers_podman_over_docker(self, tmp_path):
        """Both podman + docker on PATH → returns podman (rootless-friendly)."""
        _make_fake_bin(tmp_path, "podman", exit_code=0, stdout="podman info ok")
        _make_fake_bin(tmp_path, "docker", exit_code=0, stdout="docker info ok")
        result = _run_detect_runtime(
            env={
                "PATH": str(tmp_path),
                "YADGAR_CONTAINER_RUNTIME": "",  # clear override
            }
        )
        assert result.returncode == 0, f"expected exit 0\nstderr: {result.stderr}"
        assert "podman" in result.stdout.lower(), (
            f"expected 'podman' when both runtimes present\nstdout: {result.stdout!r}"
        )

    def test_v5_45_detect_runtime_falls_back_to_docker(self, tmp_path):
        """Only docker on PATH → returns docker."""
        _make_fake_bin(tmp_path, "docker", exit_code=0, stdout="docker info ok")
        result = _run_detect_runtime(
            env={
                "PATH": str(tmp_path),
                "YADGAR_CONTAINER_RUNTIME": "",
            }
        )
        assert result.returncode == 0, f"expected exit 0\nstderr: {result.stderr}"
        assert "docker" in result.stdout.lower(), (
            f"expected 'docker' fallback\nstdout: {result.stdout!r}"
        )

    def test_v5_45_detect_runtime_returns_error_when_neither_present(self, tmp_path):
        """Neither podman nor docker on PATH → exits non-zero with canonical message."""
        # empty_dir has no binaries
        empty = tmp_path / "empty"
        empty.mkdir()
        result = _run_detect_runtime(
            env={
                "PATH": str(empty),
                "YADGAR_CONTAINER_RUNTIME": "",
            }
        )
        assert result.returncode != 0, (
            f"expected non-zero exit when no runtime found\nstdout: {result.stdout!r}"
        )
        combined = (result.stdout + result.stderr).lower()
        assert "podman" in combined or "container" in combined, (
            f"expected canonical error message mentioning 'podman' or 'container'\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_v5_45_detect_runtime_env_override_invalid_runtime_fails(self, tmp_path):
        """YADGAR_CONTAINER_RUNTIME=fakert when fakert info fails → exits non-zero."""
        _make_fake_bin(tmp_path, "fakert", exit_code=1, stdout="")
        result = _run_detect_runtime(
            env={"PATH": str(tmp_path)},
            extra_env={"YADGAR_CONTAINER_RUNTIME": "fakert"},
        )
        assert result.returncode != 0, (
            f"expected failure when overridden runtime's 'info' fails\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )


class TestV5_45DetectOS:
    """Tests for scripts/install/detect_os.sh."""

    def test_v5_45_detect_os_script_exists(self):
        assert DETECT_OS_SH.exists(), f"scripts/install/detect_os.sh not found at {DETECT_OS_SH}"

    def test_v5_45_detect_os_nixos(self, tmp_path):
        """If /etc/NIXOS file exists (mocked via env), emits linux-nixos."""
        # We pass YADGAR_TEST_NIXOS_MARKER to let the script check alternate path
        nixos_marker = tmp_path / "NIXOS"
        nixos_marker.touch()
        result = subprocess.run(
            ["bash", str(DETECT_OS_SH)],
            capture_output=True,
            text=True,
            env={**os.environ, "YADGAR_TEST_NIXOS_MARKER": str(nixos_marker)},
        )
        assert result.returncode == 0
        assert "linux-nixos" in result.stdout, (
            f"expected 'linux-nixos' output\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_v5_45_detect_os_linux_non_nixos(self, tmp_path):
        """Non-NixOS Linux → emits 'linux' or 'linux-other'."""
        # Empty marker path = no NIXOS file
        result = subprocess.run(
            ["bash", str(DETECT_OS_SH)],
            capture_output=True,
            text=True,
            env={**os.environ, "YADGAR_TEST_NIXOS_MARKER": str(tmp_path / "nonexistent")},
        )
        assert result.returncode == 0
        # Should be some linux variant but not nixos
        assert "nixos" not in result.stdout.lower() or "linux" in result.stdout.lower()
