"""Pytest configuration and shared fixtures."""

import hashlib
import os
import socket
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Hook-install real-HOME sentinel (#64)
#
# Capture the developer's REAL ~/.claude/hooks/ contents at conftest IMPORT time
# — BEFORE any fixture redirects HOME. This is the tripwire that catches a test
# which bypasses the HOME-isolation guard (session/function fixtures above) and
# mutates the real hooks dir in place (the actual 2026-07-13 incident: a partial
# install unlinked ~/.claude/hooks/yadgar-db-lockdown-check.py, breaking Bash).
#
# A dir-mtime check on ~/.claude would MISS a child-file unlink/overwrite one
# level down, so snapshot every entry directly under hooks/ as (name, size, mtime).
# Guarded on exists() so CI (no ~/.claude) is a no-op. Read-only stat — the
# sentinel NEVER writes to the real dir. Under xdist the snapshot is per-worker;
# any worker that mutates real HOME trips its own snapshot.
# ---------------------------------------------------------------------------
_REAL_HOME_HOOKS_DIR: Path | None = None
_REAL_HOOKS_SENTINEL: frozenset[tuple[str, int, int]] | None = None


def _snapshot_hooks_dir(hooks_dir: Path) -> frozenset[tuple[str, int, int]]:
    """(name, st_size, st_mtime_ns) for every direct child of *hooks_dir*."""
    entries: set[tuple[str, int, int]] = set()
    try:
        for child in hooks_dir.iterdir():
            try:
                st = child.stat()
            except OSError:
                continue
            entries.add((child.name, st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    return frozenset(entries)


_real_home_env = os.environ.get("HOME", "")
if _real_home_env:
    _candidate = Path(_real_home_env) / ".claude" / "hooks"
    if _candidate.exists():
        _REAL_HOME_HOOKS_DIR = _candidate
        _REAL_HOOKS_SENTINEL = _snapshot_hooks_dir(_candidate)

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
# the session-scoped `_surreal_url_reserve` fixture sets it to a random free
# port after collection completes, so the guard never fires.
#
# How to run locally against a test URL: set YADGAR_TEST=1 (or true/yes/on)
# alongside any YADGAR_DB_URL, or leave YADGAR_DB_URL unset (the harness
# will lazily start its own SurrealDB instance on first DB demand).
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


@pytest.fixture(scope="session", autouse=True)
def _isolate_yadgar_paths_session(tmp_path_factory):
    """Session-scoped hermetic env, active BEFORE any module-scoped fixture.

    v5.101 (module-scope P1): the function-scoped ``isolate_yadgar_paths`` below
    redirects config/XDG/data dirs to a per-test tmp dir, but a **module-scoped**
    ``_engines`` fixture (which calls ``init_engines()`` once per file) runs at
    module setup — BEFORE any function-scoped fixture.  Without a session-scoped
    guard, that ``init_engines()`` reads the developer's real
    ``~/.config/yadgar/config.yaml`` (e.g. ``offload_tools: true`` → the
    lifecycle RuntimeError) and writes to the real ``~/.local/share/yadgar``.

    This session fixture establishes the same hermetic redirection at session
    start using ``pytest.MonkeyPatch()`` (the function-scoped ``monkeypatch``
    cannot be requested from session scope — ScopeMismatch) so module-scoped
    engine init sees an isolated, offload-disabled environment.  The
    function-scoped ``isolate_yadgar_paths`` still re-applies per-test dirs on
    top (function scope wins), so per-test hermeticity is unchanged.

    Mirrors the session-scoping rationale of ``_isolate_surrealdb`` (which is
    session-scoped precisely so its patch precedes module-scoped ``_engines``).
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    root = tmp_path_factory.mktemp("session_yadgar_paths")
    config_dir = root / "config" / "yadgar"
    data_dir = root / "data" / "yadgar"
    state_dir = root / "state" / "yadgar"
    for d in (config_dir, data_dir, state_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Hook-install HOME isolation (#64): redirect HOME to a session tmp dir so any
    # code resolving `Path.home()` / `expanduser("~")` at MODULE setup (before a
    # function-scoped fixture applies) — notably the install_hooks MCP wrapper and
    # CLI which hardcode `home_dir=Path.home()` — can NEVER write to or unlink
    # inside the developer's real `~/.claude/hooks`. The function-scoped fixture
    # re-applies a per-test HOME on top (function scope wins). Env-patch alone
    # suffices: on POSIX both `Path.home()` and `expanduser("~")` read $HOME.
    session_home = root / "home"
    (session_home / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    mp.setenv("HOME", str(session_home))
    mp.setenv("XDG_CONFIG_HOME", str(root / "config"))
    mp.setenv("XDG_DATA_HOME", str(root / "data"))
    mp.setenv("XDG_STATE_HOME", str(root / "state"))
    mp.setenv("YADGAR_DATA_DIR", str(data_dir))
    mp.setenv("YADGAR_CONFIG_FILE", str(config_dir / "config.yaml"))
    mp.setenv("YADGAR_LOG_DIR", str(data_dir / "logs"))
    mp.setenv("YADGAR_CACHE_SNAPSHOT_DIR", str(root / "embed_cache_snap"))
    mp.delenv("YADGAR_DB_PATH", raising=False)
    # Disable tool-body offload for module-scoped engine init: with no
    # YADGAR_EMBED_URL the local-engine offload path raises in lifecycle.py.
    # Offload-specific tests re-enable it per-function (function scope wins).
    mp.setenv("YADGAR_OFFLOAD_TOOLS", "0")
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="session", autouse=True)
def _hooks_dir_sentinel():
    """Tripwire: fail the session loudly if the real ~/.claude/hooks/ was mutated.

    Belt-and-suspenders behind the HOME-isolation guard (#64). The snapshot is
    taken at conftest import (before any HOME redirect). At session teardown we
    re-stat the real dir and assert the entry set is unchanged — a drift means a
    test leaked past the HOME guard and wrote/unlinked inside the developer's
    real hooks dir. No-op when the real dir doesn't exist (CI).
    """
    yield
    if _REAL_HOME_HOOKS_DIR is None or _REAL_HOOKS_SENTINEL is None:
        return
    after = _snapshot_hooks_dir(_REAL_HOME_HOOKS_DIR)
    if after != _REAL_HOOKS_SENTINEL:
        removed = {e[0] for e in _REAL_HOOKS_SENTINEL} - {e[0] for e in after}
        added = {e[0] for e in after} - {e[0] for e in _REAL_HOOKS_SENTINEL}
        changed = {e[0] for e in (_REAL_HOOKS_SENTINEL ^ after)} - removed - added
        raise AssertionError(
            "HOOK-INSTALL LEAK (#64): the real ~/.claude/hooks/ was mutated during "
            f"the test session (dir={_REAL_HOME_HOOKS_DIR}). "
            f"removed={sorted(removed)} added={sorted(added)} changed={sorted(changed)}. "
            "A test bypassed the HOME-isolation guard — patch HOME to a tmp dir."
        )


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
    # Hook-install HOME isolation (#64): per-test HOME on top of the session
    # default (function scope wins). Any test that calls the install_hooks
    # MCP/CLI wrapper (which hardcodes `home_dir=Path.home()`) or `sync_instructions`
    # without its own HOME patch now writes under this tmp HOME — never real
    # `~/.claude/hooks`. The session sentinel (below) is the tripwire if a test
    # bypasses this redirect. A distinct dir name (`_guard_home`, NOT `home`) so
    # tests that build their own explicit `tmp_path / "home"` as a home_dir param
    # don't collide with this fixture's pre-created tree.
    guard_home = tmp_path / "_guard_home"
    (guard_home / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(guard_home))
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
        import yadgar.core.server.http as _srv_http  # noqa: PLC0415

        _srv_http._reset_readiness_state()
    except Exception:  # noqa: BLE001 — never block a test on this defensive reset
        pass
    # Clear the process-lifetime epoch-keyed read-tool caches (Car 1/Car 2). The
    # per-test YADGAR_DATA_DIR above restarts the on-disk epoch counters from 0,
    # so each test replays the SAME deterministic bump sequence — cache keys from
    # a PREVIOUS test collide exactly with the current test's keys and the cache
    # serves the previous test's payload (stale brief/wiki/prelude). Epoch-in-key
    # is correct in prod (one persistent data dir) but decorative under per-test
    # tmp dirs; the caches must start empty alongside the fresh DB + data dir.
    for _mod_name, _attr in (
        ("yadgar.core.server.tools.project", "_project_brief_cache"),
        ("yadgar.core.server.tools.wiki", "_wiki_read_cache"),
        ("yadgar.core.server.tools.wiki", "_wiki_query_cache"),
        ("yadgar.core.server.tools.dispatch_helper", "_prompt_cache"),
    ):
        try:
            import importlib  # noqa: PLC0415

            _cache = getattr(importlib.import_module(_mod_name), _attr, None)
            if _cache is not None:
                _cache.clear()
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

# ---------------------------------------------------------------------------
# Car 2 (test-suite hardening): LAZY session SurrealDB
#
# Pre-Car-2 the session `surreal_server` fixture was autouse: EVERY xdist
# worker spawned a SurrealDB subprocess (~300MB) at its first test regardless
# of whether any test on that worker touched the DB — the dominant per-worker
# RAM floor alongside the eager embedding-model warmup (see
# `_defer_embed_warmup_in_init_engines` below).
#
# New shape (request-scoped demand, no hand-markers):
#   1. `_surreal_url_reserve` (session, autouse) reserves a free port and sets
#      YADGAR_DB_URL WITHOUT spawning.  Env-dependent behavior (StorageEngine
#      mode selection, guards, CLI subprocess inheritance) is unchanged from
#      the eager era — only the subprocess itself is deferred.
#   2. `_ensure_surreal_spawned()` spawns on FIRST demand (idempotent).
#      Demand points:
#        * `_isolate_surrealdb`'s `_patched_init_schema` — fires on the first
#          StorageEngine construction against the reserved URL, wherever it
#          happens (module fixtures, local fixtures, test bodies).  A test's
#          needs derive from what it (transitively) constructs.
#        * the `surreal_server` fixture — tests whose DB demand lives in a
#          CHILD process (CLI subprocess tests) request it explicitly.
#   3. Logic-only tests never trigger either point: no spawn, no per-test
#      HTTP wipe, no liveness poll.
#
# Embedded-mode tests (monkeypatch.delenv YADGAR_DB_URL) are unaffected: their
# engines see no URL, `_patched_init_schema` sees `self._db_url is None`, and
# no spawn fires for them.
# ---------------------------------------------------------------------------

# Handle for the lazily-spawned session server: {proc, port, data_dir, respawns}.
# None until the first DB demand (or forever, on logic-only workers).
_SURREAL_HANDLE: dict | None = None

# Port reserved by `_surreal_url_reserve` (None → embedded mode or external URL).
_SURREAL_RESERVED_PORT: int | None = None

# YADGAR_DB_URL that was already present at conftest import (user-provided
# external server, e.g. YADGAR_TEST=1 runs).  We never spawn/teardown for it,
# but per-test wipes stay active against it.
_EXTERNAL_DB_URL: str | None = os.environ.get("YADGAR_DB_URL") or None

# Guards concurrent spawn attempts from engine constructions on worker threads.
_SURREAL_SPAWN_LOCK = threading.Lock()


def _ensure_surreal_spawned() -> dict | None:
    """Spawn the session SurrealDB on first demand (idempotent, per worker).

    Returns the live handle, or None when no port was reserved (no `surreal`
    binary → embedded mode; or an external YADGAR_DB_URL is in charge).
    """
    global _SURREAL_HANDLE
    if _SURREAL_HANDLE is not None:
        return _SURREAL_HANDLE
    if _SURREAL_RESERVED_PORT is None:
        return None
    with _SURREAL_SPAWN_LOCK:
        if _SURREAL_HANDLE is not None:  # lost the race — already spawned
            return _SURREAL_HANDLE
        import tempfile

        from yadgar.tests._surreal_helpers import spawn_surreal

        data_dir = tempfile.mkdtemp(prefix="surreal_session_")
        proc = spawn_surreal(port=_SURREAL_RESERVED_PORT, data_dir=data_dir)
        _wait_for_health(_SURREAL_RESERVED_PORT)
        _SURREAL_HANDLE = {
            "proc": proc,
            "port": _SURREAL_RESERVED_PORT,
            "data_dir": data_dir,
            "respawns": 0,
        }
    return _SURREAL_HANDLE


@pytest.fixture(scope="session", autouse=True)
def _surreal_url_reserve():
    """Reserve the session SurrealDB URL WITHOUT spawning the server.

    Sets YADGAR_DB_URL (and `_REAL_DB_URL`) at session start so mode selection
    and subprocess env inheritance match the pre-Car-2 eager era; the actual
    subprocess is spawned lazily by `_ensure_surreal_spawned()` on first DB
    demand.  No-op when the `surreal` binary is absent (embedded mode) or when
    an external YADGAR_DB_URL was already provided.
    """
    global _REAL_DB_URL, _SURREAL_HANDLE, _SURREAL_RESERVED_PORT
    import shutil

    if _EXTERNAL_DB_URL is not None or not shutil.which("surreal"):
        yield
        return

    port = _find_free_port()
    _SURREAL_RESERVED_PORT = port
    real_url = f"http://127.0.0.1:{port}"
    os.environ["YADGAR_DB_URL"] = real_url
    _REAL_DB_URL = real_url
    try:
        yield
    finally:
        handle = _SURREAL_HANDLE
        if handle is not None:
            from yadgar.tests._surreal_helpers import teardown_surreal_proc

            teardown_surreal_proc(handle["proc"], wait_timeout=5)
        _SURREAL_HANDLE = None
        _SURREAL_RESERVED_PORT = None
        os.environ.pop("YADGAR_DB_URL", None)
        _REAL_DB_URL = None


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


@pytest.fixture(scope="session")
def surreal_server(_surreal_url_reserve):
    """Session SurrealDB handle — LAZY: requesting this fixture spawns the server.

    Car 2: no longer autouse.  Most DB tests never request this directly — the
    spawn fires from `_patched_init_schema` on their first StorageEngine
    construction.  Request it explicitly when the DB demand lives OUTSIDE this
    process's fixture graph: CLI-subprocess tests whose child process inherits
    YADGAR_DB_URL and connects to it, and e2e suites that hand the URL to a
    daemon.

    Returns None in embedded mode (no `surreal` binary) or when an external
    YADGAR_DB_URL is in charge — same contract as the pre-Car-2 fixture.
    Spawn is delegated to _surreal_helpers.spawn_surreal() which registers
    the PID for atexit cleanup (v5.10.0 orphan-reap hardening).
    """
    return _ensure_surreal_spawned()


@pytest.fixture(autouse=True)
def _surreal_liveness():
    """Respawn a dead SurrealDB server before each test (server-mode only).

    Converts the failure mode where one xdist worker's surreal dies mid-run and
    every subsequent test ERRORs with ConnectError (the session-wide cascade)
    into transparent recovery.  Partial by design: tests later in a module whose
    surreal died mid-module still fail, because their module-scoped ``_engines``
    fixture populated the ``server._storage`` singleton against the now-wiped DB
    and won't re-run ``_init_schema``.  It bounds the blast radius to the current
    module instead of the whole session.

    Car 2: reads the lazy module-level handle instead of depending on the
    ``surreal_server`` fixture (which would force an eager spawn on every
    worker).  Before the first DB demand there is nothing to keep alive.
    """
    handle = _SURREAL_HANDLE
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
        import yadgar._shared.config as _cfg

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
    from yadgar.core import server as _s

    monkeypatch.setattr(_s, "_file_queue", None)
    monkeypatch.setattr(_s, "_queue_drainer", None)
    yield
    # After the test, stop and clear the drainer so it doesn't bleed into teardown
    if _s._queue_drainer is not None:
        _s._queue_drainer.stop()
    monkeypatch.setattr(_s, "_file_queue", None)
    monkeypatch.setattr(_s, "_queue_drainer", None)


@pytest.fixture(scope="session", autouse=True)
def _isolate_surrealdb(_surreal_url_reserve):
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

    Car 2: this patch is ALSO the lazy-spawn demand point.  A StorageEngine
    constructed against the reserved (not-yet-spawned) session URL triggers
    `_ensure_surreal_spawned()` right before its first schema call — so the
    server exists exactly when the first DB test needs it, wherever the engine
    is constructed (module fixtures, local fixtures, test bodies).

    In embedded mode (no YADGAR_DB_URL), this fixture is a no-op.
    """
    if not os.environ.get("YADGAR_DB_URL"):
        yield
        return

    from yadgar._shared import storage as _sm

    original_init_schema = _sm.StorageEngine._init_schema

    def _patched_init_schema(self):
        if self._db_url and self._db_url == _REAL_DB_URL and _SURREAL_HANDLE is None:
            # First DB demand on this worker — spawn the reserved session server.
            _ensure_surreal_spawned()
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
      cli/_shared.py::silence_logging() (T2 Car B; formerly
      init_replay_lightweight); persists for the worker process lifetime,
      silencing all subsequent logging (Root-B/C xdist pollution).
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
        import yadgar.core.server as _srv
        import yadgar.core.server._app as _app

        saved_app_mcp = _app.mcp_server
        saved_srv_mcp = _srv.__dict__.get("mcp_server")
    except Exception:
        yield
        return
    yield
    try:
        import yadgar.core.server as _srv
        import yadgar.core.server._app as _app

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
        from yadgar.core import server as _s

        _s._action_batch.clear()
        _s._project_roots.clear()
        _s._last_session_context.clear()
        _s._last_prompt_recall.clear()
        _s._last_recalled_ids.clear()
        _s._event_queue.clear()
        _s._detect_branch_cached.cache_clear()
        _s._get_default_branch_cached.cache_clear()
        # Car 1 added @lru_cache to _resolve_project_root; clear it like its
        # siblings so stale git-root paths don't leak across tests.
        from yadgar.core.server.tools import project as _proj  # noqa: PLC0415

        _proj._resolve_project_root.cache_clear()
        # #28: _worktree_canonical_root is lru_cached at process level — a test
        # that anchors with context="global" from a worktree CWD would corrupt
        # this cache (resolves "global" as a relative path → finds worktree .git
        # FILE → returns canonical repo root instead of None). Clear per-test so
        # CWD-sensitive resolutions never bleed across test boundaries.
        from yadgar._shared.server_helpers import server_helpers as _sh  # noqa: PLC0415

        _sh._worktree_canonical_root.cache_clear()
    except Exception:
        pass
    # T3 Car 2: drain + drop the deferred recall side-effect executors so a
    # deferred session worker (or a forked DB task) from one test cannot fire
    # after its fixtures tear down and pollute the next test's _st state.
    try:
        from yadgar._shared.runtime.recall_side_effects_fork import (  # noqa: PLC0415
            reset_db_tasks,
            reset_session_executor,
        )

        reset_session_executor()
        reset_db_tasks()
    except Exception:  # noqa: BLE001 — never block a test on this defensive reset
        pass
    _reset_backend_caches()


def _reset_backend_caches() -> None:
    """Flush every process-global backend cache + the scope-version map.

    The backend caches (memory_doc / graph / engram_slot / ce, and any other
    namespace registered in ``yadgar.backend.cache._REGISTRY``) are PROCESS-GLOBAL
    and keyed by ids the test DB reuses: each test wipes the DB, so memory /
    entity / slot ids RESTART at 1, and a prior test's cached ``memory_doc[1]``
    (or slot/graph adjacency) would be served for a NEW test's id-1 row — a stale
    HIT returning the wrong row's content/rank. Prod is safe (ids are monotonic,
    never reused); this is a TEST-ISOLATION bug only. Clearing between tests (not
    disabling) keeps the cache-path coverage the pre-existing recall tests rely on.

    Iterate the live ``_REGISTRY`` rather than the ``get_*_cache`` factories: those
    lazily materialise + register a cache if absent, so calling them in teardown
    would create caches that never existed for this test. The scope-version map is
    not in the registry — cleared separately under its own lock. Never raise in
    teardown; skip silently if the backend module isn't importable.
    """
    try:
        from yadgar.backend import cache as _bc  # noqa: PLC0415
    except Exception:
        return
    try:
        for _c in list(_bc._REGISTRY.values()):
            try:
                _c.clear()
            except Exception:
                pass
    except Exception:
        pass
    try:
        _sv = _bc._SCOPE_VERSIONS
        with _sv._lock:
            _sv._versions.clear()
    except Exception:
        pass


_WIPE_TABLES = (
    "memory",
    "wiki_page",
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
    # v5.101 (module-scope P1): under a per-file (module-scoped) namespace the
    # DATA wipe — not a fresh namespace — is the ONLY per-test isolation, so it
    # must cover EVERY data-bearing table any test writes.  The tables below are
    # created by _init_schema but were absent from the wipe set; under function
    # scope each test got a fresh namespace so their residue was harmless, but
    # under module scope it leaks between tests in the same file.
    #
    # DELIBERATELY EXCLUDED — structural state SEEDED ONCE at engine init, NOT
    # per-test data.  Wiping these breaks tests that assert their init population:
    #   * engram_slot   — EngramAllocator.__init__ seeds HOPFIELD_MAX_PATTERNS
    #                     (5000) rows once (yadgar/engram.py:29); wiping it left
    #                     0 rows → e2e BC-C1 consolidation invariant violation
    #                     ("engram_slot has 0 rows (expected 5000)").
    #   * schema_version — schema-init marker; wiping it would drop the module's
    #                     migration record.
    "consolidation_meta",
    "graph_layout_cache",
    "file_hash",
    "memory_cluster",
    "astrocyte_process",
    "memory_transition",
    "causal_dag_edge",
    "user_profile",
    "derived_belief",
    "counter",
    "wiki_crossref",
    "wiki_page_version",
    "memory_embedding_backup",
)

# Worker-local set of all SurrealDB database namespaces used so far on this
# xdist worker.  Populated by `_isolate_surrealdb`'s patch — every time a
# StorageEngine runs _init_schema it registers its per-path namespace here.
# `_wipe_surrealdb_data` uses this set to wipe ALL known namespaces (not just
# the live _st._storage one) so tests that call server.shutdown() before the
# wipe fixture runs are still cleaned up.  "main" is always wiped in addition
# to catch CLI-subprocess writes that bypass the isolation patch.
_USED_SURREAL_NAMESPACES: set[str] = set()

# Authoritative test-surreal URL captured by the session-scoped
# `_surreal_url_reserve` fixture at reserve time (v5.104 PIECE C; Car 2 made the
# spawn itself lazy).  `_wipe_surrealdb_data` MUST use THIS,
# not ``os.environ["YADGAR_DB_URL"]`` — a test may monkeypatch YADGAR_DB_URL to an
# unreachable host (e.g. test_admin_config sets ``http://yadgar-backend:8000``, a
# Docker-internal name unresolvable from the runner).  Reading the env there made
# the HTTP-fallback wipe block on connect per namespace → the 114.8s teardown
# outlier.  Using the captured URL keeps the wipe pointed at the live server.
_REAL_DB_URL: str | None = None


def _authoritative_db_url() -> str | None:
    """The real session-surreal URL, resilient to per-test env monkeypatching.

    Prefers the URL captured by `surreal_server` at spawn; falls back to the
    environment only when no server was captured (embedded mode → None anyway).
    """
    return _REAL_DB_URL or os.environ.get("YADGAR_DB_URL")


# ---------------------------------------------------------------------------
# v5.104 PIECE B — module-scoped `storage` StorageEngine registry.
#
# A module-scoped `storage` fixture inits its schema ONCE per file (killing the
# 46% per-test setup floor from the function-scoped fixture that ran
# _init_schema() every test).  Per-test isolation then comes ONLY from the
# data-wipe, but the module-scoped engine's namespace is snapshotted into
# `pre_test_namespaces` at setup, so `_wipe_via_http_fallback`'s v5.56 guard
# PRESERVES it (correct for seed-once corpora, wrong for these engines).
#
# So registered engines are wiped EXPLICITLY every test via a single batched
# `_q` (PIECE A), sidestepping the snapshot guard entirely.  A file opts in by
# using the shared `module_storage` fixture (below) or by registering its own
# module-scoped engine.
# ---------------------------------------------------------------------------
_MODULE_SCOPED_STORAGE_ENGINES: list[object] = []


def _register_module_storage(engine: object) -> None:
    """Register a module-scoped StorageEngine for explicit per-test data-wipe."""
    _MODULE_SCOPED_STORAGE_ENGINES.append(engine)


def _deregister_module_storage(engine: object) -> None:
    """Remove a module-scoped StorageEngine from the wipe registry (at teardown)."""
    try:
        _MODULE_SCOPED_STORAGE_ENGINES.remove(engine)
    except ValueError:
        pass


def _wipe_registered_module_engines() -> None:
    """Batched data-wipe of every registered module-scoped StorageEngine.

    One `_q` per engine (PIECE A batching).  Errors are swallowed — an engine may
    have been closed by its module teardown before this function-scoped fixture's
    teardown runs on the last test of the module.
    """
    for engine in list(_MODULE_SCOPED_STORAGE_ENGINES):
        try:
            engine._q(_batched_delete_sql(_WIPE_TABLES))
        except Exception:
            pass


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


def _batched_delete_sql(tables) -> str:
    """Build one semicolon-joined ``DELETE`` statement covering every table.

    SurrealDB's ``/sql`` endpoint runs each ``;``-separated statement
    independently (NOT wrapped in a transaction), so a table that is missing in
    one namespace can't roll back the deletes for the others.  This is exactly
    the per-table loop's behaviour, collapsed into a single HTTP round-trip.
    """
    return " ".join(f"DELETE {table};" for table in tables)


def _wipe_tables_with_client(db_url: str, db_name: str, auth: str) -> None:
    """Batch-DELETE every wipe table in ONE POST using a short-lived httpx client.

    v5.104 PIECE A: was one HTTP round-trip per table (~29/namespace); now a
    single semicolon-joined ``DELETE`` batch.  Behaviourally identical to the old
    per-table loop while cutting teardown to one round-trip.
    """
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
        _delete_tables_safe(client, _WIPE_TABLES)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _delete_tables_safe(client, tables) -> None:
    """POST one batched DELETE covering all tables; ignore all errors."""
    try:
        client.post("/sql", content=_batched_delete_sql(tables))
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
    # Car 2: nothing to wipe when no server was ever spawned on this worker
    # (logic-only tests).  Wiping against the reserved-but-unspawned URL would
    # burn a connect timeout per test.  External URLs (user-provided) still wipe.
    if _SURREAL_HANDLE is None and _EXTERNAL_DB_URL is None:
        return
    # PIECE C: use the URL captured at session reserve, NOT os.environ — a test
    # may have monkeypatched YADGAR_DB_URL to an unreachable host (hang on connect).
    db_url = _authoritative_db_url()
    if not db_url:
        return
    _do_wipe_after_test(db_url, pre_test_namespaces)


def _do_wipe_after_test(db_url: str, pre_test_namespaces: frozenset[str]) -> None:
    """Wipe all data tables after a test, tolerating server.shutdown() having run."""
    try:
        from yadgar.core import server as _s

        storage = _s._storage
    except Exception:
        storage = None

    if storage is not None:
        _wipe_via_live_storage(storage, db_url)
    else:
        _wipe_via_http_fallback(db_url, pre_test_namespaces)

    # v5.104 PIECE B: explicitly wipe module-scoped `storage` engines whose
    # namespaces the snapshot guard preserves.  Runs on BOTH paths so a
    # module-scoped storage engine is cleaned even when server._storage is live
    # (its namespace differs from server._storage's).
    _wipe_registered_module_engines()


def _wipe_via_live_storage(storage, db_url: str) -> None:
    """Fast path: wipe via the live StorageEngine (namespace already set on client).

    v5.104 PIECE A: one batched ``_q`` (semicolon-joined DELETEs) instead of one
    ``_q`` per table — cuts ~29 HTTP round-trips to one on the dominant teardown
    path.  ``_q_server`` raises on the first ``ERR`` entry, so the whole batch is
    wrapped in try/except; every ``_WIPE_TABLES`` table is schema-created in the
    live namespace, so no missing-table ERR is expected here.
    """
    try:
        storage._q(_batched_delete_sql(_WIPE_TABLES))
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
    constructor overhead.  Construction does NOT load the model — every encode
    path lazy-loads via _ensure_model() on first use.
    """
    from yadgar._shared.embeddings import EmbeddingEngine

    return EmbeddingEngine("all-MiniLM-L6-v2")


@pytest.fixture(scope="session", autouse=True)
def _defer_embed_warmup_in_init_engines():
    """Defer init_engines' eager embedding-model warmup (Car 2).

    ``lifecycle.init_engines()`` ends with ``_st._embeddings._ensure_model()``
    — an eager warmup so the daemon's first recall isn't slow.  In tests that
    warmup costs ~700MB RSS (torch + sentence-transformers + weights) on every
    xdist worker that inits engines, even for modules that never encode.
    Every encode path calls ``_ensure_model()`` itself, so skipping ONLY the
    direct warmup call is behavior-neutral: the model loads on the first
    actual encode.  Mirrors the v5.54.5 ``YADGAR_MODEL_PRELOAD=false`` rerank
    deferral at the top of this file.

    The warmup call is distinguished from real load requests by the immediate
    caller's code name — only the direct ``init_engines`` frame is skipped.
    encode()/dimension()/backfill paths load normally even when they run
    INSIDE init_engines (e.g. ``_run_wiki_embedding_backfill`` → encode →
    _ensure_model has ``encode``'s frame as the immediate caller, not
    ``init_engines``).  Pinned by
    ``_meta/test_lazy_fixtures_car2.py::test_init_engines_defers_model_load``,
    which fails loudly if lifecycle renames the function.
    """
    import sys as _sys

    from yadgar._shared.embeddings import EmbeddingEngine

    _orig_ensure_model = EmbeddingEngine._ensure_model

    def _lazy_ensure_model(self):
        if _sys._getframe(1).f_code.co_name == "init_engines":
            return  # deferred: first encode loads on demand
        return _orig_ensure_model(self)

    EmbeddingEngine._ensure_model = _lazy_ensure_model
    try:
        yield
    finally:
        EmbeddingEngine._ensure_model = _orig_ensure_model


@pytest.fixture(scope="module")
def module_storage(tmp_path_factory, request):
    """Shared module-scoped StorageEngine — schema inits ONCE per file (v5.104 P1B).

    Replaces per-file function-scoped `storage` fixtures that built a fresh
    `StorageEngine()` every test (running `_init_schema()` + migrations each time
    — the 46% CI setup floor).  Per-test data isolation is preserved by
    registering the engine so the function-scoped `_wipe_surrealdb_data` teardown
    wipes its data after every test.

    A converting file writes exactly:

        from yadgar.tests.conftest import module_storage as storage  # noqa: F401

    or requests `module_storage` directly.  Uses `tmp_path_factory` (session
    scope) — a module-scoped fixture cannot request the function-scoped
    `tmp_path` (ScopeMismatch).  The temp dir is keyed on the requesting module
    name so two converted files on the same xdist worker never collide on a
    namespace.
    """
    from yadgar._shared.storage import StorageEngine

    safe = request.module.__name__.rsplit(".", 1)[-1]
    db_path = str(tmp_path_factory.mktemp(f"modstorage_{safe}") / "storage.db")
    engine = StorageEngine(db_path)
    _register_module_storage(engine)
    try:
        yield engine
    finally:
        _deregister_module_storage(engine)
        engine.close()


@pytest.fixture
def flush_queue():
    """Force the QueueDrainer to drain before continuing — for tests that do
    `memorize() → recall()` in the same test and rely on the drainer to flush."""

    def _flush():
        from yadgar.core import server as _s

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
    from yadgar.core import server as _s

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


# ---------------------------------------------------------------------------
# Phase 2a migration fixture: forward_to_backend bypass
#
# After Phase 2a, recall() is a pure forwarder to the backend HTTP endpoint.
# Tests that call recall() directly and don't mock _forward_to_backend will
# fail with "YADGAR_EMBED_URL is not set" because there is no backend server
# in the unit test environment.
#
# This fixture patches _forward_to_backend to call _fanout_recall directly
# (bypassing HTTP) using the same _st module object that the test's engine
# fixture has already wired. This preserves the test's semantic intent while
# removing the HTTP dependency.
#
# Opt-in: tests that call recall() without mocking _forward_to_backend
# automatically get this bypass because it's module-scoped autouse=False.
# Autouse: disabled globally to avoid interfering with tests that deliberately
# mock _forward_to_backend (those inner-scope mocks win regardless, but we
# avoid adding noise to the patch stack).
#
# Usage: add `recall_backend_bypass` as a fixture argument, or mark the test
# module with `pytestmark = pytest.mark.usefixtures("recall_backend_bypass")`.
# ---------------------------------------------------------------------------


@pytest.fixture
def recall_backend_bypass(monkeypatch):
    """Patch _forward_to_backend to call _fanout_recall directly (no HTTP).

    Phase 2a migration fixture: provides a backend-equivalent call path for
    tests that call recall() directly without mocking _forward_to_backend.

    The patch routes _forward_to_backend → _fanout_recall synchronously, using
    the same yadgar.server._state (_st) module that the test's engine fixture
    has already populated. This gives tests full fanout behavior (MemoryProvider +
    WikiProvider + fusion) without requiring a running backend HTTP server.

    DB side effects (_apply_recall_db_side_effects: heat updates, activity log)
    are intentionally skipped — unit tests don't test persistence behavior.
    """
    import sys

    from yadgar.backend.retrieval.compose import ensure_retrieval_engine
    from yadgar.backend.retrieval.recall_pipeline import _fanout_recall

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]

    def _bypass_forward(  # noqa: PLR0913 — mirrors full recall() forwarding signature
        query,
        max_results,
        min_heat,
        directory,
        current_branch,
        default_branch,
        type_filter,
        tags,
        mode=None,
        profile=None,
    ):
        """Direct _fanout_recall call — bypasses HTTP, same _st engines."""
        if mode is not None and mode != "landscape":
            # Unknown mode — return empty (forward-only would have 400'd)
            return []
        if mode == "landscape":
            # Landscape not fully wired in unit tests — return empty (no AstrocytePool)
            return []
        # Unit tests store memories with branch=YADGAR_CI_BRANCH.  recall.py may
        # detect current_branch=None for fake test directories (e.g. /home/user/project).
        # With current_branch=None and default_branch='master' (git fallback), the
        # BranchFilter clause is (branch IS NONE OR branch='master') — excluding
        # feat/* memories.  Fix: fill current_branch from YADGAR_CI_BRANCH so the
        # clause becomes (branch IS NONE OR branch='master' OR branch='feat/test-branch')
        # — includes unit-test memories without disabling branch isolation.
        import os as _os

        _ci_branch = _os.environ.get("YADGAR_CI_BRANCH")
        _effective_branch = current_branch or _ci_branch or None
        # T2 Car E2: compose the backend retriever lazily against the test's
        # live engines (idempotent; the shared root no longer builds it).
        ensure_retrieval_engine()
        return _fanout_recall(
            query=query,
            max_results=max_results,
            min_heat=min_heat,
            directory=directory,
            current_branch=_effective_branch,
            default_branch=default_branch,
            type_filter=type_filter,
            tags=tags,
            profile=profile,
        )

    monkeypatch.setattr(_recall_module, "_forward_to_backend", _bypass_forward)
    yield


# ---------------------------------------------------------------------------
# R3 Car 3a migration fixture: _forward_admin bypass
#
# After Car 3a, the pure-CRUD write tools (bookmark_*, block_*) are pure
# forwarders to the backend /admin endpoint. Tests that call these tools
# directly without mocking _forward_admin fail with "YADGAR_EMBED_URL is not
# set" because there is no backend HTTP server in the unit-test environment.
#
# This fixture patches _forward_admin to call run_admin_op directly (bypassing
# HTTP) against the same _st storage the test's engine fixture already wired.
# The storage write still happens for real via run_admin_op — assertions on
# rows / results stay meaningful (#52).
#
# Opt-in: add ``admin_backend_bypass`` as a fixture argument, or mark the module
# with ``pytestmark = pytest.mark.usefixtures("admin_backend_bypass")``.
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_backend_bypass(monkeypatch):
    """Patch _forward_admin to call run_admin_op directly (no HTTP).

    Car 3a migration fixture: provides a backend-equivalent call path for tests
    that invoke the CRUD write tools directly without mocking _forward_admin.
    The op runs against the same yadgar._shared.runtime.state (_st) storage the
    test's engine fixture populated, so the storage write is real.
    """
    import sys

    import yadgar.core.server.tools._forward as _forward_module
    from yadgar.backend.admin_exec import run_admin_op

    def _bypass_admin(op, payload, timeout_s=30.0):
        """Direct run_admin_op call — bypasses HTTP, same _st storage."""
        return run_admin_op(op, payload)

    # Patch the source AND every consumer module: the CRUD tools bind the helper
    # by name (``from ._forward import _forward_admin``), so patching only the
    # source module would not rebind the consumers' local references.
    monkeypatch.setattr(_forward_module, "_forward_admin", _bypass_admin)
    for _consumer in (
        "yadgar.core.server.tools.bookmarks",
        "yadgar.core.server.tools.blocks",
        # R3 Car 3b: memory/rules writes now forward too.
        "yadgar.core.server.tools.admin_other",
        "yadgar.core.server.tools.admin_archive",
        # R3 Car 3c: wiki-edit family + agent_prompt_save now forward too.
        "yadgar.core.server.tools.wiki",
        "yadgar.core.server.tools.agent_prompts",
        # R3 Car 3d: audit/invariants/project + dispatch marker forward too.
        "yadgar.core.server.tools.audit",
        "yadgar.core.server.tools.admin_invariants",
        "yadgar.core.server.tools.project",
        "yadgar.core.server.tools.dispatch_helper",
    ):
        _mod = sys.modules.get(_consumer)
        if _mod is not None and hasattr(_mod, "_forward_admin"):
            monkeypatch.setattr(_mod, "_forward_admin", _bypass_admin)
    yield


# ---------------------------------------------------------------------------
# R3 write-path autouse harness for unit tests
#
# R3 Car 1 + Car 3 made write/admin/recall/consolidation forward-only to the
# backend HTTP endpoint (YADGAR_EMBED_URL).  Unit tests have no backend server,
# so three failure mechanics arise:
#
#   (a) RuntimeError "YADGAR_EMBED_URL is not set" from forwarders.
#   (b) memorize/wiki writes return {stored,queued,queue_id} with no drainer to
#       land them → reads find nothing (id-asserts, empty-data cascades).
#   (c) consolidation hits _forward_to_backend → RuntimeError.
#
# This autouse fixture installs all four harness pieces (from _backend_harness)
# for tests that use the engine stack.  The gate (engine fixture presence in
# request.fixturenames) prevents applying the bypass to tests that deliberately
# exercise the RuntimeError path (test_admin_forward_unit.py etc.) — those tests
# run WITHOUT engine fixtures and must stay untouched.
#
# CALL-TIME guard: when YADGAR_EMBED_URL IS set (e.g. test sets it to exercise a
# real backend contract), the bypass functions delegate to the original HTTP
# forwarder — the real contract is tested, not the in-process shim.
#
# Engine fixture names that signal "this test uses the engine stack":
#   _engines        — most unit test files define this module-scoped fixture
#   module_storage  — shared module-scoped StorageEngine (conftest factory above)
#   server_engines  — integration tests (test_integration.py) use this name
#
# The existing opt-in admin_backend_bypass / recall_backend_bypass fixtures
# remain available for tests in Car 3c files that opt in explicitly.  Their
# bodies are unchanged; the autouse fixture below delegates to the same harness
# functions so there is one canonical implementation.
# ---------------------------------------------------------------------------


# Set of fixture names whose presence indicates an in-process engine stack.
_ENGINE_FIXTURE_NAMES = frozenset({"_engines", "module_storage", "server_engines"})


@pytest.fixture(autouse=True)
def _unit_backend_harness(request, monkeypatch, _isolate_file_queue):
    """Install in-process backend harness when the test uses an engine stack.

    Gate: only wires when one of ``_ENGINE_FIXTURE_NAMES`` is in
    ``request.fixturenames``.  Tests that deliberately test the forward-only
    RuntimeError path (e.g. test_admin_forward_unit.py) carry NO engine fixture
    and are unaffected.

    Pieces installed (all CALL-TIME guarded on YADGAR_EMBED_URL):
      1. QueueDrainer + ConsolidationScheduler in-process via ``wire_drainer``.
      2. ``_forward_admin`` → ``run_admin_op`` (in-process).
      3. Recall ``_forward_to_backend`` → ``_fanout_recall`` (in-process).
      4. Orchestrator ``_forward_to_backend`` → ``run_consolidation_cycle``.

    Depends on ``_isolate_file_queue`` so it runs AFTER the per-test FileQueue
    reset; ``_get_file_queue()`` then returns the live per-test queue.
    """
    if not any(n in request.fixturenames for n in _ENGINE_FIXTURE_NAMES):
        yield
        return

    from yadgar.core import server as _server
    from yadgar.tests._backend_harness import (
        patch_admin_bypass,
        patch_consolidate_bypass,
        patch_recall_bypass,
        patch_restore_bypass,
        patch_viz_bypass,
        teardown_consolidate_bypass,
        wire_drainer,
    )

    # Provide a default branch so memorize/anchor/wiki_add calls in unit tests
    # don't need explicit branch_hint.  Tests that explicitly assert "no branch"
    # behaviour already remove this via monkeypatch.delenv("YADGAR_CI_BRANCH").
    monkeypatch.setenv("YADGAR_CI_BRANCH", "feat/test-branch")

    # Install the five forward bypasses (monkeypatch unwinds at test teardown).
    patch_admin_bypass(monkeypatch)
    patch_recall_bypass(monkeypatch)
    patch_consolidate_bypass(monkeypatch)
    patch_restore_bypass(monkeypatch)
    patch_viz_bypass(monkeypatch)

    # Wire the drainer + consolidation scheduler in-process.
    with wire_drainer(_server._get_file_queue) as drainer:
        yield drainer

    # Drop the memoised backend scheduler singleton after every test so the next
    # test (or module) rebuilds it against its own live storage.
    teardown_consolidate_bypass()
