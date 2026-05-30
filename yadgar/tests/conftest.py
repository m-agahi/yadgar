"""Pytest configuration and shared fixtures."""

import hashlib
import os
import socket
import tempfile
import time
import urllib.parse

import pytest

# ---------------------------------------------------------------------------
# Multi-agent session isolation (v5.10.0)
#
# YADGAR_TEST_NAMESPACE=<name>  → redirects TMPDIR to /tmp/pytest-<name>/
#   so concurrent claude sessions don't collide on /tmp/pytest-of-max/.
#
# YADGAR_TEST_PORT_BASE=<int>   → deterministic port range for xdist workers.
#   Formula: base + worker_index * 100 + n  (see _surreal_helpers.allocate_port).
#   Default: 12000.  TEST-ONLY — NOT registered in yadgar production config.
# ---------------------------------------------------------------------------
_ns = os.environ.get("YADGAR_TEST_NAMESPACE", "")
if _ns:
    _ns_tmp = f"/tmp/pytest-{_ns}"
    os.makedirs(_ns_tmp, exist_ok=True)
    os.environ["TMPDIR"] = _ns_tmp
    tempfile.tempdir = _ns_tmp

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


def pytest_sessionfinish(session, exitstatus):
    """Final cleanup — kill any leftover spawned SurrealDB workers.

    Fires unconditionally regardless of exitstatus, including on SIGINT,
    timeout-induced teardown, and pytest-timeout thread unwind.
    Belt-and-suspenders alongside atexit.register in _surreal_helpers.
    """
    from yadgar.tests._surreal_helpers import kill_all_spawned_surreal

    kill_all_spawned_surreal()


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
    Spawn is delegated to _surreal_helpers.spawn_surreal() which registers
    the PID for atexit cleanup (v5.10.0 orphan-reap hardening).
    """
    import shutil

    from yadgar.tests._surreal_helpers import spawn_surreal, teardown_surreal_proc

    if not shutil.which("surreal"):
        yield
        return

    db = tmp_path_factory.mktemp("surreal_data")
    port = _find_free_port()
    proc = spawn_surreal(port=port, data_dir=str(db))

    # Track PID so workers can identify the process in case of cleanup races.
    pid_file = db / "surreal.pid"
    pid_file.write_text(str(proc.pid))

    os.environ["YADGAR_DB_URL"] = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(port)
        yield
    finally:
        teardown_surreal_proc(proc, wait_timeout=5)
        pid_file.unlink(missing_ok=True)
        os.environ.pop("YADGAR_DB_URL", None)


@pytest.fixture(autouse=True)
def _isolate_yaml_config(monkeypatch):
    """Point YADGAR_CONFIG_FILE at a nonexistent path so every test starts from
    true defaults, unaffected by the developer's ~/.yadgar/config.yaml.

    Tests that need a specific yaml file (TestYamlOverride etc.) override
    YADGAR_CONFIG_FILE with their own monkeypatch.setenv — LIFO ordering means
    their value wins inside their test and is restored afterward.
    """
    monkeypatch.setenv("YADGAR_CONFIG_FILE", "/nonexistent/yadgar-test-isolated.yaml")
    try:
        from yadgar.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass
    yield
    try:
        from yadgar.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass


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


@pytest.fixture(scope="session", autouse=True)
def _isolate_surrealdb(surreal_server):
    """Route every StorageEngine to its own SurrealDB database, derived from
    the storage path, by patching _init_schema to swap the surreal-db header
    before any schema work runs.

    Session-scoped so the patch is active before module-scoped fixtures (e.g.
    test_frontier_integration's _engines) create engines.  v4.9 used a
    function-scoped monkeypatch; module-scoped fixtures fired before it
    applied, leaking writes into the shared "main" database.

    Patching _init_schema (rather than __init__) means the header is correct
    on the *first* schema call instead of being re-applied after — avoids
    doubling up on _run_migrations()'s global flock under xdist parallelism.

    In embedded mode (no YADGAR_DB_URL), this fixture is a no-op.
    """
    if not os.environ.get("YADGAR_DB_URL"):
        yield
        return

    from yadgar import storage as _sm

    original_init_schema = _sm.StorageEngine._init_schema

    def _patched_init_schema(self):
        if self._db_url and hasattr(self, "_http"):
            path_hash = hashlib.md5(str(self._db_path).encode()).hexdigest()[:12]
            self._http.headers["surreal-db"] = f"t{path_hash}"
        original_init_schema(self)

    _sm.StorageEngine._init_schema = _patched_init_schema
    try:
        yield
    finally:
        _sm.StorageEngine._init_schema = original_init_schema


@pytest.fixture(autouse=True)
def _reset_server_state():
    """Clear server.py module-level mutable state between tests.

    Module-scoped _engines fixtures call server.init_engines() which populates
    server globals that accumulate across tests in the same module.  Clearing
    them at function teardown prevents leakage between tests within a module
    and between modules that happen to land on the same xdist worker.
    """
    yield
    try:
        from yadgar import server as _s

        _s._action_batch.clear()
        _s._project_roots.clear()
        _s._last_session_context.clear()
        _s._last_prompt_recall.clear()
        _s._last_recalled_ids.clear()
        _s._event_queue.clear()
        _s._detect_branch_cached.cache_clear()
        _s._get_default_branch_cached.cache_clear()
    except Exception:
        pass


_WIPE_TABLES = (
    "memory",
    "wiki_page",
    "wiki_draft",
    "wiki_bookmark",
    "entity",
    "relationship",
    "memory_rule",
)


@pytest.fixture(autouse=True)
def _wipe_surrealdb_data():
    """Delete all rows from data tables after each test (server-mode only).

    Keeps the per-file namespace warm (schema stays) but prevents data written
    by one test from leaking into the next test on the same xdist worker.
    """
    yield
    if not os.environ.get("YADGAR_DB_URL"):
        return
    try:
        from yadgar import server as _s

        storage = _s._storage
        if storage is None:
            return
        for table in _WIPE_TABLES:
            try:
                storage._q(f"DELETE {table};")
            except Exception:
                pass
    except Exception:
        pass


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
