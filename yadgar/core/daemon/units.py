"""The unit BUILDERS — one renderer, driven by an explicit input record (ADR-0190).

task:0110 Stage A. ``yadgar/core/daemon/systemd.py`` used to hold two f-string
unit templates; ``scripts/install/*.in`` holds nine ``sed`` templates for the
same job. D3 of the plan says the converged renderer takes a **mode, not a
fork**: one set of builders, and everything the two install surfaces genuinely
disagree about arrives as a field on :class:`UnitSpec`.

Stage A re-expresses the two EXISTING Python units in the model with no
behaviour change — the characterization fixtures under
``yadgar/tests/core/snapshots/install_systemd_service/`` pin that byte-for-byte.
Stage B moves those builders onto the ``.in`` templates' shape; Stage C adds the
seven units the Python side never had.

Nothing here reads the environment, the filesystem or ``Path.home()``: a builder
that probed host state could not be pinned by a committed fixture. Every input
arrives on the spec, and the two callers are what resolve host state.
"""

from __future__ import annotations

from dataclasses import dataclass

from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.unit_model import Directive, Section, UnitFile

__all__ = [
    "UnitSpec",
    "build_backend_unit",
    "build_core_unit",
    "build_units",
    "setup_unit_spec",
]


@dataclass(frozen=True)
class UnitSpec:
    """Every input the nine units need, resolved by the caller before rendering.

    The two arms differ only in the VALUES here — ``daemon install-service``
    keeps its named volume and host-adaptive memory (plan §9.5: a data-path move
    does not belong in a generator car), while the ``yadgar-setup`` arm carries
    the templates' host bind and fixed budgets.
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
    hf_cache_mount: str | None = None
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
    surreal_port: int = 8000,
    hf_cache_mount: str | None = None,
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
        hf_cache_mount=hf_cache_mount,
        core_container="yadgar",
        core_image="${YADGAR_IMAGE_TAG}",
        core_data_mount=data_dir,
        core_port=8765,
        core_cpus="1",
        core_memory="1g",
        stop_timeout=30,
    )


def _gate(url: str, retries: int) -> str:
    """An ``ExecStartPost=`` value that blocks until *url* answers 200 (task:0105).

    ``curl`` is written bare and resolved from the unit's ``$PATH``, the same
    convention ``scripts/install/yadgar.service.in`` uses for its ``mkdir -p``
    ExecStartPre. No shell wrapper, so systemd's own ``$`` expansion never
    applies; curl's ``--retry`` does the polling. ``--retry-connrefused`` covers
    "port not listening yet", ``--retry-all-errors`` covers "listening but not
    healthy yet" (``--fail`` turns a 5xx into an error, which plain ``--retry``
    would not retry).
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

    Replaces the pre-rendered string blobs ``_readiness_directives`` returned:
    the model needs one ``Directive`` per line, not a ``"Type=…\\nNotify…\\n"``
    chunk, or duplicate-key and ordering guarantees cannot be checked at all.
    """

    directives: tuple[Directive, ...]
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
            directives=(
                Directive("Type", "notify"),
                Directive("NotifyAccess", "all"),
                Directive("TimeoutStartSec", str(budget)),
            ),
            sdnotify="    --sdnotify=healthy \\\n",
        )
    return Readiness(
        directives=(Directive("Type", "exec"), Directive("TimeoutStartSec", str(budget))),
        gate=_gate(url, retries),
    )


def _backend_exec_start(spec: UnitSpec, sdnotify: str) -> str:
    """The backend's multi-line ``ExecStart=`` value."""
    swap = f"    --memory-swap {spec.backend_memory_swap} \\\n" if spec.backend_memory_swap else ""
    queue = f"    -v {spec.backend_queue_mount}:{spec.backend_queue_base} \\\n"
    return (
        f"{spec.runtime} run --rm \\\n"
        f"    --name {spec.backend_container} \\\n"
        f"    --network {spec.network} \\\n"
        f"    --cpus {spec.backend_cpus} \\\n"
        f"    --memory {spec.backend_memory} \\\n"
        f"{swap}"
        f"    --user root \\\n"
        f"{sdnotify}"
        f'    --health-cmd "curl -f http://localhost:8001/health || exit 1" \\\n'
        f"    --health-start-period=60s \\\n"
        f"    -v {spec.backend_data_mount}:/data \\\n"
        f"{queue if spec.backend_queue_mount else ''}"
        f"    -p {spec.backend_embed_port}:8001 \\\n"
        f"    -e SURREAL_USER=${{SURREAL_USER}} \\\n"
        f"    -e SURREAL_PASS=${{SURREAL_PASS}} \\\n"
        f"    -e YADGAR_RW_USER=${{YADGAR_RW_USER:-}} \\\n"
        f"    -e YADGAR_RW_PASS=${{YADGAR_RW_PASS:-}} \\\n"
        f"    -e YADGAR_RO_USER=${{YADGAR_RO_USER:-}} \\\n"
        f"    -e YADGAR_RO_PASS=${{YADGAR_RO_PASS:-}} \\\n"
        f"    -e YADGAR_QUEUE_BASE={spec.backend_queue_base} \\\n"
        f"    -e YADGAR_MCP_AUTH_TOKEN=${{YADGAR_MCP_AUTH_TOKEN}} \\\n"
        f"{spec.hf_cache_mount or ''}"
        f"    {spec.backend_image}"
    )


