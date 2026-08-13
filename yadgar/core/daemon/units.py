"""The unit BUILDERS — driven by an explicit input record (ADR-0190).

task:0110. ``yadgar/core/daemon/systemd.py`` used to hold two f-string unit
templates; ``scripts/install/*.in`` held nine ``sed`` templates for the same
job (deleted in Stage D). D3 of the plan says the converged renderer takes a
**mode, not a fork**:
one set of builders, and everything the two install surfaces genuinely disagree
about arrives as a field on :class:`UnitSpec`.

Stage A re-expressed the two EXISTING Python units in the model with no
behaviour change. Stage B moved them onto the ``.in`` templates' shape — the
union of both generators, so the profile arm gains everything §1.3 of the plan
listed as a regression the naive delegation would have shipped (the SurrealDB
loopback publish, the state-dir bind + ``YADGAR_VACUUM_TRIGGER_PATH``, the viz
port, ``ExecReload``, ``--stop-timeout``, ``--security-opt label=disable``,
``TimeoutStopSec`` and the trigger-dir ``ExecStartPre``). Stage C added the seven
units the Python side never had — they live in
``yadgar/core/daemon/maintenance_units.py`` and are composed in here by
:func:`build_units`, so all nine now come out of this module. ``flake.nix``
remains a separate, unconverged renderer — see :data:`ALL_UNIT_NAMES`.

Comments are emitted, not dropped: the parity baseline is the ``sed`` render of
the templates, and those carry ~60 comment lines per unit explaining WHY each
directive is shaped the way it is. They are held as module constants so the
builders stay inside the function-LOC cap.

Nothing here reads the environment, the filesystem or ``Path.home()``: a builder
that probed host state could not be pinned by a committed fixture. Every input
arrives on the spec, and the two spec factories are what resolve host state.
"""

from __future__ import annotations

from dataclasses import dataclass

from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.maintenance_units import (
    MAINTENANCE_UNIT_NAMES,
    HostExecs,
    build_maintenance_units,
)
from yadgar.core.daemon.unit_docs import (
    BACKEND_SERVICE_DOC,
    BACKEND_UNIT_DOC,
    CORE_DOCKER_HOST_DOC,
    CORE_GATE_DOC,
    CORE_MKDIR_DOC,
    CORE_READINESS_DOC,
    CORE_STATE_DOC,
    CORE_WANTS_DOC,
)
from yadgar.core.daemon.unit_model import Comment, Directive, Entry, Section, UnitFile, comments

__all__ = [
    "ALL_UNIT_NAMES",
    "SERVICE_UNIT_NAMES",
    "UnitSpec",
    "build_backend_unit",
    "build_core_unit",
    "build_units",
    "setup_unit_spec",
]

# The two units BOTH arms render. `daemon install-service` renders only these.
SERVICE_UNIT_NAMES = ("yadgar.service", "yadgar-backend.service")

# The unit set the `yadgar-setup` arm installs, DERIVED rather than spelled.
# task:0110 Stage D: `generate_systemd.sh` used to carry its own `UNITS` array
# (`:214`) and two test modules carried a third and fourth transcription of the
# same nine names. Those three are gone — they import this. `uninstall.sh:109`
# keeps a literal shell array on purpose (uninstall must work after the package
# is removed, so it cannot query the CLI); that mirror is pinned against this
# tuple by test_v5_169_maintenance_unit_parity.py.
#
# NOT a single source of truth for the repo: `flake.nix` builds its units
# declaratively at nix eval time and enumerates its own set — a DIFFERENT set,
# eight units with per-unit `Install.WantedBy` and no `yadgar.target` at all.
# Nothing here can derive that; the five *_cross_generator.py suites are what
# keep it honest.
ALL_UNIT_NAMES = SERVICE_UNIT_NAMES + MAINTENANCE_UNIT_NAMES


