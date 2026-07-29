"""v5.46.0 — yadgar-setup.sh make setup chain equivalence test.

Verifies that yadgar-setup --dryrun covers the same building-block steps
as make setup (detect, pull, secrets, generate units, enable, hooks,
agents, config, rules, anchors).

RED phase: fails until yadgar-setup.sh is implemented.
"""

import os
import re
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
    "code-graph",  # code-graph-install (codebase-memory-mcp binary + enabled flag)
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


def test_setup_sh_and_make_agree_on_linger_step():
    """v5.169 drift guard (R5): both install surfaces must carry the linger step.

    Fails if either `yadgar-setup.sh` or the Makefile gains or loses systemd
    lingering alone — divergent install paths are the bug class this train is
    cleaning up. Asserts the shared ``linger`` token, not either surface's exact
    wording (the shell path prints ``loginctl enable-linger <user>``; make -n
    prints ``enable_linger.sh``).
    """
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"
    env["YADGAR_TEST_OS_MARKER"] = "linux"
    sh_result = subprocess.run(
        ["bash", str(SETUP_SH), "--dryrun"],
        capture_output=True,
        text=True,
        env=env,
    )
    sh_out = (sh_result.stdout + sh_result.stderr).lower()

    make_env = os.environ.copy()
    make_env["INSTALL_NONINTERACTIVE"] = "1"
    make_result = subprocess.run(
        ["make", "-n", "_enable-units-auto"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=make_env,
    )
    make_out = (make_result.stdout + make_result.stderr).lower()

    assert "linger" in sh_out, f"yadgar-setup.sh lost the linger step:\n{sh_out[-2000:]}"
    assert "linger" in make_out, f"Makefile lost the linger step:\n{make_out[-2000:]}"


def test_setup_sh_and_make_agree_on_code_graph_step():
    """v5.169 drift guard: both install surfaces must provision code_graph.

    The twin of the linger guard above, and the mechanism that stops this exact
    divergence recurring: `7cd74ea0` made the Python `yadgar setup` provision
    code_graph by default, but neither shell surface invokes `yadgar setup`, so
    both shipped machines with `code_graph.enabled` resolving true (ADR-0163: no
    row -> true) and no `codebase-memory-mcp` binary on disk. Nothing asserted
    the two surfaces agreed, so nothing noticed.

    Pinned on the hyphenated CLI token `code-graph` — the invocation both
    surfaces literally contain. NOT on prose and NOT on `code_graph`
    (underscore): the shell step's log line uses the underscore form, so that
    pick would be satisfied by the log line alone even if the invocation were
    deleted.
    """
    env = os.environ.copy()
    env["YADGAR_CONTAINER_RUNTIME"] = "echo"
    env["YADGAR_TEST_OS_MARKER"] = "linux"
    sh_result = subprocess.run(
        ["bash", str(SETUP_SH), "--dryrun"],
        capture_output=True,
        text=True,
        env=env,
    )
    sh_out = (sh_result.stdout + sh_result.stderr).lower()

    make_env = os.environ.copy()
    make_env["INSTALL_NONINTERACTIVE"] = "1"
    make_result = subprocess.run(
        ["make", "-n", "setup"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=make_env,
    )
    make_out = (make_result.stdout + make_result.stderr).lower()

    assert "code-graph" in sh_out, f"yadgar-setup.sh lost the code_graph step:\n{sh_out[-2000:]}"
    assert "code-graph" in make_out, f"Makefile lost the code_graph step:\n{make_out[-2000:]}"


def test_setup_sh_and_make_agree_on_activated_macos_plists():
    """v5.169 drift guard (task:0077 §1.3): the three enable sites must activate
    the SAME set of macOS plists.

    They had already diverged — ``yadgar-setup.sh::_step_enable_units`` bootstrapped
    all six plists while both Makefile sites bootstrapped two, so ``make setup`` on
    macOS rendered six maintenance jobs and loaded two of them. ``--doctor`` lints
    all six but only greps ``launchctl list``, so the gap was invisible.

    Compares the plist LABELS each surface names, read straight out of the two
    files — not a third hardcoded list, which would just be another drift site.
    """
    plist_re = re.compile(r"com\.openfantasy\.yadgar[a-z\-]*\.plist")

    setup_labels = set(plist_re.findall(SETUP_SH.read_text()))
    makefile_text = (REPO_ROOT / "Makefile").read_text()
    make_labels = set(plist_re.findall(makefile_text))

    assert setup_labels, "yadgar-setup.sh names no plists at all"
    assert make_labels, "Makefile names no plists at all"
    assert setup_labels == make_labels, (
        "macOS activation drift between install surfaces.\n"
        f"  only in yadgar-setup.sh: {sorted(setup_labels - make_labels)}\n"
        f"  only in Makefile:        {sorted(make_labels - setup_labels)}\n"
        "Both `make setup` and `yadgar-setup` must load every plist "
        "generate_launchd.sh renders, or the unloaded ones never fire."
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
