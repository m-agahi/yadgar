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

logger = logging.getLogger(__name__)

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


def spawn_surreal(port: int, data_dir: str, **popen_kwargs: Any) -> subprocess.Popen:
    """Start a SurrealDB subprocess bound to *port*, register its PID.

    Args:
        port: Local TCP port for `--bind 127.0.0.1:<port>`.
        data_dir: Directory path for `surrealkv://<data_dir>`.
        **popen_kwargs: Extra kwargs forwarded to subprocess.Popen (e.g.
            stdout=subprocess.DEVNULL).  stdout/stderr default to DEVNULL.

    Returns:
        The running subprocess.Popen instance.

    Raises:
        FileNotFoundError: If the `surreal` binary is not on PATH.
    """
    defaults: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    defaults.update(popen_kwargs)

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
            f"surrealkv://{data_dir}",
        ],
        **defaults,
    )
    _SPAWNED_SURREAL_PIDS.append(proc.pid)
    logger.debug("spawn_surreal: pid=%d port=%d data_dir=%s", proc.pid, port, data_dir)
    return proc


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
atexit.register(kill_all_spawned_surreal)

# ---------------------------------------------------------------------------
# Deterministic port allocation (xdist-aware)
# ---------------------------------------------------------------------------


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


def _port_in_use(port: int) -> bool:
    """Return True if *port* is currently accepting connections on 127.0.0.1.

    Uses connect() rather than bind() so that SO_REUSEADDR on the probe socket
    does not produce a false "free" reading against a listening socket.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        result = s.connect_ex(("127.0.0.1", port))
        return result == 0


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
