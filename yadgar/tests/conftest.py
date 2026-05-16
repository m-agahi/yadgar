"""Pytest configuration and shared fixtures."""

import hashlib
import os
import socket
import subprocess
import time
import urllib.parse

import pytest

# ---------------------------------------------------------------------------
# Credentials escape hatch — set before any module-level import of yadgar.
#
# v5.0 hardens credentials: storage.py raises KeyError when YADGAR_DB_PASS
# is unset unless YADGAR_ALLOW_ROOT=1. Set both here so the full test suite
# continues to work against the isolated test SurrealDB started by the
# surreal_server fixture (which uses root:root for simplicity).
# ---------------------------------------------------------------------------
if "YADGAR_ALLOW_ROOT" not in os.environ:
    os.environ["YADGAR_ALLOW_ROOT"] = "1"
os.environ.setdefault("YADGAR_DB_PASS", "root")
os.environ.setdefault("YADGAR_DB_USER", "root")

# ---------------------------------------------------------------------------
# Production-DB isolation guard
#
# Prevent tests from accidentally writing to a live production SurrealDB.
# Fires at collection time (pytest_configure) when YADGAR_DB_URL parses to
# a production-looking endpoint and YADGAR_TEST is not set to a truthy value.
#
# Heuristic: parse YADGAR_DB_URL with urllib.parse.urlsplit; treat the URL
# as production iff hostname (case-insensitive) is one of the forbidden
# hosts AND port == 8000 (the default production daemon port).
#
# Forbidden hosts: 127.0.0.1, localhost, 0.0.0.0, ::1, yadgar-backend.
#
# Bypass: set YADGAR_TEST to one of {"1", "true", "yes", "on"} (case-
# insensitive). Any other value (including empty) is treated as not-set.
#
# On trip, the guard calls pytest.exit(..., returncode=78) — sysexits.h
# EX_CONFIG, signalling a configuration error distinct from test failure (1)
# or pytest's own usage error (2).
#
# How CI avoids the guard: the CI runner starts with YADGAR_DB_URL unset;
# the session-scoped `surreal_server` fixture sets it to a random free port
# after collection completes, so the guard never fires.
#
# How to run locally against a test URL: set YADGAR_TEST=1 (or true/yes/on)
# alongside any YADGAR_DB_URL, or leave YADGAR_DB_URL unset (the fixture
# will start its own SurrealDB instance).
# ---------------------------------------------------------------------------

_FORBIDDEN_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1", "yadgar-backend"})
_FORBIDDEN_PORT = 8000
_YADGAR_TEST_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_production_url(db_url: str) -> bool:
    if not db_url:
        return False
    try:
        parsed = urllib.parse.urlsplit(db_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port  # lazy property; raises ValueError on out-of-range ports
    except ValueError:
        return False
    return host in _FORBIDDEN_HOSTS and port == _FORBIDDEN_PORT


def pytest_configure(config):
    db_url = os.environ.get("YADGAR_DB_URL", "")
    yadgar_test = os.environ.get("YADGAR_TEST", "").lower()
    if _is_production_url(db_url) and yadgar_test not in _YADGAR_TEST_TRUTHY:
        pytest.exit(
            f"YADGAR_DB_URL={db_url!r} resolves to a production endpoint "
            f"(host in {sorted(_FORBIDDEN_HOSTS)}, port {_FORBIDDEN_PORT}). "
            "Refusing to run tests against the live DB. "
            "Set YADGAR_TEST=1 (or true/yes/on) to override, or unset "
            "YADGAR_DB_URL to let the test suite start its own isolated SurrealDB.",
            returncode=78,
        )


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
    # Track PID so workers can identify the process in case of cleanup races.
    pid_file = db / "surreal.pid"
    pid_file.write_text(str(proc.pid))

    os.environ["YADGAR_DB_URL"] = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(port)
        yield
    finally:
        # Explicit kill: terminate first, escalate to SIGKILL if needed.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        pid_file.unlink(missing_ok=True)
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
    # Merge case: curator may have merged new content into an existing memory,
    # changing stored content to "<existing>\n<new>".  Scan for a memory in the
    # same directory whose content *contains* the original string so callers
    # that test deduplication still get a dict with 'id'.
    try:
        recent = storage.get_memories_by_heat(min_heat=0.0, limit=100)
        for row in recent:
            stored = row.get("content", "")
            if content in stored and row.get("directory_context") == context:
                row.pop("embedding", None)
                return row
    except Exception:
        pass
    # Fallback: return the queued response if we can't find it
    return result
