"""Pytest configuration and shared fixtures."""

import hashlib
import os
import socket
import subprocess
import time

import pytest


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(port: int, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"SurrealDB did not start on port {port}")


@pytest.fixture(scope="session", autouse=True)
def surreal_server(tmp_path_factory):
    """Start a real SurrealDB HTTP server for the test session.

    Falls back to embedded mode if the `surreal` binary is not on PATH.
    """
    import shutil

    if not shutil.which("surreal"):
        yield
        return

    db = tmp_path_factory.mktemp("surreal_data")
    port = _find_free_port()
    proc = subprocess.Popen(
        [
            "surreal",
            "start",
            "--no-banner",
            "--bind",
            f"127.0.0.1:{port}",
            "--user",
            "root",
            "--pass",
            "root",
            f"surrealkv://{db}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["YADGAR_DB_URL"] = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(port)
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.environ.pop("YADGAR_DB_URL", None)


@pytest.fixture(autouse=True)
def _isolate_surrealdb(monkeypatch):
    """Give each test its own SurrealDB database to prevent state leakage.

    In server mode all StorageEngine instances connect to the same SurrealDB
    process. Without isolation, data inserted by one test leaks into the next.

    Strategy: derive a deterministic database name from the storage path so that:
    - two engines opened on the same path share one database (intended sharing)
    - engines opened on different tmp_path values get separate databases

    In embedded mode (no YADGAR_DB_URL), this fixture is a no-op.
    """
    if not os.environ.get("YADGAR_DB_URL"):
        return

    from yadgar import storage as _sm

    original_init = _sm.StorageEngine.__init__

    def _patched_init(self, db_path, **kwargs):
        original_init(self, db_path, **kwargs)
        if self._db_url and hasattr(self, "_http"):
            path_hash = hashlib.md5(str(db_path).encode()).hexdigest()[:12]
            self._http.headers["surreal-db"] = f"t{path_hash}"
            self._init_schema()  # create tables/indexes in the isolated DB

    monkeypatch.setattr(_sm.StorageEngine, "__init__", _patched_init)
