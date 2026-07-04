"""Yadgar daemon management — Docker container lifecycle.

`pip install yadgar` produces a thin client. All heavy compute (server,
embeddings, consolidation, tests, linting) runs inside a resource-capped
Docker container. The host only manages the container lifecycle.

Two profiles — prod and dev — can run simultaneously on separate ports
(8765 and 8766) with separate named volumes so dev experiments don't
pollute prod data.
"""
# Module size justified: single-responsibility container lifecycle manager — all methods share YadgarDaemon state (self.port, self.db_path).

import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from yadgar.observability.observe import observe


@observe(tier="stage")
def _safe_urlopen(url: str, **kwargs):
    """§8: urlopen wrapper that rejects non-http/https schemes."""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in {"http", "https"}:
        raise ValueError(f"Disallowed URL scheme: {scheme!r}")
    return urllib.request.urlopen(url, **kwargs)  # noqa: S310


DEFAULT_PORT = 8765
DEFAULT_DEV_PORT = 8766
_HEALTH_TIMEOUT = 60.0  # Docker startup takes longer than a local process

# Container runtime detected at first check_runtime() call.
# v5.45.0: only check_runtime() + highest-traffic callsites migrated.
# TODO(v5.46): propagate _RUNTIME through all ~20 remaining subprocess callsites:
#   start_backend(), pull(), push(), build(), exec_in_container(), _image_exists(),
#   _container_running(), _container_exists(), _ensure_network(), status(), logs()
_RUNTIME: str | None = None


@observe(tier="hot")
def _get_runtime() -> str:
    """Return the active container runtime name (podman or docker).

    Uses YADGAR_CONTAINER_RUNTIME env if set; otherwise uses cached _RUNTIME
    from last check_runtime() call; otherwise probes once.
    v5.45.0: replaces hardcoded "docker" in highest-traffic callsites.
    """
    env_rt = os.environ.get("YADGAR_CONTAINER_RUNTIME", "").strip()
    if env_rt:
        return env_rt
    if _RUNTIME is not None:
        return _RUNTIME
    # Lazy probe: first invocation before check_runtime() called explicitly
    for rt in ("podman", "docker"):
        try:
            r = subprocess.run([rt, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return rt
        except (FileNotFoundError, subprocess.TimeoutExpired):  # fmt: skip
            continue
    return "docker"  # final fallback keeps backward compat


@observe(tier="hot")
def _default_image(repo: str) -> str:
    """Return repo:version using the installed package version, fallback to :latest."""
    try:
        from importlib.metadata import version as _pkg_version

        return f"{repo}:{_pkg_version('yadgar')}"
    except Exception:
        return f"{repo}:latest"


@observe(tier="stage")
def _backend_version() -> str:
    """Return the backend image version from the bundled server.json.

    v5.49.2 Bug 12: backend image track is independent of the core (pip) version.
    server.json is the single source of truth for backend_version.
    Falls back to yadgar.BACKEND_VERSION (also from server.json), then 'latest'.
    """
    try:
        import json as _json  # noqa: PLC0415

        _server_json = _source_root() / "server.json"
        _data = _json.loads(_server_json.read_text())
        return _data.get("backend_version", "latest")
    except Exception:
        pass
    try:
        from yadgar import BACKEND_VERSION  # noqa: PLC0415

        return BACKEND_VERSION
    except Exception:
        return "latest"


DOCKERHUB_IMAGE = _default_image("docker.io/openfantasy/yadgar")
# v5.49.2 Bug 12: use _backend_version() — backend image is on an independent version track.
DOCKERHUB_BACKEND_IMAGE = f"docker.io/openfantasy/yadgar-backend:{_backend_version()}"
DEFAULT_BACKEND_EMBED_PORT = 8001
_BACKEND_CONTAINER = "yadgar-backend"
_BACKEND_VOLUME = "yadgar-db-data"
_NETWORK_NAME = "yadgar-net"


# ── Host memory detection ──────────────────────────────────────────────────────


@observe(tier="stage")
def _host_memory_bytes() -> int:
    """Detect total host RAM. Linux /proc/meminfo, macOS sysctl, POSIX fallback."""
    if platform.system() == "Linux":
        try:
            with open("/proc/meminfo") as f:
                mem_line = next((ln for ln in f if ln.startswith("MemTotal:")), None)
            if mem_line:
                return int(mem_line.split()[1]) * 1024
        except OSError:
            pass
    if platform.system() == "Darwin":
        try:
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
            )
            return int(r.stdout.strip())
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as _e:
            pass
    # POSIX fallback
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError) as _e:
        return 8 * 1024 * 1024 * 1024  # assume 8 GB


