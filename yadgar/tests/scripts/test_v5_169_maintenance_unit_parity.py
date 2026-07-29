"""v5.169 — installer maintenance-unit parity for the non-nix surfaces.

``fix-installer-maintenance-unit-parity-2026-07-29`` (task:0077). Three bugs in
one family, all of which render/lint green today:

1. **No maintenance units on non-nix systemd.** ``generate_systemd.sh`` rendered
   exactly three files, so on a ``make setup`` Linux install consolidation, heat
   decay, episode formation and the weekly vacuum never fired. No error, because
   nothing ever tried.
2. **SurrealDB unreachable from the host on BOTH non-nix surfaces.** The nightly
   and vacuum entry points execute on the HOST (``flake.nix`` rationale: the
   vacuum flow interleaves phases needing different daemon states and the image
   has no ``systemctl``), and both default ``--backend-url`` to ``$YADGAR_DB_URL``
   → ``http://127.0.0.1:8000``. Neither ``yadgar-backend.service.in`` nor
   ``com.openfantasy.yadgar-backend.plist.in`` published :8000, so the macOS jobs
   — which ``yadgar-setup.sh::_step_enable_units`` DOES bootstrap — fired on
   schedule and connection-refused into ``nightly-cycle.err.log``.
3. **Activation drift.** ``Makefile`` bootstrapped 2 of 6 macOS plists while
   ``yadgar-setup.sh`` bootstrapped 6 of 6 — three enable sites, already diverged.

Plus a fourth, found while implementing: the launchd nightly wrapper exec'd
``yadgar nightly-cycle``, which is **not a subcommand** (the only nightly entry
point is the ``yadgar-nightly-cycle`` console script, ``pyproject.toml``
``[project.scripts]``; ``nightly_cycle.main`` has no argparse at all). It exited
2 on argparse before ever opening a socket — so fixing (2) alone would have
shipped a claimed macOS repair that did not repair.

The activation assertions live next to the render assertions on purpose: a
timer/path unit wired only via ``[Install] WantedBy=timers.target`` renders
correctly and never activates, because every install entry point runs only
``systemctl --user enable yadgar.target``.
"""

from __future__ import annotations

import functools
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import render_launchd, render_systemd

INSTALL_DIR = REPO_ROOT / "scripts" / "install"
LAUNCHD_DIR = INSTALL_DIR / "launchd"
UNINSTALL_SH = INSTALL_DIR / "uninstall.sh"
SETUP_SH = INSTALL_DIR / "yadgar-setup.sh"
MAKEFILE = REPO_ROOT / "Makefile"
FLAKE_NIX = REPO_ROOT / "flake.nix"

# The nine unit files generate_systemd.sh must render (three pre-existing plus
# the six maintenance units transcribed from flake.nix:568-690).
EXPECTED_SYSTEMD_UNITS = {
    "yadgar.service",
    "yadgar-backend.service",
    "yadgar.target",
    "yadgar-vacuum.service",
    "yadgar-vacuum.timer",
    "yadgar-vacuum-trigger.path",
    "yadgar-vacuum-trigger.service",
    "yadgar-nightly-cycle.service",
    "yadgar-nightly-cycle.timer",
}

EXPECTED_LAUNCHD_PLISTS = {
    "com.openfantasy.yadgar.plist",
    "com.openfantasy.yadgar-backend.plist",
    "com.openfantasy.yadgar-vacuum.plist",
    "com.openfantasy.yadgar-nightly-cycle.plist",
    "com.openfantasy.yadgar-vacuum-trigger.plist",
    "com.openfantasy.yadgar-worktree-sweep.plist",
}

# `-p 127.0.0.1:<host>:8000` — loopback only. A 0.0.0.0 / bare-port publish would
# expose the SurrealDB HTTP API (root creds live in secrets.env) to the LAN.
_LOOPBACK_SURREAL_PUBLISH = re.compile(r"-p\s+127\.0\.0\.1:(\S+?):8000\b")


