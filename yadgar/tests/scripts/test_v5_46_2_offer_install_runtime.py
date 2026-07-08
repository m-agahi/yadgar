"""v5.46.2 TDD — install_runtime.sh interactive prompt + install logic (RED).

Tests the shared install helper that both yadgar-setup.sh and Makefile call.

Test seams (all must be baked into install_runtime.sh):
  YADGAR_TEST_OS_RELEASE=<path>   Override /etc/os-release read path
  YADGAR_TEST_INSTALL_DRYRUN=1    Print install cmd without running sudo
  YADGAR_TEST_TTY=0|1             Override TTY detection (0=no-TTY, 1=TTY)
  INSTALL_NONINTERACTIVE=1        Non-interactive mode gate

Distro map tested:
  ubuntu/debian → apt-get install -y podman
  fedora        → dnf install -y podman
  arch          → pacman -S --noconfirm podman
  alpine        → apk add podman
  opensuse-leap → zypper install -y podman
  (macos)       → brew install podman (+ machine init follow-up printed)
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from yadgar.tests._paths import REPO_ROOT

INSTALL_RUNTIME_SH = REPO_ROOT / "scripts" / "install" / "install_runtime.sh"
DETECT_RUNTIME_SH = REPO_ROOT / "scripts" / "install" / "detect_runtime.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


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


def _run_install_runtime(
    extra_env: dict | None = None,
    args: list[str] | None = None,
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess:
    """Run install_runtime.sh with test seams."""
    env = dict(os.environ)
    env["YADGAR_TEST_INSTALL_DRYRUN"] = "1"  # always dryrun in tests — no sudo
    env["YADGAR_TEST_TTY"] = "0"  # no TTY by default
    env["INSTALL_NONINTERACTIVE"] = "0"
    # Force no real runtime so prompt triggers
    env["PATH"] = "/usr/bin:/bin"
    env["YADGAR_CONTAINER_RUNTIME"] = ""
    if extra_env:
        env.update(extra_env)
    cmd = [BASH, str(INSTALL_RUNTIME_SH)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        input=stdin_data,
    )


class TestV5_46_2InstallRuntimeScriptExists:
    def test_install_runtime_sh_exists(self):
        """install_runtime.sh must exist at scripts/install/."""
        assert INSTALL_RUNTIME_SH.exists(), (
            f"install_runtime.sh not found at {INSTALL_RUNTIME_SH}. "
            "Create scripts/install/install_runtime.sh."
        )


class TestV5_46_2InstallRuntimeNonInteractive:
    """INSTALL_NONINTERACTIVE=1 must print install command + exit non-zero."""

    def _run_noninteractive(self, distro_id: str, id_like: str = "") -> subprocess.CompletedProcess:
        f = _make_fake_os_release(distro_id, id_like)
        try:
            return _run_install_runtime(
                extra_env={
                    "INSTALL_NONINTERACTIVE": "1",
                    "YADGAR_TEST_OS_RELEASE": str(f),
                }
            )
        finally:
            f.unlink(missing_ok=True)

    def test_noninteractive_debian_prints_apt_and_exits_1(self):
        result = self._run_noninteractive("debian")
        assert result.returncode == 1, (
            f"Expected exit 1 in non-interactive mode\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "apt-get" in combined or "apt" in combined, (
            f"Non-interactive Debian mode should print apt-get install\ncombined: {combined!r}"
        )
        assert "podman" in combined

    def test_noninteractive_fedora_prints_dnf_and_exits_1(self):
        result = self._run_noninteractive("fedora")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "dnf" in combined, f"Non-interactive Fedora should print dnf\ncombined: {combined!r}"

    def test_noninteractive_arch_prints_pacman_and_exits_1(self):
        result = self._run_noninteractive("arch")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "pacman" in combined

    def test_noninteractive_alpine_prints_apk_and_exits_1(self):
        result = self._run_noninteractive("alpine")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "apk" in combined

    def test_noninteractive_opensuse_prints_zypper_and_exits_1(self):
        result = self._run_noninteractive("opensuse-leap")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "zypper" in combined

    def test_noninteractive_macos_prints_brew_and_exits_1(self):
        """macOS non-interactive: print brew install podman + machine init guidance."""
        result = _run_install_runtime(
            extra_env={
                "INSTALL_NONINTERACTIVE": "1",
                "YADGAR_TEST_OS_MARKER": "macos",
                "YADGAR_TEST_OS_RELEASE": "",
            }
        )
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "brew" in combined and "podman" in combined, (
            f"macOS non-interactive should print brew install podman\ncombined: {combined!r}"
        )
        # Follow-up guidance (machine init) should also appear
        assert "machine" in combined, (
            f"macOS should mention 'podman machine init'\ncombined: {combined!r}"
        )

    def test_noninteractive_id_like_fallback(self):
        """Pop!_OS (ID=pop, ID_LIKE='ubuntu debian') must resolve to apt-get."""
        result = self._run_noninteractive("pop", id_like="ubuntu debian")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "apt-get" in combined or "apt" in combined

    def test_noninteractive_unknown_distro_exits_1(self):
        """Unknown distro must still exit 1 with a helpful message."""
        result = self._run_noninteractive("unknownos")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "podman" in combined


class TestV5_46_2InstallRuntimeInteractivePrompt:
    """Interactive mode: prompt text and Y/N response handling (TTY=1 mode)."""

    def _run_interactive(
        self, distro_id: str, answer: str, id_like: str = ""
    ) -> subprocess.CompletedProcess:
        f = _make_fake_os_release(distro_id, id_like)
        try:
            return _run_install_runtime(
                extra_env={
                    "INSTALL_NONINTERACTIVE": "0",
                    "YADGAR_TEST_TTY": "1",
                    "YADGAR_TEST_OS_RELEASE": str(f),
                    "YADGAR_TEST_INSTALL_DRYRUN": "1",
                },
                stdin_data=answer + "\n",
            )
        finally:
            f.unlink(missing_ok=True)

    def test_interactive_prompt_text_shown(self):
        """Prompt must ask 'Install podman now? [Y/n]'."""
        result = self._run_interactive("debian", "n")
        combined = result.stdout + result.stderr
        assert "Install" in combined and "podman" in combined, (
            f"Prompt text must include 'Install' and 'podman'\ncombined: {combined!r}"
        )
        # [Y/n] format
        assert "[Y/n]" in combined or "[y/n]" in combined or "Y/n" in combined, (
            f"Prompt must show Y/n choice\ncombined: {combined!r}"
        )

    def test_interactive_n_answer_exits_1(self):
        """Answering N to install prompt must exit non-zero."""
        result = self._run_interactive("debian", "n")
        assert result.returncode == 1, (
            f"N answer should exit 1\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_interactive_y_answer_dryrun_prints_install_cmd(self):
        """Answering Y + YADGAR_TEST_INSTALL_DRYRUN=1 must print install command."""
        result = self._run_interactive("fedora", "y")
        combined = result.stdout + result.stderr
        assert "dnf" in combined, (
            f"Y answer should print dnf install cmd (dryrun)\ncombined: {combined!r}"
        )
        # In dryrun mode, install command printed but NOT executed (sudo not run)
        assert "dryrun" in combined.lower() or "dry" in combined.lower() or "dnf" in combined, (
            f"Dryrun mode should indicate command not executed\ncombined: {combined!r}"
        )

    def test_interactive_empty_answer_defaults_to_y(self):
        """Empty/Enter answer must default to Y (install)."""
        result = self._run_interactive("debian", "")  # empty = Enter
        combined = result.stdout + result.stderr
        # In dryrun mode with Y answer, should print the install command
        assert "apt-get" in combined or "apt" in combined, (
            f"Empty answer (Enter) should default to Y and show apt-get\ncombined: {combined!r}"
        )

    def test_no_tty_no_interactive_falls_back_to_noninteractive(self):
        """No TTY + NONINTERACTIVE=0 must fall back to non-interactive (print + exit 1)."""
        f = _make_fake_os_release("ubuntu")
        try:
            result = _run_install_runtime(
                extra_env={
                    "INSTALL_NONINTERACTIVE": "0",
                    "YADGAR_TEST_TTY": "0",  # no TTY
                    "YADGAR_TEST_OS_RELEASE": str(f),
                }
            )
        finally:
            f.unlink(missing_ok=True)
        assert result.returncode == 1, "No-TTY without NONINTERACTIVE=1 should still exit 1"


class TestV5_46_2InstallRuntimeFlags:
    """--install-runtime and --no-install-runtime flag handling."""

    def test_no_install_runtime_flag_exits_1_prints_hint(self):
        """--no-install-runtime must skip prompt, print hint, exit 1."""
        f = _make_fake_os_release("debian")
        try:
            result = _run_install_runtime(
                extra_env={"YADGAR_TEST_OS_RELEASE": str(f)},
                args=["--no-install-runtime"],
            )
        finally:
            f.unlink(missing_ok=True)
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "apt-get" in combined or "apt" in combined or "podman" in combined

    def test_install_runtime_flag_dryrun_prints_install_cmd(self):
        """--install-runtime with YADGAR_TEST_INSTALL_DRYRUN=1 must print install cmd."""
        f = _make_fake_os_release("fedora")
        try:
            result = _run_install_runtime(
                extra_env={
                    "YADGAR_TEST_OS_RELEASE": str(f),
                    "YADGAR_TEST_INSTALL_DRYRUN": "1",
                },
                args=["--install-runtime"],
            )
        finally:
            f.unlink(missing_ok=True)
        combined = result.stdout + result.stderr
        assert "dnf" in combined, (
            f"--install-runtime with dryrun should print dnf cmd\ncombined: {combined!r}"
        )


class TestV5_46_2InstallRuntimePostInstallRetry:
    """After install, script must retry detect_runtime.sh."""

    def test_dryrun_y_answer_mentions_retry(self):
        """In dryrun mode after Y answer, output should indicate re-detection attempt."""
        f = _make_fake_os_release("debian")
        try:
            result = _run_install_runtime(
                extra_env={
                    "INSTALL_NONINTERACTIVE": "0",
                    "YADGAR_TEST_TTY": "1",
                    "YADGAR_TEST_OS_RELEASE": str(f),
                    "YADGAR_TEST_INSTALL_DRYRUN": "1",
                },
                stdin_data="y\n",
            )
        finally:
            f.unlink(missing_ok=True)
        combined = result.stdout + result.stderr
        # Should mention re-checking / retrying detection after install
        retry_keywords = ["retry", "re-run", "detect", "check", "verify", "again", "recheck"]
        found = any(kw in combined.lower() for kw in retry_keywords)
        assert found, (
            f"After dryrun install, output should mention retry/re-detect\ncombined: {combined!r}"
        )
