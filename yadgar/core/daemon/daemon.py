"""Yadgar daemon management — Docker container lifecycle.

`pip install yadgar` produces a thin client. All heavy compute (server,
embeddings, consolidation, tests, linting) runs inside a resource-capped
Docker container. The host only manages the container lifecycle.

Two profiles — prod and dev — can run simultaneously on separate ports
(8765 and 8766) with separate named volumes so dev experiments don't
pollute prod data.

Car C3 (module-standardization train) split the 948-LOC supervisor into
cohesive siblings inside this package:
  runtime.py   — container-runtime + host-env probing (_RUNTIME cache, image
                 resolution, host memory, source root, _safe_urlopen)
  profiles.py  — ContainerProfile + _prod_profile/_dev_profile + _ensure_network
  systemd.py   — systemd user-unit rendering (install_systemd_service body)
This module keeps the YadgarDaemon orchestrator that drives them. The package
``__init__`` re-exports every previously-importable symbol, so external
importers are byte-unaffected.
"""

# I13 note (ADR-0130): this module is an ACCEPTED single-file case over the ≤500
# soft cap — do NOT split it in future audits. `YadgarDaemon` is one cohesive
# orchestrator class: every lifecycle method shares `self` state + the internal
# helper suite (_container_running / _health_ok / _image_exists / _container_exists),
# and Car C3 already extracted the pure utilities (runtime/profiles/systemd). No
# further seam exists. Kept in the soft baseline ratchet intentionally.

import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.profiles import (
    _dev_profile,
    _ensure_network,
    _prod_profile,
)
from yadgar.core.daemon.runtime import (
    _BACKEND_CONTAINER,
    _BACKEND_VOLUME,
    _HEALTH_TIMEOUT,
    _NETWORK_NAME,
    DEFAULT_BACKEND_EMBED_PORT,
    DEFAULT_PORT,
    DOCKERHUB_BACKEND_IMAGE,
    _container_memory_mb,
    _get_runtime,
    _safe_urlopen,
    _source_root,
)
from yadgar.core.daemon.runtime import check_runtime as _check_runtime
from yadgar.core.daemon.systemd import install_systemd_service as _install_systemd_service


