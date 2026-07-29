"""v5.169 TDD — both shell installers must provision code_graph (RED first).

Bug: `7cd74ea0` made the Python `yadgar setup` install the codebase-memory-mcp
binary by default and persist `code_graph.enabled` so the flag and the
filesystem always agree. That fix is UNREACHABLE from the two installers most
users actually run: `scripts/install/yadgar-setup.sh` (pipx / brew / nix-profile)
runs its own building-block chain and never invokes `yadgar setup`, and the
`Makefile` `setup:` chain is the same shape. Since `code_graph.enabled` defaults
to true with no row (ADR-0163), both produced a machine with code_graph ON and
`~/.local/bin/codebase-memory-mcp` absent — the exact incoherence the Python fix
existed to eliminate. `grep` for `code_graph` / `codebase-memory-mcp` under
`scripts/` and in the `Makefile` returned zero hits.

Seam (verified, not assumed): both surfaces call `yadgar code-graph install`
(a new subcommand). NOT a bash reimplementation — `yadgar-setup.sh` fail-fasts
with exit 2 on a missing `_REQUIRED_HELPERS` entry, so a new bash helper would
hard-break every pipx install that picks up a new script without it.

Mirrors `test_v5_169_setup_linger.py`, the sibling car's per-car file, including
its `make setup` sub-make opt-out-propagation twin.

Criteria numbering follows docs/plans/fix-shell-installer-code-graph-gap-2026-07-29.md.
"""

import os
import subprocess

import pytest

from yadgar.tests._paths import REPO_ROOT

SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
MAKEFILE = REPO_ROOT / "Makefile"

#: The token both surfaces must carry. Pinned on the hyphenated CLI invocation,
#: NOT on prose and NOT on `code_graph` (underscore) — the log line uses the
#: underscore form, so a naive shared-token pick would be satisfied by prose
#: alone. Same argument the linger guard's own docstring makes.
CLI_TOKEN = "yadgar code-graph install"


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