@dataclass(frozen=True)
class UnitSpec:
    """Every input the units need, resolved by the caller before rendering.

    The two arms differ only in the VALUES here — ``daemon install-service``
    keeps its named volume at the core's ``/data``, its separate queue volume and
    its host-adaptive memory (plan §9.5: a data-path move does not belong in a
    generator car), while the ``yadgar-setup`` arm carries the templates' host
    bind and fixed budgets.
    """

    runtime: str
    network: str
    secrets_env_file: str
    upgrade_env_file: str
    state_dir: str
    data_dir: str
    # backend
    backend_container: str
    backend_image: str
    backend_data_mount: str
    backend_embed_port: int
    backend_surreal_port: int
    backend_cpus: str
    backend_memory: str
    backend_memory_swap: str | None = None
    backend_queue_base: str = "/data"
    backend_queue_mount: str | None = None
    hf_cache_dir: str | None = None
    # core
    core_container: str = "yadgar"
    core_image: str = "${YADGAR_IMAGE_TAG}"
    core_data_mount: str = ""
    core_port: int = 8765
    core_viz_port: int = 42069
    core_cpus: str = "1"
    core_memory: str = "1g"
    core_memory_swap: str | None = None
    # shared
    stop_timeout: int | None = None
    suffix: str = ""
    # The HOST entry points the maintenance units exec (task:0110 Stage C).
    # None means "this arm installs no maintenance units": build_units then emits
    # only the two service units, because yadgar.target's second Wants= names
    # timers and a .path that would not exist. Resolved by
    # yadgar/core/daemon/unit_install.py:resolve_host_exec, which aborts rather
    # than baking a broken ExecStart into a unit that fails at 4am.
    execs: HostExecs | None = None
    # Both halves of the vacuum-trigger pair, or neither. The core container's
    # YADGAR_VACUUM_TRIGGER_PATH write is only observable if this install also
    # ships yadgar-vacuum-trigger.path to watch the projected host dir; setting
    # the env var without a watcher is the silent no-op
    # yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py exists to
    # prevent (vacuum_now() refuses rather than writing into a void).
    vacuum_trigger: bool = False

    @property
    def backend_unit_name(self) -> str:
        return f"yadgar-backend{self.suffix}.service"

    @property
    def core_unit_name(self) -> str:
        return f"yadgar{self.suffix}.service"


@observe(tier="hot")
def setup_unit_spec(
    *,
    runtime: str,
    data_dir: str,
    state_dir: str,
    secrets_env_file: str,
    backend_image: str,
    execs: HostExecs,
    surreal_port: int = 8000,
    hf_cache_dir: str | None = None,
) -> UnitSpec:
    """The ``yadgar-setup`` arm's input record — the templates' shape, as values.

    This is D3's "mode, not a fork": the same builders run, and everything the
    documented installer does differently from ``daemon install-service`` lives
    here as a field. The fixed budgets (``--memory 1g --cpus 1`` core, ``4g``/2
    backend) are the templates' literals; the core's ``/data`` is the HOST bind
    and its image comes from ``${YADGAR_IMAGE_TAG}`` in ``upgrade.env``, which is
    the rewrite target the upgrade orchestrator owns.

    ``backend_queue_base`` stays ``/data``: the shell path has always kept the
    queue beside the DB, and moving it to a separate volume would orphan queued
    writes on upgrade. A data move does not belong in a generator car (plan §9.5).

    *execs* is REQUIRED here and optional on :class:`UnitSpec`: this arm renders
    all nine units, so both host entry points must already be resolved
    (fail-loud) before a spec exists, while the ``daemon install-service`` arm
    renders only the two service units and has no host CLI to resolve.
    """
    return UnitSpec(
        runtime=runtime,
        network="yadgar-net",
        secrets_env_file=secrets_env_file,
        upgrade_env_file="%h/.local/state/yadgar/upgrade.env",
        state_dir=state_dir,
        data_dir=data_dir,
        backend_container="yadgar-backend",
        backend_image=backend_image,
        backend_data_mount=data_dir,
        backend_embed_port=8001,
        backend_surreal_port=surreal_port,
        backend_cpus="2",
        backend_memory="4g",
        backend_queue_base="/data",
        hf_cache_dir=hf_cache_dir,
        core_container="yadgar",
        core_image="${YADGAR_IMAGE_TAG}",
        core_data_mount=data_dir,
        core_port=8765,
        core_cpus="1",
        core_memory="1g",
        stop_timeout=30,
        vacuum_trigger=True,
        execs=execs,
    )


def _gate(url: str, retries: int) -> str:
    """An ``ExecStartPost=`` value that blocks until *url* answers 200 (task:0105).

    ``curl`` is written bare and resolved from the unit's ``$PATH``, the same
    convention the templates use for their ``mkdir -p`` ExecStartPre. No shell
    wrapper, so systemd's own ``$`` expansion never applies; curl's ``--retry``
    does the polling. ``--retry-connrefused`` covers "port not listening yet",
    ``--retry-all-errors`` covers "listening but not healthy yet" (``--fail``
    turns a 5xx into an error, which plain ``--retry`` would not retry).
    """
    return (
        f"curl --fail --silent --show-error --output /dev/null "
        f"--retry {retries} --retry-delay 2 --retry-connrefused "
        f"--retry-all-errors {url}"
    )


