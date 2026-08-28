"""Core-daemon process/system metrics sampler (T2 Car E3 split).

Historically lived in ``graph_api.py``; split out when the GraphAPI data
assembly moved to the backend (census verdict #11). This half is CORE by
nature — it introspects the core daemon process (``/proc/<pid>``), the host
``/proc`` system files, and the local DB directory, and feeds the
``_st._system_metrics_cache`` the ``/api/system`` route serves.

Consumers: ``core.daemon.daemons._metrics_loop`` (background sampler thread).
"""

import gc
import logging
import os
import time
from pathlib import Path

from yadgar._shared.observability.metrics import (
    yadgar_process_cpu_percent,
    yadgar_process_open_fds,
    yadgar_process_rss_bytes,
    yadgar_python_gc_duration_ms,
)
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# ── GC duration instrumentation ───────────────────────────────────────────────

_gc_start_times: dict[int, float] = {}


def _gc_callback(phase: str, info: dict) -> None:
    """Record GC collection duration into yadgar_python_gc_duration_ms histogram.

    Shutdown guard: at interpreter teardown module globals (time,
    _gc_start_times, yadgar_python_gc_duration_ms) are set to None before GC
    finishes draining callbacks.  Accessing attributes on None raises
    AttributeError — surfaced as "Exception ignored while calling GC callback"
    in journald and can cause a non-zero exit code.  Return immediately when
    any critical global is None.
    """
    # Must be first — any attribute access below this line may raise if None.
    if time is None or _gc_start_times is None:
        return
    if phase == "start":
        _gc_start_times[info["generation"]] = time.perf_counter()
    elif phase == "stop":
        start = _gc_start_times.pop(info["generation"], None)
        if start is not None:
            duration_ms = (time.perf_counter() - start) * 1000
            yadgar_python_gc_duration_ms.labels(generation=str(info["generation"])).observe(
                duration_ms
            )


# Idempotent registration — safe across importlib.reload().
# Check by __qualname__ because reload() creates new function objects with a
# different identity, so `_gc_callback not in gc.callbacks` would always be True.
_already_registered = any(
    getattr(cb, "__qualname__", "") == _gc_callback.__qualname__ for cb in gc.callbacks
)
if not _already_registered:
    gc.callbacks.append(_gc_callback)


# ── System metrics (no extra deps — reads /proc) ──────────────────────────────

_metrics_cache: dict = {}
_metrics_sampled_at: float = 0.0
_prev_cpu_ticks: int = 0
_prev_cpu_time: float = 0.0


def _observe_dbsize_ms(elapsed_ms: float) -> None:
    """Record dbsize sampling duration. Non-fatal; helper keeps cyclo of caller clean."""
    try:
        from yadgar._shared.observability.metrics import (
            yadgar_viz_dbsize_sample_duration_ms,  # noqa: PLC0415
        )

        yadgar_viz_dbsize_sample_duration_ms.observe(elapsed_ms)
    except (ImportError, ValueError):  # fmt: skip
        pass


@observe(tier="stage")
def _sample_cpu_pct(pid: int, clk_tck: int) -> float:
    """Read /proc/<pid>/stat and return CPU% via two-sample delta against module globals."""
    global _prev_cpu_ticks, _prev_cpu_time
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
    cpu_ticks = int(parts[13]) + int(parts[14])
    now = time.monotonic()
    if _prev_cpu_time > 0:
        elapsed = now - _prev_cpu_time
        delta = cpu_ticks - _prev_cpu_ticks
        cpu_pct = round(delta / clk_tck / max(elapsed, 0.001) * 100, 1)
    else:
        cpu_pct = 0.0
    _prev_cpu_ticks = cpu_ticks
    _prev_cpu_time = now
    return cpu_pct


