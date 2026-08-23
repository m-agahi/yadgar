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
    """--doctor flag must be recognized (no unknown flag error).

    Matched against the arg-parser's OWN rejection string rather than a bare
    "unknown"/"unrecognized" substring sweep of the whole run. The sweep was
    both too broad and machine-dependent: `--doctor` shells out to
    `yadgar verify-hooks`, whose report legitimately names its four hook
    statuses (missing / unrecognized / foreign / unexpected), so the test's
    outcome turned on which `yadgar` build happened to be on PATH and on the
    live settings.json. Any probe that ever prints one of those words as
    ordinary output would have broken it the same way.

    "ERROR: Unknown flag:" is the literal string yadgar-setup.sh emits in its
    `*)` case arm — the same one test_yadgar_setup_rejects_unknown_flag
    exercises. The coupling is deliberate: if that message is ever reworded,
    both tests must be updated together.

    Host-coupling fix (task 324, car C3): yadgar-setup.sh now honors
    YADGAR_TEST_YADGAR_BIN (shim path) + YADGAR_TEST_SETTINGS_JSON (fixture).
    The shim emulates a yadgar build that DOES carry verify-hooks and prints
    a known string so the probe's "OK / warn" path is observable without
    touching the real PATH or settings.json.
    """
    import json
    import tempfile
    import textwrap

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shim = tmp_path / "yadgar-shim.sh"
        # Shim exits rc=1 with a unique marker on stderr so the test can
        # observe dispatch via the WARN branch (line 891-895 echoes $report
        # on rc!=0). The OK branch (line 890) swallows $report, so an
        # rc=0 shim would not surface here even though it was called.
        shim.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                # Test shim for yadgar-setup.sh doctor probe (task 324).
                case "$1" in
                    verify-hooks)
                        if [[ "$2" == "--help" ]]; then
                            echo "usage: yadgar verify-hooks [--settings PATH]"
                            exit 0
                        fi
                        # rc=1 + unique marker → yadgar-setup.sh echoes the
                        # captured $report to stderr under the WARN branch,
                        # letting the test prove the shim was actually invoked
                        # through the host-coupling escape hatches.
                        echo "SHIM_DISPATCH_MARKER: yadgar verify-hooks invoked via test shim (YADGAR_TEST_YADGAR_BIN honored)" >&2
                        exit 1
                        ;;
                    *) echo "shim: unexpected subcommand $*" >&2; exit 2 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        shim.chmod(0o755)
        settings_fixture = tmp_path / "settings.json"
        settings_fixture.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

        result = _run_setup(
            "--doctor",
            "--dryrun",
            env_extra={
                "YADGAR_TEST_YADGAR_BIN": str(shim),
                "YADGAR_TEST_SETTINGS_JSON": str(settings_fixture),
            },
        )
    combined = result.stdout + result.stderr
    assert "Unknown flag:" not in combined, f"--doctor flag not recognized:\n{combined}"
    # Not-rejected is weaker than dispatched. Assert the flag actually reached
    # _run_doctor, so a parse arm that swallowed --doctor without running it
    # could not pass.
    assert "Doctor: Running verification probes" in combined, (
        f"--doctor parsed but never dispatched to _run_doctor:\n{combined}"
    )
    # The shim must have been invoked through the new overrides — proves the
    # host-coupling escape hatches (YADGAR_TEST_YADGAR_BIN,
    # YADGAR_TEST_SETTINGS_JSON) actually reach _probe_managed_hooks. Without
    # this assertion the test could pass on a host where the live settings.json
    # happens to be wired correctly, masking the regression this car fixes.
    # The marker is emitted on stderr by the shim; _probe_managed_hooks
    # captures stderr into $report and echoes $report to stderr under the
    # WARN branch (rc != 0), so the marker reaches combined output only if
    # the shim was actually invoked through YADGAR_TEST_YADGAR_BIN.
    assert "SHIM_DISPATCH_MARKER" in combined, (
        "doctor did not invoke the injected yadgar shim; "
        "_probe_managed_hooks is reading PATH / live settings.json instead "
        "of the test overrides. Combine with the live-claude guard if this "
        "fails on a host with already-wired hooks:\n" + combined
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
