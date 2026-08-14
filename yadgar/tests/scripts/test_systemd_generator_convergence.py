"""The nine-unit parity harness (task:0110, ADR-0190).

ADR-0190 converges the two systemd unit generators by ABSORB-then-delegate: the
Python renderer reaches parity with every ``scripts/install/*.in`` template
FIRST, and only then does ``generate_systemd.sh`` stop rendering. This module is
the mechanical acceptance test for "reached parity".

**The baseline is COMMITTED FIXTURES, not a render at test time.** The helper
that produces the ``sed`` render (``yadgar/tests/_unit_render.py``) invokes
``generate_systemd.sh``, which renders nothing after the Stage-D flip — the
baseline generator stops working in the same commit that most needs it. The
fixtures under ``snapshots/systemd/{podman,docker}/`` were captured ONCE, in
Stage A, from a tree that already carries task:0111's ``Wants=`` (plan §10): the
core unit's ``Wants=yadgar-backend.service`` is therefore enforced by byte-parity
itself, so reintroducing ``Requires=`` produces a diff line with no delta entry
and fails.

Capture inputs are :data:`SNAPSHOT_ENV` verbatim. Only ``YADGAR_STATE_DIR`` is a
``/tmp`` path — ``generate_systemd.sh`` ``mkdir -p``s it at render time, so it
must be creatable, and it is rendered into the unit text.

**The ledger, not a pile of xfails.** :data:`PARITY_UNITS` names the units that
render at parity today; :data:`PENDING_UNITS` names the remainder and the stage
that closes each. ``test_pending_units_still_diverge`` asserts the remainder
really does diverge, so converging a unit without moving it between the two sets
is a hard failure rather than a silently-passing xfail.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from yadgar import __version__
from yadgar.core.daemon.maintenance_units import HostExecs
from yadgar.core.daemon.unit_install import UNIT_SCHEMA_VERSION, stamp_unit
from yadgar.core.daemon.unit_model import render_unit
from yadgar.core.daemon.units import ALL_UNIT_NAMES, build_units, setup_unit_spec
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import render_systemd

SNAPSHOTS = REPO_ROOT / "yadgar" / "tests" / "scripts" / "snapshots" / "systemd"
RUNTIMES = ("podman", "docker")

SNAPSHOT_ENV = {
    "YADGAR_INSTALL_PREFIX": "/home/testuser/.local/share/yadgar",
    "YADGAR_SECRETS_ENV_FILE": "/home/testuser/.config/yadgar/secrets.env",
    "YADGAR_BACKEND_IMAGE": "docker.io/openfantasy/yadgar-backend:9.9.9",
    "YADGAR_CORE_IMAGE": "docker.io/openfantasy/yadgar:9.9.9",
    "YADGAR_STATE_DIR": "/tmp/yadgar-parity-fixture/state",  # noqa: S108 — see module docstring
    "YADGAR_BACKEND_SURREAL_PORT": "8000",
    "YADGAR_HOST_CLI": "/home/testuser/.local/bin/yadgar",
    "YADGAR_HOST_NIGHTLY_CLI": "/home/testuser/.local/bin/yadgar-nightly-cycle",
}

# Stage D: this was a fourth hand-transcription of the same nine names
# (generate_systemd.sh's UNITS array, uninstall.sh's SYSTEMD_UNITS,
# test_v5_169's EXPECTED_SYSTEMD_UNITS, and this). It is now derived.
ALL_UNITS = ALL_UNIT_NAMES

# ── The convergence ledger ───────────────────────────────────────────────────
# Moving a unit from PENDING_UNITS to PARITY_UNITS is the only way to make its
# parity assertion run — and leaving it in PENDING once it converges fails
# test_pending_units_still_diverge. Both directions are gated.

PARITY_UNITS: frozenset[str] = frozenset(ALL_UNITS)

# Stage C emptied this: all nine render at parity, so the ledger's pending half
# is now the PROOF rather than a to-do list. Both it and
# test_pending_units_still_diverge stay — Stage D still needs the ledger, and an
# empty dict is what says "nothing is outstanding". Re-populating it is how a
# future unit re-enters the ratchet.
PENDING_UNITS: dict[str, str] = {}


@dataclass(frozen=True)
class Delta:
    """One approved difference between the Python render and the ``sed`` baseline.

    *pattern* is a regex searched against each changed diff line; *reason* says
    WHY, and the harness asserts it is non-empty — a blank reason is how this
    list rots into a mute allowlist.
    """

    pattern: str
    reason: str


# Keyed PER UNIT: a delta approved for yadgar.service does not excuse the same
# text elsewhere. A unit with no entry must match byte-for-byte. Exactly TWO
# units may ever carry entries (see test_only_the_divergent_two_carry_deltas) —
# a third is the tripwire that the port is drifting rather than converging.
INTENTIONAL_DELTAS: dict[str, list[Delta]] = {
    "yadgar-backend.service": [
        Delta(
            r"^(Type=(simple|notify|exec)|NotifyAccess=all|TimeoutStartSec=180)$",
            "D4 of the plan: the converged backend takes the READINESS shape. The template "
            "was Type=simple on both runtimes, under which systemd calls the unit started the "
            "instant `podman run` forks — so the core's TimeoutStartSec=120 had the backend's "
            "cold model load inside it, and ADR-0187's 'the backend is already HEALTHY' premise "
            "was false on the documented install path. 180s is flake.nix's field-proven budget "
            "for this identical unit shape.",
        ),
        Delta(
            r"^    --sdnotify=healthy \\$",
            "The podman half of the readiness shape: podman's sd_notify proxy emits READY=1 on "
            "the first HEALTHY healthcheck, which is what makes Type=notify meaningful here.",
        ),
        Delta(
            r"^    --health-(cmd|start-period)",
            "The container healthcheck --sdnotify=healthy reports on. The 60s grace covers "
            "cold model load; the backend's /health is 200 only when db_ok and engine_loaded.",
        ),
        Delta(
            r"^    -v \S+huggingface:/home/yadgar/\.cache/huggingface \\$",
            "Part of the UNION the Python generator carried and the templates lacked: binding "
            "the host HuggingFace cache stops the backend re-downloading models on every "
            "container replacement. Emitted only when the host cache directory exists.",
        ),
        Delta(
            r"^ExecStartPost=curl .*127\.0\.0\.1:8001/health$",
            "The docker half of the readiness shape (ADR-0185). Docker has no sd_notify proxy "
            "at all, so a bounded /health poll is the only way to give After= the same ordering "
            "guarantee Type=notify buys on podman. 75 x 2s = 150s, inside the 180s budget.",
        ),
        Delta(
            r"^# (default socket / active context|This unit stays Type=simple|make conditional"
            r"|task:0110 / ADR-0190|renders `notify` for podman|HEALTHY healthcheck, and `exec`"
            r"|instead\. It was `simple`|started the instant `podman run` FORKED"
            r"|backend's cold model load inside it)",
            "Prose that the render itself contradicts. The template asserted 'This unit stays "
            "Type=simple on BOTH runtimes'; after D4 that is false of the very unit carrying "
            "the comment. Replaced with what the readiness shape is and why, plus the ADR-0187 "
            "correction. A comment that lies about its own unit is worse than no comment.",
        ),
    ],
    "yadgar.service": [
        Delta(
            r"^Description=Yadgar Memory Engine / MCP Server",
            "Plan §1.4: the template's '(Docker)' suffix is unconditional, so every podman "
            "install got a unit describing itself as Docker. The runtime is visible in the "
            "Exec lines; the Description does not need to guess at it.",
        ),
        Delta(
            r"^# (generate_systemd\.sh either STRIPS|The renderer emits each runtime-conditional)",
            "The column-0 marker mechanism ADR-0185 introduced is retired by ADR-0190 — the "
            "runtime conditional becomes a Python branch. The sentence describing sed markers "
            "would be false of a unit this renderer produced.",
        ),
        Delta(
            r"^# (Docker readiness gate\.|and podman above\.|applies; curl's --retry"
            r"|listening yet\", --retry-all-errors|\(--fail makes a 5xx|45 \* 2s = 90s"
            r"|start timeout would turn|/health \(readiness\) not|emitted last, after the full"
            r"|unit active EARLIER)",
            "Plan §1.4: only the ExecStartPost line carried the column-0 @DOCKER_ONLY@ marker, "
            "so the ten-line block explaining it survived the podman render as an orphan — "
            "documentation for a directive that arm does not have. The renderer emits the "
            "rationale with the directive it explains, so it is still there on docker.",
        ),
    ],
}


def _snapshot(runtime: str, unit: str) -> str:
    return (SNAPSHOTS / runtime / unit).read_text()


def _rendered(
    runtime: str, hf_cache_dir: str | None = "/home/testuser/.cache/huggingface"
) -> dict[str, str]:
    """The Python renderer's output for the ``yadgar-setup`` arm, same inputs."""
    spec = setup_unit_spec(
        runtime=runtime,
        data_dir=SNAPSHOT_ENV["YADGAR_INSTALL_PREFIX"],
        state_dir=SNAPSHOT_ENV["YADGAR_STATE_DIR"],
        secrets_env_file=SNAPSHOT_ENV["YADGAR_SECRETS_ENV_FILE"],
        backend_image=SNAPSHOT_ENV["YADGAR_BACKEND_IMAGE"],
        surreal_port=int(SNAPSHOT_ENV["YADGAR_BACKEND_SURREAL_PORT"]),
        hf_cache_dir=hf_cache_dir,
        # The fixtures were captured with these two exported, so the shell's
        # _resolve_host_exec took its override branch. Passing the same values
        # pins the render without the test depending on what is installed here.
        execs=HostExecs(
            vacuum=SNAPSHOT_ENV["YADGAR_HOST_CLI"],
            nightly=SNAPSHOT_ENV["YADGAR_HOST_NIGHTLY_CLI"],
        ),
    )
    return {name: render_unit(u) for name, u in build_units(spec).items()}


