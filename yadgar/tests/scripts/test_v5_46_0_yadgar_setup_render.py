"""v5.46.0 — yadgar-setup.sh flag parsing + dryrun output tests.

RED phase: tests fail until scripts/install/yadgar-setup.sh is implemented.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
SHELLCHECK = shutil.which("shellcheck")


def _run_setup(*args, env_extra=None):
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"  # no-op runtime for dryrun
    # Spoof Linux to bypass NixOS guard (test environment may be NixOS)
    env["YADGAR_TEST_OS_MARKER"] = "linux"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SETUP_SH), *args],
        capture_output=True,
        text=True,
        env=env,
    )


# ── script existence ──────────────────────────────────────────────────────────


def test_yadgar_setup_sh_exists():
    """yadgar-setup.sh must exist at scripts/install/yadgar-setup.sh."""
    assert SETUP_SH.exists(), f"Missing: {SETUP_SH}"


def test_yadgar_setup_sh_is_executable():
    """yadgar-setup.sh must be executable (chmod +x)."""
    assert os.access(SETUP_SH, os.X_OK), f"Not executable: {SETUP_SH}"


# ── --help exits 0 ────────────────────────────────────────────────────────────


def test_yadgar_setup_help_exits_0():
    """--help exits 0 and prints usage."""
    result = _run_setup("--help")
    assert result.returncode == 0, f"--help exited {result.returncode}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "usage" in combined.lower() or "yadgar-setup" in combined.lower()


# ── --dryrun ──────────────────────────────────────────────────────────────────


def test_yadgar_setup_dryrun_exits_0():
    """--dryrun must exit 0 without executing real setup."""
    result = _run_setup("--dryrun")
    assert result.returncode == 0, f"--dryrun exited {result.returncode}\n{result.stderr}"


def test_yadgar_setup_dryrun_prints_commands():
    """--dryrun must print the commands it would run (not execute them)."""
    result = _run_setup("--dryrun")
    combined = result.stdout + result.stderr
    # Must mention the key building blocks
    assert any(
        keyword in combined for keyword in ["detect", "pull", "systemd", "launchd", "hooks"]
    ), f"--dryrun output missing expected commands:\n{combined}"


def test_yadgar_setup_dryrun_no_side_effects(tmp_path):
    """--dryrun must NOT create systemd unit files or write to ~/.yadgar."""
    yadgar_dir = tmp_path / ".yadgar"
    systemd_dir = tmp_path / "systemd"
    result = _run_setup(
        "--dryrun",
        env_extra={
            "YADGAR_DIR": str(yadgar_dir),
            "YADGAR_SYSTEMD_OUTPUT_DIR": str(systemd_dir),
        },
    )
    assert result.returncode == 0
    assert not yadgar_dir.exists(), f"--dryrun created {yadgar_dir}"
    assert not systemd_dir.exists(), f"--dryrun created {systemd_dir}"


# ── --noninteractive ──────────────────────────────────────────────────────────


def test_yadgar_setup_noninteractive_recognized():
    """--noninteractive flag must be recognized (no unknown flag error)."""
    result = _run_setup("--noninteractive", "--dryrun")
    assert result.returncode == 0, (
        f"--noninteractive not recognized (exit {result.returncode}):\n{result.stderr}"
    )


# ── --doctor ──────────────────────────────────────────────────────────────────


def test_yadgar_setup_doctor_recognized():
    """--doctor flag must be recognized (no unknown flag error)."""
    result = _run_setup("--doctor", "--dryrun")
    # --doctor may fail if not on macOS — check it doesn't fail on flag parse
    combined = result.stdout + result.stderr
    assert "unknown" not in combined.lower() and "unrecognized" not in combined.lower(), (
        f"--doctor flag not recognized:\n{combined}"
    )


# ── unknown flag rejects ──────────────────────────────────────────────────────


def test_yadgar_setup_rejects_unknown_flag():
    """Unknown flags must be rejected with non-zero exit."""
    result = _run_setup("--totally-fake-flag-xyz")
    assert result.returncode != 0, "Unknown flag should have been rejected"


# ── shellcheck ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not SHELLCHECK, reason="shellcheck not in PATH")
def test_yadgar_setup_sh_passes_shellcheck():
    """yadgar-setup.sh must pass shellcheck -S warning."""
    result = subprocess.run(
        [SHELLCHECK, "-S", "warning", str(SETUP_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"shellcheck failed:\n{result.stdout}\n{result.stderr}"