@dataclass(frozen=True)
class Readiness:
    """The runtime-conditional readiness shape for one unit (task:0105/0106).

    Podman proxies sd_notify, so its units are ``Type=notify``. Docker has no
    sd_notify proxy at all — nothing would ever send ``READY=1`` there — so it
    gets ``Type=exec`` plus a bounded ``ExecStartPost=`` ``/health`` poll, which
    per ``man systemd.service`` is "taken into account for the purpose of
    Before=/After= ordering constraints".

    ``type_directives`` and ``budget`` are separate because the two units place
    them differently: the core template carries ``Type=`` at the top of
    ``[Service]`` and ``TimeoutStartSec=`` at the bottom. Pre-rendered blobs (the
    shape ``_readiness_directives`` used) cannot express that at all.
    """

    type_directives: tuple[Directive, ...]
    budget: int
    sdnotify: str = ""
    gate: str | None = None


@observe(tier="hot")
def readiness_for(runtime: str, *, url: str, retries: int, budget: int) -> Readiness:
    """Readiness for one unit on *runtime*, with a *budget*-second start timeout.

    A non-zero ``ExecStartPost`` FAILS the unit and ``Restart=on-failure`` would
    then loop it, so each gate is sized strictly inside its own budget: backend
    75 x 2s = 150s inside 180, core 45 x 2s = 90s inside 120 (task:0105). The
    podman arm carries the same budgets (task:0106) because ``--sdnotify=healthy``
    reports on 30s health ticks after a 60s grace, so systemd's 90s default is
    structurally too tight there.

    Compared on the BASENAME because the runtime may legitimately arrive as a
    path (``YADGAR_CONTAINER_RUNTIME`` and ``flake.nix``'s ``runtime`` option
    both allow one).
    """
    if runtime.rsplit("/", 1)[-1] == "podman":
        return Readiness(
            type_directives=(Directive("Type", "notify"), Directive("NotifyAccess", "all")),
            budget=budget,
            sdnotify="    --sdnotify=healthy \\\n",
        )
    return Readiness(
        type_directives=(Directive("Type", "exec"),), budget=budget, gate=_gate(url, retries)
    )


@observe(tier="hot")
def _resources(memory: str, cpus: str, swap: str | None, stop_timeout: int | None) -> str:
    """The single flags line the templates put immediately before the image ref."""
    line = f"    --memory {memory} --cpus {cpus}"
    if swap:
        line += f" --memory-swap {swap}"
    if stop_timeout is not None:
        line += f" --stop-timeout {stop_timeout}"
    return f"{line} \\\n"


# ── Backend unit ─────────────────────────────────────────────────────────────


def _backend_exec_start(spec: UnitSpec, ready: Readiness) -> str:
    """The backend's multi-line ``ExecStart=`` value."""
    queue = (
        f"    -v {spec.backend_queue_mount}:{spec.backend_queue_base} \\\n"
        if spec.backend_queue_mount
        else ""
    )
    hf = (
        f"    -v {spec.hf_cache_dir}:/home/yadgar/.cache/huggingface \\\n"
        if spec.hf_cache_dir
        else ""
    )
    return (
        f"{spec.runtime} run --name {spec.backend_container} --rm "
        f"--security-opt label=disable --user root \\\n"
        f"    --network {spec.network} \\\n"
        f"{ready.sdnotify}"
        f'    --health-cmd "curl -f http://localhost:8001/health || exit 1" \\\n'
        f"    --health-start-period=60s \\\n"
        f"    -p 127.0.0.1:{spec.backend_surreal_port}:8000 \\\n"
        f"    -p 127.0.0.1:{spec.backend_embed_port}:8001 \\\n"
        f"    -v {spec.backend_data_mount}:/data \\\n"
        f"{queue}{hf}"
        f"    -e SURREAL_USER=${{SURREAL_USER}} \\\n"
        f"    -e SURREAL_PASS=${{SURREAL_PASS}} \\\n"
        f"    -e YADGAR_RW_USER=${{YADGAR_RW_USER}} \\\n"
        f"    -e YADGAR_RW_PASS=${{YADGAR_RW_PASS}} \\\n"
        f"    -e YADGAR_RO_USER=${{YADGAR_RO_USER}} \\\n"
        f"    -e YADGAR_RO_PASS=${{YADGAR_RO_PASS}} \\\n"
        f"    -e YADGAR_QUEUE_BASE={spec.backend_queue_base} \\\n"
        f"    -e YADGAR_MCP_AUTH_TOKEN=${{YADGAR_MCP_AUTH_TOKEN}} \\\n"
        f"{_resources(spec.backend_memory, spec.backend_cpus, spec.backend_memory_swap, spec.stop_timeout)}"
        f"    {spec.backend_image}"
    )