@observe(tier="hot")
def build_backend_unit(spec: UnitSpec) -> UnitFile:
    """``yadgar-backend.service`` — SurrealDB + the embedding service.

    ADR-0180: the ExecStart MUST forward ``YADGAR_MCP_AUTH_TOKEN`` into the
    CONTAINER. The backend serves ``/admin/*`` and compares the presented bearer
    against its own copy (``_require_admin_token``); ``EnvironmentFile`` puts it
    in the UNIT's env only, which the container never sees without an ``-e``.
    """
    ready = readiness_for(
        spec.runtime,
        url=f"http://127.0.0.1:{spec.backend_embed_port}/health",
        retries=75,
        budget=180,
    )
    service: list[object] = [
        *ready.directives,
        Directive("EnvironmentFile", spec.secrets_env_file),
        Directive("ExecStartPre", f"-{spec.runtime} network create --driver bridge {spec.network}"),
        Directive("ExecStartPre", f"-{spec.runtime} stop {spec.backend_container}"),
        Directive("ExecStartPre", f"-{spec.runtime} rm {spec.backend_container}"),
        Directive("ExecStart", _backend_exec_start(spec, ready.sdnotify)),
    ]
    if ready.gate:
        service.append(Directive("ExecStartPost", ready.gate))
    service += [
        Directive("ExecStop", f"{spec.runtime} stop {spec.backend_container}"),
        Directive("Restart", "on-failure"),
        Directive("RestartSec", "10"),
    ]
    return UnitFile(
        name=spec.backend_unit_name,
        sections=(
            Section(
                "Unit",
                (
                    Directive("Description", "Yadgar Backend — SurrealDB and Embedding Service"),
                    Directive("After", "network.target"),
                ),
            ),
            Section("Service", tuple(service)),  # type: ignore[arg-type]
            Section("Install", (Directive("WantedBy", "default.target"),)),
        ),
    )


def _core_exec_start(spec: UnitSpec) -> str:
    """The core's multi-line ``ExecStart=`` value."""
    swap = f"    --memory-swap {spec.core_memory_swap} \\\n" if spec.core_memory_swap else ""
    return (
        f"{spec.runtime} run --rm \\\n"
        f"    --name {spec.core_container} \\\n"
        f"    --network {spec.network} \\\n"
        f"    --cpus {spec.core_cpus} \\\n"
        f"    --memory {spec.core_memory} \\\n"
        f"{swap}"
        f"    --user root \\\n"
        f"    -v {spec.core_data_mount}:/data \\\n"
        f"    -p {spec.core_port}:8765 \\\n"
        f"    -e YADGAR_DB_URL=http://{spec.backend_container}:8000 \\\n"
        f"    -e YADGAR_EMBED_URL=http://{spec.backend_container}:8001 \\\n"
        f"    -e YADGAR_DATA_DIR=/data \\\n"
        f"    -e YADGAR_MCP_AUTH_TOKEN=${{YADGAR_MCP_AUTH_TOKEN}} \\\n"
        f"    -e YADGAR_DB_USER=${{YADGAR_RW_USER:-${{YADGAR_DB_USER:-${{SURREAL_USER}}}}}} \\\n"
        f"    -e YADGAR_DB_PASS=${{YADGAR_RW_PASS:-${{YADGAR_DB_PASS:-${{SURREAL_PASS}}}}}} \\\n"
        f"    {spec.core_image}"
    )


@observe(tier="hot")
def build_core_unit(spec: UnitSpec) -> UnitFile:
    """``yadgar.service`` — the MCP core server.

    ``Wants=``, NOT ``Requires=`` (task:0111 / ADR-0188): ``Requires=``
    propagates STOP, so the backend stop every vacuum performs would take the
    core down with it. ``After=`` still supplies the start ordering.
    """
    ready = readiness_for(
        spec.runtime,
        url=f"http://127.0.0.1:{spec.core_port}/health",
        retries=45,
        budget=120,
    )
    backend = f"yadgar-backend{spec.suffix}.service"
    service: list[object] = [
        *ready.directives,
        Directive("EnvironmentFile", spec.secrets_env_file),
        Directive("EnvironmentFile", f"-{spec.upgrade_env_file}"),
        Directive("ExecStartPre", f"-{spec.runtime} stop {spec.core_container}"),
        Directive("ExecStartPre", f"-{spec.runtime} rm {spec.core_container}"),
        Directive("ExecStart", _core_exec_start(spec)),
    ]
    if ready.gate:
        service.append(Directive("ExecStartPost", ready.gate))
    service += [
        Directive("ExecStop", f"{spec.runtime} stop {spec.core_container}"),
        Directive("Restart", "on-failure"),
        Directive("RestartSec", "5"),
    ]
    return UnitFile(
        name=spec.core_unit_name,
        sections=(
            Section(
                "Unit",
                (
                    Directive("Description", "Yadgar Memory Engine — MCP Core Server"),
                    Directive("Wants", backend),
                    Directive("After", f"network.target {backend}"),
                ),
            ),
            Section("Service", tuple(service)),  # type: ignore[arg-type]
            Section("Install", (Directive("WantedBy", "default.target"),)),
        ),
    )


@observe(tier="boundary")
def build_units(spec: UnitSpec) -> dict[str, UnitFile]:
    """Every unit the Python renderer can currently emit, keyed by filename.

    Stage A/B: the two service units. Stage C adds ``yadgar.target``, the vacuum
    trio and the nightly pair — the parity harness's unit-set assertion is the
    ratchet that makes that addition observable.
    """
    return {u.name: u for u in (build_backend_unit(spec), build_core_unit(spec))}