def _changed_lines(expected: str, actual: str) -> list[str]:
    """Every added/removed line of the unified diff, without the +/- marker."""
    diff = difflib.unified_diff(expected.splitlines(), actual.splitlines(), n=0, lineterm="")
    return [line[1:] for line in diff if line[:1] in "+-" and not line.startswith(("---", "+++"))]


def _unmatched(unit: str, changed: list[str]) -> tuple[list[str], set[str]]:
    """Split *changed* into (lines no delta explains, patterns that did match)."""
    deltas = INTENTIONAL_DELTAS.get(unit, [])
    unmatched, used = [], set()
    for line in changed:
        hits = [d for d in deltas if re.search(d.pattern, line)]
        if hits:
            used.update(d.pattern for d in hits)
        else:
            unmatched.append(line)
    return unmatched, used


# ── The ledger ───────────────────────────────────────────────────────────────


def test_ledger_covers_every_unit_exactly_once():
    """Every one of the nine is either at parity or explicitly pending, never both."""
    assert PARITY_UNITS <= set(ALL_UNITS)
    assert set(PENDING_UNITS) <= set(ALL_UNITS)
    assert not (PARITY_UNITS & set(PENDING_UNITS)), "a unit cannot be both converged and pending"
    assert PARITY_UNITS | set(PENDING_UNITS) == set(ALL_UNITS)
    assert all(reason.strip() for reason in PENDING_UNITS.values()), (
        "every pending unit must name the stage that closes it"
    )


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_pending_units_still_diverge(runtime):
    """The pending remainder really does diverge — the RED half of the harness.

    Without this a pending unit that silently reached parity would sit
    unasserted forever. It also proves the diff machinery fires at all: a broken
    ``_changed_lines`` would make every unit look converged.
    """
    rendered = _rendered(runtime)
    for unit in PENDING_UNITS:
        actual = rendered.get(unit)
        if actual is None:
            continue  # not emitted at all — divergent by construction
        assert actual != _snapshot(runtime, unit), (
            f"{unit} now renders at parity on {runtime} but is still listed in "
            f"PENDING_UNITS. Move it to PARITY_UNITS so its byte-diff is enforced."
        )


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_converged_units_match_the_baseline_modulo_intentional_deltas(runtime):
    """Every unit in PARITY_UNITS matches the ``sed`` baseline, deltas excepted."""
    if not PARITY_UNITS:
        pytest.skip("Stage A: nothing has converged yet — the ledger test is the gate")
    rendered = _rendered(runtime)
    for unit in sorted(PARITY_UNITS):
        assert unit in rendered, f"{unit} is listed at parity but the renderer never emits it"
        unmatched, _ = _unmatched(unit, _changed_lines(_snapshot(runtime, unit), rendered[unit]))
        assert not unmatched, (
            f"{runtime}/{unit}: {len(unmatched)} diff line(s) with no INTENTIONAL_DELTAS entry:\n"
            + "\n".join(f"  {line}" for line in unmatched)
        )