def _container_memory_mb() -> int:
    """1/8 of host RAM, clamped to [512, 8192] MB."""
    eighth = _host_memory_bytes() // (8 * 1024 * 1024)
    return max(512, min(int(eighth), 8192))


# ── Source root detection ──────────────────────────────────────────────────────


@observe(tier="hot")
def _source_root() -> Path:
    """Walk up from this file to find the repo root (contains pyproject.toml)."""
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return here.parent


# ── Container profiles ─────────────────────────────────────────────────────────


@dataclass
class ContainerProfile:
    container_name: str
    image_name: str
    volume_name: str
    port: int  # host port — maps to container port 8765
    cpus: float
    restart_policy: str
    is_dev: bool


@observe(tier="hot")
def _prod_profile(port: int = DEFAULT_PORT) -> ContainerProfile:
    return ContainerProfile(
        container_name=os.environ.get("YADGAR_CONTAINER", "yadgar"),
        image_name=os.environ.get("YADGAR_IMAGE", DOCKERHUB_IMAGE),
        volume_name=os.environ.get("YADGAR_VOLUME", "yadgar-data"),
        port=port,
        cpus=1.0,
        restart_policy="on-failure:3",
        is_dev=False,
    )


@observe(tier="hot")
def _dev_profile(port: int = DEFAULT_DEV_PORT) -> ContainerProfile:
    return ContainerProfile(
        container_name=os.environ.get("YADGAR_DEV_CONTAINER", "yadgar-dev"),
        image_name=os.environ.get("YADGAR_DEV_IMAGE", "yadgar-dev"),
        volume_name=os.environ.get("YADGAR_DEV_VOLUME", "yadgar-dev-data"),
        port=port,
        cpus=2.0,
        restart_policy="no",
        is_dev=True,
    )


# ── Network helper ────────────────────────────────────────────────────────────


@observe(tier="stage")
def _ensure_network() -> None:
    """Create the yadgar Docker network if it doesn't exist."""
    result = subprocess.run(
        ["docker", "network", "inspect", _NETWORK_NAME],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["docker", "network", "create", "--driver", "bridge", _NETWORK_NAME],
            check=True,
            capture_output=True,
        )


# ── Main class ─────────────────────────────────────────────────────────────────


