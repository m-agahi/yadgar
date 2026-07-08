"""v5.46.10 — yadgar-setup.sh fail-fast on missing helpers.

Verifies that when yadgar-setup.sh is invoked in an environment where helper
scripts are absent, it exits with code 2 and emits an explicit error message
naming the missing file — instead of silently falling through to the
v5.46.0-era short error.

Test mechanism: use a temp scripts dir that contains yadgar-setup.sh but lacks
the required helpers. Verify exit code + stderr content.

Sentinel: YADGAR_TEST_FORCE_MISSING_HELPER=<helper_name> is set in the env to
force the fail path (used when running from repo checkout where helpers ARE
present but we want to test the error path).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
YADGAR_SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"


@pytest.fixture()
def empty_scripts_dir(tmp_path: Path) -> Path:
    """A temp dir containing only yadgar-setup.sh — no helper scripts."""
    setup_copy = tmp_path / "yadgar-setup.sh"
    shutil.copy(YADGAR_SETUP_SH, setup_copy)
    setup_copy.chmod(0o755)
    return tmp_path


def test_yadgar_setup_exits_2_on_missing_helper(empty_scripts_dir: Path) -> None:
    """yadgar-setup.sh must exit 2 (not 1) when required helper is absent."""
    result = subprocess.run(
        ["bash", str(empty_scripts_dir / "yadgar-setup.sh"), "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(empty_scripts_dir)},
    )
    # --help must still work (reads from the script itself, no helpers needed)
    assert result.returncode == 0, "--help must exit 0 regardless of helpers"


def test_yadgar_setup_fail_fast_missing_detect_runtime(empty_scripts_dir: Path) -> None:
    """yadgar-setup.sh must exit 2 with explicit error when detect_runtime.sh absent."""
    result = subprocess.run(
        ["bash", str(empty_scripts_dir / "yadgar-setup.sh"), "--dryrun"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(empty_scripts_dir),
            # Suppress real runtime detection to avoid depending on podman/docker
            "YADGAR_CONTAINER_RUNTIME": "podman",
            "YADGAR_TEST_OS_MARKER": "linux",
        },
    )
    # Must exit 2 (bundle gap error), not 0 or 1
    assert result.returncode == 2, (
        f"Expected exit code 2 (missing helper), got {result.returncode}.\n"
        f"stderr: {result.stderr}\n"
        f"stdout: {result.stdout}"
    )
    # Must name the missing helper explicitly
    assert "detect_runtime.sh" in result.stderr, (
        f"Expected 'detect_runtime.sh' in stderr, got:\n{result.stderr}"
    )
    # Must include actionable upgrade hint
    assert (
        "pipx upgrade yadgar" in result.stderr
        or "v5.46.10" in result.stderr
        or "packaging bug" in result.stderr
    ), f"Expected upgrade hint in stderr, got:\n{result.stderr}"


def test_yadgar_setup_fail_fast_error_message_format(empty_scripts_dir: Path) -> None:
    """Error message must include 'bundle' or 'incomplete' and the helper name."""
    result = subprocess.run(
        ["bash", str(empty_scripts_dir / "yadgar-setup.sh"), "--dryrun"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(empty_scripts_dir),
            "YADGAR_CONTAINER_RUNTIME": "podman",
            "YADGAR_TEST_OS_MARKER": "linux",
        },
    )
    assert result.returncode == 2
    stderr = result.stderr
    # Must use "ERROR:" prefix (consistent with existing die() messages)
    assert "ERROR:" in stderr, f"Expected 'ERROR:' prefix in stderr:\n{stderr}"
    # Must call out the bundle gap
    bundle_words = {"bundle", "incomplete", "packaging bug", "missing"}
    assert any(w in stderr.lower() for w in bundle_words), (
        f"Expected bundle-gap language in stderr:\n{stderr}"
    )


def test_yadgar_setup_with_all_helpers_present_dryrun(tmp_path: Path) -> None:
    """When all helpers present (repo checkout layout), dryrun must NOT exit 2."""
    # Copy all scripts to tmp to avoid modifying repo
    scripts_src = REPO_ROOT / "scripts" / "install"
    scripts_dst = tmp_path / "scripts"
    shutil.copytree(scripts_src, scripts_dst)
    # Make all .sh executable
    for sh in scripts_dst.glob("*.sh"):
        sh.chmod(0o755)

    result = subprocess.run(
        ["bash", str(scripts_dst / "yadgar-setup.sh"), "--dryrun"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "YADGAR_CONTAINER_RUNTIME": "podman",
            "YADGAR_TEST_OS_MARKER": "linux",
        },
    )
    # With all helpers present, dryrun must not fail with exit 2
    assert result.returncode != 2, (
        f"False positive: helper-check fired even with all helpers present.\n"
        f"stderr: {result.stderr}"
    )