@observe(tier="hot")
def build_backend_unit(spec: UnitSpec) -> UnitFile:
    """``yadgar-backend.service`` — SurrealDB + the embedding service."""
    ready = readiness_for(
        spec.runtime,
        url=f"http://127.0.0.1:{spec.backend_embed_port}/health",
        retries=75,
        budget=180,
    )
    service: list[Entry] = [*comments(BACKEND_SERVICE_DOC)]
    if ready.sdnotify:  # podman only — see BACKEND_SERVICE_DOC
        service.append(Directive("Environment", "DOCKER_HOST=unix:///run/podman/podman.sock"))
    service += [
        Directive("EnvironmentFile", spec.secrets_env_file),
        Directive("ExecStartPre", f"-{spec.runtime} stop {spec.backend_container}"),
        Directive("ExecStartPre", f"-{spec.runtime} rm {spec.backend_container}"),
        Directive("ExecStartPre", f"-{spec.runtime} network create {spec.network}"),
        Directive("ExecStart", _backend_exec_start(spec, ready)),
    ]
    if ready.gate:
        service.append(Directive("ExecStartPost", ready.gate))
    service += [
        Directive("ExecStop", f"{spec.runtime} stop {spec.backend_container}"),
        Directive("Restart", "on-failure"),
        Directive("RestartSec", "5"),
        *ready.type_directives,
        Directive("TimeoutStartSec", str(ready.budget)),
    ]
    return UnitFile(
        name=spec.backend_unit_name,
        sections=(
            Section(
                "Unit",
                (
                    Directive("Description", "Yadgar Backend (SurrealDB + Embeddings)"),
                    Directive("After", "network.target"),
                    *comments(BACKEND_UNIT_DOC),
                ),
            ),
            Section("Service", tuple(service)),
            Section("Install", (Directive("WantedBy", "default.target"),)),
        ),
    )


# ── Core unit ────────────────────────────────────────────────────────────────


@observe(tier="hot")
def _core_exec_start(spec: UnitSpec) -> str:
    """The core's multi-line ``ExecStart=`` value."""
    # Both halves or neither — see UnitSpec.vacuum_trigger.
    state_bind = f"    -v {spec.state_dir}:/root/.local/state/yadgar \\\n"
    trigger_env = (
        "    -e YADGAR_VACUUM_TRIGGER_PATH=/root/.local/state/yadgar/triggers/vacuum_requested \\\n"
    )
    if not spec.vacuum_trigger:
        state_bind = trigger_env = ""
    return (
        f"{spec.runtime} run --name {spec.core_container} --rm "
        f"--security-opt label=disable --user root \\\n"
        f"    --network {spec.network} \\\n"
        f"    -p 127.0.0.1:{spec.core_port}:8765 \\\n"
        f"    -p 127.0.0.1:{spec.core_viz_port}:42069 \\\n"
        f"    -v {spec.core_data_mount}:/data \\\n"
        f"{state_bind}"
        f"    -e YADGAR_DB_URL=http://{spec.backend_container}:8000 \\\n"
        f"{trigger_env}"
        f"    -e YADGAR_EMBED_URL=http://{spec.backend_container}:8001 \\\n"
        f"    -e YADGAR_HOST=0.0.0.0 \\\n"
        f"    -e YADGAR_PORT=8765 \\\n"
        f"    -e YADGAR_DATA_DIR=/data \\\n"
        f"    -e YADGAR_DB_USER=${{YADGAR_RW_USER}} \\\n"
        f"    -e YADGAR_DB_PASS=${{YADGAR_RW_PASS}} \\\n"
        f"    -e YADGAR_RW_USER=${{YADGAR_RW_USER}} \\\n"
        f"    -e YADGAR_RW_PASS=${{YADGAR_RW_PASS}} \\\n"
        f"    -e YADGAR_MCP_AUTH_TOKEN=${{YADGAR_MCP_AUTH_TOKEN}} \\\n"
        f"{_resources(spec.core_memory, spec.core_cpus, spec.core_memory_swap, spec.stop_timeout)}"
        f"    {spec.core_image}"
    )


