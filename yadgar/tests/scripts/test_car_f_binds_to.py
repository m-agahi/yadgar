"""Car F (task #61) — systemd ``BindsTo=yadgar-backend.service`` lint.

The BindsTo relation lives in the rendered unit template, not in
runtime code. A unit test would have to start systemd, which is not
realistic in CI. The cheap, correct shape is a template-rendering
lint: build the unit from a spec, render to text, assert the directive
is present in the [Unit] section. Catches removal AND mis-spelling
AND drift to a different unit name.
"""

from __future__ import annotations

from yadgar.core.daemon.unit_model import render_unit
from yadgar.core.daemon.units import (
    UnitSpec,
    build_core_unit,
)


def _spec() -> UnitSpec:
    # Minimal spec — every default is fine for the lint; the only field
    # the test cares about is ``suffix`` (which drives the backend unit
    # name and therefore the BindsTo= value).
    return UnitSpec(
        runtime="podman",
        network="yadgar-net",
        secrets_env_file="/dev/null",
        upgrade_env_file="/dev/null",
        state_dir="/tmp",
        data_dir="/tmp",
        backend_container="yadgar-backend",
        backend_image="docker.io/openfantasy/yadgar-backend:5.73.0",
        backend_data_mount="/tmp",
        backend_embed_port=8001,
        backend_surreal_port=8000,
        backend_cpus="1",
        backend_memory="1g",
    )


def test_core_unit_binds_to_backend_service() -> None:
    """The rendered ``yadgar.service`` carries ``BindsTo=yadgar-backend.service``.

    The directive must appear in the [Unit] section, not [Service] —
    BindsTo is a Unit-section directive per ``man systemd.unit``. The
    test pins the value to the exact backend service name the spec
    generates (with suffix), so a future ``suffix`` rename also fires
    this test.
    """
    spec = _spec()
    unit = build_core_unit(spec)
    rendered = render_unit(unit)
    assert "BindsTo=yadgar-backend.service" in rendered, (
        f"yadgar.service must BindsTo=yadgar-backend.service; got:\n{rendered}"
    )
    # Belt-and-braces: the BindsTo= value must match the same backend
    # unit name the spec's After=/Wants= name (no drift between the
    # three relations on the same peer).
    backend_unit = spec.backend_unit_name
    assert f"BindsTo={backend_unit}" in rendered
    assert f"After=network.target {backend_unit}" in rendered
    assert f"Wants={backend_unit}" in rendered


def test_core_unit_binds_to_respects_suffix() -> None:
    """The dev arm uses ``-dev`` suffix on the unit names; the BindsTo= must follow.

    Carries the regression net for the ``dev=True`` path: the lint
    pins that a unit named ``yadgar-dev.service`` binds to
    ``yadgar-backend-dev.service``, not the prod name. The install
    surface relies on this so a dev-mode install can never bring up
    the prod backend by accident.
    """
    spec = _spec()
    # dataclass(frozen=True) — replace() builds a new spec with the
    # suffix set; the build_core_unit path consumes ``suffix`` to
    # generate the per-arm backend unit name.
    spec = UnitSpec(**{**spec.__dict__, "suffix": "-dev"})
    unit = build_core_unit(spec)
    rendered = render_unit(unit)
    assert "BindsTo=yadgar-backend-dev.service" in rendered, (
        f"suffixed install must bind to suffixed backend; got:\n{rendered}"
    )
    assert "BindsTo=yadgar-backend.service" not in rendered, (
        f"suffixed install must NOT bind to the unsuffixed prod name; got:\n{rendered}"
    )
