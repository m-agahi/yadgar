"""Shared helpers for spawning and reaping SurrealDB subprocesses.

Extracted from yadgar/tests/_surreal_helpers.py (v5.10.0) into a non-test
module so that benchmarks (benchmarks/run_longmemeval.py) and tests share a
single implementation.  v5.25.1.

Design goals (inherited from v5.10.0):
- Single registry for all spawned SurrealDB pids across the process lifetime.
- atexit handler fires on clean exit, ^C (SIGINT), and timeout-induced teardown.
- Per-caller teardown helper (terminate → wait → kill) keeps logic central.
- Deterministic port allocation: YADGAR_TEST_PORT_BASE + worker_index * 100 + n.
- EADDRINUSE retry with linear backoff (10 retries, 100ms step).

NOTE: YADGAR_TEST_PORT_BASE is TEST-ONLY — NOT registered in yadgar production
config (no three-way-sync, no env-knob boilerplate in config.py).
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import socket
import subprocess
import time
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


@observe(tier="stage")
def _resolve_db_creds() -> tuple[str, str]:
    """Return (user, password) using the canonical four-tier env precedence.

    Credential precedence (vacuum is an admin operation, needs root IAM):
      1. SURREAL_USER / SURREAL_PASS  (preferred — root IAM, same creds used by entrypoint)
      2. YADGAR_RW_USER / YADGAR_RW_PASS  (canonical post-rename; new installs only write RW)
      3. YADGAR_DB_USER / YADGAR_DB_PASS  (legacy alias — backward compat for old installs)
      4. root / root  (built-in SurrealDB default)

    When a USER env var is set without a matching PASS, the password defaults to
    ``"root"`` (SurrealDB's built-in root password) rather than empty string.

    Lives here (next to ``spawn_surreal``) so the vacuum HTTP client and the
    side-backend spawn share one source of truth — drift between the two was the
    root cause of #43 (side backend started root/root while the client sent env
    creds → HTTP 401 on namespace bootstrap).
    """
    if os.environ.get("SURREAL_USER"):
        return os.environ["SURREAL_USER"], os.environ.get("SURREAL_PASS", "root")
    if os.environ.get("YADGAR_RW_USER"):
        return os.environ["YADGAR_RW_USER"], os.environ.get("YADGAR_RW_PASS", "root")
    if os.environ.get("YADGAR_DB_USER"):
        return os.environ["YADGAR_DB_USER"], os.environ.get("YADGAR_DB_PASS", "root")
    return "root", "root"


# ---------------------------------------------------------------------------
# PID registry
# ---------------------------------------------------------------------------

_SPAWNED_SURREAL_PIDS: list[int] = []
"""Module-level registry of all SurrealDB subprocess PIDs spawned in this process.