@observe(tier="hot")
def _core_service_entries(spec: UnitSpec, ready: Readiness) -> list[Entry]:
    """``[Service]`` for the core, in the templates' order."""
    entries: list[Entry] = [
        *comments(CORE_READINESS_DOC),
        *ready.type_directives,
        *comments(CORE_DOCKER_HOST_DOC),
    ]
    if ready.sdnotify:  # podman only
        entries.append(Directive("Environment", "DOCKER_HOST=unix:///run/podman/podman.sock"))
    entries += [
        Directive("EnvironmentFile", spec.secrets_env_file),
        Comment(
            "upgrade.env carries YADGAR_IMAGE_TAG; leading '-' makes missing file "
            "non-fatal (first install)."
        ),
        Directive("EnvironmentFile", f"-{spec.upgrade_env_file}"),
        Directive("ExecStartPre", f"-{spec.runtime} stop {spec.core_container}"),
        Directive("ExecStartPre", f"-{spec.runtime} rm {spec.core_container}"),
    ]
    if spec.vacuum_trigger:
        entries += [
            *comments(CORE_MKDIR_DOC, runtime=spec.runtime),
            Directive("ExecStartPre", f"mkdir -p {spec.state_dir}/triggers"),
        ]
    entries.append(Directive("ExecStart", _core_exec_start(spec)))
    if ready.gate:
        # The ten-line gate rationale is emitted ONLY where the gate is. On podman
        # the sed render left it behind as an orphan (plan §1.4); it documents a
        # directive that arm does not have.
        entries += [*comments(CORE_GATE_DOC, runtime=spec.runtime)]
        entries.append(Directive("ExecStartPost", ready.gate))
    entries += [
        Directive("ExecStop", f"{spec.runtime} stop {spec.core_container}"),
        Comment(
            "ExecReload: reserved for future config-only reload; no-op until orchestrator wires it."
        ),
        Directive("ExecReload", "/bin/true"),
        Directive("Restart", "on-failure"),
        Directive("RestartSec", "5"),
        Directive("TimeoutStartSec", str(ready.budget)),
        Directive("TimeoutStopSec", "45"),
    ]
    return entries


@observe(tier="hot")
def build_core_unit(spec: UnitSpec) -> UnitFile:
    """``yadgar.service`` — the MCP core server."""
    ready = readiness_for(
        spec.runtime, url=f"http://127.0.0.1:{spec.core_port}/health", retries=45, budget=120
    )
    backend = f"yadgar-backend{spec.suffix}.service"
    trigger = spec.vacuum_trigger
    return UnitFile(
        name=spec.core_unit_name,
        sections=(
            Section(
                "Unit",
                (
                    Directive("Description", "Yadgar Memory Engine / MCP Server"),
                    Directive("After", f"network.target {backend}"),
                    *comments(CORE_WANTS_DOC),
                    Directive("Wants", backend),
                    *(comments(CORE_STATE_DOC, state_dir=spec.state_dir) if trigger else ()),
                ),
            ),
            Section("Service", tuple(_core_service_entries(spec, ready))),
            Section("Install", (Directive("WantedBy", "default.target"),)),
        ),
    )


@observe(tier="boundary")
def build_units(spec: UnitSpec) -> dict[str, UnitFile]:
    """Every unit *spec* asks for, keyed by filename.

    The two service units always. The seven greenfield units only when the spec
    carries :class:`~yadgar.core.daemon.maintenance_units.HostExecs` — the
    ``daemon install-service`` arm resolves no host CLI and installs no timers,
    and emitting ``yadgar.target`` there would name four units that arm never
    writes.

    ``spec.state_dir`` reaches both the core unit's ``-v`` bind source and the
    ``.path`` unit's ``PathChanged=``. One renderer makes that a shared input
    rather than the exact-string cross-generator comparison it used to be
    (plan §4.2).
    """
    units = [build_backend_unit(spec), build_core_unit(spec)]
    out = {u.name: u for u in units}
    if spec.execs is not None:
        out |= build_maintenance_units(
            state_dir=spec.state_dir,
            data_dir=spec.data_dir,
            secrets_env_file=spec.secrets_env_file,
            surreal_port=spec.backend_surreal_port,
            execs=spec.execs,
        )
    return out
