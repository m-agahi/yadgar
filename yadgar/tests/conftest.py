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
# Rerank-model warmup OFF in tests (v5.54.5)
#
# YADGAR_MODEL_PRELOAD=true eagerly loads the CE/NLI/pair cross-encoders
# (~2.5 GB) on EVERY xdist worker at startup — the dominant cause of the
# `-n auto` OOM on a many-core box (23 workers x ~3GB). Tests that genuinely
# need a model still lazy-load it on first use (only on the workers that run
# them); tests exercising warmup itself re-enable via monkeypatch. setdefault
# respects an explicit override.
# ---------------------------------------------------------------------------
os.environ.setdefault("YADGAR_MODEL_PRELOAD", "false")

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


# ---------------------------------------------------------------------------
# RAM-aware xdist worker cap (v5.54.5)
#
# Each worker holds a Python process plus (lazily) ML models and a SurrealDB
# subprocess — up to ~3-4 GB under load. An unguarded `-n auto` on a many-core
# box spawns one worker per core and saturates RAM (a 23-worker run OOM'd a
# 64 GB machine). Clamp the worker count to floor(MemAvailable / 4 GB), both
# for `-n auto` (via pytest_xdist_auto_num_workers) and for an explicit large
# `-n N` (via the clamp in pytest_configure).
# ---------------------------------------------------------------------------
_PER_WORKER_GB = 4


def _available_ram_gb() -> float:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return float("inf")  # unknown → do not clamp


