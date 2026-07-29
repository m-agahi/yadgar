"""Shared helper: run ``scripts/install/generate_systemd.sh`` / ``generate_launchd.sh``
into a tmp dir with a patched ``HOME``.

Both generators write outside their output dir (``generate_systemd.sh`` seeds
``$HOME/.local/state/yadgar/upgrade.env`` and pre-creates the trigger dir;
``generate_launchd.sh`` installs wrapper scripts under
``$HOME/.local/share/yadgar/scripts``), so every test that renders them MUST
patch ``HOME`` — otherwise the suite writes into the developer's real home.

Underscore-prefixed module name keeps pytest from collecting it as a suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from yadgar.tests._paths import REPO_ROOT

__all__ = ["BASH", "render_launchd", "render_systemd"]

BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"
INSTALL_DIR = REPO_ROOT / "scripts" / "install"
GENERATE_SYSTEMD_SH = INSTALL_DIR / "generate_systemd.sh"
GENERATE_LAUNCHD_SH = INSTALL_DIR / "generate_launchd.sh"


def _base_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "YADGAR_RUNTIME": "podman",
            "YADGAR_INSTALL_PREFIX": "/home/testuser/.yadgar",
            "YADGAR_SECRETS_ENV_FILE": "/home/testuser/.yadgar/secrets.env",
            "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
        }
    )
    return env


def render_systemd(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Render systemd units into ``tmp_path/"units"``. Returns the completed process.

    ``check=False`` returns the failed process instead of asserting, so tests can
    assert on the fail-the-install branch of ``@VACUUM_EXEC@`` resolution.
    """
    out_dir = tmp_path / "units"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _base_env(tmp_path)
    env["YADGAR_SYSTEMD_OUTPUT_DIR"] = str(out_dir)
    env["YADGAR_STATE_DIR"] = str(tmp_path / "state")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [BASH, str(GENERATE_SYSTEMD_SH)], capture_output=True, text=True, env=env
    )
    if check:
        assert result.returncode == 0, f"generate_systemd.sh failed\n{result.stderr}"
    return result


def render_launchd(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Render launchd plists into ``tmp_path/"units"``. Returns the completed process."""
    out_dir = tmp_path / "units"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _base_env(tmp_path)
    env["YADGAR_LAUNCHD_OUTPUT_DIR"] = str(out_dir)
    env["YADGAR_HOME"] = env["HOME"]
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [BASH, str(GENERATE_LAUNCHD_SH)], capture_output=True, text=True, env=env
    )
    if check:
        assert result.returncode == 0, f"generate_launchd.sh failed\n{result.stderr}"
    return result
