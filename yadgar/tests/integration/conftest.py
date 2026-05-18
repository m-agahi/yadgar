"""Fixtures for vacuum end-to-end integration tests.

Module-scoped: one container per test module, torn down after all tests in the
module complete.  Skip the whole module if docker/podman is unavailable.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import pytest


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_200(url: str, timeout: float = 60.0, interval: float = 1.0) -> bool:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


@pytest.fixture(scope="module")
def live_backend_container(tmp_path_factory):
    """Spin up a real yadgar-backend container; yield connection info; tear down.

    Skips if neither docker nor podman is available on the host.

    Yields a dict:
        backend_url  : str   e.g. "http://127.0.0.1:32100"
        embed_url    : str   e.g. "http://127.0.0.1:32101"  (embed / admin service)
        data_dir     : Path  tmp dir bind-mounted as /data inside the container
        surreal_user : str   "root"
        surreal_pass : str   "root"
        container_name : str  for docker stop/start
        container_cmd  : str  "docker" or "podman"
    """
    docker_cmd = shutil.which("docker") or shutil.which("podman")
    if docker_cmd is None:
        pytest.skip("docker/podman not available on this host")

    data_dir = tmp_path_factory.mktemp("yadgar_itest_data")
    # The container runs as user 'yadgar' (non-root); make the data dir world-writable
    # so SurrealKV can create its data files inside the bind-mounted volume.
    data_dir.chmod(0o777)
    port_db = _find_free_port()
    port_embed = _find_free_port()
    container_name = f"yadgar-vacuum-itest-{os.getpid()}"

    cmd = [
        docker_cmd,
        "run",
        "-d",
        "--name",
        container_name,
        # Run as root inside the container.  In rootless podman/docker this maps
        # to the calling OS user (e.g. uid=1000) via user-namespace remapping, so
        # files written to the bind-mounted /data volume are owned by the test user
        # and can be read/copied by the vacuum code (shutil.copytree in phase 2).
        "--user",
        "root",
        "-p",
        f"127.0.0.1:{port_db}:8000",
        "-p",
        f"127.0.0.1:{port_embed}:8001",
        "-v",
        f"{data_dir}:/data",
        "-e",
        "SURREAL_USER=root",
        "-e",
        "SURREAL_PASS=root",
        "-e",
        "YADGAR_RW_USER=yadgar-rw",
        "-e",
        "YADGAR_RW_PASS=test123",
        "-e",
        "YADGAR_RO_USER=yadgar-ro",
        "-e",
        "YADGAR_RO_PASS=test123",
        "-e",
        "YADGAR_MCP_AUTH_TOKEN=test-token",
        "openfantasy/yadgar-backend:5.0.1",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip(f"Failed to start yadgar-backend container: {result.stderr.strip()}")

    backend_url = f"http://127.0.0.1:{port_db}"
    embed_url = f"http://127.0.0.1:{port_embed}"

    if not _wait_http_200(f"{backend_url}/health", timeout=60.0):
        subprocess.run([docker_cmd, "stop", container_name], capture_output=True)
        pytest.skip("yadgar-backend container did not become healthy within 60 s")

    yield {
        "backend_url": backend_url,
        "embed_url": embed_url,
        "data_dir": data_dir,
        "surreal_user": "root",
        "surreal_pass": "root",
        "rw_pass": "test123",  # matches YADGAR_RW_PASS env var above
        "container_name": container_name,
        "container_cmd": docker_cmd,
    }

    # Teardown — stop and remove the container.
    subprocess.run([docker_cmd, "stop", container_name], capture_output=True, timeout=30)
    subprocess.run([docker_cmd, "rm", "-f", container_name], capture_output=True, timeout=10)

    # In rootless podman the container user namespace may map UIDs such that
    # files written to the bind-mounted volume are owned by a remapped UID that
    # the test user cannot delete (e.g. surreal_db/wal with mode 750 owned by
    # subUID 100999).  Use `podman unshare` to chmod the directory inside the
    # user namespace so pytest's temp-path cleanup can succeed.
    podman_cmd = shutil.which("podman")
    if podman_cmd and (data_dir / "surreal_db").exists():
        subprocess.run(
            [podman_cmd, "unshare", "chmod", "-R", "777", str(data_dir / "surreal_db")],
            capture_output=True,
            timeout=15,
        )
