"""Fixtures for vacuum end-to-end integration tests.

Module-scoped: one container per test module, torn down after all tests in the
module complete.  Skip the whole module if docker/podman is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_SERVER_JSON = Path(__file__).resolve().parents[3] / "server.json"


def _backend_image() -> str:
    """Return current backend image tag from server.json (single source of truth).

    ``YADGAR_TEST_BACKEND_IMAGE`` overrides the lookup. This exists because
    server.json carries the PENDING version, and backend images are published at
    RELEASE — i.e. after merge. So on any PR that bumps ``backend_version`` the
    derived tag does not exist yet and the container start fails, which this
    fixture reports as a SKIP. Car A3 (task 156) wired this suite into CI for the
    first time and its skip-is-not-a-pass gate caught exactly that on the very
    first run: ``openfantasy/yadgar-backend:5.76.4: not found``.

    The CI job therefore resolves a PULLABLE tag itself (pending if published,
    else master's released one) and passes it here. It fails loudly rather than
    skipping when neither can be pulled — a skip in that job is treated as a
    failure, so this override must never become a silent fallback.
    """
    override = os.environ.get("YADGAR_TEST_BACKEND_IMAGE", "").strip()
    if override:
        return override
    try:
        data = json.loads(_SERVER_JSON.read_text())
        version = data["backend_version"]
        return f"openfantasy/yadgar-backend:{version}"
    except (OSError, KeyError, json.JSONDecodeError) as e:
        pytest.skip(f"Cannot read backend_version from {_SERVER_JSON}: {e}")


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker_endpoint_host() -> str:
    """Host to reach a published container port on — NOT always 127.0.0.1.

    The CI runner talks to a docker:dind SIDECAR (``DOCKER_HOST=tcp://dind:2375``),
    so the daemon that creates the container lives in a DIFFERENT network
    namespace from the pytest process. A port published by that daemon appears on
    the dind container's address, not on the runner's loopback. Measured on the
    self-hosted runner 2026-08-19: runner 10.89.1.6, dind 10.89.1.2, one shared
    network. So probing 127.0.0.1 from the runner reaches the runner's own
    loopback, where nothing listens — the health wait below could only ever burn
    its full 60 s and skip. That is exactly what CI did on every run.

    Returns the ``DOCKER_HOST`` hostname when it is a TCP URL, else 127.0.0.1
    (unix socket or unset — daemon and caller share the namespace, the ordinary
    local/dev case).
    """
    raw = os.environ.get("DOCKER_HOST", "").strip()
    if not raw.startswith("tcp://"):
        return "127.0.0.1"
    hostport = raw[len("tcp://") :]
    host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
    return host.strip("[]") or "127.0.0.1"


#: Absolute path visible at the SAME absolute path to BOTH this process and the
#: container runtime's daemon.  Set by CI; unset everywhere else.
SHARED_DATA_ROOT_ENV = "YADGAR_TEST_SHARED_DATA_ROOT"


def _provision_data_dir(tmp_path_factory) -> tuple[Path, bool]:
    """Return ``(data_dir, is_shared)`` — where the backend's ``/data`` lives.

    THE BIND-MOUNT HALF OF THE NAMESPACE SPLIT.  ``docker run -v {data_dir}:/data``
    is a string the DAEMON resolves, not this process.  When the daemon is a
    ``docker:dind`` sidecar it resolves ``/tmp/pytest-of-root/pytest-N/...`` inside
    the SIDECAR's filesystem, creates it empty there, and the backend writes its
    whole DB somewhere this process cannot see — while the dir here stays empty and
    ``cmd_vacuum_impl``'s preflight reports ``DB dir not found``.  That is the same
    split :func:`_docker_endpoint_host` fixes for the published port, one layer
    down.  ``docker cp`` is not an answer: the vacuum does ``os.rename`` /
    ``shutil.copytree`` / ``shutil.rmtree`` in-process and its crash-mid-swap
    recovery rests on POSIX rename atomicity, so proxying the filesystem would
    leave the invariant untested while looking tested.

    The fix is a path that means the SAME DIRECTORY on both sides.  CI names one in
    ``YADGAR_TEST_SHARED_DATA_ROOT`` — on the GitHub self-hosted runner the runner
    container and its dind sidecar both bind-mount the host's runner-work dir at
    ``/runner/work``, so anything beneath it is already namespace-invariant, for
    free.  This also closes phase 3's side-build container (``launcher.py``:
    ``-v {side_path.parent}:/data``), which mounts the very same directory.

    Unset → ``tmp_path_factory``, i.e. exactly today's behaviour.  An env var
    rather than sniffing ``DOCKER_HOST``: the coupling to CI's layout is then
    greppable and declared at the call site instead of guessed inside test code,
    and ordinary local dev cannot accidentally take the shared branch.

    ISOLATION — read before "simplifying" this to a fixed directory.  Both vacuum
    e2e tests assert on the ABSENCE of ``surreal_db.old-*`` / ``surreal_db.new-*``
    / ``*.tmp``, so residue from an earlier module (or an earlier CI run on a
    runner whose workspace persists) would fail a later test for a reason that has
    nothing to do with the code under test.  ``mkdtemp`` therefore gives every
    fixture instantiation its own subdirectory — the same one-dir-per-module
    lifetime ``tmp_path_factory.mktemp`` already provided — and the caller removes
    it on teardown, because nothing else will.
    """
    root = os.environ.get(SHARED_DATA_ROOT_ENV, "").strip()
    if not root:
        return tmp_path_factory.mktemp("yadgar_itest_data"), False
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="yadgar_itest_data", dir=base)), True


def _wait_http_200(url: str, timeout: float = 60.0, interval: float = 1.0) -> bool:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.status == 200:
                return True
        except OSError:
            # URLError / HTTPError / socket.timeout while the server boots.
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

    data_dir, data_dir_is_shared = _provision_data_dir(tmp_path_factory)
    # The container runs as user 'yadgar' (non-root); make the data dir world-writable
    # so SurrealKV can create its data files inside the bind-mounted volume.
    data_dir.chmod(0o777)
    port_db = _find_free_port()
    port_embed = _find_free_port()
    container_name = f"yadgar-vacuum-itest-{os.getpid()}"
    endpoint_host = _docker_endpoint_host()
    _bind_prefix = "127.0.0.1:" if endpoint_host == "127.0.0.1" else ""

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
        # Bind host: loopback-only when the daemon is local (keeps a dev box from
        # exposing a test backend on its LAN), but ALL interfaces when the daemon
        # is a remote dind — binding to dind's loopback publishes the port
        # somewhere this process cannot reach. See _docker_endpoint_host().
        "-p",
        f"{_bind_prefix}{port_db}:8000",
        "-p",
        f"{_bind_prefix}{port_embed}:8001",
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
        _backend_image(),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip(f"Failed to start yadgar-backend container: {result.stderr.strip()}")

    backend_url = f"http://{endpoint_host}:{port_db}"
    embed_url = f"http://{endpoint_host}:{port_embed}"

    if not _wait_http_200(f"{backend_url}/health", timeout=60.0):
        # FAIL, do not skip. The container STARTED — `docker run` returned 0 just
        # above — so this is a broken fixture, not an absent capability, and the
        # same rule already applies further down ("If the user never appears the
        # fixture is broken — fail loudly, don't skip"). Skipping here cost three
        # CI rounds: the gate reported only "2 skipped", which is indistinguishable
        # from "no docker on this host", and the container logs that explain it
        # were thrown away with the container.
        logs = subprocess.run(
            [docker_cmd, "logs", "--tail", "50", container_name],
            capture_output=True,
            text=True,
        )
        subprocess.run([docker_cmd, "stop", container_name], capture_output=True)
        subprocess.run([docker_cmd, "rm", "-f", container_name], capture_output=True)
        pytest.fail(
            f"yadgar-backend container did not become healthy within 60 s.\n"
            f"  probed      : {backend_url}/health\n"
            f"  DOCKER_HOST : {os.environ.get('DOCKER_HOST', '(unset)')}\n"
            f"  published as: {_bind_prefix}{port_db}:8000\n"
            f"If DOCKER_HOST names a remote daemon, the port must be published on "
            f"all interfaces and probed at that daemon's host — not 127.0.0.1.\n"
            f"--- container logs (last 50) ---\n{logs.stdout}\n{logs.stderr}"
        )

    yield {
        "backend_url": backend_url,
        "embed_url": embed_url,
        "data_dir": data_dir,
        "surreal_user": "root",
        "surreal_pass": "root",
        "rw_pass": "test123",  # matches YADGAR_RW_PASS env var above
        "ro_pass": "test123",  # matches YADGAR_RO_PASS env var above
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

    # A shared-root dir is OURS to remove — pytest's tmp-path retention policy does
    # not reach outside its own base dir, and the CI runner's workspace survives
    # the job, so leaving it would accumulate whole SurrealDB copies run over run
    # AND leak `surreal_db.old-*` residue into the absence assertions of whatever
    # runs next.  Best-effort: a leftover that cannot be removed must not turn a
    # passing suite red during teardown.
    if data_dir_is_shared:
        shutil.rmtree(data_dir, ignore_errors=True)
