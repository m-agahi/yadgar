"""Container-runtime + host-environment probing for the Yadgar daemon.

Split out of ``daemon.py`` (Car C3, module-standardization train). Groups the
runtime-detection cache (``_RUNTIME`` + ``_get_runtime`` + ``check_runtime``),
image/version resolution, host-memory sizing, source-root discovery, and the
scheme-guarded urlopen wrapper — all the environment-probing primitives the
``YadgarDaemon`` orchestrator relies on.

The ``_RUNTIME`` cache writer (``check_runtime``) and reader (``_get_runtime``)
MUST live in the same module: ``check_runtime`` binds ``global _RUNTIME`` to
THIS module, and ``_get_runtime`` reads it here. Splitting them would silently
break the cache (writer sets one module's global, reader reads another's).
"""

import os
import platform
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from yadgar._shared.observability.observe import observe

DEFAULT_PORT = 8765
DEFAULT_DEV_PORT = 8766
_HEALTH_TIMEOUT = 60.0  # Docker startup takes longer than a local process


@observe(tier="stage")
def _safe_urlopen(url: str, **kwargs):
    """§8: urlopen wrapper that rejects non-http/https schemes."""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in {"http", "https"}:
        raise ValueError(f"Disallowed URL scheme: {scheme!r}")
    return urllib.request.urlopen(url, **kwargs)  # noqa: S310


# Container runtime detected at first check_runtime() call.
# v5.45.0 migrated check_runtime() + the highest-traffic callsites; task:0083
# finished the job — EVERY subprocess callsite in this package now resolves the
# binary through _get_runtime(). A literal "docker" argv head re-appearing here
# is a regression, guarded by tests/core/test_daemon_runtime_binary.py.
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


@observe(tier="boundary")
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
