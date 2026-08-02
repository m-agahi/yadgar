"""Cross-generator regression: no generator may emit a PODMAN-ONLY readiness
construct into a unit it renders for DOCKER.

task:0104 fixed the runtime BINARY in the generated systemd units — 13 hardcoded
``docker`` literals now resolve via ``_get_runtime()``. It did not make the
generators runtime-agnostic end to end, and said so in its own module docstring
(``yadgar/core/daemon/systemd.py``). The residual defect is READINESS:

* ``Type=notify`` needs something to send ``READY=1``. On podman that is the
  sd_notify proxy — ``--sdnotify=healthy`` for the backend, and the default
  ``--sdnotify=container`` mode forwarding the daemon's own emit for the core.
  **Docker has no sd_notify proxy at all**: it never sets ``NOTIFY_SOCKET``
  inside the container, so ``yadgar/core/daemon/sd_notify.py``'s emit is a silent
  no-op (it returns False when ``NOTIFY_SOCKET`` is unset) and the unit sits
  until ``TimeoutStartSec`` kills it.
* ``--sdnotify`` is not a ``docker run`` flag at all, so the backend unit exits
  non-zero before the container is even created.
* ``Environment=DOCKER_HOST=unix:///run/podman/podman.sock`` is inert on podman
  (podman reads ``CONTAINER_HOST``; this is a docker-compat-shim belt) but
  actively fatal on docker — it redirects the docker CLI at a socket that does
  not exist, so every ``Exec*=`` in the unit fails.

The docker answer is ``Type=exec`` plus a bounded ``ExecStartPost=`` health gate:
``man systemd.service`` — *"the execution of ExecStartPost= is taken into account
for the purpose of Before=/After= ordering constraints"* — which is the same
ordering guarantee ``Type=notify`` buys on podman.

Design: ``docs/plans/archive/runtime-agnostic-systemd-readiness-2026-08-01.md``.

Same structural template as its siblings ``test_admin_token_cross_generator.py``
(ADR-0180), ``test_backend_db_mount_cross_generator.py`` (Bug 11) and
``test_backend_unit_queue_base_cross_generator.py``: a FUTURE generator — or a
change to an existing one — that reintroduces a podman-only readiness construct
on the docker path fails this ONE shared test.

**Why this is a new file rather than more of ``test_daemon_runtime_binary.py``:**
that module's detectors key on a runtime BINARY NAME appearing where a resolved
runtime belongs. The invariant here is about runtime-specific FLAGS and
DIRECTIVES that contain no binary name at all (``--sdnotify=healthy``,
``Type=notify``), so it needs a different detector, not a wider one. The
marker-blindness fix that the ``.in`` template syntax DOES create is an
extension of that module, and lives there
(``test_unit_directive_guard_sees_through_runtime_conditional_markers``).

**Deliberately out of scope** (absence is a decision, not an oversight):

* ``flake.nix`` — a fourth generator, deliberately podman-pinned by design
  (``flake.nix``: "Default runtime: podman via the docker-compat shim"), and not
  reachable through ``YADGAR_RUNTIME``. Nothing renders it for docker.
* ``docker-compose.yml`` — a self-contained dev/CI stack with no systemd.
* ``yadgar/core/systemd/yadgar.service`` — a checked-in ``Type=simple`` unit that
  runs ``python3 -m yadgar`` directly; no container, so no readiness proxy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile
from yadgar.tests._unit_render import render_launchd, render_systemd

# Constructs that only mean anything under podman's sd_notify proxy. Each entry
# is (pattern, why-it-is-fatal-on-docker) — the message is what a future reader
# gets when their generator change trips this.
_PODMAN_ONLY = (
    (
        re.compile(r"--sdnotify\b"),
        "`--sdnotify` is not a `docker run` flag — the container is never created",
    ),
    (
        re.compile(r"^\s*Type\s*=\s*notify", re.MULTILINE),
        "docker never sets NOTIFY_SOCKET in the container, so nothing sends "
        "READY=1 and the unit sits until TimeoutStartSec",
    ),
    (
        re.compile(r"DOCKER_HOST\s*=\s*unix:///run/podman/podman\.sock"),
        "points the docker CLI at a podman socket that does not exist",
    ),
)

# The docker readiness gate: an ExecStartPost= poll of a /health endpoint.
_GATE_RE = re.compile(r"^\s*ExecStartPost\s*=.*\bcurl\b.*--retry\b.*/health", re.MULTILINE)

# task:0106 — a unit whose START is gated on readiness, and the two numbers whose
# relation that gating depends on. Type=simple is deliberately absent: such a
# unit is "started" the instant the fork succeeds, so TimeoutStartSec never binds.
_READINESS_TYPE_RE = re.compile(
    r"^[^\S\n]*Type[^\S\n]*=[^\S\n]*(notify|exec)[^\S\n]*$", re.MULTILINE
)
# Horizontal whitespace only ([^\S\n], not \s) on both sides of the `=`: plain
# ``\s*`` would let ``TimeoutStartSec=`` with an EMPTY value — which systemd reads
# as "reset to the default", i.e. the exact 90s this car is escaping — swallow the
# newline and capture the NEXT directive as its value.
_TIMEOUT_RE = re.compile(r"^[^\S\n]*TimeoutStartSec[^\S\n]*=[^\S\n]*(\S+)[^\S\n]*$", re.MULTILINE)
_GATE_BUDGET_RE = re.compile(r"--retry\s+(\d+)\s+--retry-delay\s+(\d+)")

# The default a readiness-gated unit inherits when it declares nothing (``man
# systemd.service``: DefaultTimeoutStartSec, 90s). The floor, not the value: this
# guard pins the RELATION and the escape from the default, deliberately NOT the
# literal 180/120, so a future retune with evidence does not have to fight a test.
_SYSTEMD_DEFAULT_START_SEC = 90


# ── Renderers: (label) -> {unit-name: rendered text} for a given runtime ──────


def _systemd_sh_units(tmp_path: Path, _mp: pytest.MonkeyPatch, runtime: str) -> dict[str, str]:
    """The shell installer's two container units, rendered for *runtime*."""
    root = tmp_path / f"sh-{runtime}"
    render_systemd(root, {"YADGAR_RUNTIME": runtime})
    out = root / "units"
    return {name: (out / name).read_text() for name in ("yadgar.service", "yadgar-backend.service")}


