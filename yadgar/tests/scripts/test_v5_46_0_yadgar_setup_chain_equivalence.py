"""v5.46.0 — yadgar-setup.sh make setup chain equivalence test.

Verifies that yadgar-setup --dryrun covers the same building-block steps
as make setup (detect, pull, secrets, generate units, enable, hooks,
agents, config, rules, anchors).

RED phase: fails until yadgar-setup.sh is implemented.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"

# Canonical set of building blocks that make setup invokes (from Makefile).
# yadgar-setup --dryrun output must mention all of them.
REQUIRED_BUILDING_BLOCKS = [
    "detect",  # detect_runtime / detect_os
    "pull",  # pull-images
    "secrets",  # bootstrap-secrets
    "hooks",  # install-hooks
    "agent",  # install-agents / install-subagents
    "config",  # config-sync
    "rules",  # install-rules / append_claude_rules
    "anchor",  # seed-anchors
]


@pytest.mark.skipif(not SETUP_SH.exists(), reason="yadgar-setup.sh not yet created")
def test_setup_sh_dryrun_covers_make_setup_chain():
    """yadgar-setup --dryrun output must mention all make setup building blocks."""
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"
    env["YADGAR_TEST_OS_MARKER"] = "linux"  # bypass NixOS guard on NixOS test hosts
    result = subprocess.run(
        ["bash", str(SETUP_SH), "--dryrun"],
        capture_output=True,
        text=True,
        env=env,
    )
    combined = (result.stdout + result.stderr).lower()
    missing = [block for block in REQUIRED_BUILDING_BLOCKS if block not in combined]
    assert not missing, (
        f"yadgar-setup --dryrun missing blocks: {missing}\nOutput was:\n{combined[:2000]}"
    )


@pytest.mark.skipif(not SETUP_SH.exists(), reason="yadgar-setup.sh not yet created")
def test_setup_sh_unit_generation_step_present():
    """yadgar-setup --dryrun must mention unit generation (systemd or launchd)."""
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"
    env["YADGAR_TEST_OS_MARKER"] = "linux"  # bypass NixOS guard on NixOS test hosts
    result = subprocess.run(
        ["bash", str(SETUP_SH), "--dryrun"],
        capture_output=True,
        text=True,
        env=env,
    )
    combined = (result.stdout + result.stderr).lower()
    assert "systemd" in combined or "launchd" in combined, (
        f"yadgar-setup --dryrun missing unit generation step:\n{combined[:2000]}"
    )


@pytest.mark.skipif(not SETUP_SH.exists(), reason="yadgar-setup.sh not yet created")
def test_setup_sh_dryrun_exits_clean():
    """yadgar-setup --dryrun must exit 0."""
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"
    env["YADGAR_TEST_OS_MARKER"] = "linux"  # bypass NixOS guard on NixOS test hosts
    result = subprocess.run(
        ["bash", str(SETUP_SH), "--dryrun"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"yadgar-setup --dryrun exited {result.returncode}\nstderr: {result.stderr}"
    )