@pytest.mark.parametrize(
    ("runtime", "expected", "forbidden"),
    [("podman", "Type=notify", "Type=exec"), ("docker", "Type=exec", "Type=notify")],
)
def test_setup_arm_backend_takes_the_right_readiness_type(runtime, expected, forbidden):
    """D4's headline change, pinned per arm rather than left to a blanket delta.

    The backend readiness delta's matcher approves any ``Type=`` value, so a
    podman render that came out ``Type=exec`` (nothing can gate readiness there —
    podman's sd_notify proxy is the whole mechanism) would satisfy it. Nothing
    else covers this render: the readiness cross-generator suite's Python arm
    exercises ``install_systemd_service``, not ``setup_unit_spec``.
    """
    text = _rendered(runtime)["yadgar-backend.service"]
    assert f"\n{expected}\n" in text, f"setup-arm backend on {runtime} is not {expected}"
    assert f"\n{forbidden}\n" not in text, f"setup-arm backend on {runtime} carries {forbidden}"
    assert "\nType=simple\n" not in text, (
        "the converged backend must never be Type=simple — systemd would call it "
        "started the instant the container command forks (ADR-0190 D4)"
    )


def test_no_intentional_delta_entry_is_stale():
    """An unused delta fails: a stale allowlist entry is a hard failure in this repo."""
    used: set[str] = set()
    for runtime in RUNTIMES:
        rendered = _rendered(runtime)
        for unit in PARITY_UNITS:
            if unit in rendered:
                _, hit = _unmatched(unit, _changed_lines(_snapshot(runtime, unit), rendered[unit]))
                used |= hit
    stale = [
        f"{unit}: {d.pattern}"
        for unit, deltas in INTENTIONAL_DELTAS.items()
        for d in deltas
        if d.pattern not in used
    ]
    assert not stale, "INTENTIONAL_DELTAS entries that matched nothing:\n" + "\n".join(stale)