def _python_systemd_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> dict[str, str]:
    """``install_systemd_service``'s two units, rendered for *runtime*.

    HOME is redirected so nothing touches the real ``~/.config/systemd/user``;
    this is pure text rendering, no systemctl involved.
    """
    home = tmp_path / f"py-{runtime}"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", runtime)
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    result = systemd_mod.install_systemd_service(_prod_profile(8765), dev=False)
    return {
        "yadgar.service": Path(result["core_service"]).read_text(),
        "yadgar-backend.service": Path(result["backend_service"]).read_text(),
    }


def _launchd_units(tmp_path: Path, _mp: pytest.MonkeyPatch, runtime: str) -> dict[str, str]:
    """Every rendered launchd plist, as raw XML text.

    macOS has no sd_notify and launchd has no notify protocol, so there is
    nothing runtime-conditional for these to express — which is exactly why they
    are pinned here rather than left to inspection. Read as raw text (not
    ``plistlib``) so a podman-only construct hiding in a comment, a key, or an
    EnvironmentVariables value is still caught.
    """
    root = tmp_path / f"launchd-{runtime}"
    render_launchd(root, {"YADGAR_RUNTIME": runtime})
    return {p.name: p.read_text() for p in sorted((root / "units").glob("*.plist"))}


_RENDERERS = {
    "generate_systemd.sh": _systemd_sh_units,
    "install_systemd_service (Python)": _python_systemd_units,
    "generate_launchd.sh": _launchd_units,
}

# Only the systemd generators have a readiness contract to gate; launchd has no
# notify protocol, so it is checked for ABSENCE of podman-only constructs only.
_SYSTEMD_RENDERERS = ("generate_systemd.sh", "install_systemd_service (Python)")