def _ram_safe_workers() -> int:
    return max(1, int(_available_ram_gb() // _PER_WORKER_GB))


def pytest_xdist_auto_num_workers(config):
    """Cap `-n auto` to a RAM-safe worker count (consulted by pytest-xdist)."""
    return _ram_safe_workers()


def _clamp_workers_to_ram(config):
    n = getattr(config.option, "numprocesses", None)
    if not isinstance(n, int) or n <= 1:
        return  # serial, or `auto` handled by pytest_xdist_auto_num_workers
    safe = _ram_safe_workers()
    if n > safe:
        config.option.numprocesses = safe
        import warnings

        warnings.warn(
            f"xdist workers clamped {n}->{safe} to fit ~{_available_ram_gb():.0f}GB "
            f"available RAM (~{_PER_WORKER_GB}GB/worker). Free RAM or lower -n to raise it.",
            stacklevel=2,
        )


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

    # Master-only guardrails (v5.54.5) — skip xdist worker subprocesses, which
    # would otherwise reap each other's live databases.
    if not hasattr(config, "workerinput"):
        from yadgar.tests._surreal_helpers import reap_stale_surreal

        reap_stale_surreal()
        _clamp_workers_to_ram(config)


def pytest_sessionfinish(session, exitstatus):
    """Final cleanup — kill any leftover spawned SurrealDB workers.

    Fires unconditionally regardless of exitstatus, including on SIGINT,
    timeout-induced teardown, and pytest-timeout thread unwind.
    Belt-and-suspenders alongside atexit.register in _surreal_helpers.
    """
    from yadgar.tests._surreal_helpers import kill_all_spawned_surreal

    kill_all_spawned_surreal()


@pytest.fixture(autouse=True)
def isolate_yadgar_paths(tmp_path, monkeypatch):
    """Redirect all yadgar path env vars to tmp_path subdirs.

    Autouse so every test is hermetic — no test writes to real XDG or
    ~/.yadgar directories.  Added v5.47.0 (Phase A2 of XDG migration).

    Tests that need specific paths can override individual env vars via their
    own monkeypatch calls after this fixture runs (function scope wins).
    """
    config_dir = tmp_path / "config" / "yadgar"
    data_dir = tmp_path / "data" / "yadgar"
    state_dir = tmp_path / "state" / "yadgar"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("YADGAR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_dir / "config.yaml"))
    monkeypatch.setenv("YADGAR_LOG_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("YADGAR_CACHE_SNAPSHOT_DIR", str(tmp_path / "embed_cache_snap"))
    monkeypatch.delenv("YADGAR_DB_PATH", raising=False)
    # #74 fix #1: the readiness anti-flap counter (server.http) is module-global;
    # reset it per test so a prior test's failing /health probes can't leak a
    # latent-503 into an unrelated test.
    try:
        import yadgar.server.http as _srv_http  # noqa: PLC0415

        _srv_http._reset_readiness_state()
    except Exception:  # noqa: BLE001 — never block a test on this defensive reset
        pass


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


# Cap on session respawns before we stop masking and fail loudly.  A handful of
# OOM-kills under memory pressure is recoverable; a server that dies repeatedly
# signals genuine infra instability that should surface, not be papered over.
_MAX_SURREAL_RESPAWNS = 8


def _ensure_surreal_alive(handle: dict) -> bool:
    """Respawn the session SurrealDB subprocess if it died, on the SAME port.

    Pure respawn+health primitive (no session-registry side effects — the caller
    owns those).  Reusing the port keeps ``YADGAR_DB_URL`` valid so existing
    httpx clients reconnect transparently once the server is back.  A SIGKILL'd
    surrealkv store is likely locked/corrupt, so we spawn against a fresh data
    dir rather than reusing the old one.

    Returns True if a respawn occurred, False if the process was already alive.
    Raises RuntimeError once ``_MAX_SURREAL_RESPAWNS`` is exceeded so chronic
    death surfaces instead of becoming a silent ConnectError cascade.
    """
    import tempfile

    from yadgar.tests._surreal_helpers import spawn_surreal

    if handle["proc"].poll() is None:
        return False  # alive

    handle["respawns"] += 1
    if handle["respawns"] > _MAX_SURREAL_RESPAWNS:
        raise RuntimeError(
            f"SurrealDB test server died and was respawned "
            f"{handle['respawns'] - 1}× (cap {_MAX_SURREAL_RESPAWNS}) — infra "
            "unstable; aborting to avoid masking real failures as a cascade."
        )

    new_dir = tempfile.mkdtemp(prefix="surreal_respawn_")
    handle["proc"] = spawn_surreal(port=handle["port"], data_dir=new_dir)
    handle["data_dir"] = new_dir
    _wait_for_health(handle["port"])
    return True


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
    # Mutable handle so the function-scoped _surreal_liveness gate can respawn a
    # dead server in place (same port) and update the proc reference here.
    handle = {"proc": proc, "port": port, "data_dir": str(db), "respawns": 0}
    try:
        _wait_for_health(port)
        yield handle
    finally:
        teardown_surreal_proc(handle["proc"], wait_timeout=5)
        pid_file.unlink(missing_ok=True)
        os.environ.pop("YADGAR_DB_URL", None)


@pytest.fixture(autouse=True)
def _surreal_liveness(surreal_server):
    """Respawn a dead SurrealDB server before each test (server-mode only).

    Converts the failure mode where one xdist worker's surreal dies mid-run and
    every subsequent test ERRORs with ConnectError (the session-wide cascade)
    into transparent recovery.  Partial by design: tests later in a module whose
    surreal died mid-module still fail, because their module-scoped ``_engines``
    fixture populated the ``server._storage`` singleton against the now-wiped DB
    and won't re-run ``_init_schema``.  It bounds the blast radius to the current
    module instead of the whole session.
    """
    handle = surreal_server
    if handle is None or not os.environ.get("YADGAR_DB_URL"):
        yield
        return
    if _ensure_surreal_alive(handle):
        # Respawn yields a fresh, empty DB — drop the stale namespace registry so
        # the wipe fixture and _init_schema rebuild namespaces on next engine init.
        _USED_SURREAL_NAMESPACES.clear()
    yield


def _replace_module_binding(mod, canonical_gs) -> None:
    """Swap a module's get_settings binding to canonical and clear any stale cache.

    Separated out to keep _resync_get_settings_bindings nesting ≤ 4 (I13 HARD cap).
    """
    gs = mod.__dict__.get("get_settings")
    if gs is None or not callable(getattr(gs, "cache_clear", None)):
        return
    if gs is not canonical_gs:
        try:
            gs.cache_clear()
        except Exception:
            pass
        try:
            mod.__dict__["get_settings"] = canonical_gs
        except Exception:
            pass


def _resync_get_settings_bindings():
    """Re-point every module's get_settings binding to the current yadgar.config.get_settings.

    importlib.reload(yadgar.config) replaces yadgar.config.get_settings with a NEW
    lru_cache function.  Modules that imported get_settings via
    ``from yadgar.config import get_settings`` still hold a reference to the OLD
    function.  When a fixture (e.g. _engines/init_engines) populates the OLD
    function's cache, and a test only calls ``get_settings.cache_clear()`` on the
    NEW function, the OLD cache survives.  Subsequent calls from admin_other,
    audit, lifecycle, etc. all see the stale Settings object (wrong THRESHOLD or
    CONSOLIDATION flag).

    This helper walks sys.modules, finds every yadgar module that has a stale (old)
    get_settings attribute, and replaces it with the current canonical function.
    It also calls cache_clear() on every distinct get_settings function found.

    Root-D xdist pollution fix (v5.56): test_config_yaml_container_path reloads
    yadgar.config; subsequent anchor-audit tests then see stale Settings with
    ANCHOR_AUDIT_THRESHOLD=15 (init_engines default) overriding the test's
    THRESHOLD=0/2 env var, so consolidate_now skips and writes no sentinel.
    """
    import sys

    try:
        import yadgar.config as _cfg

        canonical_gs = _cfg.get_settings
    except Exception:
        return

    # v5.58 fix: if canonical_gs is a plain monkeypatched function (no cache_clear),
    # bail out entirely.  Propagating a plain fn into submodule __dict__ would leak
    # the monkeypatch to the next test on the worker.  pytest's monkeypatch teardown
    # restores yadgar.config.get_settings to the real lru_cache after this fixture
    # yields; the next test's autouse setup resync then runs with a real canonical.
    if not callable(getattr(canonical_gs, "cache_clear", None)):
        return

    canonical_gs.cache_clear()

    for mod_name, mod in list(sys.modules.items()):
        # Only scan yadgar modules; scanning all sys.modules modules triggers
        # third-party module-level imports (e.g. HuggingFace transformers has a
        # 'get_settings' compat shim that imports torchvision on attribute access).
        if not mod_name.startswith("yadgar") or mod is None:
            continue
        # Use mod.__dict__ to avoid triggering __getattr__ shims.
        _replace_module_binding(mod, canonical_gs)


@pytest.fixture(autouse=True)
def _isolate_yaml_config(monkeypatch):
    """Point YADGAR_CONFIG_FILE at a nonexistent path so every test starts from
    true defaults, unaffected by the developer's ~/.yadgar/config.yaml.

    Tests that need a specific yaml file (TestYamlOverride etc.) override
    YADGAR_CONFIG_FILE with their own monkeypatch.setenv — LIFO ordering means
    their value wins inside their test and is restored afterward.

    Also re-syncs all get_settings bindings across sys.modules at both setup and
    teardown.  importlib.reload(yadgar.config) (used by test_config_yaml_container_path)
    creates a new get_settings lru_cache function; modules that captured the old
    reference otherwise keep stale Settings objects across tests on the same xdist
    worker (Root-D pollution — v5.56 fix).
    """
    monkeypatch.setenv("YADGAR_CONFIG_FILE", "/nonexistent/yadgar-test-isolated.yaml")
    _resync_get_settings_bindings()
    yield
    _resync_get_settings_bindings()


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
            ns = f"t{path_hash}"
            self._http.headers["surreal-db"] = ns
            # Register so _wipe_surrealdb_data can clean up even after shutdown.
            _USED_SURREAL_NAMESPACES.add(ns)
        original_init_schema(self)

    _sm.StorageEngine._init_schema = _patched_init_schema
    try:
        yield
    finally:
        _sm.StorageEngine._init_schema = original_init_schema


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Snapshot and restore global logging state after each test.

    Restores:
    - logging.root.manager.disable — set by logging.disable(CRITICAL) in
      cli/_shared.py::init_replay_lightweight(); persists for the worker process
      lifetime, silencing all subsequent logging (Root-B/C xdist pollution).
    - logging.root.level — set by test_tracing.py and others; a raised root level
      blocks records from reaching handlers even when disable is not set.
      (Root-C: test_uvicorn_access_emits_json sees empty output when root level
      is left at WARNING or CRITICAL by a prior test — v5.56 fix.)

    Note: uvicorn.* logger propagate flags are restored by source fixes in
    test_graceful_shutdown.py::test_uvicorn_abandons_hanging_request_within_budget
    rather than here — snapshotting them in the fixture is circular when a prior
    test already left them in the bad state.
    """
    import logging

    saved_disable = logging.root.manager.disable
    saved_root_level = logging.root.level
    saved_root_handlers = list(logging.root.handlers)
    yield
    logging.disable(saved_disable)
    logging.root.setLevel(saved_root_level)
    # Remove any handlers added during the test; close them to release file locks.
    # test_graceful_shutdown.py reloads yadgar.server._app which calls configure_logging()
    # and installs a RotatingJSONLFileHandler on root.  Without cleanup, _install_file_handler's
    # idempotency check bails early for subsequent tests, leaving the stale handler.
    # test_opt_out_empty_path_no_file_handler expects 0 file handlers but sees 1
    # (Root-E xdist pollution — v5.56 fix).
    for h in list(logging.root.handlers):
        if h not in saved_root_handlers:
            logging.root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _restore_mcp_server():
    """Snapshot and restore the mcp_server singleton after each test.

    Tests in test_graceful_shutdown.py reload yadgar.server._app, replacing the
    FastMCP instance at _app.mcp_server with a fresh (empty) one.  Tests in
    test_security_headers.py then reload yadgar.server, propagating the empty
    instance into server.mcp_server.  Later tests on the same xdist worker see
    an empty route/tool registry — causing 404s, missing tools, and missing /health.

    This backstop restores both binding points after every test so reload-based
    polluters cannot corrupt the singleton for subsequent tests.  (Root-A xdist
    pollution — v5.56 fix.  Source fixes in test_graceful_shutdown.py and
    test_security_headers.py are the primary fix; this is belt-and-suspenders.)
    """
    try:
        import yadgar.server as _srv
        import yadgar.server._app as _app

        saved_app_mcp = _app.mcp_server
        saved_srv_mcp = _srv.__dict__.get("mcp_server")
    except Exception:
        yield
        return
    yield
    try:
        import yadgar.server as _srv
        import yadgar.server._app as _app

        _app.mcp_server = saved_app_mcp
        if saved_srv_mcp is not None:
            _srv.__dict__["mcp_server"] = saved_srv_mcp
    except Exception:
        pass


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
    "checkpoint",
    "action_log",
    "episode",
    "memory_archive",
    "memory_similarity_link",
    "memory_block",
    "prospective_memory",
    "narrative_entry",
    "consolidation_log",
)

# Worker-local set of all SurrealDB database namespaces used so far on this
# xdist worker.  Populated by `_isolate_surrealdb`'s patch — every time a
# StorageEngine runs _init_schema it registers its per-path namespace here.
# `_wipe_surrealdb_data` uses this set to wipe ALL known namespaces (not just
# the live _st._storage one) so tests that call server.shutdown() before the
# wipe fixture runs are still cleaned up.  "main" is always wiped in addition
# to catch CLI-subprocess writes that bypass the isolation patch.
_USED_SURREAL_NAMESPACES: set[str] = set()


def _wipe_namespace_via_http(db_url: str, db_name: str) -> None:
    """DELETE all rows in _WIPE_TABLES in the given SurrealDB namespace.

    Creates a short-lived httpx.Client so it works even after the test's
    StorageEngine has been closed.  Errors per-table are silently ignored —
    the table may not exist yet in the namespace (empty namespace is fine).
    """
    import base64

    user = os.environ.get("YADGAR_DB_USER", "root")
    pw = os.environ.get("YADGAR_DB_PASS", "root")
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    _wipe_tables_with_client(db_url, db_name, auth)


def _wipe_tables_with_client(db_url: str, db_name: str, auth: str) -> None:
    """Issue DELETE for each wipe table using a short-lived httpx client."""
    import httpx

    try:
        client = httpx.Client(
            base_url=db_url,
            headers={
                "Authorization": f"Basic {auth}",
                "surreal-ns": "yadgar",
                "surreal-db": db_name,
                "Accept": "application/json",
            },
            timeout=5.0,
        )
    except Exception:
        return
    try:
        for table in _WIPE_TABLES:
            _delete_table_safe(client, table)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _delete_table_safe(client, table: str) -> None:
    """POST DELETE for one table; ignore all errors (table may not exist)."""
    try:
        client.post("/sql", content=f"DELETE {table};")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _wipe_surrealdb_data():
    """Delete all rows from data tables after each test (server-mode only).

    Keeps the per-file namespace warm (schema stays) but prevents data written
    by one test from leaking into the next test on the same xdist worker.

    Robust fallback (v5.56 iteration 2): when `_st._storage` is None because
    the test called `server.shutdown()` before this fixture's teardown runs,
    fall back to per-namespace HTTP wipes using `_USED_SURREAL_NAMESPACES`.
    Also always wipes the "main" namespace to clean up CLI-subprocess tests
    (subprocesses inherit YADGAR_DB_URL but not the _isolate_surrealdb patch,
    so their writes land in "main" and are never reached by the live storage).

    Snapshot guard (v5.56 iteration 3): snapshot ``_USED_SURREAL_NAMESPACES``
    at test-setup time (after all longer-scoped fixtures have run their setup).
    The HTTP fallback only wipes namespaces that are *new* since the snapshot —
    i.e. namespaces registered during the test body itself.  Namespaces
    belonging to module- or session-scoped fixtures are already in the snapshot
    and are therefore excluded, so their data survives across function-scoped
    tests (fixing the regression where the module-scoped characterization corpus
    was wiped after the first test in the module, causing tests 1-9 to see an
    empty DB).
    """
    # Snapshot before the test body runs — module/session fixtures have already
    # registered their namespaces via _patched_init_schema at this point.
    pre_test_namespaces: frozenset[str] = frozenset(_USED_SURREAL_NAMESPACES)
    yield
    db_url = os.environ.get("YADGAR_DB_URL")
    if not db_url:
        return
    _do_wipe_after_test(db_url, pre_test_namespaces)


def _do_wipe_after_test(db_url: str, pre_test_namespaces: frozenset[str]) -> None:
    """Wipe all data tables after a test, tolerating server.shutdown() having run."""
    try:
        from yadgar import server as _s

        storage = _s._storage
    except Exception:
        storage = None

    if storage is not None:
        _wipe_via_live_storage(storage, db_url)
    else:
        _wipe_via_http_fallback(db_url, pre_test_namespaces)


def _wipe_via_live_storage(storage, db_url: str) -> None:
    """Fast path: wipe via the live StorageEngine (namespace already set on client)."""
    for table in _WIPE_TABLES:
        try:
            storage._q(f"DELETE {table};")
        except Exception:
            pass
    # Always wipe "main" — CLI subprocesses write there bypassing isolation patch.
    try:
        _wipe_namespace_via_http(db_url, "main")
    except Exception:
        pass


def _wipe_via_http_fallback(db_url: str, pre_test_namespaces: frozenset[str]) -> None:
    """Slow path: storage shut down. Wipe only test-local namespaces + 'main' via HTTP.

    Only namespaces that are *new* since the pre-test snapshot are wiped —
    namespaces from module- or session-scoped fixtures are excluded so their
    data survives across function-scoped tests on the same xdist worker.
    """
    for ns in list(_USED_SURREAL_NAMESPACES):
        if ns in pre_test_namespaces:
            # Namespace existed before this test — belongs to a longer-scoped
            # fixture; do not wipe it.
            continue
        try:
            _wipe_namespace_via_http(db_url, ns)
        except Exception:
            pass
    try:
        _wipe_namespace_via_http(db_url, "main")
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
