"""systemd user-unit generation for the Yadgar daemon.

Split out of ``daemon.py`` (Car C3, module-standardization train). Renders the
two systemd user service units (``yadgar-backend.service`` + ``yadgar.service``)
and writes them under ``~/.config/systemd/user``. ``YadgarDaemon.install_systemd_service``
is a thin wrapper that resolves the profile then delegates here.
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
)


@observe(tier="boundary")
def install_systemd_service(profile: ContainerProfile, dev: bool = False) -> dict:
    """Write two systemd user service units: yadgar-backend.service and yadgar.service."""

    from yadgar._shared import paths as _paths  # noqa: PLC0415

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

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

    backend_unit = f"""\
[Unit]
Description=Yadgar Backend — SurrealDB and Embedding Service
Requires=docker.service
After=docker.service

[Service]
Type=notify
NotifyAccess=all
EnvironmentFile={secrets_env_path}
ExecStartPre=-docker network create --driver bridge {_NETWORK_NAME}
ExecStartPre=-docker stop {backend_name}
ExecStartPre=-docker rm {backend_name}
ExecStart=docker run --rm \\
    --name {backend_name} \\
    --network {_NETWORK_NAME} \\
    --cpus 1.0 \\
    --memory 4g \\
    --memory-swap 4g \\
    --user root \\
    --sdnotify=healthy \\
    --health-cmd curl -f http://localhost:8001/health || exit 1 \\
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
{hf_mount}    {backend_image_tagged}
ExecStop=docker stop {backend_name}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

    # yadgar.service — core (MCP server)
    suffix = "-dev" if dev else ""
    # Bug 7: use XDG state path for upgrade.env (not /root/.yadgar/upgrade.env)
    upgrade_env_path = _paths.STATE_DIR / "upgrade.env"
    # Phase 7 follow-up: Type=notify + upgrade.env EnvironmentFile to match yadgar.service.in template.
    # sd_notify signals (READY=1, STOPPING=1) require Type=notify.
    # EnvironmentFile leading '-' makes missing file non-fatal (first install).
    core_unit = f"""\
[Unit]
Description=Yadgar Memory Engine — MCP Core Server
Requires=docker.service yadgar-backend{suffix}.service
After=docker.service yadgar-backend{suffix}.service

[Service]
Type=notify
NotifyAccess=all
EnvironmentFile={secrets_env_path}
EnvironmentFile=-{upgrade_env_path}
ExecStartPre=-docker stop {profile.container_name}
ExecStartPre=-docker rm {profile.container_name}
ExecStart=docker run --rm \\
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
ExecStop=docker stop {profile.container_name}
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
