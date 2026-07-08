"""v5.46.2 TDD — detect_runtime.sh error messages UX hotfix (RED).

Verifies:
1. No stale "yadgar install" string in error output.
2. Final message says "yadgar-setup" (not "yadgar install").
3. OS-aware install hints for 7 distros + macOS when no runtime found.
4. --quiet flag suppresses install hint (for chained script calls).
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT

DETECT_RUNTIME_SH = REPO_ROOT / "scripts" / "install" / "detect_runtime.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


def _run_detect_runtime(
    extra_env: dict | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run detect_runtime.sh with no real runtime, optional env overrides and args."""
    env = dict(os.environ)
    # Force no real runtime found by pointing to a fake PATH with no podman/docker
    env["PATH"] = "/usr/bin:/bin"  # minimal PATH, no podman/docker typically
    env["YADGAR_CONTAINER_RUNTIME"] = ""  # no override
    if extra_env:
        env.update(extra_env)
    cmd = [BASH, str(DETECT_RUNTIME_SH)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_fake_os_release(id_value: str, id_like: str = "") -> Path:
    """Write a minimal /etc/os-release substitute and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".os-release", delete=False, prefix="yadgar_test_"
    )
    tmp.write(f'ID="{id_value}"\n')
    if id_like:
        tmp.write(f'ID_LIKE="{id_like}"\n')
    tmp.write(f'NAME="Test OS ({id_value})"\n')
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


class TestV5_46_2DetectRuntimeNoStaleMessage:
    """detect_runtime.sh must not emit stale 'yadgar install' message."""

    def test_detect_runtime_script_exists(self):
        assert DETECT_RUNTIME_SH.exists(), f"detect_runtime.sh not found at {DETECT_RUNTIME_SH}"

    def test_no_stale_yadgar_install_string_in_script(self):
        """Script source must not contain 'yadgar install' (stale pre-make-canonical message)."""
        content = DETECT_RUNTIME_SH.read_text()
        assert "yadgar install" not in content, (
            "detect_runtime.sh still contains stale 'yadgar install' string. "
            "Replace with 'yadgar-setup'."
        )

    def test_final_error_message_says_yadgar_setup(self):
        """Error block must reference 'yadgar-setup' not 'yadgar install'."""
        result = _run_detect_runtime()
        assert result.returncode == 1, (
            f"Expected exit 1 when no runtime found\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        combined = result.stderr + result.stdout
        assert "yadgar-setup" in combined, (
            f"Expected 'yadgar-setup' in error output\nstderr: {result.stderr!r}\nstdout: {result.stdout!r}"
        )
        assert "yadgar install" not in combined, (
            f"Stale 'yadgar install' still in error output\nstderr: {result.stderr!r}"
        )


class TestV5_46_2DetectRuntimeInstallHints:
    """detect_runtime.sh error block must print OS-aware install hints."""

    @pytest.fixture(autouse=True)
    def cleanup(self, tmp_path):
        """Track temp files for cleanup."""
        self._tmp_files = []
        yield
        for p in self._tmp_files:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

    def _run_with_distro(self, distro_id: str, id_like: str = "") -> subprocess.CompletedProcess:
        f = _make_fake_os_release(distro_id, id_like)
        self._tmp_files.append(str(f))
        return _run_detect_runtime(extra_env={"YADGAR_TEST_OS_RELEASE": str(f)})

    def test_hint_debian(self):
        """Debian should show apt-get install -y podman."""
        result = self._run_with_distro("debian")
        combined = result.stderr + result.stdout
        assert "apt-get" in combined or "apt" in combined, (
            f"Debian hint missing apt-get/apt\nstderr: {result.stderr!r}"
        )
        assert "podman" in combined

    def test_hint_ubuntu(self):
        """Ubuntu should show apt-get install -y podman."""
        result = self._run_with_distro("ubuntu")
        combined = result.stderr + result.stdout
        assert "apt-get" in combined or "apt" in combined, (
            f"Ubuntu hint missing apt-get/apt\nstderr: {result.stderr!r}"
        )

    def test_hint_fedora(self):
        """Fedora should show dnf install -y podman."""
        result = self._run_with_distro("fedora")
        combined = result.stderr + result.stdout
        assert "dnf" in combined, f"Fedora hint missing dnf\nstderr: {result.stderr!r}"
        assert "podman" in combined

    def test_hint_arch(self):
        """Arch should show pacman -S --noconfirm podman."""
        result = self._run_with_distro("arch")
        combined = result.stderr + result.stdout
        assert "pacman" in combined, f"Arch hint missing pacman\nstderr: {result.stderr!r}"
        assert "podman" in combined

    def test_hint_alpine(self):
        """Alpine should show apk add podman."""
        result = self._run_with_distro("alpine")
        combined = result.stderr + result.stdout
        assert "apk" in combined, f"Alpine hint missing apk\nstderr: {result.stderr!r}"
        assert "podman" in combined

    def test_hint_opensuse(self):
        """openSUSE should show zypper install -y podman."""
        result = self._run_with_distro("opensuse-leap")
        combined = result.stderr + result.stdout
        assert "zypper" in combined, f"openSUSE hint missing zypper\nstderr: {result.stderr!r}"
        assert "podman" in combined

    def test_hint_macos(self):
        """macOS should show brew install podman."""
        result = _run_detect_runtime(
            extra_env={"YADGAR_TEST_OS_MARKER": "macos", "YADGAR_TEST_OS_RELEASE": ""}
        )
        combined = result.stderr + result.stdout
        assert "brew" in combined, f"macOS hint missing brew\nstderr: {result.stderr!r}"
        assert "podman" in combined

    def test_hint_id_like_fallback_ubuntu_derivative(self):
        """Pop!_OS (ID=pop, ID_LIKE='ubuntu debian') should resolve to apt-get."""
        result = self._run_with_distro("pop", id_like="ubuntu debian")
        combined = result.stderr + result.stdout
        assert "apt-get" in combined or "apt" in combined, (
            f"Pop!_OS (ID_LIKE=ubuntu debian) hint should use apt-get\nstderr: {result.stderr!r}"
        )

    def test_hint_unknown_distro_fallback(self):
        """Unknown distro should fall back to URL hint."""
        result = self._run_with_distro("unknownos")
        combined = result.stderr + result.stdout
        # Should at least mention podman.io or a URL
        assert "podman.io" in combined or "https://" in combined or "podman" in combined, (
            f"Unknown distro hint should include podman.io URL\nstderr: {result.stderr!r}"
        )

    def test_quiet_flag_suppresses_install_hint(self):
        """--quiet flag must suppress the install hint block (used by chained scripts)."""
        result = _run_detect_runtime(args=["--quiet"])
        # Should still exit 1 but minimal output
        assert result.returncode == 1
        # Quiet mode: no long install instructions, just a terse error
        combined = result.stderr + result.stdout
        # Should NOT contain the multi-line install hint
        assert "apt-get" not in combined and "dnf" not in combined and "pacman" not in combined, (
            f"--quiet flag should suppress install hints\nstderr: {result.stderr!r}"
        )
