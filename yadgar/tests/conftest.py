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
def _isolate_file_queue(tmp_path, monkeypatch):
    """Give each test its own file queue directory so queue items from one test
    cannot leak into another test's drain pass.

    Sets YADGAR_DATA_DIR to a per-test tmp path and resets the global file queue
    / drainer so they reinitialise lazily using the new path.
    """
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "yadgar_data"))
    # Reset lazy globals so the new data dir is picked up
    from yadgar import server as _s

    monkeypatch.setattr(_s, "_file_queue", None)
    monkeypatch.setattr(_s, "_queue_drainer", None)
    yield
    # After the test, stop and clear the drainer so it doesn't bleed into teardown
    if _s._queue_drainer is not None:
        _s._queue_drainer.stop()
    monkeypatch.setattr(_s, "_file_queue", None)
    monkeypatch.setattr(_s, "_queue_drainer", None)


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


@pytest.fixture(scope="session")
def embeddings():
    """Session-scoped EmbeddingEngine instance.

    The model weights are cached at the class level anyway, so sharing one
    instance across tests in a worker process is safe and avoids redundant
    constructor overhead.
    """
    from yadgar.embeddings import EmbeddingEngine

    return EmbeddingEngine("all-MiniLM-L6-v2")


@pytest.fixture
def flush_queue():
    """Force the QueueDrainer to drain before continuing — for tests that do
    `memorize() → recall()` in the same test and rely on the drainer to flush."""

    def _flush():
        from yadgar import server as _s

        if _s._queue_drainer is not None:
            _s._queue_drainer.drain_now()

    return _flush


def memorize_sync(content: str, context: str, tags: list, **kwargs) -> dict:
    """Call memorize(), flush the queue, then return the stored memory dict with 'id'.

    Drop-in replacement for tests that previously relied on memorize() returning
    the full memory dict synchronously. After v4.4 the fast path returns
    {stored, queued, queue_id}; this helper flushes the drainer and fetches
    the memory so callers get a dict with 'id', 'content', 'heat', etc.
    """
    from yadgar import server as _s

    result = _s.memorize(content, context, tags, **kwargs)
    # Early-reject paths return synchronously without queuing
    if not result.get("queued"):
        return result
    # Flush the drainer so the memory is actually in the DB
    if _s._queue_drainer is not None:
        _s._queue_drainer.drain_now()
    # Retrieve the just-stored memory by exact content match.
    # First try FTS (fast), then fall back to a full content scan (reliable).
    storage = _s._get_storage()
    try:
        rows = storage.search_memories_fts(content[:100], min_heat=0.0, limit=20)
        for row in rows:
            if row.get("content") == content and row.get("directory_context") == context:
                row.pop("embedding", None)
                return row
    except Exception:
        pass
    # FTS may miss content with special chars or no tokenised match — fall back to
    # a recent hot-memories scan with exact content comparison.
    try:
        recent = storage.get_memories_by_heat(min_heat=0.0, limit=100)
        for row in recent:
            if row.get("content") == content and row.get("directory_context") == context:
                row.pop("embedding", None)
                return row
    except Exception:
        pass
    # Fallback: return the queued response if we can't find it
    return result