@observe(tier="stage")
def _sample_rss_threads(pid: int) -> tuple[int, int]:
    """Read /proc/<pid>/status; return (rss_kb, threads)."""
    rss_kb = 0
    threads = 0
    with open(f"/proc/{pid}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
    return rss_kb, threads


@observe(tier="stage")
def _sample_open_fds() -> int:
    """Count open file descriptors via /proc/self/fd."""
    return len(os.listdir("/proc/self/fd"))


@observe(tier="stage")
def _sample_meminfo() -> tuple[int, int]:
    """Read /proc/meminfo; return (total_ram_kb, avail_ram_kb)."""
    total_ram_kb = avail_ram_kb = 0
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                total_ram_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_ram_kb = int(line.split()[1])
    return total_ram_kb, avail_ram_kb


@observe(tier="stage")
def _sample_loadavg() -> tuple[float, float, float]:
    """Read /proc/loadavg; return (load_1m, load_5m, load_15m)."""
    with open("/proc/loadavg") as fh:
        la = fh.read().split()
    return float(la[0]), float(la[1]), float(la[2])


@observe(tier="stage")
def _sample_db_size(storage: object, db_path: str) -> float:
    """Return db_size_mb — via storage proxy in server mode, or path walk otherwise."""
    if storage is not None:
        _db_url = getattr(storage, "_db_url", None)
        if _db_url is not None:
            try:
                size_data = storage.get_db_size()  # type: ignore[union-attr]
                size_bytes = size_data.get("db_size_bytes", 0)
                return round(size_bytes / 1024 / 1024, 1)
            except Exception:  # noqa: BLE001 — `storage` is typed `object` here (an in-process engine in one mode, a forwarding proxy in the other), so the raisable set of `get_db_size` is not knowable; a fault falls through to the path-walk below
                pass
    try:
        db_dir = Path(db_path).expanduser()
        if db_dir.is_dir():
            size_bytes = sum(f.stat().st_size for f in db_dir.rglob("*") if f.is_file())
            return round(size_bytes / 1024 / 1024, 1)
    except (OSError, RuntimeError):  # fmt: skip
        pass
    return 0.0


@observe(tier="stage")
def sample_system_metrics(pid: int, db_path: str, storage: object = None) -> dict:
    """Sample system metrics from /proc and update the in-process cache.

    Args:
        pid: PID of the daemon process (for /proc reads).
        db_path: Local filesystem path to the SurrealDB directory.
        storage: Optional StorageEngine instance.  When provided *and* the
            storage is in server mode (YADGAR_DB_URL is set), db_size_mb is
            obtained via ``storage.get_db_size()`` which proxies to the embed
            service's /admin/dbsize endpoint — the local path doesn't exist in
            that topology and would always return 0.
    """
    # PR-I: heartbeat — called at the start of every sampler iteration (lifecycle.py thread)
    try:
        from yadgar._shared.observability.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat("metrics_sampler")
    except Exception:  # noqa: BLE001
        pass

    global _metrics_cache, _metrics_sampled_at

    result: dict = dict(_metrics_cache)  # start with last known values

    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError):  # fmt: skip
        clk_tck = 100

    # CPU% (two-sample delta via /proc/<pid>/stat; Fields 13=utime, 14=stime)
    try:
        result["daemon_cpu_pct"] = _sample_cpu_pct(pid, clk_tck)
    except (OSError, IndexError, ValueError):  # fmt: skip
        result.setdefault("daemon_cpu_pct", 0.0)

    # RSS + thread count from /proc/{pid}/status
    try:
        rss_kb, threads = _sample_rss_threads(pid)
    except (OSError, IndexError, ValueError):  # fmt: skip
        rss_kb, threads = 0, 0
    result["daemon_rss_mb"] = round(rss_kb / 1024, 1)
    result["rss_bytes"] = rss_kb * 1024
    result["daemon_threads"] = threads

    # Open file descriptors (self — /proc/self/fd is always accessible)
    try:
        result["open_fds"] = _sample_open_fds()
    except OSError:
        result.setdefault("open_fds", 0)

    # System RAM
    try:
        total_ram_kb, avail_ram_kb = _sample_meminfo()
    except (OSError, IndexError, ValueError):  # fmt: skip
        total_ram_kb = avail_ram_kb = 0
    result["system_ram_total_mb"] = round(total_ram_kb / 1024, 1)
    result["system_ram_available_mb"] = round(avail_ram_kb / 1024, 1)

    # Load average
    try:
        la1, la5, la15 = _sample_loadavg()
        result["load_avg_1m"] = la1
        result["load_avg_5m"] = la5
        result["load_avg_15m"] = la15
    except (OSError, IndexError, ValueError):  # fmt: skip
        result.setdefault("load_avg_1m", 0.0)
        result.setdefault("load_avg_5m", 0.0)
        result.setdefault("load_avg_15m", 0.0)

    # DB directory size — uses storage proxy in server mode, path walk otherwise.
    _dbsize_t0 = time.time()
    result["db_size_mb"] = _sample_db_size(storage, db_path)
    # P11: observe dbsize sampling duration (non-fatal; bare call avoids cyclo branch).
    _observe_dbsize_ms((time.time() - _dbsize_t0) * 1000.0)

    result["sampled_at"] = time.time()
    _metrics_cache = result
    _metrics_sampled_at = time.time()

    # ── Bridge: push sampled values into Prometheus gauges ────────────────────
    yadgar_process_rss_bytes.set(result.get("rss_bytes", 0))
    yadgar_process_cpu_percent.set(result.get("daemon_cpu_pct", 0.0))
    yadgar_process_open_fds.set(result.get("open_fds", 0))

    return result


def run_metrics_sampler(pid: int, db_path: str, interval: float = 5.0) -> None:
    """Background thread: sample system metrics every `interval` seconds."""
    # First sample to prime the CPU delta baseline
    sample_system_metrics(pid, db_path)
    while True:
        time.sleep(interval)
        try:
            sample_system_metrics(pid, db_path)
        except Exception:  # noqa: BLE001 — daemon sampler loop: one bad sample must never kill the sampler thread
            pass
