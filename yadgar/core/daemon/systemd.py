"""systemd user-unit generation for the Yadgar daemon.

Split out of ``daemon.py`` (Car C3, module-standardization train). Renders the
two systemd user service units (``yadgar-backend.service`` + ``yadgar.service``)
and writes them under ``~/.config/systemd/user``. ``YadgarDaemon.install_systemd_service``
is a thin wrapper that resolves the profile then delegates here.

task:0104 made the units name the RESOLVED runtime binary. task:0105 closes the
readiness half it left open: both units were ``Type=notify``, which needs
something to send ``READY=1``. On podman that is the sd_notify proxy —
``--sdnotify=healthy`` for the backend, and the default ``--sdnotify=container``
mode forwarding the daemon's own emit for the core. **Docker has no sd_notify
proxy at all**: it never sets ``NOTIFY_SOCKET`` in the container, so
``sd_notify.py``'s emit is a silent no-op, and ``--sdnotify`` is not even a
``docker run`` flag. So the units are now runtime-conditional:

* podman — unchanged: ``Type=notify`` + ``--sdnotify=healthy`` + ``--health-cmd``.
* docker — ``Type=exec`` plus a bounded ``ExecStartPost=`` poll of ``/health``.
  Per ``man systemd.service``, "the execution of ExecStartPost= is taken into
  account for the purpose of Before=/After= ordering constraints", which is the
  same ordering guarantee ``Type=notify`` buys on podman.

The gate polls ``/health`` (readiness) rather than the image HEALTHCHECK's
``/health/live`` (liveness, ADR-0019) on purpose: podman's core readiness is the
in-container ``sd_notify.ready()`` emitted LAST, after the full engine set
(``bootstrap.py``). ``/health/live`` goes green as soon as the HTTP server binds,
so gating on it would mark the docker unit active EARLIER than podman does.

Design + rejected alternatives:
``docs/plans/archive/runtime-agnostic-systemd-readiness-2026-08-01.md``.
Cross-generator guard: ``yadgar/tests/scripts/test_runtime_readiness_cross_generator.py``.
"""

import os
from pathlib import Path

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


def _health_gate(url: str, retries: int) -> str:
    """An ``ExecStartPost=`` line that blocks until *url* answers 200 (task:0105).

    ``curl`` is written bare and resolved from the unit's ``$PATH`` — the same
    convention ``scripts/install/yadgar.service.in`` already uses for its
    ``mkdir -p`` ExecStartPre. No shell wrapper, so systemd's own ``$`` expansion
    never applies and there is nothing to escape as ``$$``; curl's ``--retry``
    does the polling. ``--retry-connrefused`` covers "port not listening yet",
    ``--retry-all-errors`` covers "listening but not healthy yet" (``--fail``
    turns a 5xx into an error, which plain ``--retry`` would not retry).
    """
    return (
        f"ExecStartPost=curl --fail --silent --show-error --output /dev/null "
        f"--retry {retries} --retry-delay 2 --retry-connrefused "
        f"--retry-all-errors {url}\n"
    )