The atexit handler iterates this list on process exit. Tests manipulate it
directly only in test_harness_hardening.py for unit-testing the kill logic;
production fixtures and benchmarks append via spawn_surreal().
"""

_DEFAULT_PORT_BASE = 12000
_RETRY_BACKOFF_MS = 100  # ms per retry step (linear)

# ---------------------------------------------------------------------------
# Spawn + teardown
# ---------------------------------------------------------------------------


@observe(tier="boundary")
def spawn_surreal(
    port: int,
    data_dir: str,
    surreal_user: str = "root",
    surreal_pass: str = "root",
    binary: str = "surreal",
    **popen_kwargs: Any,
) -> subprocess.Popen:
    """Start a SurrealDB subprocess bound to *port*, register its PID.

    Args:
        port: Local TCP port for `--bind 127.0.0.1:<port>`.
        data_dir: Directory path for `surrealkv://<data_dir>`.
        surreal_user: SurrealDB root username passed via ``--user``.
            Defaults to ``"root"`` (built-in SurrealDB default).
        surreal_pass: SurrealDB root password passed via ``--pass``.
            Defaults to ``"root"`` (built-in SurrealDB default).
        binary: Path (or bare name) of the `surreal` executable to spawn.
            Defaults to the bare string ``"surreal"`` — a plain PATH-resolved
            lookup, preserving every existing caller.  Task 0107:
            ``HostBinaryLauncher.start`` passes the ABSOLUTE path already
            resolved by ``yadgar.core.vacuum.launcher._resolve_surreal_binary``
            instead of relying on this Popen doing its own independent PATH
            lookup — that independence was the third of three PATH-dependent
            resolution points that could disagree with each other.
        **popen_kwargs: Extra kwargs forwarded to subprocess.Popen (e.g.
            stdout=subprocess.DEVNULL).  stdout/stderr default to DEVNULL.

    Returns:
        The running subprocess.Popen instance.

    Raises:
        FileNotFoundError: If *binary* is not on PATH (or does not exist, if
            an absolute path was passed).
    """
    defaults: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    defaults.update(popen_kwargs)

    proc = subprocess.Popen(
        [
            binary,
            "start",
            "--no-banner",
            "--bind",
            f"127.0.0.1:{port}",
            "--user",
            surreal_user,
            "--pass",
            surreal_pass,
            f"surrealkv://{data_dir}",
        ],
        **defaults,
    )
    _SPAWNED_SURREAL_PIDS.append(proc.pid)
    logger.debug("spawn_surreal: pid=%d port=%d data_dir=%s", proc.pid, port, data_dir)
    return proc


@observe(tier="boundary")
def teardown_surreal_proc(proc: subprocess.Popen, wait_timeout: float = 5.0) -> None:
    """Terminate *proc* cleanly, escalating to SIGKILL if it doesn't exit.

    Args:
        proc: The Popen instance to tear down.
        wait_timeout: Seconds to wait after SIGTERM before escalating to SIGKILL.
    """
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=wait_timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Kill all registered subprocesses
# ---------------------------------------------------------------------------


@observe(tier="boundary")
def kill_all_spawned_surreal() -> None:
    """SIGTERM all registered SurrealDB PIDs, then SIGKILL stragglers.

    Safe to call multiple times (double-kill is suppressed via ProcessLookupError).
    Registered as an atexit handler at module import time.
    """
    if not _SPAWNED_SURREAL_PIDS:
        return

    for pid in list(_SPAWNED_SURREAL_PIDS):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.debug("kill_all_spawned_surreal: SIGTERM pid=%d failed: %s", pid, exc)

    time.sleep(0.5)

    for pid in list(_SPAWNED_SURREAL_PIDS):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.debug("kill_all_spawned_surreal: SIGKILL pid=%d failed: %s", pid, exc)

    logger.debug("kill_all_spawned_surreal: processed %d pid(s)", len(_SPAWNED_SURREAL_PIDS))


# Register once at module import — fires on clean exit, ^C, pytest-timeout unwind.
# Wrap so an at-exit error (e.g. logging.debug to an already-closed pytest capture
# stream — "ValueError: I/O operation on closed file") cannot propagate out of the
# atexit handler and force a non-zero process exit. Direct test callers still hit
# kill_all_spawned_surreal() unwrapped. (folder-split #17: surfaced in e2e teardown.)
def _kill_all_spawned_surreal_atexit() -> None:
    try:
        kill_all_spawned_surreal()
    except Exception:  # noqa: BLE001 — atexit cleanup must never raise
        pass
    # Task 307: the processes were reaped here since v5.10.0, their surrealkv
    # stores never were.  Runs after the SIGTERM/SIGKILL pass above so nothing
    # is still writing into a dir being removed.
    try:
        purge_registered_test_data_dirs()
    except Exception:  # noqa: BLE001 — atexit cleanup must never raise
        pass


atexit.register(_kill_all_spawned_surreal_atexit)


@observe(tier="stage")
def _session_tmp_base() -> str:
    """Root tmp dir for THIS test session's SurrealDB data.

    Mirrors conftest's namespace logic: YADGAR_TEST_NAMESPACE redirects to
    /tmp/pytest-<ns>; otherwise pytest's default /tmp/pytest-of-<user>.
    Used to scope the stale-orphan reaper to this session only, so concurrent
    sessions under different namespaces never kill each other's databases.
    """
    ns = os.environ.get("YADGAR_TEST_NAMESPACE", "")
    if ns:
        return f"/tmp/pytest-{ns}"
    tmp = os.environ.get("TMPDIR", "").rstrip("/")
    if tmp:
        return tmp
    import getpass

    return f"/tmp/pytest-of-{getpass.getuser()}"


@observe(tier="boundary")
def reap_stale_surreal() -> int:
    """SIGKILL orphaned test SurrealDB procs left by prior crashed runs.

    Registry cleanup (kill_all_spawned_surreal) misses these: atexit and
    pytest_sessionfinish never fire on SIGKILL, and a fresh run's PID registry
    can't see a previous run's PIDs, so orphans stack across runs (39 stray
    surreals once observed — a secondary cause of the -n auto OOM).

    Scans /proc (Linux) for `surreal start` whose data path is under THIS
    session's tmp base. Never touches the production daemon (binds
    /data/surreal_db) nor a concurrent session under a different namespace.
    Best-effort; silent on permission errors. Returns the number killed.
    """
    base = _session_tmp_base()
    if not base:
        return 0
    self_pid = os.getpid()
    killed = 0
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "surreal start" not in cmd or base not in cmd:
            continue
        if "/data/surreal_db" in cmd:  # production daemon — never touch
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    if killed:
        logger.warning("reap_stale_surreal: killed %d orphaned test surreal proc(s)", killed)
    return killed


# ---------------------------------------------------------------------------
# Data-directory cleanup (task 307)
#
# ``reap_stale_surreal`` above reaps PROCESSES and nothing else, so every
# surrealkv store a killed run left behind stayed on disk forever: 4838 dirs /
# 49GB accumulated between 2026-08-01 and 2026-08-27, and the pre-push
# ``make e2e`` gate was OOM-killed (Error 137) with 1132 of them (12GB) live.
#
# Worse, neither existing reaper ever matched these runs at all.  Both scope on
# the data path: ``reap-test-surreal.sh`` requires ``/tmp/pytest`` in the
# cmdline, ``reap_stale_surreal`` requires ``_session_tmp_base()``.  The session
# fixture's ``mkdtemp(prefix="surreal_session_")`` lands at the top of TMPDIR
# (``/tmp/surreal_session_XXXXXXXX``), which matches neither — so with TMPDIR
# unset these surreals are invisible to the process reapers too.  The sweep
# below closes both halves for the two prefixes the fixtures own.
# ---------------------------------------------------------------------------

_TEST_DATA_DIR_PREFIXES: tuple[str, ...] = ("surreal_session_", "surreal_respawn_")
"""Basename prefixes owned by the pytest session fixtures.

