"""v5.169 TDD — installer must enable systemd lingering (RED first).

Bug: every yadgar unit is a systemd *user* unit with
``WantedBy=default.target``. Without ``loginctl enable-linger`` the per-user
systemd manager is torn down at logout and never started at boot, so a
correctly-enabled install silently does not persist on a headless host.

Seam (verified, not assumed): ``yadgar setup`` (the Python subcommand) never
touches systemd. Unit enablement lives in ``scripts/install/yadgar-setup.sh``
(``_step_enable_units``) and the ``Makefile`` (``enable-units`` /
``_enable-units-auto``). Both surfaces delegate to the shared helper
``scripts/install/enable_linger.sh`` — mirroring the ``install_runtime.sh``
precedent, and the only way to exercise the failure paths in isolation without
performing a real install.

Criteria numbering follows docs/plans/fix-setup-linger-persistence-2026-07-29.md.
"""

import os
import shutil
import subprocess
import time

import pytest

from yadgar.tests._paths import REPO_ROOT

SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
LINGER_SH = REPO_ROOT / "scripts" / "install" / "enable_linger.sh"
MAKEFILE = REPO_ROOT / "Makefile"


def _current_user() -> str:
    """Resolve the username the same way the helper does (``id -un``)."""
    return subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()


def _run_setup(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run yadgar-setup.sh with the container runtime + OS detection stubbed out."""
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"
    env["YADGAR_TEST_OS_MARKER"] = "linux"  # bypass NixOS guard on NixOS test hosts
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SETUP_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def _make_dry_run(*targets: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``make -n <targets>`` in the repo root (idiom from test_v5_46_2_*)."""
    env = os.environ.copy()
    env["INSTALL_NONINTERACTIVE"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["make", "-n", *targets],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


def _write_stub_loginctl(tmp_path, *, linger: str = "no", enable_rc: int = 0, sleep: int = 0):
    """Write a fake ``loginctl`` onto a tmp bin dir. Returns (bin_dir, log_path)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "loginctl.log"
    stub = bin_dir / "loginctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log_path}"\n'
        f"[ {sleep} -gt 0 ] && sleep {sleep}\n"
        'case "$1" in\n'
        f'  show-user)     echo "Linger={linger}"; exit 0 ;;\n'
        f"  enable-linger) exit {enable_rc} ;;\n"
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bin_dir, log_path


def _run_linger(*args: str, bin_dir=None, extra_env: dict | None = None, timeout: int = 60):
    """Run the shared enable_linger.sh helper in isolation."""
    env = os.environ.copy()
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(LINGER_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


# ── criterion 1 / 2 — shell installer dryrun ─────────────────────────────────


def test_c1_dryrun_prints_loginctl_enable_linger():
    """--dryrun must print the loginctl enable-linger command on Linux."""
    result = _run_setup("--dryrun")
    combined = result.stdout + result.stderr
    assert "loginctl enable-linger" in combined, (
        f"yadgar-setup --dryrun must print the linger command\n{combined[-2000:]}"
    )


def test_c2_dryrun_no_enable_linger_prints_nothing():
    """--no-enable-linger must suppress the linger step entirely."""
    result = _run_setup("--dryrun", "--no-enable-linger")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"--no-enable-linger must be a known flag\n{combined[-2000:]}"
    assert "loginctl" not in combined, (
        f"--no-enable-linger must print no loginctl command\n{combined[-2000:]}"
    )


# ── criterion 3 — help text (incl. no-op-flag negative guard) ─────────────────


def test_c3_help_documents_no_enable_linger():
    result = _run_setup("--help")
    assert "--no-enable-linger" in result.stdout, result.stdout


def test_c3_help_has_no_opt_in_flag():
    """Negative guard: no --enable-linger opt-in flag (a no-op for a default-on step).

    The sibling car in this train deleted --code-graph for exactly this defect.
    """
    result = _run_setup("--help")
    assert "--enable-linger" not in result.stdout, (
        "an opt-IN flag for default-on behaviour is a no-op; only --no-enable-linger "
        f"may exist\n{result.stdout}"
    )


# ── criterion 4 — idempotence ────────────────────────────────────────────────


def test_c4_already_lingering_is_skipped(tmp_path):
    bin_dir, log_path = _write_stub_loginctl(tmp_path, linger="yes")
    result = _run_linger(bin_dir=bin_dir)
    assert result.returncode == 0, result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "already" in combined, combined
    assert "enable-linger" not in log_path.read_text(), (
        f"must not mutate when Linger=yes\n{log_path.read_text()}"
    )


# ── criterion 5 — failure is a warning, never fatal ──────────────────────────


def test_c5_failure_warns_and_continues(tmp_path):
    bin_dir, _ = _write_stub_loginctl(tmp_path, linger="no", enable_rc=1)
    result = _run_linger(bin_dir=bin_dir)
    assert result.returncode == 0, (
        f"linger failure must never abort the install\n{result.stdout}\n{result.stderr}"
    )
    assert "enable-linger" in result.stderr, result.stderr
    assert _current_user() in result.stderr, result.stderr


def test_c5_call_site_is_guarded():
    """yadgar-setup.sh runs under `set -euo pipefail` — the call site must not abort.

    The plan asserted the script has no `set -e`; the file (line 24) says
    otherwise, so the guard is load-bearing rather than decorative.
    """
    text = SETUP_SH.read_text()
    assert "set -euo pipefail" in text, "premise changed — re-check the guard requirement"
    guarded = [
        line
        for line in text.splitlines()
        if "enable_linger.sh" in line or ("linger_sh" in line and "bash" in line)
    ]
    assert guarded, "yadgar-setup.sh must invoke the shared enable_linger.sh helper"
    assert any("|| true" in line for line in guarded), (
        f"linger invocation must be guarded against set -e\n{guarded}"
    )


# ── criterion 6 — loginctl absent is informational, not a warning ────────────


def test_c6_loginctl_absent_is_silent_skip():
    result = _run_linger(extra_env={"YADGAR_TEST_LOGINCTL": ""})
    assert result.returncode == 0, result.stderr
    assert "WARN" not in result.stderr, (
        f"no logind on host is not a warning-worthy condition\n{result.stderr}"
    )
    assert "loginctl" in (result.stdout + result.stderr).lower(), result.stdout


# ── criterion 7 — Makefile surface ───────────────────────────────────────────


def test_c7_makefile_declares_default_on():
    assert "YADGAR_ENABLE_LINGER ?= 1" in MAKEFILE.read_text()


@pytest.mark.parametrize("target", ["enable-units", "_enable-units-auto"])
def test_c7_make_targets_invoke_linger(target):
    result = _make_dry_run(target)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "enable_linger.sh" in combined, f"{target} must invoke the linger helper\n{combined}"


@pytest.mark.parametrize("target", ["enable-units", "_enable-units-auto"])
def test_c7_make_opt_out_removes_linger(target):
    result = _make_dry_run(target, extra_env={"YADGAR_ENABLE_LINGER": "0"})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "enable_linger.sh" not in combined, (
        f"YADGAR_ENABLE_LINGER=0 must remove the step from {target}\n{combined}"
    )


def test_c7_make_setup_reaches_linger_step():
    """`make setup` — the README's repo-checkout path — must reach the step.

    `setup` delegates via `$(MAKE) _enable-units-auto`; asserting the leaf
    targets alone would not prove the primary install path fires.
    """
    result = _make_dry_run("setup")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert "enable_linger.sh" in combined, (
        f"make setup must reach the linger step\n{combined[-3000:]}"
    )


def test_c7_make_setup_opt_out_propagates_to_submake():
    """`make setup YADGAR_ENABLE_LINGER=0` is documented in the README.

    Guards env propagation across the `$(MAKE) _enable-units-auto` boundary —
    this Makefile otherwise passes such vars explicitly to sub-makes.
    """
    result = _make_dry_run("setup", extra_env={"YADGAR_ENABLE_LINGER": "0"})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert "enable_linger.sh" not in combined, (
        f"opt-out must survive the sub-make hop\n{combined[-3000:]}"
    )


# ── criterion 8 — regression: linger must not displace unit enablement ───────


def test_c8_setup_still_enables_target():
    combined = _run_setup("--dryrun").stdout
    assert "systemctl --user enable yadgar.target" in combined, combined[-2000:]


def test_c8_make_still_enables_target():
    combined = _make_dry_run("enable-units").stdout
    assert "systemctl --user enable --now yadgar.target" in combined, combined


# ── criterion 10 — doctor probe (read-only) ─────────────────────────────────


def test_c10_doctor_check_reports_enabled(tmp_path):
    bin_dir, log_path = _write_stub_loginctl(tmp_path, linger="yes")
    result = _run_linger("--check", bin_dir=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "WARN" not in result.stderr, result.stderr
    assert "linger" in (result.stdout + result.stderr).lower()
    assert "enable-linger" not in log_path.read_text(), "doctor must never mutate"


def test_c10_doctor_check_warns_when_disabled(tmp_path):
    bin_dir, log_path = _write_stub_loginctl(tmp_path, linger="no")
    result = _run_linger("--check", bin_dir=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr, result.stderr
    assert f"loginctl enable-linger {_current_user()}" in result.stderr, result.stderr
    assert "enable-linger" not in log_path.read_text(), (
        f"doctor must never mutate linger state\n{log_path.read_text()}"
    )


def test_c10_doctor_wires_the_probe(tmp_path):
    """`yadgar-setup --doctor` must actually reach the probe (read-only path)."""
    bin_dir, log_path = _write_stub_loginctl(tmp_path, linger="no")
    env_path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    result = _run_setup("--doctor", extra_env={"PATH": env_path})
    combined = (result.stdout + result.stderr).lower()
    assert "linger" in combined, combined[-3000:]
    assert "enable-linger" not in log_path.read_text(), "doctor must never mutate"


# ── criterion 11 — R6: logind present but wedged ────────────────────────────


@pytest.mark.skipif(shutil.which("timeout") is None, reason="coreutils timeout unavailable")
def test_c11_hung_loginctl_is_bounded(tmp_path):
    bin_dir, _ = _write_stub_loginctl(tmp_path, linger="no", sleep=30)
    started = time.monotonic()
    result = _run_linger(
        bin_dir=bin_dir,
        extra_env={"YADGAR_LINGER_TIMEOUT": "1"},
        timeout=25,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, f"timeout must warn-and-continue\n{result.stderr}"
    assert elapsed < 20, f"linger step must be bounded; took {elapsed:.1f}s"
    assert "enable-linger" in result.stderr, result.stderr
