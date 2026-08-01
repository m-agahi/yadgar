"""Shape guards for the seven greenfield units (task:0110 Stage C, ADR-0190).

The parity harness proves the seven render byte-identically to the ``sed``
baseline. It cannot prove they are still RIGHT after the templates are deleted
in Stage D, because at that point the fixtures are the only baseline and a
fixture is not an argument. These tests pin the properties that would otherwise
be re-derivable only from a template that no longer exists — plan §6.5.

Each one guards a failure mode that renders cleanly and passes a naive
"contains" assertion:

* dropping ``yadgar.target``'s SECOND ``Wants=`` stops all background
  maintenance while every render assertion still passes,
* reversing the trigger service's two ``ExecStart=`` lines lets a failed vacuum
  pin the ``.path`` unit active so it never fires again,
* normalising the two timers' timezones through a shared helper silently moves
  one of them by up to a day's worth of offset,
* adding ``[Install]`` to a unit that deliberately has none changes what
  ``systemctl --user enable`` pulls in.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from yadgar.core.daemon.maintenance_units import (
    MAINTENANCE_UNIT_NAMES,
    HostExecs,
    build_maintenance_units,
    build_target_unit,
)
from yadgar.core.daemon.unit_model import Directive, Section, UnitFile, render_unit
from yadgar.core.daemon.units import build_units, setup_unit_spec

STATE_DIR = "/home/testuser/.local/state/yadgar"
DATA_DIR = "/home/testuser/.local/share/yadgar"
SECRETS = "/home/testuser/.config/yadgar/secrets.env"
EXECS = HostExecs(
    vacuum="/home/testuser/.local/bin/yadgar",
    nightly="/home/testuser/.local/bin/yadgar-nightly-cycle",
)

# The three units that deliberately carry NO [Install] — they are started by
# their timer / path, never enabled. yadgar.target's Wants= is the activation
# mechanism for all of them. (Plan §4.1 says "four of the nine"; the templates
# say three — yadgar-vacuum-trigger.path DOES ship an [Install]. Observed
# code wins, and this set is what makes the drift visible either way.)
NO_INSTALL_UNITS = frozenset(
    {
        "yadgar-vacuum.service",
        "yadgar-vacuum-trigger.service",
        "yadgar-nightly-cycle.service",
    }
)


def _seven() -> dict[str, UnitFile]:
    return build_maintenance_units(
        state_dir=STATE_DIR,
        data_dir=DATA_DIR,
        secrets_env_file=SECRETS,
        surreal_port=8000,
        execs=EXECS,
    )


def _spec(runtime: str):
    return setup_unit_spec(
        runtime=runtime,
        data_dir=DATA_DIR,
        state_dir=STATE_DIR,
        secrets_env_file=SECRETS,
        backend_image="docker.io/openfantasy/yadgar-backend:9.9.9",
        execs=EXECS,
    )


def _nine(runtime: str) -> dict[str, UnitFile]:
    return build_units(_spec(runtime))


# ── yadgar.target: the duplicate Wants= ──────────────────────────────────────

MAINTENANCE_PULL_INS = frozenset(
    {"yadgar-vacuum.timer", "yadgar-nightly-cycle.timer", "yadgar-vacuum-trigger.path"}
)


def _wanted_units(target: UnitFile) -> set[str]:
    """Every unit the target pulls in, unioned across ALL its ``Wants=`` lines.

    systemd unions repeated directives; ``Section.values`` is the accessor that
    preserves them. Reading only the first (what any dict-keyed model would do)
    is the bug this whole helper exists to make visible.
    """
    unit = target.section("Unit")
    assert unit is not None
    return {name for value in unit.values("Wants") for name in value.split()}


def test_target_wants_is_written_on_two_lines():
    unit = build_target_unit().section("Unit")
    assert unit is not None
    assert len(unit.values("Wants")) == 2, (
        "yadgar.target must carry TWO Wants= directives — the core/backend pair "
        "and the maintenance trio. Merging them into one line is harmless; "
        "keeping only one is how background maintenance silently stops."
    )


def test_target_pulls_in_every_maintenance_unit():
    assert MAINTENANCE_PULL_INS <= _wanted_units(build_target_unit())


def test_dropping_the_second_wants_is_caught():
    """Mutation proof: the assertion above is not satisfied by the first line alone.

    Without this, a model that collapsed duplicates would still pass
    ``test_target_pulls_in_every_maintenance_unit`` if the FIRST ``Wants=``
    happened to be the survivor — it would not, but nothing would say so.
    """
    target = build_target_unit()
    section = target.section("Unit")
    assert section is not None
    seen_wants = False
    kept = []
    for entry in section.entries:
        if isinstance(entry, Directive) and entry.key == "Wants":
            if seen_wants:
                continue  # drop the SECOND Wants= — the maintenance trio
            seen_wants = True
        kept.append(entry)
    mutant = UnitFile(
        name=target.name,
        sections=(Section("Unit", tuple(kept)), *target.sections[1:]),
    )
    assert not MAINTENANCE_PULL_INS & _wanted_units(mutant), (
        "the mutant still pulls in maintenance units — the guard is vacuous"
    )
    assert "Wants=" in render_unit(mutant), (
        "the mutant must still RENDER cleanly and still contain Wants= — that is "
        "exactly why a 'contains Wants=' assertion cannot catch this"
    )


# ── The trigger service: two ExecStart=, in order ────────────────────────────


def test_trigger_service_has_two_execstart_in_order():
    service = _seven()["yadgar-vacuum-trigger.service"].section("Service")
    assert service is not None
    execs = service.values("ExecStart")
    assert len(execs) == 2, "the trigger service needs both the rm and the start"
    assert execs[0] == f"rm -f {STATE_DIR}/triggers/vacuum_requested"
    assert execs[1] == "systemctl --user start yadgar-vacuum.service"
    assert service.values("Type") == ["oneshot"], (
        "two ExecStart= lines are legal ONLY under Type=oneshot; systemd refuses "
        "to load the unit otherwise"
    )


# ── No [Install] where the templates omit it ─────────────────────────────────


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_only_the_timer_started_units_omit_install(runtime):
    omitted = {n for n, u in _nine(runtime).items() if u.section("Install") is None}
    assert omitted == NO_INSTALL_UNITS, (
        "an [Install] section changes what `systemctl --user enable yadgar.target` "
        f"pulls in. Units missing [Install]: {sorted(omitted)}"
    )


# ── Timer timezones deliberately differ ──────────────────────────────────────


def test_timer_timezones_are_not_normalised():
    units = _seven()
    vacuum = units["yadgar-vacuum.timer"].section("Timer")
    nightly = units["yadgar-nightly-cycle.timer"].section("Timer")
    assert vacuum is not None and nightly is not None
    assert vacuum.values("OnCalendar") == ["Sun *-*-* 04:00:00"], (
        "the weekly vacuum is LOCAL time, matching flake.nix"
    )
    assert nightly.values("OnCalendar") == ["*-*-* 19:00:00 UTC"], (
        "the nightly cycle is UTC, matching flake.nix"
    )
    assert vacuum.values("RandomizedDelaySec") == ["30min"]
    assert vacuum.values("Persistent") == nightly.values("Persistent") == ["true"]


# ── The seven take no runtime ────────────────────────────────────────────────


def test_the_seven_render_identically_on_both_runtimes():
    """Only the two service units are runtime-conditional.

    Without this, a runtime branch could be introduced into a maintenance unit
    and the parity harness would still pass — each arm would simply match its
    own fixture, which would have been re-captured to agree with the bug.
    """
    podman, docker = _nine("podman"), _nine("docker")
    for name in MAINTENANCE_UNIT_NAMES:
        assert render_unit(podman[name]) == render_unit(docker[name]), (
            f"{name} differs between runtimes; the seven have no readiness contract"
        )


# ── The state dir is one shared constant, not two agreeing strings ───────────


def test_state_dir_reaches_the_core_bind_and_the_path_unit():
    """Plan §4.2: one renderer turns a cross-generator invariant into an input.

    The core container's ``YADGAR_VACUUM_TRIGGER_PATH`` write is only observable
    on the host if the ``.path`` unit watches the directory the ``-v`` bind
    projects it into. They used to be two strings a test compared; now they are
    one ``spec.state_dir``.
    """
    units = _nine("podman")
    core = units["yadgar.service"].section("Service")
    path = units["yadgar-vacuum-trigger.path"].section("Path")
    assert core is not None and path is not None
    assert f"-v {STATE_DIR}:/root/.local/state/yadgar" in core.values("ExecStart")[0]
    assert path.values("PathExists") == [f"{STATE_DIR}/triggers/vacuum_requested"]


# ── systemd-analyze verify ───────────────────────────────────────────────────

_ANALYZE = shutil.which("systemd-analyze")
# systemd-analyze resolves every Exec* command against the LOCAL filesystem, so
# `podman`, `rm` and a /home/testuser CLI are all reported missing on a machine
# that has none of them. Those lines are about this host, not about the unit.
_HOST_NOISE = "is not executable"


def _verify(paths: list[Path]) -> list[str]:
    proc = subprocess.run(  # noqa: S603 — argv list, resolved binary, no shell
        [str(_ANALYZE), "verify", "--user", "--recursive-errors=no", *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )
    out = f"{proc.stdout}\n{proc.stderr}"
    return [line for line in out.splitlines() if line.strip() and _HOST_NOISE not in line]


@pytest.mark.skipif(_ANALYZE is None, reason="systemd-analyze not available on this host")
@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_systemd_analyze_accepts_all_nine(tmp_path: Path, runtime):
    units = _nine(runtime)
    paths = []
    for name, unit in units.items():
        path = tmp_path / name
        path.write_text(render_unit(unit))
        paths.append(path)
    assert len(paths) == 9
    assert not _verify(paths), "systemd-analyze rejected a rendered unit"


@pytest.mark.skipif(_ANALYZE is None, reason="systemd-analyze not available on this host")
def test_systemd_analyze_rejects_two_execstart_without_oneshot(tmp_path: Path):
    """Mutation proof for the filter above: real errors are not swallowed by it."""
    trigger = _seven()["yadgar-vacuum-trigger.service"]
    service = trigger.section("Service")
    assert service is not None
    mutant = UnitFile(
        name=trigger.name,
        sections=(
            trigger.sections[0],
            Section(
                "Service",
                tuple(
                    Directive("Type", "simple")
                    if isinstance(e, Directive) and e.key == "Type"
                    else e
                    for e in service.entries
                ),
            ),
        ),
    )
    path = tmp_path / "yadgar-vacuum-trigger.service"
    path.write_text(render_unit(mutant))
    complaints = _verify([path])
    assert any("more than one ExecStart" in line for line in complaints), complaints


# ── The gate on build_units emitting the seven ───────────────────────────────


def test_the_seven_are_omitted_without_host_execs():
    """``daemon install-service`` resolves no host CLI and installs no timers.

    Emitting yadgar.target there would name four units that arm never writes,
    so ``systemctl --user enable yadgar.target`` would fail on missing units.
    The execs are one optional field rather than two, so "one resolved, one not"
    is unrepresentable instead of merely untested.
    """
    spec = _spec("podman")
    assert set(build_units(spec)) >= set(MAINTENANCE_UNIT_NAMES)
    emitted = set(build_units(replace(spec, execs=None)))
    assert emitted == {"yadgar.service", "yadgar-backend.service"}