Deliberately NARROW, and the narrowness is the safety property.  ``spawn_surreal``
is ALSO called by ``vacuum.launcher.HostBinaryLauncher.start`` with a real
side-backend path, and by benchmarks with their own dirs.  No cleanup path in this
module may act on "whatever data_dir was spawned" — only on a directory whose
basename says the test fixtures created it.  The production daemon's ``/data``
store cannot match either prefix under any TMPDIR.
"""

_SPAWNED_SURREAL_DATA_DIRS: list[str] = []
"""Test data dirs created in THIS process, purged at teardown / atexit.

Populated by ``register_test_data_dir`` from the pytest fixtures, NOT by
``spawn_surreal`` — see ``_TEST_DATA_DIR_PREFIXES`` for why that distinction
is load-bearing.
"""

_SWEEP_MIN_AGE_S = 60.0
"""Age floor for the orphan sweep.

Second belt behind the in-use guard: a dir is created by ``mkdtemp`` a moment
BEFORE ``spawn_surreal`` puts its path into a process cmdline, so a brand-new
dir is briefly invisible to the in-use check.  60s is far longer than that
window and far shorter than any real leak.
"""


def _is_test_data_dir(path: str) -> bool:
    """True when *path*'s basename carries a fixture-owned prefix."""
    return os.path.basename(os.path.normpath(path)).startswith(_TEST_DATA_DIR_PREFIXES)