# ── host entry-point truth ────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _yadgar_subcommands() -> frozenset[str]:
    """Real ``yadgar <sub>`` subcommands, read off the live argparse parser.

    ``yadgar/__main__.py::cli`` builds the parser inline and parses immediately,
    so there is nothing importable to introspect — ``--help`` is the parser's own
    account of itself, which is exactly the authority we want here.
    """
    result = subprocess.run(
        [sys.executable, "-m", "yadgar", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"`yadgar --help` failed:\n{result.stderr}"
    # The usage line carries several `{a,b}` choice groups (--transport's comes
    # first); the subcommand group is the one holding known subcommands.
    groups = [frozenset(g.split(",")) for g in re.findall(r"\{([a-z0-9,\-]+)\}", result.stdout)]
    for group in groups:
        if "vacuum" in group:
            return group
    raise AssertionError(f"could not parse subcommand list from:\n{result.stdout[:800]}")


@functools.lru_cache(maxsize=1)
def _console_scripts() -> frozenset[str]:
    """Names under ``[project.scripts]`` in pyproject.toml."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    block = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return frozenset(line.split("=", 1)[0].strip() for line in block.splitlines() if "=" in line)


def _assert_is_real_entry_point(command: str, label: str) -> None:
    """Assert *command* names an entry point that actually exists.

    Catches bug 4's shape: ``<prefix>/yadgar nightly-cycle`` looks plausible,
    passes every render assertion, and exits 2 on argparse because
    ``nightly-cycle`` is a console script, not a subcommand.
    """
    parts = command.split()
    assert parts, f"{label}: empty command"
    program = Path(parts[0]).name
    args = [a for a in parts[1:] if not a.startswith("-")]

    if program in {"python", "python3", sys.executable}:
        # `python3 -m <module>` — the module must be importable and runnable.
        assert "-m" in parts, f"{label}: bare python invocation {command!r}"
        module = parts[parts.index("-m") + 1]
        probe = subprocess.run(
            [sys.executable, "-c", f"import importlib.util as u; assert u.find_spec({module!r})"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert probe.returncode == 0, f"{label}: `-m {module}` is not importable"
        return

    assert program in _console_scripts(), (
        f"{label}: {program!r} is not a console script in [project.scripts] "
        f"({sorted(_console_scripts())})"
    )
    if program == "yadgar" and args:
        assert args[0] in _yadgar_subcommands(), (
            f"{label}: `yadgar {args[0]}` is NOT a subcommand "
            f"({sorted(_yadgar_subcommands())}). It would exit 2 on argparse "
            f"before doing any work. The nightly entry point is the "
            f"`yadgar-nightly-cycle` console script, invoked bare."
        )


# ── Phase 1: SurrealDB reachable from the host ────────────────────────────────


def test_systemd_backend_unit_publishes_surreal_port_on_loopback(tmp_path):
    render_systemd(tmp_path)
    unit = (tmp_path / "units" / "yadgar-backend.service").read_text()
    match = _LOOPBACK_SURREAL_PUBLISH.search(unit)
    assert match, (
        "yadgar-backend.service does not publish SurrealDB :8000 on 127.0.0.1 — "
        "the host-executed nightly/vacuum units default YADGAR_DB_URL to "
        "http://127.0.0.1:8000 and would connection-refuse on every fire"
    )
    assert match.group(1) == "8000", f"unexpected default host port {match.group(1)!r}"


def test_launchd_backend_plist_publishes_surreal_port_on_loopback(tmp_path):
    render_launchd(tmp_path)
    plist = plistlib.loads(
        (tmp_path / "units" / "com.openfantasy.yadgar-backend.plist").read_bytes()
    )
    run_cmd = " ".join(plist["ProgramArguments"])
    assert _LOOPBACK_SURREAL_PUBLISH.search(run_cmd), (
        "com.openfantasy.yadgar-backend.plist does not publish SurrealDB :8000 on "
        "127.0.0.1 — yadgar-setup.sh bootstraps the nightly + vacuum jobs on macOS, "
        "so they fire on schedule and connection-refuse today"
    )


def test_flake_backend_unit_publishes_surreal_port_on_loopback():
    """The precedent this car mirrors — asserted so it cannot silently regress.

    Own pattern: the flake's port is a Nix interpolation containing a space
    (``${toString cfg.backendSurrealPort}``), which the rendered-unit regex
    deliberately does not admit.
    """
    assert re.search(r"-p 127\.0\.0\.1:\$\{[^}]+\}:8000", FLAKE_NIX.read_text()), (
        "flake.nix no longer publishes SurrealDB on loopback"
    )


def test_systemd_surreal_port_is_overridable(tmp_path):
    """R2: :8000 is commonly occupied; the publish must be re-pointable."""
    render_systemd(tmp_path, extra_env={"YADGAR_BACKEND_SURREAL_PORT": "18000"})
    unit = (tmp_path / "units" / "yadgar-backend.service").read_text()
    assert "-p 127.0.0.1:18000:8000" in unit, f"YADGAR_BACKEND_SURREAL_PORT not honoured:\n{unit}"
    nightly = (tmp_path / "units" / "yadgar-nightly-cycle.service").read_text()
    assert "http://127.0.0.1:18000" in nightly, (
        "nightly unit's YADGAR_DB_URL does not follow the overridden port — it "
        f"would connect to the wrong port:\n{nightly}"
    )


def test_launchd_wrappers_derive_db_url_from_configurable_port():
    """Wrappers are copied verbatim (not sed-rendered), so the port has to reach
    them through the environment rather than a template token."""
    for wrapper in ("yadgar-vacuum-wrapper.sh", "yadgar-nightly-cycle-wrapper.sh"):
        text = (LAUNCHD_DIR / wrapper).read_text()
        assert "YADGAR_BACKEND_SURREAL_PORT" in text, (
            f"{wrapper} hardcodes the SurrealDB port — it cannot follow the "
            f"publish when YADGAR_BACKEND_SURREAL_PORT re-points it (R2)"
        )


# ── Phase 2: core unit state-dir bind + trigger env ───────────────────────────


def test_systemd_core_unit_mounts_state_dir_under_the_trigger_path(tmp_path):
    render_systemd(tmp_path)
    core = (tmp_path / "units" / "yadgar.service").read_text()
    assert "-e YADGAR_VACUUM_TRIGGER_PATH=" in core, (
        "core unit sets no YADGAR_VACUUM_TRIGGER_PATH — vacuum_now() refuses"
    )
    state = str(tmp_path / "state")
    assert f"-v {state}:/root/.local/state/yadgar" in core, (
        f"core unit does not bind the host state dir:\n{core}"
    )


def test_generate_systemd_pre_creates_the_trigger_dir(tmp_path):
    """Mirrors generate_launchd.sh:91 — removes the first-boot race."""
    render_systemd(tmp_path)
    assert (tmp_path / "state" / "triggers").is_dir(), (
        "generate_systemd.sh did not pre-create <state>/triggers"
    )


# ── Phase 3: the six new units render ─────────────────────────────────────────


def test_generate_systemd_renders_all_nine_units(tmp_path):
    render_systemd(tmp_path)
    written = {p.name for p in (tmp_path / "units").iterdir() if p.is_file()}
    missing = EXPECTED_SYSTEMD_UNITS - written
    assert not missing, f"generate_systemd.sh did not render: {sorted(missing)}"


def test_no_unsubstituted_template_token_survives(tmp_path):
    """A surviving `@TOKEN@` renders, activates, fires and fails at 4am."""
    render_systemd(tmp_path)
    for unit in sorted((tmp_path / "units").iterdir()):
        if not unit.is_file():
            continue
        leftover = re.findall(r"@[A-Z_]+@", unit.read_text())
        assert not leftover, f"{unit.name}: unsubstituted tokens {sorted(set(leftover))}"


def test_generate_systemd_summary_lists_every_rendered_unit(tmp_path):
    """The closing summary is what an installing user reads; a unit missing from
    it is a unit they never learn exists."""
    result = render_systemd(tmp_path)
    missing = [u for u in EXPECTED_SYSTEMD_UNITS if u not in result.stdout]
    assert not missing, f"units absent from the summary: {sorted(missing)}\n{result.stdout}"


@pytest.mark.parametrize(
    ("unit", "needles"),
    [
        (
            "yadgar-vacuum.service",
            ["Type=oneshot", "TimeoutStartSec=30min", "--service-mode=systemd"],
        ),
        (
            "yadgar-vacuum.timer",
            ["OnCalendar=Sun *-*-* 04:00:00", "RandomizedDelaySec=30min", "Persistent=true"],
        ),
        ("yadgar-vacuum-trigger.path", ["PathExists="]),
        ("yadgar-vacuum-trigger.service", ["Type=oneshot"]),
        ("yadgar-nightly-cycle.service", ["Type=oneshot", "TimeoutStartSec=1h"]),
        ("yadgar-nightly-cycle.timer", ["OnCalendar=*-*-* 19:00:00 UTC", "Persistent=true"]),
    ],
)
def test_systemd_maintenance_units_mirror_the_flake_shape(unit, needles, tmp_path):
    """D4: mirror flake.nix exactly, so the two systemd surfaces are comparable."""
    render_systemd(tmp_path)
    text = (tmp_path / "units" / unit).read_text()
    for needle in needles:
        assert needle in text, f"{unit} missing {needle!r}:\n{text}"


def test_maintenance_units_tolerate_a_missing_secrets_file(tmp_path):
    """`EnvironmentFile=-...`: a missing secrets file must not wedge a timer into
    a permanent start failure. Mirrors flake.nix's `-${cfg.secretsEnvFile}`."""
    render_systemd(tmp_path)
    for unit in ("yadgar-vacuum.service", "yadgar-nightly-cycle.service"):
        text = (tmp_path / "units" / unit).read_text()
        assert re.search(r"^EnvironmentFile=-", text, re.MULTILINE), (
            f"{unit}: EnvironmentFile is not optional (no leading '-')\n{text}"
        )


# ── D3: @VACUUM_EXEC@ / @NIGHTLY_EXEC@ resolution ─────────────────────────────


def _fake_shim(bin_dir: Path, name: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / name
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    return shim


def test_vacuum_exec_prefers_the_local_bin_shim(tmp_path):
    """D3 step 2: the pipx shape wins over `python3 -m yadgar`."""
    shim = _fake_shim(tmp_path / "home" / ".local" / "bin", "yadgar")
    render_systemd(tmp_path)
    unit = (tmp_path / "units" / "yadgar-vacuum.service").read_text()
    assert f"ExecStart={shim} vacuum" in unit, f"~/.local/bin/yadgar shim not preferred:\n{unit}"


def test_nightly_exec_prefers_the_local_bin_shim(tmp_path):
    """The nightly entry point is a DIFFERENT binary from `yadgar` — it must be
    resolved separately, not spelled `yadgar nightly-cycle`."""
    _fake_shim(tmp_path / "home" / ".local" / "bin", "yadgar")
    shim = _fake_shim(tmp_path / "home" / ".local" / "bin", "yadgar-nightly-cycle")
    render_systemd(tmp_path)
    unit = (tmp_path / "units" / "yadgar-nightly-cycle.service").read_text()
    assert f"ExecStart={shim}" in unit, f"nightly shim not preferred:\n{unit}"


def test_host_cli_env_override_wins(tmp_path):
    """D3 step 1: the escape hatch for odd layouts."""
    _fake_shim(tmp_path / "home" / ".local" / "bin", "yadgar")
    override = _fake_shim(tmp_path / "elsewhere", "yadgar")
    render_systemd(tmp_path, extra_env={"YADGAR_HOST_CLI": str(override)})
    unit = (tmp_path / "units" / "yadgar-vacuum.service").read_text()
    assert f"ExecStart={override} vacuum" in unit, (
        f"YADGAR_HOST_CLI did not win over the shim:\n{unit}"
    )


def _path_with_no_yadgar_and_a_failing_python(stub_dir: Path) -> str:
    """A PATH that keeps coreutils/sed but resolves NO yadgar CLI, and whose
    `python3` fails the importability probe.

    Built by subtraction from the real PATH rather than hardcoded, so it works
    on NixOS (where /usr/bin is nearly empty) as well as ordinary distros — and
    it needs no test-only backdoor inside the generator.
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "python3"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    kept = [
        p for p in os.environ.get("PATH", "").split(":") if p and not (Path(p) / "yadgar").exists()
    ]
    return ":".join([str(stub_dir), *kept])


def test_generate_systemd_fails_the_install_when_no_host_cli_resolves(tmp_path):
    """D3 step 5. Fail at render with an actionable message rather than at 4am
    with a unit that starts, fails, and is never looked at."""
    result = render_systemd(
        tmp_path,
        extra_env={"PATH": _path_with_no_yadgar_and_a_failing_python(tmp_path / "stub")},
        check=False,
    )
    assert result.returncode != 0, (
        "generate_systemd.sh rendered maintenance units with no resolvable host "
        f"CLI — those units would fail on every fire.\nstdout:\n{result.stdout}"
    )
    assert "pipx install yadgar" in (result.stdout + result.stderr), (
        f"error message names no fix:\n{result.stdout}\n{result.stderr}"
    )


def test_rendered_exec_targets_are_real_entry_points(tmp_path):
    """The bug-4 guard on the systemd surface."""
    render_systemd(tmp_path)
    for unit in ("yadgar-vacuum.service", "yadgar-nightly-cycle.service"):
        text = (tmp_path / "units" / unit).read_text()
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                _assert_is_real_entry_point(line.split("=", 1)[1], unit)


def test_launchd_wrappers_exec_real_entry_points():
    """BUG 4: `yadgar nightly-cycle` is not a subcommand — the wrapper exits 2 on
    argparse, so the macOS nightly job has never run, port publish or not."""
    for wrapper in ("yadgar-vacuum-wrapper.sh", "yadgar-nightly-cycle-wrapper.sh"):
        text = (LAUNCHD_DIR / wrapper).read_text()
        match = re.search(r"^exec\s+(.+)$", text, re.MULTILINE)
        assert match, f"{wrapper}: no exec line"
        # Strip the `"$TIMEOUT_BIN" <seconds>` prefix and shell quoting; expand
        # ${HOME} so Path(...).name sees the real program name.
        command = match.group(1).replace('"$TIMEOUT_BIN"', "").replace('"', "")
        command = re.sub(r"^\s*\d+\s+", "", command).replace("${HOME}", "/home/testuser")
        _assert_is_real_entry_point(command, wrapper)


# ── Phase 4: ACTIVATED, not merely rendered ───────────────────────────────────


def test_yadgar_target_wants_every_maintenance_unit(tmp_path):
    """D2: `Wants=` on the target is the ACTUAL activation mechanism.

    `systemctl --user enable yadgar.target` installs yadgar.target's own
    `WantedBy=default.target` symlink and nothing else — it does NOT enable units
    that merely declare `WantedBy=timers.target`. One site (the template) cannot
    drift; the three enable sites already have (§1.3).
    """
    render_systemd(tmp_path)
    target = (tmp_path / "units" / "yadgar.target").read_text()
    wants = " ".join(
        line.split("=", 1)[1] for line in target.splitlines() if line.startswith("Wants=")
    ).split()
    for unit in ("yadgar-vacuum.timer", "yadgar-nightly-cycle.timer", "yadgar-vacuum-trigger.path"):
        assert unit in wants, f"yadgar.target does not pull in {unit}; Wants={wants}"


def test_timers_keep_a_manual_enable_stanza(tmp_path):
    """`Wants=` is the mechanism, but a user who wants `systemctl --user enable
    yadgar-vacuum.timer` to work should not be told 'unit has no installation
    config'. Both, not either."""
    render_systemd(tmp_path)
    for unit, target in (
        ("yadgar-vacuum.timer", "timers.target"),
        ("yadgar-nightly-cycle.timer", "timers.target"),
        ("yadgar-vacuum-trigger.path", "paths.target"),
    ):
        text = (tmp_path / "units" / unit).read_text()
        assert f"WantedBy={target}" in text, f"{unit} has no [Install] WantedBy={target}"


# ── Phase 5: uninstall covers everything a generator renders ──────────────────


def test_uninstall_removes_every_systemd_unit_the_generator_renders(tmp_path):
    """Derived from an actual render, not a second hardcoded list — a hardcoded
    expectation here would be a THIRD drift site."""
    render_systemd(tmp_path)
    rendered = {p.name for p in (tmp_path / "units").iterdir() if p.is_file()}
    uninstall = UNINSTALL_SH.read_text()
    missing = sorted(u for u in rendered if u not in uninstall)
    assert not missing, f"uninstall.sh leaves these rendered units behind: {missing}"


def test_uninstall_removes_every_launchd_plist_the_generator_renders(tmp_path):
    render_launchd(tmp_path)
    rendered = {p.name for p in (tmp_path / "units").iterdir() if p.is_file()}
    uninstall = UNINSTALL_SH.read_text()
    missing = sorted(p for p in rendered if p not in uninstall)
    assert not missing, f"uninstall.sh leaves these rendered plists behind: {missing}"


def test_uninstall_clears_persistent_timer_stamps():
    """`man systemd.timer`: the Persistent=true stamp under
    ~/.local/share/systemd/timers/ outlives the unit file. Without a
    `systemctl clean --what=state`, a reinstall inherits a stale last-fire."""
    text = UNINSTALL_SH.read_text()
    assert "--what=state" in text, (
        "uninstall.sh does not clean persistent timer stamps — a reinstalled "
        "timer inherits the old timestamp and may fire (or not) unexpectedly"
    )


def test_doctor_probes_timer_and_path_activation():
    """R5/§1.3: a never-activated unit must be VISIBLE at --doctor, not silent."""
    text = SETUP_SH.read_text()
    assert "list-timers" in text, "doctor never probes timer state on Linux"
    assert "yadgar-vacuum-trigger.path" in text, (
        "doctor never probes whether the vacuum-trigger .path unit is active"
    )


# ── systemd's own opinion of the rendered units ───────────────────────────────


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available")
def test_rendered_oncalendar_expressions_actually_parse(tmp_path):
    """systemd's own opinion of the schedules — the one thing in this file a
    string assertion genuinely cannot check.

    Deliberately NOT `systemd-analyze verify`. On a unit set that references a
    container runtime and PATH-resolved helpers, verify's output is dominated by
    'Command <x> is not executable' lines that say nothing about correctness
    (observed on the dev host: podman, /bin/true, rm), and any filter loose
    enough to ignore those is loose enough to ignore real defects — a
    renders-but-validates-nothing test, i.e. this car's own bug class moved into
    the test layer. `systemd-analyze calendar` has no such noise: a malformed
    expression exits non-zero, and one that parses but can never elapse (typo'd
    weekday, impossible date) prints no 'Next elapse'.
    """
    render_systemd(tmp_path)
    expressions = [
        (unit.name, line.split("=", 1)[1].strip())
        for unit in sorted((tmp_path / "units").glob("*.timer"))
        for line in unit.read_text().splitlines()
        if line.startswith("OnCalendar=")
    ]
    assert expressions, "no OnCalendar= found in any rendered timer"

    for unit_name, expr in expressions:
        result = subprocess.run(
            ["systemd-analyze", "calendar", expr], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"{unit_name}: systemd rejects OnCalendar={expr!r}\n{result.stderr}"
        )
        assert "Next elapse" in result.stdout, (
            f"{unit_name}: OnCalendar={expr!r} parses but never elapses\n{result.stdout}"
        )