def test_every_intentional_delta_carries_a_reason():
    for unit, deltas in INTENTIONAL_DELTAS.items():
        for d in deltas:
            assert d.reason.strip(), f"{unit}: delta {d.pattern!r} has a blank reason"
            assert len(d.reason) >= 30, f"{unit}: delta {d.pattern!r} reason is too thin"


def test_only_the_divergent_two_carry_deltas():
    """The tripwire: a third unit needing a delta means the port is drifting.

    The seven greenfield units have no Python counterpart to disagree with, so
    they must come out byte-identical. If one needs an approved difference, stop
    and re-examine rather than adding a line here.
    """
    assert set(INTENTIONAL_DELTAS) <= {"yadgar.service", "yadgar-backend.service"}, (
        f"unexpected units carry INTENTIONAL_DELTAS: {sorted(set(INTENTIONAL_DELTAS))}"
    )


# ── Fixture integrity ────────────────────────────────────────────────────────


def test_snapshot_set_is_exactly_the_nine_units_per_runtime():
    for runtime in RUNTIMES:
        assert {p.name for p in (SNAPSHOTS / runtime).iterdir()} == set(ALL_UNITS)


# REPLACED task:0110 Stage D — test_snapshots_are_a_faithful_render_of_the_templates.
# It re-rendered scripts/install/*.in through `sed` and required byte-equality
# with the fixtures, which stopped a template edit from silently invalidating the
# baseline. The templates are deleted, so it had nothing left to render. Its job
# — "the thing the installer actually writes is the thing this file diffs" — is
# taken over by the end-to-end test below, which is stronger: it goes through the
# real wrapper, so a wrapper that resolved the wrong renderer or skipped the
# delegation fails here rather than passing on an in-process render.