@observe(tier="stage")
def register_test_data_dir(path: str) -> bool:
    """Record *path* for purge at fixture teardown / atexit.

    Returns True when the path was accepted (prefix-gated); False when it was
    refused as not fixture-owned.
    """
    if not _is_test_data_dir(path):
        logger.debug("register_test_data_dir: refused non-test path %s", path)
        return False
    if path not in _SPAWNED_SURREAL_DATA_DIRS:
        _SPAWNED_SURREAL_DATA_DIRS.append(path)
    return True


@observe(tier="stage")
def remove_test_data_dir(path: str) -> bool:
    """Delete *path* if — and ONLY if — it is a fixture-owned test data dir.

    Best-effort (``ignore_errors=True``): cleanup must never fail a run.
    Returns True when the directory is gone afterwards.
    """
    if not _is_test_data_dir(path):
        logger.warning("remove_test_data_dir: refused non-test path %s", path)
        return False
    import shutil

    shutil.rmtree(path, ignore_errors=True)
    if path in _SPAWNED_SURREAL_DATA_DIRS:
        _SPAWNED_SURREAL_DATA_DIRS.remove(path)
    return not os.path.exists(path)


@observe(tier="boundary")
def purge_registered_test_data_dirs() -> int:
    """Remove every data dir registered in this process. Returns the count.

    Called from the atexit handler and ``pytest_sessionfinish``, so it covers
    clean exit, ^C (SIGINT) and pytest-timeout unwind.  It CANNOT cover SIGKILL
    / OOM — that is what ``sweep_orphan_surreal_data_dirs`` is for.
    """
    removed = 0
    for path in list(_SPAWNED_SURREAL_DATA_DIRS):
        if remove_test_data_dir(path):
            removed += 1
    if removed:
        logger.debug("purge_registered_test_data_dirs: removed %d dir(s)", removed)
    return removed


@observe(tier="stage")
def _live_surreal_cmdlines() -> list[str]:
    """Cmdlines of every live process whose ``comm`` is exactly ``surreal``.

    Same discriminator ``scripts/reap-test-surreal.sh`` uses (name, not a
    substring of the whole cmdline) so a wrapper shell whose own arguments
    mention a data dir is never mistaken for the server.
    """
    out: list[str] = []
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/comm") as fh:
                if fh.read().strip() != "surreal":
                    continue
            with open(f"/proc/{entry.name}/cmdline", "rb") as fh:
                out.append(fh.read().replace(b"\x00", b" ").decode("utf-8", "replace"))
        except OSError:
            continue
    return out


@observe(tier="hot")
def _sweep_one(path: str, *, live: str, now: float, min_age_s: float) -> bool:
    """Remove one candidate dir unless a live surreal owns it or it is too young."""
    if not os.path.isdir(path):
        return False
    if path in live:  # a live surreal is serving out of this store
        return False
    try:
        if now - os.stat(path).st_mtime < min_age_s:
            return False
    except OSError:
        return False
    return remove_test_data_dir(path)