def _write_stub_yadgar(tmp_path, *, has_install: bool):
    """A fake `yadgar` on PATH. `has_install=False` simulates a staged upgrade:
    a NEW yadgar-setup.sh against an OLDER installed CLI."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "yadgar.log"
    stub = bin_dir / "yadgar"
    install_rc = 0 if has_install else 2
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log_path}"\n'
        'if [ "$1 $2" = "code-graph install" ]; then\n'
        f"  exit {install_rc}\n"
        "fi\n"
        # step 9 probes `yadgar install --help | grep -q -- '--rules'`
        'if [ "$1" = "install" ]; then echo "  --rules"; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return bin_dir, log_path


# ── criterion 1 / 2 — shell installer dryrun ─────────────────────────────────


def test_c1_dryrun_prints_the_provisioning_command():
    """--dryrun must print the real invocation, not merely mention code_graph."""
    result = _run_setup("--dryrun")
    combined = result.stdout + result.stderr
    assert CLI_TOKEN in combined, (
        f"yadgar-setup --dryrun must reach the code_graph step\n{combined[-3000:]}"
    )


def test_c2_dryrun_opt_out_forwards_the_flag():
    """--no-code-graph must reach the CLI, not silently skip the step.

    Skipping would leave `code_graph.enabled` at its true default with no
    binary — the original bug, inverted. The opt-out has to run so the `false`
    row lands.
    """
    result = _run_setup("--dryrun", "--no-code-graph")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"--no-code-graph must be a known flag\n{combined[-2000:]}"
    assert f"{CLI_TOKEN} --no-code-graph" in combined, combined[-3000:]


def test_c2_default_does_not_pass_the_opt_out():
    result = _run_setup("--dryrun")
    combined = result.stdout + result.stderr
    assert f"{CLI_TOKEN} --no-code-graph" not in combined, combined[-2000:]


# ── criterion 3 — help text (incl. no-op-flag negative guard) ────────────────


def test_c3_help_documents_no_code_graph():
    result = _run_setup("--help")
    assert "--no-code-graph" in result.stdout, result.stdout


def test_c3_help_has_no_opt_in_flag():
    """Negative guard: no `--code-graph` opt-in for a default-on step.

    A sibling car deleted exactly that flag from `yadgar setup`: an opt-IN for
    default-on behaviour is a no-op and pushes scripted installs onto the
    negative form.
    """
    result = _run_setup("--help")
    lines = [ln for ln in result.stdout.splitlines() if "--code-graph" in ln]
    assert not [ln for ln in lines if "--no-code-graph" not in ln], (
        f"only --no-code-graph may exist\n{result.stdout}"
    )


# ── criterion 4 — daemon-down must NOT skip the binary install ───────────────


def test_c4_step_has_no_wait_for_daemon_gate():
    """Steps 10/11 SKIP themselves when `_wait_for_daemon` times out. Copying
    that here would mean daemon-down → no binary → the divergence this step
    exists to remove survives the fix.

    The binary install needs no daemon; only the persist does, and
    `provision_code_graph` already fails soft on it. Asserted on the script
    text because the alternative (booting a real install with a dead daemon)
    is not a unit-testable shape.
    """
    text = SETUP_SH.read_text()
    start = text.index("_step_code_graph()")
    end = text.index("\n}", start)
    # Comments are stripped: the step's own comment EXPLAINS why it has no gate,
    # and a substring match on the prose would fail for saying so.
    code = [ln for ln in text[start:end].splitlines() if not ln.strip().startswith("#")]
    assert not [ln for ln in code if "_wait_for_daemon" in ln], (
        f"the code_graph step must not gate on daemon health\n{code}"
    )


def test_c5_call_site_is_guarded():
    """The script runs under `set -euo pipefail` — a failed provision must never
    abort an otherwise-good install (the `|| true` precedent at the linger call
    site)."""
    text = SETUP_SH.read_text()
    assert "set -euo pipefail" in text, "premise changed — re-check the guard requirement"
    invocations = [ln for ln in text.splitlines() if CLI_TOKEN in ln and "run " in ln]
    assert invocations, "yadgar-setup.sh must invoke `yadgar code-graph install`"
    assert all("|| true" in ln for ln in invocations), (
        f"every invocation must be guarded against set -e\n{invocations}"
    )


# ── criterion 6 — staged upgrade: old CLI without the subcommand ─────────────


def test_c6_missing_subcommand_warns_and_continues(tmp_path):
    """A new script against an older installed `yadgar` must warn and skip —
    never abort (the `_step_install_rules` feature-probe precedent)."""
    bin_dir, log_path = _write_stub_yadgar(tmp_path, has_install=False)
    env_path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    result = _run_setup("--dryrun", extra_env={"PATH": env_path})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"a missing subcommand must not abort\n{combined[-3000:]}"
    assert "WARN" in combined and "code-graph" in combined, combined[-3000:]


def test_c6_probe_does_not_run_the_install_on_old_cli(tmp_path):
    bin_dir, log_path = _write_stub_yadgar(tmp_path, has_install=False)
    env_path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    _run_setup("--dryrun", extra_env={"PATH": env_path})
    calls = log_path.read_text().splitlines() if log_path.exists() else []
    non_probe = [c for c in calls if c.startswith("code-graph install") and "--help" not in c]
    assert not non_probe, f"must not invoke the subcommand it just found missing\n{calls}"


# ── criterion 7 — Makefile surface (required for the both-surfaces guard) ────


def test_c7_makefile_declares_default_on():
    assert "YADGAR_CODE_GRAPH ?= 1" in MAKEFILE.read_text()


def test_c7_make_setup_reaches_the_step():
    """`make setup` — the README's repo-checkout path — must reach the step.

    `setup` delegates via `$(MAKE)`; asserting the leaf target alone would not
    prove the primary install path fires.
    """
    result = _make_dry_run("setup")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert CLI_TOKEN in combined, f"make setup must reach the step\n{combined[-3000:]}"


def test_c7_make_setup_opt_out_propagates_to_submake():
    """Guards env propagation across the `$(MAKE)` boundary — this Makefile
    otherwise passes such vars explicitly to sub-makes."""
    result = _make_dry_run("setup", extra_env={"YADGAR_CODE_GRAPH": "0"})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert f"{CLI_TOKEN} --no-code-graph" in combined, (
        f"opt-out must survive the sub-make hop\n{combined[-3000:]}"
    )


def test_c7_make_opt_out_still_persists_the_false_row():
    """`YADGAR_CODE_GRAPH=0` must RUN the subcommand with `--no-code-graph`,
    not echo-skip it.

    An echo-skip would leave `code_graph.enabled` at its true default with no
    binary — precisely the incoherence this car removes. Same reasoning as the
    shell `--no-code-graph` path.
    """
    result = _make_dry_run("setup", extra_env={"YADGAR_CODE_GRAPH": "0"})
    combined = result.stdout + result.stderr
    assert "Skipping code_graph" not in combined, combined[-2000:]


# ── criterion 8 — regression: the new step must not break the chain ──────────


def test_c8_dryrun_still_exits_clean():
    result = _run_setup("--dryrun")
    assert result.returncode == 0, f"stderr: {result.stderr[-2000:]}"


def test_c8_dryrun_downloads_nothing():
    """--dryrun must route through `run`, or it performs a real ~large download."""
    result = _run_setup("--dryrun")
    combined = result.stdout + result.stderr
    assert f"[dryrun] {CLI_TOKEN}" in combined, combined[-3000:]
    assert "Installing codebase-memory-mcp" not in combined, combined[-3000:]


@pytest.mark.parametrize("label", ["Step 12/12"])
def test_c8_step_numbering_is_consistent(label):
    """Renumbering 11→12 is cosmetic (no test pins the counts) but a half-done
    renumber is a visible install-log defect."""
    text = SETUP_SH.read_text()
    assert label in text
    assert "/11:" not in text, "a `Step N/11` label survived the renumber"
