"""systemd user-unit generation for the Yadgar daemon.

Split out of ``daemon.py`` (Car C3, module-standardization train). Writes the two
systemd user service units (``yadgar-backend.service`` + ``yadgar.service``)
under ``~/.config/systemd/user``. ``YadgarDaemon.install_systemd_service`` is a
thin wrapper that resolves the profile then delegates here.

task:0110 Stage A (ADR-0190) moved the unit TEXT out of this module: the units
are now built as data by ``yadgar/core/daemon/units.py`` and rendered by
``yadgar/core/daemon/unit_model.py``. What remains here is the arm-specific
half — resolving host state (runtime, RAM, XDG paths, backend version, the
HuggingFace cache) into a :class:`~yadgar.core.daemon.units.UnitSpec`. The
builders are pure so a committed fixture can pin them
(``yadgar/tests/core/test_install_systemd_service_characterization.py``); this
function is where every probe lives.

Readiness stays runtime-conditional (task:0105/0106) and now lives on
``units.readiness_for``: podman proxies sd_notify so its units are
``Type=notify``; docker has no sd_notify proxy at all, so nothing would ever send
``READY=1`` there and it gets ``Type=exec`` plus a bounded ``ExecStartPost=``
poll of ``/health`` — which per ``man systemd.service`` is "taken into account
for the purpose of Before=/After= ordering constraints", the same ordering
guarantee notify buys. The gate polls ``/health`` (readiness) and not the image
HEALTHCHECK's ``/health/live`` (liveness, ADR-0019) because podman's core
readiness is the in-container ``sd_notify.ready()`` emitted LAST, after the full
engine set; ``/health/live`` goes green as soon as the HTTP server binds.

Design + rejected alternatives:
``docs/plans/archive/runtime-agnostic-systemd-readiness-2026-08-01.md`` and
``docs/plans/archive/0110-converge-systemd-generators-2026-08-01.md``.
Cross-generator guards: ``yadgar/tests/scripts/test_runtime_readiness_cross_generator.py``
and ``yadgar/tests/scripts/test_systemd_generator_convergence.py``.
"""

import os
from pathlib import Path

from yadgar import __version__
from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.profiles import ContainerProfile
from yadgar.core.daemon.runtime import (
    _BACKEND_CONTAINER,
    _NETWORK_NAME,
    DEFAULT_BACKEND_EMBED_PORT,
    DOCKERHUB_BACKEND_IMAGE,
    _backend_version,
    _container_memory_mb,
    _get_runtime,
)
from yadgar.core.daemon.unit_install import write_units
from yadgar.core.daemon.unit_model import render_unit
from yadgar.core.daemon.units import UnitSpec, build_units


@observe(tier="hot")
def profile_unit_spec(profile: ContainerProfile, dev: bool = False) -> UnitSpec:
    """Resolve host state into the ``daemon install-service`` arm's input record.

    Every filesystem / env / host-RAM probe the units depend on happens HERE, so
    the builders in ``units.py`` stay pure and a committed fixture can pin them.

    task:0104: the units name the runtime that is actually installed. A literal
    ``docker`` here made every generated unit dead on a podman-only host — the
    third instance of the class after task:0083 (``daemon start``) and task:0101
    (``upgrade``). Guarded by the unit-directive detector in
    ``yadgar/tests/core/test_daemon_runtime_binary.py``.

    Bug 5 — the backend image track is independent of the core (pip) version, so
    the tag comes from ``_backend_version()`` (server.json), replacing any tag an
    env override carried. Bugs 6/7/11 — XDG paths, not ``/etc`` or ``/root``.
    Plan §9.5: this arm keeps its named volume at the core's ``/data`` and its
    host-adaptive memory; a data-path move does not belong in a generator car.
    """
    from yadgar._shared import paths as _paths  # noqa: PLC0415

    backend_image = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)
    _base = (
        backend_image.rsplit(":", 1)[0] if ":" in backend_image.split("/")[-1] else backend_image
    )
    hf_cache = Path.home() / ".cache" / "huggingface"
    memory = f"{_container_memory_mb()}m"
    return UnitSpec(
        runtime=_get_runtime(),
        network=_NETWORK_NAME,
        secrets_env_file=str(_paths.SECRETS_ENV_PATH),
        upgrade_env_file=str(_paths.STATE_DIR / "upgrade.env"),
        state_dir=str(_paths.STATE_DIR),
        data_dir=str(_paths.DATA_DIR),
        backend_container=os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER),
        backend_image=f"{_base}:{_backend_version()}",
        backend_data_mount=str(_paths.DATA_DIR),
        backend_embed_port=DEFAULT_BACKEND_EMBED_PORT,
        # Same knob the shell generator has always honoured (generate_systemd.sh:40-44):
        # :8000 is commonly occupied by a dev server, and this arm publishes SurrealDB
        # for the first time in task:0110 — without the override it would be a NEW
        # start failure on exactly those hosts.
        backend_surreal_port=int(os.environ.get("YADGAR_BACKEND_SURREAL_PORT", "8000")),
        backend_cpus="1.0",
        backend_memory="4g",
        backend_memory_swap="4g",
        backend_queue_base="/queue-data",
        backend_queue_mount=profile.volume_name,
        hf_cache_dir=str(hf_cache) if hf_cache.exists() else None,
        core_container=profile.container_name,
        core_image=profile.image_name,
        core_data_mount=profile.volume_name,
        core_port=profile.port,
        core_cpus=str(profile.cpus),
        core_memory=memory,
        core_memory_swap=memory,
        # §1.3 regression item: without it the container SIGKILL window is the
        # runtime default, not the 30s the templates have always given the queue
        # flush. Unconditional — it is a fix, not an arm-specific value.
        stop_timeout=30,
        suffix="-dev" if dev else "",
    )


@observe(tier="boundary")
def install_systemd_service(profile: ContainerProfile, dev: bool = False) -> dict:
    """Write two systemd user service units: yadgar-backend.service and yadgar.service."""

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    spec = profile_unit_spec(profile, dev=dev)
    units = build_units(spec)

    # Bug 9: renamed yadgar-db → yadgar-backend
    backend_service_name = spec.backend_unit_name
    core_service_name = spec.core_unit_name

    # Same stamped, staged, validated write path the yadgar-setup arm uses
    # (task:0110 Stage D). Both arms therefore leave units that say which schema
    # and which yadgar version produced them, and neither can leave a half-written
    # set behind.
    write_units({name: render_unit(unit) for name, unit in units.items()}, service_dir, __version__)
    backend_path = service_dir / backend_service_name
    core_path = service_dir / core_service_name

    return {
        "backend_service": str(backend_path),
        "core_service": str(core_path),
        "enable": f"systemctl --user enable {backend_service_name} {core_service_name}",
        "start": f"systemctl --user start {backend_service_name} && systemctl --user start {core_service_name}",
        "status": f"systemctl --user status {backend_service_name} {core_service_name}",
    }