@observe(tier="boundary")
def sweep_orphan_surreal_data_dirs(min_age_s: float = _SWEEP_MIN_AGE_S) -> int:
    """Delete abandoned fixture surrealkv stores left by SIGKILL'd runs.

    The backstop half of task 307: registry purge and fixture teardown both
    require the process to still be alive, and an OOM-kill grants neither.
    This runs from ``pytest_configure`` (master only, every invocation —
    including a bare ``uv run pytest``) and from
    ``scripts/reap-test-surreal.sh``.

    Three guards keep it from deleting a store somebody is using:
      1. prefix gate — only ``surreal_session_*`` / ``surreal_respawn_*``
         basenames under a tmp root, so ``/data`` and benchmark dirs are
         unreachable from here;
      2. in-use gate — any dir named in a live ``comm == "surreal"`` cmdline is
         skipped, which is what makes a concurrent session safe;
      3. age gate — *min_age_s* covers the mkdtemp→spawn window.

    Returns the number of directories removed.
    """
    import glob
    import tempfile as _tempfile

    # `/tmp` is in the set unconditionally, not just via TMPDIR: when TMPDIR IS
    # set both other roots collapse onto it, and sweeping only those misses the
    # top of /tmp — which is precisely where a TMPDIR-unset run (a bare
    # `uv run pytest`) puts its stores, and where the 4838-dir backlog was found.
    roots = {_tempfile.gettempdir(), _session_tmp_base(), "/tmp"}
    live = " ".join(_live_surreal_cmdlines())
    now = time.time()
    removed = 0
    for root, prefix in ((r, p) for r in roots if r for p in _TEST_DATA_DIR_PREFIXES):
        for path in glob.glob(os.path.join(root, prefix + "*")):
            if _sweep_one(path, live=live, now=now, min_age_s=min_age_s):
                removed += 1
    if removed:
        logger.warning("sweep_orphan_surreal_data_dirs: removed %d orphan dir(s)", removed)
    return removed


# ---------------------------------------------------------------------------
# Deterministic port allocation (xdist-aware)
# ---------------------------------------------------------------------------


@observe(tier="hot")
def _worker_index() -> int:
    """Parse the numeric part of PYTEST_XDIST_WORKER (e.g. 'gw3' → 3)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker:
        return 0
    # Standard xdist format: "gw<N>"
    if worker.startswith("gw"):
        try:
            return int(worker[2:])
        except ValueError:
            pass
    # Fallback: strip non-digits
    digits = "".join(c for c in worker if c.isdigit())
    return int(digits) if digits else 0


@observe(tier="stage")
def allocate_port(n: int = 0) -> int:
    """Return the deterministic port for this xdist worker and sequence number *n*.

    Formula: YADGAR_TEST_PORT_BASE + worker_index * 100 + n

    Env knobs (test-only — NOT in production yadgar config):
        YADGAR_TEST_PORT_BASE: Base port (default 12000).
        PYTEST_XDIST_WORKER:   Set by pytest-xdist (e.g. "gw0", "gw3").

    Args:
        n: Sequential fixture index within the worker (0-based).

    Returns:
        Integer port number.
    """
    base = int(os.environ.get("YADGAR_TEST_PORT_BASE", _DEFAULT_PORT_BASE))
    worker_idx = _worker_index()
    return base + worker_idx * 100 + n


@observe(tier="hot")
def _port_in_use(port: int) -> bool:
    """Return True if *port* is currently accepting connections on 127.0.0.1.

    Uses connect() rather than bind() so that SO_REUSEADDR on the probe socket
    does not produce a false "free" reading against a listening socket.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        result = s.connect_ex(("127.0.0.1", port))
        return result == 0


@observe(tier="stage")
def allocate_port_with_retry(n: int = 0, max_retries: int = 10) -> int:
    """Return a free port, retrying with linear backoff on EADDRINUSE.

    Starts from allocate_port(n) and increments by 1 each retry.

    Args:
        n: Sequential fixture index (passed to allocate_port).
        max_retries: Maximum number of retry attempts before raising.

    Returns:
        A free port number.

    Raises:
        RuntimeError: If all retries are exhausted (EADDRINUSE).
    """
    base_port = allocate_port(n)
    for attempt in range(max_retries):
        port = base_port + attempt
        if not _port_in_use(port):
            if attempt > 0:
                logger.warning(
                    "allocate_port_with_retry: port %d in use, advanced to %d (attempt %d)",
                    base_port,
                    port,
                    attempt + 1,
                )
            return port
        backoff_s = (_RETRY_BACKOFF_MS * (attempt + 1)) / 1000.0
        time.sleep(backoff_s)

    raise RuntimeError(
        f"EADDRINUSE: could not allocate a free port after {max_retries} retries "
        f"starting from {base_port}. "
        "Set YADGAR_TEST_PORT_BASE to a different range to avoid collisions."
    )
