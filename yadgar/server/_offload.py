"""Tool-body offload primitive — runs sync MCP tool bodies off the asyncio loop.

Fix A (P1 of the daemon-hang RCA). The core daemon registers ~60 sync `def` tool
bodies through a single wrapper (`_app._instrumented`); FastMCP runs a sync tool
body INLINE on the event loop, so a blocking body (the proven inline `git`
subprocess, or any IO) freezes the loop and starves `/health`. This module
dispatches the sync body onto a dedicated BOUNDED ThreadPoolExecutor via
`loop.run_in_executor`, wrapped in `asyncio.wait_for`, so the loop stays free.

Design (advisor-vetted — see docs/plans/daemon-offload-A-BUILD-NOTES.md):

- Default OFF for the first release. Flip ON after live soak. OFF = today's
  inline behaviour, byte-identical, with the deployed P0 health-kill backstop.
- Bounded pool: the cap IS the concurrency control. Created lazily, bound to the
  running loop; torn down in lifecycle.shutdown().
- O2 GATE — pool-saturation health signal. The in-flight counter is decremented
  on the WORKER THREAD at true completion (inside the wrapped callable's finally),
  NOT coroutine-side. A `wait_for` timeout frees the awaiting coroutine but the
  wedged worker keeps its pool slot; a coroutine-side decrement would make the
  counter lie ("free" while occupied) → /health 200 → P0 cannot kill the stall.
  Saturation uses COMPLETION-STALENESS (inflight>=max AND idle>grace), not
  "full-since", so a healthy daemon draining a legit peak is never flagged.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# ---------------------------------------------------------------------------
# Knob resolution (I25 three-way style; live os.environ read for test override)
# ---------------------------------------------------------------------------

_TRUTHY = ("1", "true", "yes", "on")


def offload_enabled() -> bool:
    """True if tool-body offload is enabled. Default OFF (first release).

    Env-only read (live, for test override) — the config.yaml layer is NOT
    consulted here, matching the existing live-override convention (e.g.
    YADGAR_AUTO_CAPTURE_RATE_LIMIT). Production flips this via container env, so
    the real path is unaffected; a yaml-only OFFLOAD_TOOLS would show in
    /admin/config but the daemon would run OFF.
    """
    return os.environ.get("YADGAR_OFFLOAD_TOOLS", "0").strip().lower() in _TRUTHY


def _pool_workers() -> int:
    try:
        n = int(os.environ.get("YADGAR_TOOL_POOL_WORKERS", "8"))
    except ValueError:
        n = 8
    return max(1, n)


def _tool_timeout_sec() -> float:
    try:
        return float(os.environ.get("YADGAR_TOOL_TIMEOUT_SEC", "30"))
    except ValueError:
        return 30.0


def _heavy_concurrency() -> int:
    """Max concurrent backend /rerank calls the core will issue (#74 fix #2).

    The fan-out gate. With offload ON the loop is free, so up to _pool_workers()
    recalls run concurrently — each issuing a backend /rerank. The backend has
    FEWER cores than the pool; an unbounded fan-out saturates it and the resulting
    slow /health feeds back into a core readiness 503 → P0 kill (#74 root cause).

    Default is CONSERVATIVE and strictly below the pool size — the binding
    constraint is backend serving capacity, NOT TOOL_POOL_WORKERS. A default ==
    pool would make the gate a no-op. Clamped to [1, pool_workers].
    """
    try:
        n = int(os.environ.get("YADGAR_RECALL_HEAVY_CONCURRENCY", "3"))
    except ValueError:
        n = 3
    return max(1, min(n, _pool_workers()))


def _rerank_gate_acquire_timeout_sec() -> float:
    """Seconds a worker waits for a heavy-rerank slot before degrading.

    Bounded so a worker parked on the gate cannot hold its pool slot past the
    tool timeout (which would leak it — fix #3). On timeout the caller degrades
    (skips rerank → pre-rerank order), reusing the breaker-open path.
    """
    try:
        return float(os.environ.get("YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC", "2.0"))
    except ValueError:
        return 2.0


def _saturation_grace_sec() -> float:
    """Idle seconds (no completion) while the pool is full before /health degrades.

    MUST be > the wait_for timeout so a legit op that completes within the timeout
    keeps resetting last_completion_ts (no false degrade), while a leaked worker
    thread (op exceeds timeout, slot held) eventually trips the signal.
    """
    try:
        return float(os.environ.get("YADGAR_TOOL_SATURATION_GRACE_SEC", "45"))
    except ValueError:
        return 45.0


# ---------------------------------------------------------------------------
# Pool + occupancy tracking (true occupancy, worker-thread accounted)
# ---------------------------------------------------------------------------

_POOL: ThreadPoolExecutor | None = None
_POOL_MAX: int = 0
_POOL_LOCK = threading.Lock()

# Occupancy counters guarded by _STAT_LOCK. _inflight is the number of callables
# reserved/EXECUTING on a worker thread (incremented on submit, decremented on
# the worker at true completion). _last_completion_ts is monotonic time of the
# most recent worker completion. _pool_full_since is the monotonic time the pool
# most recently reached full occupancy (reset to 0 whenever it drops below full)
# — it provides a staleness baseline for the cold case where the pool fills with
# wedged ops and NOTHING ever completes (so last_completion_ts stays 0).
_STAT_LOCK = threading.Lock()
_inflight: int = 0
_last_completion_ts: float = 0.0
_pool_full_since: float = 0.0


def _inc_inflight() -> None:
    """Reserve a slot (called before submit). Stamps _pool_full_since on fill."""
    global _inflight, _pool_full_since
    with _STAT_LOCK:
        _inflight += 1
        if _POOL_MAX > 0 and _inflight >= _POOL_MAX and _pool_full_since == 0.0:
            _pool_full_since = time.monotonic()


def _dec_inflight(*, completed: bool) -> None:
    """Release a slot. `completed` True → stamp last_completion (worker finished).

    Worker-side completion is the O2 truth source. Dropping below full clears the
    full-since baseline so the next fill re-arms it.
    """
    global _inflight, _last_completion_ts, _pool_full_since
    with _STAT_LOCK:
        _inflight -= 1
        if completed:
            _last_completion_ts = time.monotonic()
        if _inflight < _POOL_MAX:
            _pool_full_since = 0.0


def _ensure_pool() -> ThreadPoolExecutor:
    """Lazily create the bounded pool (bound to whatever loop is running)."""
    global _POOL, _POOL_MAX
    with _POOL_LOCK:
        if _POOL is None:
            _POOL_MAX = _pool_workers()
            _POOL = ThreadPoolExecutor(max_workers=_POOL_MAX, thread_name_prefix="yadgar-tool")
        return _POOL


def shutdown_pool(join_timeout: float = 5.0) -> None:
    """Tear down the pool on graceful stop (O10).

    cancel_futures drops still-queued work; wait=False means we do NOT block on
    wedged in-flight worker threads (they cannot be killed — P0 health-kill is the
    backstop). A bounded best-effort join keeps shutdown prompt.
    """
    global _POOL, _POOL_MAX, _inflight, _last_completion_ts, _pool_full_since
    with _POOL_LOCK:
        pool = _POOL
        _POOL = None
        _POOL_MAX = 0
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # pragma: no cover — py<3.9 has no cancel_futures
            pool.shutdown(wait=False)
    with _STAT_LOCK:
        _inflight = 0
        _last_completion_ts = 0.0
        _pool_full_since = 0.0


# ---------------------------------------------------------------------------
# Heavy-rerank fan-out gate (#74 fix #2) — a process-wide semaphore bounding
# how many concurrent backend /rerank calls the core issues. Sized to the
# backend's REAL serving capacity (a conservative default below the pool size),
# NOT TOOL_POOL_WORKERS. Module-level singleton so all RemoteMLClient instances
# are bounded COLLECTIVELY. Lazy env-read so tests can override the size.
# ---------------------------------------------------------------------------

_RERANK_GATE: threading.Semaphore | None = None
_RERANK_GATE_SIZE: int = 0
_RERANK_GATE_LOCK = threading.Lock()


def _rerank_gate() -> threading.Semaphore:
    """Lazily create the process-singleton heavy-rerank semaphore."""
    global _RERANK_GATE, _RERANK_GATE_SIZE
    with _RERANK_GATE_LOCK:
        if _RERANK_GATE is None:
            _RERANK_GATE_SIZE = _heavy_concurrency()
            _RERANK_GATE = threading.Semaphore(_RERANK_GATE_SIZE)
        return _RERANK_GATE


def acquire_rerank_slot(timeout: float | None = None) -> bool:
    """Acquire one heavy-rerank slot. Returns True on success, False on timeout.

    A False return is a DEGRADE signal — the caller should skip the rerank
    (pre-rerank order) rather than block its worker thread waiting (which would
    hold the pool slot past the tool timeout and leak it). Default timeout from
    YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC.
    """
    if timeout is None:
        timeout = _rerank_gate_acquire_timeout_sec()
    return _rerank_gate().acquire(timeout=timeout)


def release_rerank_slot() -> None:
    """Release a previously-acquired heavy-rerank slot. Never raises."""
    gate = _RERANK_GATE
    if gate is not None:
        try:
            gate.release()
        except ValueError:  # pragma: no cover — release without acquire
            pass


def reset_rerank_gate() -> None:
    """Drop the gate so the next acquire rebuilds it at the current env size.

    Test hook (mirrors shutdown_pool for the pool). Idempotent.
    """
    global _RERANK_GATE, _RERANK_GATE_SIZE
    with _RERANK_GATE_LOCK:
        _RERANK_GATE = None
        _RERANK_GATE_SIZE = 0


def _ctx_wrap(call: Callable[[], Any]) -> Callable[[], Any]:
    """Run `call` inside a copy of the current contextvars Context AND account
    occupancy on the worker thread.

    contextvars.copy_context() captures the OTel parent span (set on the loop by
    FastMCP's call chain) so the tool body's trace_span nests correctly off-loop.

    The occupancy decrement + last_completion stamp happen HERE (worker thread,
    finally) — the O2 invariant. Doing it coroutine-side would let a wait_for
    timeout free the counter while the worker still holds the slot.
    """
    ctx = contextvars.copy_context()

    def _runner() -> Any:
        try:
            return ctx.run(call)
        finally:
            _dec_inflight(completed=True)

    return _runner


async def run_offloaded(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a sync callable off the event loop on the bounded pool.

    Disabled → call inline (today's behaviour). Enabled → dispatch to the pool
    with a wait_for timeout. On timeout the awaiting coroutine is freed (loop +
    /health stay responsive); the worker keeps running until it self-releases and
    only THEN frees its slot + stamps last_completion (O2 occupancy truth).
    """
    if not offload_enabled():
        return fn(*args, **kwargs)

    pool = _ensure_pool()
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs)

    _inc_inflight()
    try:
        fut = loop.run_in_executor(pool, _ctx_wrap(call))
    except Exception:
        # Submission itself failed — release the reservation (no worker will run).
        _dec_inflight(completed=False)
        raise

    return await asyncio.wait_for(fut, timeout=_tool_timeout_sec())


# ---------------------------------------------------------------------------
# O2 — pool-saturation health signal
# ---------------------------------------------------------------------------


def pool_stats() -> dict[str, Any]:
    """Snapshot of pool occupancy for /health + observability."""
    with _STAT_LOCK:
        inflight = _inflight
        last = _last_completion_ts
        full_since = _pool_full_since
    now = time.monotonic()
    baseline = max(last, full_since)
    return {
        "enabled": offload_enabled(),
        "max": _POOL_MAX,
        "inflight": inflight,
        "idle_seconds": round(now - baseline, 1) if baseline else 0.0,
        "saturated": pool_saturated(),
    }


def pool_saturated() -> bool:
    """True when the pool is exhausted AND nothing has completed for > grace.

    Completion-staleness, NOT "full-since": under a legitimate peak the pool stays
    full while it drains, but each completion resets last_completion_ts, so the
    idle gap never exceeds grace — no false degrade. Only a set of WEDGED workers
    (ops exceeding the wait_for timeout, holding their slots with nothing
    completing) lets idle climb past grace → the daemon is genuinely stalled and
    /health must go 503 so P0 can kill it.
    """
    if not offload_enabled():
        return False
    with _STAT_LOCK:
        inflight = _inflight
        last = _last_completion_ts
        full_since = _pool_full_since
    if _POOL_MAX <= 0 or inflight < _POOL_MAX:
        return False
    # Pool is full. Staleness baseline = the more RECENT of the last completion
    # and when the pool became full. Using the later of the two means: a draining
    # peak (recent completions) resets the clock; a fresh wedge that never
    # completes still trips once `full_since` ages past the grace.
    baseline = max(last, full_since)
    if baseline == 0.0:  # pragma: no cover — full implies full_since is set
        return False
    return (time.monotonic() - baseline) > _saturation_grace_sec()