@observe(tier="hot")
def _readiness_directives(runtime: str, core_port: int) -> dict[str, str]:
    """Per-runtime readiness fragments for the two generated units (task:0105).

    Podman proxies sd_notify, so its units stay ``Type=notify``: the backend via
    ``--sdnotify=healthy``, the core via podman's default ``sdnotify=container``
    mode forwarding the daemon's own ``READY=1``. Docker has no sd_notify proxy
    at all, so nothing would ever send ``READY=1`` there; it gets ``Type=exec``
    plus a bounded ``ExecStartPost=`` health gate, which per ``man systemd.service``
    is "taken into account for the purpose of Before=/After= ordering constraints".

    A non-zero ``ExecStartPost`` FAILS the unit, and ``Restart=on-failure`` would
    then loop it — so each budget is set from the slowest real path (a first
    start, where ``docker run`` pulls the image inline) and ``TimeoutStartSec``
    is set strictly above it: backend gate 75 x 2s = 150s inside 180, core gate
    45 x 2s = 90s inside 120. A gate that could outlive its own start budget
    would make systemd's timeout the binding constraint instead of the gate.

    task:0106 gives the PODMAN arm the same two budgets. It had none — task:0105
    left it on systemd's 90s default to keep its own render byte-identical — and
    90s is structurally too tight there, independent of any measurement:
    ``--sdnotify=healthy`` emits ``READY=1`` on the first HEALTHY healthcheck,
    the backend unit passes ``--health-start-period=60s``, and it pins no
    ``--health-interval`` so podman's 30s default applies. Health results are
    therefore quantised to 30s ticks after a 60s grace, so a model load finishing
    at t=65s is not OBSERVED until t≈90s — the default, exactly. The backend's
    ``/health`` returns 200 only when ``db_ok and engine_loaded``, so model load
    genuinely sits on that path. 180 is ``flake.nix:366``'s field-proven budget
    for this identical unit shape on this identical runtime ("covers cold model
    load"). The core takes 120, matching ``flake.nix:427`` and the unconditional
    value in ``scripts/install/yadgar.service.in``; safe on podman because the
    core unit's ``Wants=``/``After=yadgar-backend.service`` means the backend is
    already HEALTHY, so backend model load is not inside the core's budget.
    ``After=`` is what provides that — the dependency is ``Wants=`` rather than
    ``Requires=`` (task:0111 / ADR-0188, so a backend stop does not cascade), and
    ``Wants=`` still pulls the backend into the same start transaction.  The one
    case that changed: a backend that FAILS to start no longer blocks the core
    start, so the core can now come up against a cold backend.
    The podman arm has no ``ExecStartPost`` gate at all (readiness is a signal),
    so the gate-vs-timeout relation above is vacuous there.

    Why this was newly reachable: before task:0105 the generated backend unit
    could not start at all (``--health-cmd`` was emitted unquoted), so no start
    budget was ever exercised.

    Compared on the BASENAME because the runtime may legitimately arrive as a
    path (the ``YADGAR_CONTAINER_RUNTIME`` escape hatch, and ``flake.nix``'s
    ``runtime`` option, both allow one).
    """
    if os.path.basename(runtime) == "podman":
        return {
            "backend_ready": "Type=notify\nNotifyAccess=all\nTimeoutStartSec=180\n",
            "backend_sdnotify": "    --sdnotify=healthy \\\n",
            "backend_gate": "",
            "core_ready": "Type=notify\nNotifyAccess=all\nTimeoutStartSec=120\n",
            "core_gate": "",
        }
    return {
        "backend_ready": "Type=exec\nTimeoutStartSec=180\n",
        "backend_sdnotify": "",
        "backend_gate": _health_gate(f"http://127.0.0.1:{DEFAULT_BACKEND_EMBED_PORT}/health", 75),
        "core_ready": "Type=exec\nTimeoutStartSec=120\n",
        "core_gate": _health_gate(f"http://127.0.0.1:{core_port}/health", 45),
    }


@observe(tier="boundary")
def install_systemd_service(profile: ContainerProfile, dev: bool = False) -> dict:
    """Write two systemd user service units: yadgar-backend.service and yadgar.service."""

    from yadgar._shared import paths as _paths  # noqa: PLC0415

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    # task:0104: the units are rendered for the runtime that is actually installed.
    # A literal ``docker`` here made every generated unit dead on a podman-only
    # host — the third instance of the class after task:0083 (`daemon start`) and
    # task:0101 (`upgrade`). Guarded by the unit-directive detector in
    # yadgar/tests/core/test_daemon_runtime_binary.py.
    runtime = _get_runtime()
    ready = _readiness_directives(runtime, profile.port)  # task:0105

    backend_name = os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER)
    backend_image = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)
    # Bug 11: use XDG DATA_DIR as host bind mount instead of named volume
    backend_data_dir = _paths.DATA_DIR

    # Bug 5: load backend_version from server.json (not core version)
    # v5.49.2: refactored to call module-level _backend_version() helper.
    _bv = _backend_version()

    # Resolve actual backend image with correct version tag
    # If image already has a tag (from env), use as-is; otherwise append backend_version
    if ":" not in backend_image.split("/")[-1]:
        backend_image_tagged = f"{backend_image}:{_bv}"
    else:
        # Replace the tag portion with backend_version
        _base = backend_image.rsplit(":", 1)[0]
        backend_image_tagged = f"{_base}:{_bv}"

    # Bug 6: use XDG secrets path (not /etc/yadgar/secrets.env)
    secrets_env_path = _paths.SECRETS_ENV_PATH

    # yadgar-backend.service — backend (SurrealDB + embed)
    # Bug 9: renamed from yadgar-db.service to yadgar-backend.service
    hf_cache = Path.home() / ".cache" / "huggingface"
    hf_mount = (
        f"    -v {hf_cache}:/home/yadgar/.cache/huggingface \\\n" if hf_cache.exists() else ""
    )

    # ADR-0180: the backend ExecStart MUST forward YADGAR_MCP_AUTH_TOKEN into the
    # CONTAINER. The backend serves /admin/* and compares the presented bearer
    # against its own copy (_require_admin_token); EnvironmentFile puts it in the
    # UNIT's env only, which the container never sees without an explicit -e.
    # Guard: yadgar/tests/scripts/test_admin_token_cross_generator.py.
    backend_unit = f"""\
[Unit]
Description=Yadgar Backend — SurrealDB and Embedding Service
After=network.target

[Service]
{ready["backend_ready"]}EnvironmentFile={secrets_env_path}
ExecStartPre=-{runtime} network create --driver bridge {_NETWORK_NAME}
ExecStartPre=-{runtime} stop {backend_name}
ExecStartPre=-{runtime} rm {backend_name}
ExecStart={runtime} run --rm \\
    --name {backend_name} \\
    --network {_NETWORK_NAME} \\
    --cpus 1.0 \\
    --memory 4g \\
    --memory-swap 4g \\
    --user root \\
{ready["backend_sdnotify"]}    --health-cmd "curl -f http://localhost:8001/health || exit 1" \\
    --health-start-period=60s \\
    -v {backend_data_dir}:/data \\
    -v {profile.volume_name}:/queue-data \\
    -p {DEFAULT_BACKEND_EMBED_PORT}:8001 \\
    -e SURREAL_USER=${{SURREAL_USER}} \\
    -e SURREAL_PASS=${{SURREAL_PASS}} \\
    -e YADGAR_RW_USER=${{YADGAR_RW_USER:-}} \\
    -e YADGAR_RW_PASS=${{YADGAR_RW_PASS:-}} \\
    -e YADGAR_RO_USER=${{YADGAR_RO_USER:-}} \\
    -e YADGAR_RO_PASS=${{YADGAR_RO_PASS:-}} \\
    -e YADGAR_QUEUE_BASE=/queue-data \\
    -e YADGAR_MCP_AUTH_TOKEN=${{YADGAR_MCP_AUTH_TOKEN}} \\
{hf_mount}    {backend_image_tagged}
{ready["backend_gate"]}ExecStop={runtime} stop {backend_name}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

    # yadgar.service — core (MCP server)
    suffix = "-dev" if dev else ""
    # Bug 7: use XDG state path for upgrade.env (not /root/.yadgar/upgrade.env)
    upgrade_env_path = _paths.STATE_DIR / "upgrade.env"
    # Phase 7 follow-up: readiness type + upgrade.env EnvironmentFile to match the
    # yadgar.service.in template. On podman the core is Type=notify: podman's
    # default --sdnotify=container mode passes NOTIFY_SOCKET into the container
    # and forwards the daemon's own READY=1 / STOPPING=1 (ADR-0017 — that proxy
    # forwards READY=1 and drops WATCHDOG=1, and only READY=1 is relied on here).
    # Docker has no such proxy, so it gets the Type=exec + health-gate shape.
    # EnvironmentFile leading '-' makes missing file non-fatal (first install).
    core_unit = f"""\
