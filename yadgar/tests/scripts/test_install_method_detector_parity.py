"""Car 0112 TDD — parity test: detect_install_method.sh vs
yadgar.core.update.install_methods.detect_install_method().

Car 0109 fixed the Python-side pipx detector to handle pipx>=1.6's XDG-based
default PIPX_HOME (~/.local/share/pipx, inserting a "share" segment) and to
honor an explicit PIPX_HOME env var first. detect_install_method.sh — the
shell mirror used by non-Python callers (Makefile, CI) — still only matched
the legacy */.local/pipx/venvs/yadgar/* layout. Car 0112 ports 0109's exact
logic shape into the shell script.

This is a NEW test file, not an extension of the five cross-generator
invariant harnesses documented on the yadgar-install-surface-generators wiki
page — none of those cover install-method detection, and their unit-rendering
harnesses don't fit this shape. It exists specifically to stop the two
mirrors from silently drifting again: every case below runs BOTH detectors
against the *same* synthesized on-disk layout and asserts they agree.

Both detectors do real end-to-end resolution (no subprocess mocking) so the
§4.3 parity caveat is actually exercised: the Python detector shells out to
`which yadgar`, the shell script uses `command -v yadgar` — under a patched
PATH both must resolve to the SAME synthesized fake binary, not a real
yadgar install elsewhere on PATH shadowing it. `_assert_resolves_to_fake`
below pins that explicitly, independent of the final classification, so a
test that "passes for the wrong reason" (real install shadowing the fake)
fails loudly instead.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from yadgar.core.update import install_methods
from yadgar.tests._paths import REPO_ROOT

DETECT_INSTALL_METHOD_SH = REPO_ROOT / "scripts" / "install" / "detect_install_method.sh"

# Resolve bash at import time — worktree may not have /bin/bash on PATH (nix hosts).
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


def _make_fake_yadgar(bin_dir: Path) -> Path:
    """Create an executable fake 'yadgar' binary at bin_dir/yadgar."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_bin = bin_dir / "yadgar"
    fake_bin.write_text("#!/usr/bin/env python3\n")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _build_env(bin_dir: Path, pipx_home: str | None) -> dict:
    """Build a full env dict with bin_dir prepended to PATH.

    pipx_home=None removes PIPX_HOME from the ambient environment entirely
    (rather than leaving whatever the host happens to have set) so every
    non-explicit-PIPX_HOME case is actually exercising the fallback branch.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if pipx_home is not None:
        env["PIPX_HOME"] = pipx_home
    else:
        env.pop("PIPX_HOME", None)
    return env


def _assert_resolves_to_fake(env: dict, fake_bin: Path) -> None:
    """Sanity: PATH must resolve 'yadgar' to OUR synthesized fake, not a real
    install shadowing it elsewhere on PATH (see module docstring, §4.3 caveat)."""
    resolved = shutil.which("yadgar", path=env.get("PATH", ""))
    assert resolved is not None and Path(resolved).resolve() == fake_bin.resolve(), (
        f"PATH resolved 'yadgar' to {resolved!r}, not the synthesized fake at "
        f"{fake_bin!r} — a real yadgar install may be shadowing it in PATH"
    )


def _run_shell_detector(env: dict) -> str:
    result = subprocess.run(
        [BASH, str(DETECT_INSTALL_METHOD_SH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"detect_install_method.sh exited {result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    return result.stdout.strip()


def _run_both(
    monkeypatch: pytest.MonkeyPatch, bin_dir: Path, pipx_home: str | None = None
) -> tuple[str, str]:
    """Run both detectors under an identical synthesized env; return (shell, python)."""
    fake_bin = bin_dir / "yadgar"
    env = _build_env(bin_dir, pipx_home)
    _assert_resolves_to_fake(env, fake_bin)

    shell_result = _run_shell_detector(env)

    # install_methods.detect_install_method() shells out to `which yadgar`
    # via subprocess.check_output() with no explicit env kwarg — Popen(env=None)
    # inherits the real OS-level process environment (execv, not execve), which
    # only tracks os.environ mutations made through its real __setitem__/
    # __delitem__ (they also call os.putenv/os.unsetenv). A bare
    # `monkeypatch.setattr(os, "environ", env)` swaps the Python-level dict
    # without syncing the OS env, so the subprocess would silently inherit the
    # ambient PATH instead — use setenv/delenv so the real process env changes.
    monkeypatch.setenv("PATH", env["PATH"])
    if "PIPX_HOME" in env:
        monkeypatch.setenv("PIPX_HOME", env["PIPX_HOME"])
    else:
        monkeypatch.delenv("PIPX_HOME", raising=False)
    python_result = install_methods.detect_install_method()

    return shell_result, python_result


class TestInstallMethodDetectorParity:
    """Both detectors must agree on every layout — the anti-drift net for Car 0112."""

    def test_legacy_pipx(self, tmp_path, monkeypatch):
        """Legacy ~/.local/pipx layout — already GREEN pre-Car-0112 (regression guard)."""
        bin_dir = tmp_path / "home" / ".local" / "pipx" / "venvs" / "yadgar" / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result == "pipx"
        assert python_result == "pipx"

    def test_modern_pipx_share_layout(self, tmp_path, monkeypatch):
        """pipx>=1.6 default PIPX_HOME under the XDG data dir (share/pipx)."""
        bin_dir = tmp_path / "home" / ".local" / "share" / "pipx" / "venvs" / "yadgar" / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result == "pipx"
        assert python_result == "pipx"

    def test_explicit_pipx_home(self, tmp_path, monkeypatch):
        """Custom PIPX_HOME env var honored first, regardless of on-disk layout."""
        pipx_home = tmp_path / "custom-pipx-home"
        bin_dir = pipx_home / "venvs" / "yadgar" / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir, pipx_home=str(pipx_home))
        assert shell_result == "pipx"
        assert python_result == "pipx"

    def test_false_positive_pipx_substring(self, tmp_path, monkeypatch):
        """A path merely containing 'pipx' as a directory-name substring — not the
        pipx/venvs/<pkg>/ layout — must NOT be misdetected as 'pipx'."""
        bin_dir = tmp_path / "opt" / "mypipxtool" / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result != "pipx"
        assert python_result != "pipx"
        assert shell_result == python_result

    def test_brew(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "opt" / "homebrew" / "Cellar" / "yadgar" / "5.47.0" / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result == "brew"
        assert python_result == "brew"

    def test_source(self, tmp_path, monkeypatch):
        git_dir = tmp_path / "git" / "yadgar" / ".git"
        git_dir.mkdir(parents=True)
        bin_dir = tmp_path / "git" / "yadgar" / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result == "source"
        assert python_result == "source"

    def test_unknown(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        _make_fake_yadgar(bin_dir)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result == "unknown"
        assert python_result == "unknown"

    @pytest.mark.skipif(
        not Path("/nix/store").is_dir(),
        reason="requires a real /nix/store filesystem (unavailable in non-Nix CI containers)",
    )
    def test_nix_flake(self, tmp_path, monkeypatch):
        """nix-flake: yadgar resolves into an existing /nix/store/* path.

        Only runnable where a real /nix/store exists (dev NixOS machines) — a
        path literally prefixed /nix/store/ cannot be synthesized under
        tmp_path. The nix-flake branch itself is untouched by this car (only
        the pipx block changed, scripts/install/detect_install_method.sh:34-50);
        this case exists to prove parity where possible, not to gate CI on
        Nix availability.
        """
        real_bash = Path(shutil.which("bash")).resolve()
        assert str(real_bash).startswith("/nix/store/"), "test host has no real nix-built bash"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "yadgar").symlink_to(real_bash)
        shell_result, python_result = _run_both(monkeypatch, bin_dir)
        assert shell_result == "nix-flake"
        assert python_result == "nix-flake"