class YadgarDaemon:
    def __init__(self, port: int = DEFAULT_PORT, db_path: str | None = None):
        self.port = port
        self.db_path = db_path  # ignored in Docker mode — DB lives inside the container

    # ── public API ──────────────────────────────────────────────────────────

    @observe(tier="boundary")
    def start(self, dev: bool = False) -> dict:
        """Start the daemon container. No-op if already running."""
        profile = _dev_profile() if dev else _prod_profile(self.port)

        if self._container_running(profile.container_name):
            return {
                "status": "already_running",
                "container": profile.container_name,
                "port": profile.port,
            }

        # Remove a stopped container with the same name so `docker run` doesn't fail
        subprocess.run(
            ["docker", "rm", profile.container_name],
            capture_output=True,
        )

        if not self._image_exists(profile.image_name):
            if dev:
                hint = "yadgar daemon --dev build"
            else:
                hint = "yadgar daemon pull"
            return {
                "status": "failed",
                "reason": f"Image {profile.image_name!r} not found. Run: {hint}",
            }

        # Ensure network exists and backend is running before starting core
        _ensure_network()
        backend_name = os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER)
        if not self._container_running(backend_name):
            be = self.start_backend()
            if be.get("status") == "failed":
                return {
                    "status": "failed",
                    "reason": f"Backend failed to start: {be.get('reason')}",
                }

        mem_mb = _container_memory_mb()

        cmd = [
            _get_runtime(),
            "run",
            "-d",
            "--name",
            profile.container_name,
            "--network",
            _NETWORK_NAME,
            "--cpus",
            str(profile.cpus),
            "--memory",
            f"{mem_mb}m",
            "--memory-swap",
            f"{mem_mb}m",  # swap == memory → effectively disabled
            "--restart",
            profile.restart_policy,
            "--user",
            "root",
            "-v",
            f"{profile.volume_name}:/data",
            "-p",
            f"{profile.port}:8765",
            "-e",
            f"YADGAR_DB_URL=http://{backend_name}:8000",
            "-e",
            f"YADGAR_EMBED_URL=http://{backend_name}:8001",
            "-e",
            "YADGAR_DATA_DIR=/data",
            "-e",
            "YADGAR_MCP_AUTH_TOKEN",
            # v5.49.2 Bug 13: pass all 6 secret DB env vars so storage layer
            # doesn't KeyError on YADGAR_DB_USER / YADGAR_DB_PASS.
            "-e",
            "YADGAR_DB_USER",
            "-e",
            "YADGAR_DB_PASS",
            "-e",
            "YADGAR_RW_USER",
            "-e",
            "YADGAR_RW_PASS",
            "-e",
            "YADGAR_RO_USER",
            "-e",
            "YADGAR_RO_PASS",
        ]

        # v5.49.2 Bug 13: pass secrets file to core container (mirrors start_backend).
        from yadgar import paths as _paths  # noqa: PLC0415

        _secrets_path = _paths.SECRETS_ENV_PATH
        if _secrets_path.exists():
            # Insert --env-file before image name (append position is fine; image appended later)
            cmd += ["--env-file", str(_secrets_path)]

        if profile.is_dev:
            source = _source_root()
            cmd += ["-v", f"{source}:/app"]
            hf_cache = Path.home() / ".cache" / "huggingface"
            if hf_cache.exists():
                cmd += ["-v", f"{hf_cache}:/home/yadgar/.cache/huggingface"]

        cmd.append(profile.image_name)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return {"status": "failed", "reason": result.stderr.strip()}

        container_id = result.stdout.strip()

        # Poll health endpoint until the server is ready
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if not self._container_running(profile.container_name):
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "20", profile.container_name],
                    capture_output=True,
                    text=True,
                ).stdout
                return {"status": "failed", "reason": f"container exited. Logs:\n{logs}"}
            if self._health_ok(profile.port):
                # v5.49.0 Phase 6: emit READY=1 once health check confirms the
                # container is serving. This signals systemd (Type=notify in
                # the service unit) that the daemon is ready.  The host CLI is
                # what systemd exec-watches — not the container process — so
                # READY=1 belongs here, not inside the container.
                try:
                    from yadgar import sd_notify as _sd  # noqa: PLC0415

                    _sd.ready()
                except Exception:
                    pass
                return {
                    "status": "started",
                    "container": profile.container_name,
                    "port": profile.port,
                    "memory_mb": mem_mb,
                    "id": container_id[:12],
                }

        return {
            "status": "started",
            "container": profile.container_name,
            "port": profile.port,
            "memory_mb": mem_mb,
            "warning": "health check timed out — server may still be loading",
        }

    @observe(tier="boundary")
    def start_backend(self) -> dict:
        """Start the backend container (SurrealDB + embed service)."""
        name = os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER)
        image = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)
        volume = os.environ.get("YADGAR_BACKEND_VOLUME", _BACKEND_VOLUME)

        if self._container_running(name):
            return {"status": "already_running", "container": name}

        subprocess.run(["docker", "rm", name], capture_output=True)

        if not self._image_exists(image):
            return {
                "status": "failed",
                "reason": f"Backend image {image!r} not found. Run: yadgar daemon pull",
            }

        _ensure_network()
        mem_mb = _container_memory_mb()

        from yadgar import paths as _paths  # noqa: PLC0415

        cmd = [
            _get_runtime(),
            "run",
            "-d",
            "--name",
            name,
            "--network",
            _NETWORK_NAME,
            "--cpus",
            "1.0",
            "--memory",
            f"{mem_mb}m",
            "--memory-swap",
            f"{mem_mb}m",
            "--restart",
            "on-failure:3",
            "--user",
            "root",
            "-v",
            f"{volume}:/data",
            "-p",
            f"{DEFAULT_BACKEND_EMBED_PORT}:8001",  # embed service only
        ]

        # Bug 1 fix: pass secrets via --env-file (if present) + explicit -e flags.
        # The daemon process has already sourced secrets.env via its own env, so
        # -e VAR (without =value) propagates the daemon's env value into the container.
        secrets_path = _paths.SECRETS_ENV_PATH
        if secrets_path.exists():
            cmd += ["--env-file", str(secrets_path)]
        cmd += [
            "-e",
            "SURREAL_USER",
            "-e",
            "SURREAL_PASS",
            "-e",
            "YADGAR_RW_USER",
            "-e",
            "YADGAR_RW_PASS",
            "-e",
            "YADGAR_RO_USER",
            "-e",
            "YADGAR_RO_PASS",
            "-e",
            "YADGAR_MCP_AUTH_TOKEN",
        ]

        cmd.append(image)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return {"status": "failed", "reason": result.stderr.strip()}

        container_id = result.stdout.strip()

        # Wait for backend embed service (port 8001) to be healthy
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(2.0)
            if not self._container_running(name):
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "20", name],
                    capture_output=True,
                    text=True,
                ).stdout
                return {"status": "failed", "reason": f"container exited. Logs:\n{logs}"}
            if self._health_ok(DEFAULT_BACKEND_EMBED_PORT):
                return {
                    "status": "started",
                    "container": name,
                    "embed_port": DEFAULT_BACKEND_EMBED_PORT,
                    "id": container_id[:12],
                }

        return {
            "status": "started",
            "container": name,
            "embed_port": DEFAULT_BACKEND_EMBED_PORT,
            "warning": "health check timed out — model may still be loading",
        }

    @observe(tier="boundary")
    def stop(self, dev: bool = False) -> dict:
        """Stop the core container (and optionally the backend)."""
        profile = _dev_profile() if dev else _prod_profile(self.port)
        results = {}

        # Stop core first
        if self._container_exists(profile.container_name):
            r = subprocess.run(
                ["docker", "stop", profile.container_name],
                capture_output=True,
                text=True,
            )
            results["core"] = "stopped" if r.returncode == 0 else r.stderr.strip()
        else:
            results["core"] = "not_running"

        # Stop backend
        backend_name = os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER)
        if self._container_exists(backend_name):
            r = subprocess.run(
                ["docker", "stop", backend_name],
                capture_output=True,
                text=True,
            )
            results["backend"] = "stopped" if r.returncode == 0 else r.stderr.strip()
        else:
            results["backend"] = "not_running"

        return {"status": "stopped", **results}

    @observe(tier="boundary")
    def status(self, dev: bool = False) -> dict:
        """Return daemon status dict."""
        profile = _dev_profile() if dev else _prod_profile(self.port)
        backend_name = os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER)
        backend_running = self._container_running(backend_name)
        core_running = self._container_running(profile.container_name)

        if not core_running:
            return {
                "running": False,
                "container": profile.container_name,
                "backend_running": backend_running,
                "backend_container": backend_name,
            }
        try:
            resp = _safe_urlopen(f"http://127.0.0.1:{profile.port}/health", timeout=2)
            health = json.loads(resp.read().decode())
            return {
                "running": True,
                "container": profile.container_name,
                "port": profile.port,
                "backend_running": backend_running,
                "backend_container": backend_name,
                **health,
            }
        except urllib.error.HTTPError as e:
            health = json.loads(e.read().decode())
            return {
                "running": True,
                "container": profile.container_name,
                "port": profile.port,
                "backend_running": backend_running,
                "backend_container": backend_name,
                **health,
            }
        except Exception:
            return {
                "running": True,
                "container": profile.container_name,
                "port": profile.port,
                "backend_running": backend_running,
                "backend_container": backend_name,
                "health": "unreachable",
            }

    def restart(self, dev: bool = False) -> dict:
        stop_result = self.stop(dev=dev)
        start_result = self.start(dev=dev)
        return {"stopped": stop_result, "started": start_result}

    @observe(tier="boundary")
    def configure_mcp(self, dev: bool = False) -> dict:
        """Switch ~/.claude.json MCP config to streamable-http transport."""
        profile = _dev_profile() if dev else _prod_profile(self.port)
        config_path = Path.home() / ".claude.json"
        config: dict = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
            except Exception:
                config = {}

        old = config.get("mcpServers", {}).get("yadgar", {})
        mcp_servers = config.get("mcpServers", {})
        entry: dict = {
            "type": "streamable-http",
            "url": f"http://127.0.0.1:{profile.port}/mcp",
        }
        token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "").strip()
        if token:
            entry["headers"] = {"Authorization": f"Bearer {token}"}
        mcp_servers["yadgar"] = entry
        config["mcpServers"] = mcp_servers
        config_path.write_text(json.dumps(config, indent=2))

        return {
            "updated": str(config_path),
            "old": old,
            "new": mcp_servers["yadgar"],
        }

    @observe(tier="boundary")
    def install_systemd_service(self, dev: bool = False) -> dict:
        """Write two systemd user service units: yadgar-backend.service and yadgar.service."""

        from yadgar import paths as _paths  # noqa: PLC0415

        profile = _dev_profile() if dev else _prod_profile(self.port)
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
    -p {DEFAULT_BACKEND_EMBED_PORT}:8001 \\
    -e SURREAL_USER=${{SURREAL_USER}} \\
    -e SURREAL_PASS=${{SURREAL_PASS}} \\
    -e YADGAR_RW_USER=${{YADGAR_RW_USER:-}} \\
    -e YADGAR_RW_PASS=${{YADGAR_RW_PASS:-}} \\
    -e YADGAR_RO_USER=${{YADGAR_RO_USER:-}} \\
    -e YADGAR_RO_PASS=${{YADGAR_RO_PASS:-}} \\
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

    @observe(tier="boundary")
    def pull(self) -> dict:
        """Pull the latest prod image from Docker Hub."""
        profile = _prod_profile(self.port)
        result = subprocess.run(["docker", "pull", profile.image_name])
        if result.returncode != 0:
            return {"ok": False, "reason": f"docker pull {profile.image_name} failed"}
        return {"ok": True, "image": profile.image_name}

    @observe(tier="boundary")
    def push(self, tag: str | None = None) -> dict:
        """Tag the prod image and push it to Docker Hub."""
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        try:
            ver = pkg_version("yadgar")
        except PackageNotFoundError:
            ver = "latest"

        profile = _prod_profile(self.port)
        if not self._image_exists(profile.image_name):
            return {
                "ok": False,
                "reason": f"Image {profile.image_name!r} not found. Build it first: yadgar daemon build",
            }

        hub_user = os.environ.get("YADGAR_DOCKERHUB_USER", "looseking")
        versioned = f"{hub_user}/yadgar:{tag or ver}"
        latest = f"{hub_user}/yadgar:latest"

        for remote_tag in (versioned, latest):
            r = subprocess.run(
                ["docker", "tag", profile.image_name, remote_tag], capture_output=True, text=True
            )
            if r.returncode != 0:
                return {"ok": False, "reason": f"docker tag failed: {r.stderr.strip()}"}

        pushed = []
        for remote_tag in (versioned, latest):
            print(f"Pushing {remote_tag}...", file=sys.stderr)
            r = subprocess.run(["docker", "push", remote_tag])
            if r.returncode != 0:
                return {"ok": False, "reason": f"docker push {remote_tag} failed"}
            pushed.append(remote_tag)

        return {"ok": True, "pushed": pushed}

    @observe(tier="boundary")
    def build(self, dev: bool = False, no_cache: bool = False, backend: bool = False) -> dict:
        """Build the Docker image for the given profile."""
        source = _source_root()
        if backend:
            dockerfile = source / "Dockerfile.backend"
            if not dockerfile.exists():
                return {"ok": False, "reason": f"No Dockerfile.backend found at {source}"}
            image_name = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)
            cmd = ["docker", "build", "-f", str(dockerfile), "-t", image_name, str(source)]
            if no_cache:
                cmd.insert(2, "--no-cache")
            print(f"Building {image_name!r} (backend)...", file=sys.stderr)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                return {"ok": False, "reason": f"docker build failed (exit {result.returncode})"}
            return {"ok": True, "image": image_name, "target": "backend"}

        profile = _dev_profile() if dev else _prod_profile(self.port)
        if not (source / "Dockerfile").exists():
            return {
                "ok": False,
                "reason": f"No Dockerfile found at {source}",
            }
        target = "dev" if dev else "prod"
        cmd = ["docker", "build", "--target", target, "-t", profile.image_name, str(source)]
        if no_cache:
            cmd.insert(2, "--no-cache")
        print(f"Building {profile.image_name!r} (target={target})...", file=sys.stderr)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            return {"ok": False, "reason": f"docker build failed (exit {result.returncode})"}
        return {"ok": True, "image": profile.image_name, "target": target}

    @observe(tier="boundary")
    def exec_in_container(
        self, args: list[str], interactive: bool = False, dev: bool = True
    ) -> int:
        """Run a command inside a container via docker exec. Returns the exit code."""
        profile = _dev_profile() if dev else _prod_profile(self.port)
        if not self._container_running(profile.container_name):
            print(
                f"Container {profile.container_name!r} is not running.",
                file=sys.stderr,
            )
            print(
                f"  Start it with: yadgar daemon start {'--dev' if dev else ''}",
                file=sys.stderr,
            )
            return 1
        docker_cmd = ["docker", "exec"]
        if interactive:
            docker_cmd += ["-it"]
        docker_cmd.append(profile.container_name)
        docker_cmd.extend(args)
        return subprocess.run(docker_cmd).returncode

    # ── Docker availability ─────────────────────────────────────────────────

    @observe(tier="boundary")
    @staticmethod
    def check_runtime() -> dict:
        """Detect and verify container runtime (podman or docker).

        Detection order (DP2 resolution):
          1. YADGAR_CONTAINER_RUNTIME env override
          2. podman (rootless-friendly)
          3. docker
          4. Neither → ok=False

        Returns: {ok, runtime, version} on success; {ok, reason} on failure.
        Populates module-level _RUNTIME on first successful call.
        """
        global _RUNTIME  # noqa: PLW0603

        # Check env override first
        env_rt = os.environ.get("YADGAR_CONTAINER_RUNTIME", "").strip()
        candidates = [env_rt] if env_rt else ["podman", "docker"]

        for rt in candidates:
            try:
                result = subprocess.run(
                    [rt, "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    version = result.stdout.strip() or "?"
                    _RUNTIME = rt
                    return {"ok": True, "runtime": rt, "version": version}
                # Binary exists but daemon not running
                exist = subprocess.run([rt, "--version"], capture_output=True, timeout=5)
                if exist.returncode == 0:
                    return {
                        "ok": False,
                        "runtime": rt,
                        "reason": f"{rt} installed but daemon not running",
                    }
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "reason": f"{rt} version timed out (is the daemon running?)",
                }

        if env_rt:
            return {
                "ok": False,
                "reason": f"{env_rt!r} not found in PATH — check YADGAR_CONTAINER_RUNTIME",
            }
        return {"ok": False, "reason": "No container runtime found — install podman or docker"}

    @staticmethod
    def check_docker() -> dict:
        """Backward-compat alias for check_runtime(). Deprecated since v5.45.0."""
        return YadgarDaemon.check_runtime()

    # ── internals ───────────────────────────────────────────────────────────

    @observe(tier="stage")
    def _image_exists(self, image_name: str) -> bool:
        """Return True if the image is present in the local Docker store."""
        return (
            subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
            ).returncode
            == 0
        )

    @observe(tier="stage")
    def _container_running(self, name: str) -> bool:
        result = subprocess.run(
            [_get_runtime(), "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    @observe(tier="stage")
    def _container_exists(self, name: str) -> bool:
        return (
            subprocess.run([_get_runtime(), "inspect", name], capture_output=True).returncode == 0
        )
        # TODO(v5.46): propagate _get_runtime() through remaining ~16 callsites:
        #   stop(), status(), pull(), push(), build(), exec_in_container(),
        #   _image_exists(), _ensure_network(), start() rm/log lines, start_backend() rm/log lines

    @observe(tier="stage")
    def _health_ok(self, port: int) -> bool:
        try:
            _safe_urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return True
        except urllib.error.HTTPError:
            # liveness = server responding at all; full-health (503-on-degraded)
            # enforced by the container's curl -f healthcheck, not this gate.
            return True
        except Exception:
            return False