def test_the_wrapper_installs_exactly_what_the_python_renderer_builds(tmp_path: Path):
    """``generate_systemd.sh`` renders nothing — it delegates, and installs THIS.

    The parity assertions above compare builder output to the fixtures in
    process. This is the only test that proves the shell entry point users
    actually run reaches that builder: it runs the wrapper and requires every
    installed file to equal ``stamp + render_unit(...)`` byte for byte.

    ``hf_cache_dir=None`` because ``render_systemd`` patches ``HOME`` to a fresh
    tmp dir, where ``~/.cache/huggingface`` does not exist — the same conditional
    the profile arm applies.
    """
    for runtime in RUNTIMES:
        root = tmp_path / runtime
        render_systemd(root, {**SNAPSHOT_ENV, "YADGAR_RUNTIME": runtime})
        expected = _rendered(runtime, hf_cache_dir=None)
        installed = {p.name: p.read_text() for p in (root / "units").iterdir() if p.is_file()}
        assert set(installed) == set(ALL_UNITS), (
            f"{runtime}: the wrapper installed {sorted(installed)}, not the nine units"
        )
        for unit, text in sorted(installed.items()):
            assert text == stamp_unit(expected[unit], __version__), (
                f"{runtime}/{unit}: the wrapper installed something other than the "
                f"Python renderer's stamped output"
            )


def test_installed_units_carry_the_schema_stamp(tmp_path: Path):
    """The stamp's own shape, pinned once (plan §7) rather than per unit.

    It is applied on the WRITE path, so it never reaches ``render_unit`` output
    and never becomes an ``INTENTIONAL_DELTAS`` entry — which is what keeps
    ``test_only_the_divergent_two_carry_deltas`` a live tripwire instead of a
    line every unit needs.
    """
    root = tmp_path / "podman"
    render_systemd(root, {**SNAPSHOT_ENV, "YADGAR_RUNTIME": "podman"})
    for unit in ALL_UNITS:
        head = (root / "units" / unit).read_text().splitlines()[:2]
        assert head[0] == f"# yadgar-unit-schema: {UNIT_SCHEMA_VERSION}", f"{unit}: {head[0]!r}"
        assert head[1] == f"# rendered-by: yadgar {__version__}", f"{unit}: {head[1]!r}"


def test_no_snapshot_carries_the_stamp():
    """The fixtures are BUILDER output, so the stamp must not have leaked in.

    If it ever does, every unit acquires a diff line, no unit is byte-identical,
    and the divergent-two tripwire fires on a correct implementation.
    """
    for runtime in RUNTIMES:
        for unit in ALL_UNITS:
            assert "yadgar-unit-schema" not in _snapshot(runtime, unit), (
                f"{runtime}/{unit}: the schema stamp leaked into the parity fixture"
            )


def test_backend_unit_health_start_period_is_within_measured_window():
    """Car K: --health-start-period must give the backend enough grace for
    cold model load (measured 20-40s on a fresh install) without
    approaching the unit's TimeoutStartSec=180. Pin a floor of 45s and
    a ceiling of 180s — below 45s and a slow first start crashes the
    healthcheck; above 180s and the period itself outlives the start
    budget, turning a slow start into a Restart=on-failure crashloop."""
    for runtime in RUNTIMES:
        text = _rendered(runtime)["yadgar-backend.service"]
        match = re.search(r"--health-start-period=(\d+)s", text)
        assert match, f"{runtime}/yadgar-backend.service: --health-start-period=Ns missing"
        seconds = int(match.group(1))
        assert 45 <= seconds <= 180, (
            f"{runtime}/yadgar-backend.service: --health-start-period={seconds}s "
            f"is outside the 45s-180s window (measured cold-start 20-40s, "
            f"TimeoutStartSec=180 budget)."
        )
