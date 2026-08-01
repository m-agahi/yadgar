"""Cross-generator regression: the core unit must WANT the backend, never REQUIRE it.

task:0111 / ADR-0188.  ``Requires=`` propagates STOP — ``systemctl --user stop
yadgar-backend`` takes ``yadgar`` down as a dependency, whether or not anyone
asked.  A vacuum has to stop the backend (it owns the surrealkv dir), so the
whole memory engine went down for ~68 s of every 136 s run and every connected
MCP client dropped its session.  ``Wants=`` keeps the pull-in and ``After=``
keeps boot ordering; only the stop propagation goes.

Two generators render this unit and they drifted before (the private nix module
was decoupled in v5.3.9; neither in-repo generator was), so the anti-recurrence
mechanism is a cross-generator test — same shape as
``test_vacuum_trigger_cross_generator.py`` and
``test_backend_unit_queue_base_cross_generator.py``.

Surfaces:

* ``scripts/install/yadgar.service.in`` via ``generate_systemd.sh`` — the
  generator that actually installs on real (non-nix) hosts.
* ``yadgar/core/daemon/systemd.py::install_systemd_service`` — the Python
  generator.

Deliberately NOT asserted: ``flake.nix`` (its ``systemd.user.services.yadgar``
declares no ``Requires``/``Wants``/``After`` at all, so there is nothing to
flip) and the launchd plists (launchd has no ``Requires=`` equivalent — the
backend plist says so in its own comment).  Asserting "every generator carries
Wants=" would fail on those two for the wrong reason.

Every arm is NON-VACUOUS by construction: it first asserts the rendered core
text mentions ``yadgar-backend`` at all, so a unit that lost the relationship
entirely cannot pass by having no ``Requires=`` line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yadgar.tests._unit_render import render_systemd


def _render_shell_core(tmp_path: Path) -> str:
    render_systemd(tmp_path)
    return (tmp_path / "units" / "yadgar.service").read_text()


def _render_python_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    from yadgar.core.daemon import systemd as systemd_mod
    from yadgar.core.daemon.profiles import _prod_profile

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    result = systemd_mod.install_systemd_service(_prod_profile(8765), dev=False)
    return Path(result["core_service"]).read_text()


_RENDERERS = {
    "generate_systemd.sh": lambda tmp, _mp: _render_shell_core(tmp),
    "install_systemd_service (Python)": _render_python_core,
}


@pytest.mark.parametrize("label", sorted(_RENDERERS))
def test_core_unit_wants_backend_and_never_requires_it(label, tmp_path, monkeypatch):
    core = _RENDERERS[label](tmp_path, monkeypatch)

    # Non-vacuity gate (the Car 0106 lesson): a core unit that dropped the
    # relationship entirely would satisfy every "no Requires=" assertion below.
    assert "yadgar-backend" in core, (
        f"{label}: the rendered core unit names no backend dependency at all — "
        f"this assertion set would be vacuous:\n{core}"
    )

    requires = [ln for ln in core.splitlines() if ln.startswith("Requires=")]
    assert not [ln for ln in requires if "yadgar-backend" in ln], (
        f"{label}: core Requires= the backend, so `systemctl --user stop "
        f"yadgar-backend` (what every vacuum does) stops the core too: {requires}"
    )

    wants = [ln for ln in core.splitlines() if ln.startswith("Wants=")]
    assert any("yadgar-backend" in ln for ln in wants), (
        f"{label}: core lost the backend pull-in — starting core must still "
        f"start the backend: {wants or core}"
    )

    after = [ln for ln in core.splitlines() if ln.startswith("After=")]
    assert any("yadgar-backend" in ln for ln in after), (
        f"{label}: core lost its backend START ordering. Wants= does not order; "
        f"only After= does, and flipping the dependency must not change start "
        f"ordering (ADR-0185 readiness shape): {after}"
    )


def test_both_generators_agree_on_the_dependency_shape(tmp_path, monkeypatch):
    """The two generators must not drift again — same directives, same values."""
    shell = _render_shell_core(tmp_path / "shell")
    python = _render_python_core(tmp_path / "python", monkeypatch)

    def _dep_lines(text: str) -> set[str]:
        return {
            ln.strip()
            for ln in text.splitlines()
            if ln.startswith(("Wants=", "Requires=", "BindsTo=")) and "yadgar-backend" in ln
        }

    assert _dep_lines(shell) == _dep_lines(python), (
        "generators disagree on the core→backend dependency:\n"
        f"  generate_systemd.sh: {sorted(_dep_lines(shell))}\n"
        f"  Python generator:    {sorted(_dep_lines(python))}"
    )
    assert _dep_lines(shell), "neither generator declares a core→backend dependency at all"