[Unit]
Description=Yadgar Memory Engine — MCP Core Server
Wants=yadgar-backend{suffix}.service
After=network.target yadgar-backend{suffix}.service

[Service]
{ready["core_ready"]}EnvironmentFile={secrets_env_path}
EnvironmentFile=-{upgrade_env_path}
ExecStartPre=-{runtime} stop {profile.container_name}
ExecStartPre=-{runtime} rm {profile.container_name}
ExecStart={runtime} run --rm \\
    --name {profile.container_name} \\
    --network {_NETWORK_NAME} \\
    --cpus {profile.cpus} \\
    --memory {_container_memory_mb()}m \\
    --memory-swap {_container_memory_mb()}m \\
    --user root \\
    -v {profile.volume_name}:/data \\
    -p {profile.port}:8765 \\
    -e YADGAR_DB_URL=http://{backend_name}:8000 \\
    -e YADGAR_EMBED_URL=http://{backend_name}:8001 \\
    -e YADGAR_DATA_DIR=/data \\
    -e YADGAR_MCP_AUTH_TOKEN=${{YADGAR_MCP_AUTH_TOKEN}} \\
    -e YADGAR_DB_USER=${{YADGAR_RW_USER:-${{YADGAR_DB_USER:-${{SURREAL_USER}}}}}} \\
    -e YADGAR_DB_PASS=${{YADGAR_RW_PASS:-${{YADGAR_DB_PASS:-${{SURREAL_PASS}}}}}} \\
    {profile.image_name}
{ready["core_gate"]}ExecStop={runtime} stop {profile.container_name}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

    # Bug 9: renamed yadgar-db → yadgar-backend
    backend_service_name = f"yadgar-backend{suffix}.service"
    core_service_name = f"yadgar{suffix}.service"

    backend_path = service_dir / backend_service_name
    core_path = service_dir / core_service_name
    backend_path.write_text(backend_unit)
    core_path.write_text(core_unit)

    return {
        "backend_service": str(backend_path),
        "core_service": str(core_path),
        "enable": f"systemctl --user enable {backend_service_name} {core_service_name}",
        "start": f"systemctl --user start {backend_service_name} && systemctl --user start {core_service_name}",
        "status": f"systemctl --user status {backend_service_name} {core_service_name}",
    }