class YadgarDaemon:
    def __init__(self, port: int = DEFAULT_PORT, db_path: str | None = None):
        self.port = port
        self.db_path = db_path  # ignored in Docker mode — DB lives inside the container

    # ── public API ──────────────────────────────────────────────────────────

    @observe(tier="boundary")
    def start(self, dev: bool = False) -> dict:
        """Start the daemon container. No-op if already running."""
        profile = _dev_profile() if dev else _prod_profile(self.port)
        rt = _get_runtime()

        if self._container_running(profile.container_name):
            return {
                "status": "already_running",
                "container": profile.container_name,
                "port": profile.port,
            }

        # Remove a stopped container with the same name so `<rt> run` doesn't fail
        subprocess.run(
            [rt, "rm", profile.container_name],
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
            rt,
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
        from yadgar._shared import paths as _paths  # noqa: PLC0415

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
                    [rt, "logs", "--tail", "20", profile.container_name],
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
                    from yadgar.core.daemon import sd_notify as _sd  # noqa: PLC0415

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
        rt = _get_runtime()
        name = os.environ.get("YADGAR_BACKEND_CONTAINER", _BACKEND_CONTAINER)
        image = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)
        volume = os.environ.get("YADGAR_BACKEND_VOLUME", _BACKEND_VOLUME)
        # R3: the backend runs the queue drainer, which reads the shared file
        # queue. Core writes it to {YADGAR_DATA_DIR}/queue on its OWN volume
        # (YADGAR_VOLUME, default "yadgar-data" — see _prod_profile). The backend
        # must mount that SAME volume and point YADGAR_QUEUE_BASE at it, or
        # _queue_base_path() returns None → drainer disabled → queued
        # memorize/wiki_add writes never commit. Mirrors docker-compose.yml.
        core_vol = os.environ.get("YADGAR_VOLUME", "yadgar-data")

        if self._container_running(name):
            return {"status": "already_running", "container": name}

        subprocess.run([rt, "rm", name], capture_output=True)

        if not self._image_exists(image):
            return {
                "status": "failed",
                "reason": f"Backend image {image!r} not found. Run: yadgar daemon pull",
            }

        _ensure_network()
        mem_mb = _container_memory_mb()

        from yadgar._shared import paths as _paths  # noqa: PLC0415

        cmd = [
            rt,
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
            # R3: shared file-queue volume (same volume core mounts at /data) so
            # the drainer can see queued writes. YADGAR_QUEUE_BASE has no fallback
            # backend-side, so both the mount and the env var are required.
            "-v",
            f"{core_vol}:/queue-data",
            "-e",
            "YADGAR_QUEUE_BASE=/queue-data",
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
                    [rt, "logs", "--tail", "20", name],
                    capture_output=True,
                    text=True,
                ).stdout
                return {"status": "failed", "reason": f"container exited. Logs:\n{logs}"}
            if self._embed_health_ok(DEFAULT_BACKEND_EMBED_PORT):
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
        rt = _get_runtime()
        results = {}

        # Stop core first
        if self._container_exists(profile.container_name):
            r = subprocess.run(
                [rt, "stop", profile.container_name],
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
                [rt, "stop", backend_name],
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
        """Switch ~/.claude.json MCP config to streamable-http transport.

        Delegates to ``mcp_register.register_mcp_for_claude_code`` (Car 1).
        The non-atomic ``write_text`` from the original implementation is
        replaced by the atomic merge path in ``clients/merge.py``.

        Return shape is preserved: ``{"updated": str, "old": dict, "new": dict}``.
        """
        profile = _dev_profile() if dev else _prod_profile(self.port)
        from yadgar.core.install.clients.mcp_register import (  # noqa: PLC0415
            register_mcp_for_claude_code,
        )

        return register_mcp_for_claude_code(port=profile.port, dev=False)

    @observe(tier="boundary")
    def install_systemd_service(self, dev: bool = False) -> dict:
        """Write two systemd user service units: yadgar-backend.service and yadgar.service."""
        profile = _dev_profile() if dev else _prod_profile(self.port)
        return _install_systemd_service(profile, dev=dev)

    @observe(tier="boundary")
    def pull(self) -> dict:
        """Pull the latest prod image from Docker Hub."""
        profile = _prod_profile(self.port)
        rt = _get_runtime()
        result = subprocess.run([rt, "pull", profile.image_name])
        if result.returncode != 0:
            return {"ok": False, "reason": f"{rt} pull {profile.image_name} failed"}
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
        rt = _get_runtime()
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
                [rt, "tag", profile.image_name, remote_tag], capture_output=True, text=True
            )
            if r.returncode != 0:
                return {"ok": False, "reason": f"{rt} tag failed: {r.stderr.strip()}"}

        pushed = []
        for remote_tag in (versioned, latest):
            print(f"Pushing {remote_tag}...", file=sys.stderr)
            r = subprocess.run([rt, "push", remote_tag])
            if r.returncode != 0:
                return {"ok": False, "reason": f"{rt} push {remote_tag} failed"}
            pushed.append(remote_tag)

        return {"ok": True, "pushed": pushed}

    @observe(tier="boundary")
    def build(self, dev: bool = False, no_cache: bool = False, backend: bool = False) -> dict:
        """Build the Docker image for the given profile."""
        source = _source_root()
        rt = _get_runtime()
        if backend:
            dockerfile = source / "Dockerfile.backend"
            if not dockerfile.exists():
                return {"ok": False, "reason": f"No Dockerfile.backend found at {source}"}
            image_name = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)
            cmd = [rt, "build", "-f", str(dockerfile), "-t", image_name, str(source)]
            if no_cache:
                cmd.insert(2, "--no-cache")
            print(f"Building {image_name!r} (backend)...", file=sys.stderr)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                return {"ok": False, "reason": f"{rt} build failed (exit {result.returncode})"}
            return {"ok": True, "image": image_name, "target": "backend"}

        profile = _dev_profile() if dev else _prod_profile(self.port)
        if not (source / "Dockerfile").exists():
            return {
                "ok": False,
                "reason": f"No Dockerfile found at {source}",
            }
        target = "dev" if dev else "prod"
        cmd = [rt, "build", "--target", target, "-t", profile.image_name, str(source)]
        if no_cache:
            cmd.insert(2, "--no-cache")
        print(f"Building {profile.image_name!r} (target={target})...", file=sys.stderr)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            return {"ok": False, "reason": f"{rt} build failed (exit {result.returncode})"}
        return {"ok": True, "image": profile.image_name, "target": target}

    @observe(tier="boundary")
    def exec_in_container(
        self, args: list[str], interactive: bool = False, dev: bool = True
    ) -> int:
        """Run a command inside a container via `<runtime> exec`. Returns the exit code."""
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
        exec_cmd = [_get_runtime(), "exec"]
        if interactive:
            exec_cmd += ["-it"]
        exec_cmd.append(profile.container_name)
        exec_cmd.extend(args)
        return subprocess.run(exec_cmd).returncode

    # ── Docker availability ─────────────────────────────────────────────────

    @staticmethod
    def check_runtime() -> dict:
        """Detect and verify container runtime (podman or docker).

        Delegates to ``runtime.check_runtime`` — the ``@observe``-instrumented
        writer of the module-level ``_RUNTIME`` cache — so the writer and the
        ``_get_runtime`` reader stay co-located in ``runtime.py`` (Car C3 seam
        law: reassigned globals reach THROUGH the module). The span lives on the
        delegate; this thin wrapper carries no second span.

        Returns: {ok, runtime, version} on success; {ok, reason} on failure.
        """
        return _check_runtime()

    @staticmethod
    def check_docker() -> dict:
        """Backward-compat alias for check_runtime(). Deprecated since v5.45.0."""
        return YadgarDaemon.check_runtime()

    # ── internals ───────────────────────────────────────────────────────────

    @observe(tier="stage")
    def _image_exists(self, image_name: str) -> bool:
        """Return True if the image is present in the local container-runtime store."""
        return (
            subprocess.run(
                [_get_runtime(), "image", "inspect", image_name],
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

    @observe(tier="stage")
    def _health_ok(self, port: int) -> bool:
        """LIVENESS gate for the CORE daemon (ADR-0019) — probes /health/live.

        Used to decide when to fire sd_notify READY=1 (v5.49.0 Phase 6). Only
        "is the process up and responding" matters here; the container's own
        curl -f --health-on-failure=kill healthcheck (also pinned to
        /health/live) is what continues to watch liveness post-startup, and
        /health (readiness, db/embed-dependent) is a monitoring signal only —
        gating startup on it would reintroduce the liveness/readiness
        conflation ADR-0019 removed (a transiently-busy backend must not delay
        or fail this gate).
        """
        try:
            _safe_urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1)
            return True
        except urllib.error.HTTPError:
            # liveness = server responding at all; full-health (503-on-degraded)
            # enforced by the container's curl -f healthcheck, not this gate.
            return True
        except Exception:
            return False

    @observe(tier="stage")
    def _embed_health_ok(self, port: int) -> bool:
        """READINESS gate for the backend embed service.

        The embed service (port 8001) exposes no /health/live liveness
        variant — ADR-0019's liveness split is scoped to the core daemon
        only — so this stays on bare /health (db + model-loaded readiness).
        """
        try:
            _safe_urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            return False