# ── The invariant ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", sorted(_RENDERERS))
def test_docker_units_carry_no_podman_only_readiness_construct(label, tmp_path, monkeypatch):
    """THE INVARIANT: rendered-for-docker units contain nothing podman-exclusive.

    The runtime is pinned to *docker* on purpose. A podman-pinned run cannot
    distinguish a resolved value from a hardcoded podman literal — the task:0101
    lesson, restated in this suite's ``docker_env`` sibling fixture.
    """
    units = _RENDERERS[label](tmp_path, monkeypatch, "docker")
    assert units, f"{label}: rendered nothing for docker"

    offenders: list[str] = []
    for name, text in units.items():
        for pattern, why in _PODMAN_ONLY:
            for match in pattern.finditer(text):
                offenders.append(f"{name}: {match.group(0)!r} — {why}")

    assert offenders == [], (
        f"{label} emits podman-only readiness constructs into a DOCKER unit, so "
        f"the whole systemd install path is dead on a docker host:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("label", sorted(_SYSTEMD_RENDERERS))
def test_docker_units_gate_readiness_on_a_health_poll(label, tmp_path, monkeypatch):
    """Removing ``--sdnotify`` is only half a fix — the readiness gate must replace it.

    task:0104 rejected gating ``--sdnotify`` on podman alone precisely because a
    unit with no notify source is dead whether or not the flag is there. This is
    the assertion that makes that half-measure fail.
    """
    units = _RENDERERS[label](tmp_path, monkeypatch, "docker")

    for name, text in units.items():
        if "backend" in name and label == "generate_systemd.sh":
            # The shell installer's backend unit is Type=simple on BOTH runtimes
            # (pre-existing, identical either way) so it has no readiness
            # contract to gate. Stated in §6 of the plan, not silently skipped.
            continue
        assert _GATE_RE.search(text), (
            f"{label}/{name}: rendered for docker with no ExecStartPost= health "
            f"gate. Type=exec alone means 'the docker CLI was exec'd', so "
            f"After= ordering guarantees nothing and the unit reports active "
            f"long before the container serves.\n{text}"
        )
        assert re.search(r"^\s*Type\s*=\s*exec", text, re.MULTILINE), (
            f"{label}/{name}: docker unit is not Type=exec"
        )


@pytest.mark.parametrize("runtime", ("podman", "docker"))
@pytest.mark.parametrize("label", sorted(_SYSTEMD_RENDERERS))
def test_readiness_gated_units_declare_a_start_budget_above_their_gate(
    label, runtime, tmp_path, monkeypatch
):
    """task:0106 — a readiness-gated unit must set ``TimeoutStartSec`` explicitly.

    Scoped by ``Type=``, not by unit name: ``TimeoutStartSec`` only *binds* when
    the unit's start is gated on something the daemon has to reach. A
    ``Type=notify`` unit is not "started" until ``READY=1`` arrives, and a
    ``Type=exec`` unit here is not started until its ``ExecStartPost=`` gate
    returns — both can outlive systemd's 90s default. A ``Type=simple`` unit
    (``scripts/install/yadgar-backend.service.in``) is "started" the instant the
    fork succeeds, so the directive is genuinely irrelevant there and that unit
    is exempt by the ``Type`` filter rather than by name.

    Why 90s is structurally too tight on the podman backend, independent of any
    measurement: ``--sdnotify=healthy`` means ``READY=1`` is emitted on the first
    *healthy* healthcheck, the unit passes ``--health-start-period=60s``, and it
    pins no ``--health-interval`` so podman's 30s default applies. Health results
    are therefore quantised to 30s ticks starting after the 60s grace — a model
    load finishing at t=65s is not *observed* until t≈90s, exactly the default.
    ``flake.nix:366`` supplies the value (180) as the field-proven budget for the
    identical unit shape on the identical runtime, comment "covers cold model
    load"; the backend's ``/health`` returns 200 only when ``db_ok and
    engine_loaded`` (``embed_service.py``), so model load really is on the
    readiness path for both arms.

    What is asserted is the FLOOR and the RELATION, deliberately not the literal
    180/120 — pinning the values would make a future evidence-backed retune fight
    a test. The floor exists because the relation alone is vacuous on podman
    (no gate to compare against), so without it this guard would still pass on a
    unit regressed to ``TimeoutStartSec=90``: the very bug being fixed, re-opened
    silently. Both halves are mutation-proven, not assumed.

    The second half is the relation car 0105 sized by hand: a gate that can
    outlive ``TimeoutStartSec`` stops being the binding constraint — the unit
    dies on timeout and ``Restart=on-failure`` loops it instead of the gate
    failing cleanly. Vacuous on podman (its readiness is a signal, and
    ``test_podman_units_keep_their_sd_notify_readiness`` asserts no gate leaks
    there), asserted wherever a gate exists.
    """
    for name, text in _RENDERERS[label](tmp_path, monkeypatch, runtime).items():
        if not _READINESS_TYPE_RE.search(text):
            continue  # Type=simple — "started" at fork; no budget to blow.

        budget = _TIMEOUT_RE.search(text)
        assert budget, (
            f"{label}/{name} rendered for {runtime} is readiness-gated "
            f"({_READINESS_TYPE_RE.search(text).group(1)}) but sets no explicit "
            f"TimeoutStartSec, so it inherits systemd's 90s default. A cold model "
            f"load that overruns it fails the unit, and Restart=on-failure then "
            f"cycles it.\n{text}"
        )
        assert budget.group(1).isdigit(), (
            f"{label}/{name}: TimeoutStartSec={budget.group(1)!r} is not plain "
            f"seconds; this guard's gate comparison cannot interpret a systemd "
            f"time span. Express it in seconds or teach this test the unit suffix."
        )
        timeout = int(budget.group(1))
        assert timeout > _SYSTEMD_DEFAULT_START_SEC, (
            f"{label}/{name} rendered for {runtime}: TimeoutStartSec={timeout} is "
            f"at or below systemd's {_SYSTEMD_DEFAULT_START_SEC}s default, so "
            f"declaring it buys nothing. Without this floor the podman arm — which "
            f"has no gate, making the gate comparison below vacuous — could be "
            f"regressed back to the default while this guard still passed."
        )

        gate = _GATE_BUDGET_RE.search(text)
        if gate is None:
            continue
        spent = int(gate.group(1)) * int(gate.group(2))
        assert spent < timeout, (
            f"{label}/{name} rendered for {runtime}: the ExecStartPost health "
            f"gate can spend {spent}s (--retry {gate.group(1)} x --retry-delay "
            f"{gate.group(2)}s) but TimeoutStartSec={timeout}. The gate must stay "
            f"strictly inside the start budget, otherwise systemd kills the unit "
            f"before the gate can fail cleanly and the failure reads as a timeout."
        )


@pytest.mark.parametrize("label", sorted(_SYSTEMD_RENDERERS))
def test_podman_units_keep_their_sd_notify_readiness(label, tmp_path, monkeypatch):
    """The podman arm must NOT be collateral damage of the docker fix.

    Guards the direction the docker work could plausibly break: deleting
    ``Type=notify`` / ``--sdnotify=healthy`` outright instead of making them
    conditional. Both must survive on podman, and the docker-only gate must NOT
    leak onto the podman path (podman's proxy already blocks on health; a second
    poll would only add startup latency).
    """
    units = _RENDERERS[label](tmp_path, monkeypatch, "podman")

    core = units["yadgar.service"]
    assert re.search(r"^\s*Type\s*=\s*notify", core, re.MULTILINE), (
        f"{label}: podman core unit lost Type=notify — podman's default "
        f"--sdnotify=container mode forwards the daemon's own READY=1"
    )

    backend = units["yadgar-backend.service"]
    if label == "install_systemd_service (Python)":
        assert "--sdnotify=healthy" in backend, (
            f"{label}: podman backend unit lost --sdnotify=healthy"
        )
        assert re.search(r"^\s*Type\s*=\s*notify", backend, re.MULTILINE), (
            f"{label}: podman backend unit lost Type=notify"
        )

    for name, text in units.items():
        assert not _GATE_RE.search(text), (
            f"{label}/{name}: the docker-only ExecStartPost health gate leaked "
            f"onto the podman path, where the sd_notify proxy already blocks"
        )


# DELETED task:0110 Stage D — test_runtime_markers_are_matched_at_column_zero_only.
# It pinned that `generate_systemd.sh` anchored its `@PODMAN_ONLY@`/`@DOCKER_ONLY@`
# sed expressions to column 0, by mutating a copy of `yadgar.service.in` and
# re-rendering. ADR-0190 RETIRES that mechanism outright: the templates are gone
# and the runtime conditional is a Python branch (`units.readiness_for`), so the
# test had no marker, no sed and no template left to exercise. Retargeting was not
# possible — there is no column-0-anchoring property in a data model to assert.
# What it protected is now covered structurally: `readiness_for` returns the
# directives for ONE runtime, so a podman render cannot carry the docker gate
# (test_docker_only_gate_is_absent_on_podman above) and no marker text exists to
# leak (test_no_unrendered_runtime_marker_survives_into_a_unit below).


def test_no_unrendered_runtime_marker_survives_into_a_unit(tmp_path, monkeypatch):
    """A ``@PODMAN_ONLY@``/``@DOCKER_ONLY@`` marker left in a rendered unit is a dead unit.

    ``sed`` silently leaves an unknown ``@TOKEN@`` in place, so a template that
    grows a marker the generator does not know about renders a systemd directive
    beginning with ``@`` — which systemd rejects at load. Cheap to assert, and it
    fails at render time instead of at ``systemctl --user start``.
    """
    marker = re.compile(r"@[A-Z_]+@")
    for runtime in ("podman", "docker"):
        for label in sorted(_RENDERERS):
            for name, text in _RENDERERS[label](tmp_path, monkeypatch, runtime).items():
                leftovers = sorted(set(marker.findall(text)))
                assert leftovers == [], (
                    f"{label}/{name} rendered for {runtime} still contains "
                    f"unsubstituted template marker(s): {leftovers}"
                )
