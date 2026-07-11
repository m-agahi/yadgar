"""CPU-awareness — the single source of truth for every recall concurrency budget.

T3 Car 3 (capability-first, user decision 2026-07-11 option B): the pipeline must
be CPU-aware and parallel-ready so that raising the backend `--cpus` fans it out
WITHOUT another code change. Everything downstream — the provider gather budget,
the heavy-rerank gate size, the torch intra-op thread count — derives from
`available_cpus()`.

`os.cpu_count()` LIES in a cgroup-limited container: it reports the host core
count, not the container's `--cpus` quota. So this module reads the cgroup quota
FIRST (v2 `cpu.max`, then v1 `cpu.cfs_quota_us` / `cpu.cfs_period_us`) and only
falls back to `os.cpu_count()` when no quota is set. The result is cached (the
quota does not change under a running process) with an explicit
`reset_cpu_cache()` for tests. It NEVER returns < 1.

Floor arithmetic — the byte-identity contract at ≤ 2 CPUs:
  - `recall_gather_budget()` = 1 at ncpu ≤ 2 (today's sequential provider calls),
    2 at ncpu ≥ 3 (capped at the provider count — memory + wiki = 2 arms, so more
    cores never widen the gather beyond 2). budget 1 ≡ the pre-Car-3 code path.
  - `recall_heavy_concurrency()` derivation (consumed by offload) = 1 at ncpu ≤ 2.
  - `torch_intraop_threads()` = 1 at ncpu ≤ 2 → today's implicit single-thread CE.

Thread-budget composition (documented, not oversubscribed): the provider gather
runs on the BACKEND (MemoryProvider → Retriever.recall → embedding/KNN). torch
intra-op threads are process-global (set once at backend lifespan). The two can
multiply on the embed/KNN stage inside a gathered provider arm, so the budgets
are chosen to compose within ncpu:

    gather_arms (≤ 2)  ×  torch_threads (≤ ncpu // 2)   ≤   ncpu  (+ 1 headroom)

i.e. at 4 CPUs: 2 gather arms × 2 torch threads = 4 ≤ 4. At 2 CPUs both collapse
to 1 (1 × 1 = 1) — the sequential floor. See `torch_intraop_threads()` for the
per-process reservation and the ADR-0011-class no-thrash rule (bounded pools
only; never spawn an unbounded fan-out on a starved box).
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path

from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe

# cgroup control-file locations (module-level so tests can monkeypatch them).
_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

# The recall gather fans out one arm per active provider. There are exactly two
# providers today (MemoryProvider + WikiProvider); the gather budget is capped at
# this so more cores never widen it past the real parallel work available.
_PROVIDER_COUNT = 2

# Cached result of available_cpus(). None = not yet computed. Guarded by _LOCK.
_CACHED_CPUS: int | None = None
_LOCK = threading.Lock()


@observe(tier="stage", span=False)
def _read_int(path: str) -> int | None:
    """Read a single integer from a cgroup control file, or None if unreadable."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@observe(tier="stage", span=False)
def _cgroup_v2_cpus() -> int | None:
    """Parse cgroup-v2 `cpu.max` = "<quota> <period>" → ceil(quota/period).

    Returns None when the file is unreadable or the quota is "max" (unlimited),
    signalling the caller to fall through to v1 / os.cpu_count().
    """
    try:
        raw = Path(_CGROUP_V2_CPU_MAX).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split()
    if not parts or parts[0] == "max":
        return None
    try:
        quota = int(parts[0])
        period = int(parts[1]) if len(parts) > 1 else 100_000
    except (ValueError, IndexError):  # fmt: skip
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


@observe(tier="stage", span=False)
def _cgroup_v1_cpus() -> int | None:
    """Parse cgroup-v1 quota/period → ceil. None when quota is -1 (unlimited)."""
    quota = _read_int(_CGROUP_V1_QUOTA)
    if quota is None or quota <= 0:  # -1 = unlimited
        return None
    period = _read_int(_CGROUP_V1_PERIOD)
    if period is None or period <= 0:
        period = 100_000
    return max(1, math.ceil(quota / period))


@observe(tier="stage", span=False)
def _detect_cpus() -> int:
    """Detect the effective CPU budget: override → cgroup v2 → v1 → os. Never < 1.

    The AVAILABLE_CPUS override resolves env > config.yaml > default(0); 0 = auto
    (fall through to cgroup/os detection), any positive value pins the count.
    """
    override = resolve_knob("YADGAR_AVAILABLE_CPUS", "AVAILABLE_CPUS", int, 0)
    if override > 0:
        return max(1, override)

    for detector in (_cgroup_v2_cpus, _cgroup_v1_cpus):
        n = detector()
        if n is not None:
            return max(1, n)

    return max(1, os.cpu_count() or 1)


@observe(tier="stage", span=False)
def available_cpus() -> int:
    """Effective CPU budget for this process. Cached; NEVER < 1.

    Resolution order: `YADGAR_AVAILABLE_CPUS` env override → cgroup-v2 `cpu.max`
    quota → cgroup-v1 `cpu.cfs_quota_us`/`cpu.cfs_period_us` → `os.cpu_count()`.
    The quota is fixed for the process lifetime, so the value is computed once and
    cached. Tests invalidate via `reset_cpu_cache()`.
    """
    global _CACHED_CPUS
    cached = _CACHED_CPUS
    if cached is not None:
        return cached
    with _LOCK:
        if _CACHED_CPUS is None:
            _CACHED_CPUS = _detect_cpus()
        return _CACHED_CPUS


@observe(tier="stage", span=False)
def reset_cpu_cache() -> None:
    """Drop the cached CPU count so the next `available_cpus()` recomputes.

    Test hook (mirrors offload's `reset_rerank_gate`). Idempotent.
    """
    global _CACHED_CPUS
    with _LOCK:
        _CACHED_CPUS = None


# ---------------------------------------------------------------------------
# Budget derivations — all a pure function of available_cpus().
# ---------------------------------------------------------------------------


@observe(tier="stage", span=False)
def _parallelism_disabled() -> bool:
    """True when `YADGAR_RECALL_PARALLELISM=1` forces sequential (ops escape hatch).

    Default `auto` = derive from available_cpus(). Any value that parses to 1
    forces the sequential floor regardless of core count; `auto` (or unset) lets
    the budget scale. This is the no-thrash / operator-override knob.
    """
    raw = resolve_knob("YADGAR_RECALL_PARALLELISM", "RECALL_PARALLELISM", str, "auto")
    raw = raw.strip().lower()
    if raw in ("auto", ""):
        return False
    try:
        return int(raw) <= 1
    except ValueError:
        return False  # unrecognised → treat as auto


@observe(tier="stage", span=False)
def recall_gather_budget() -> int:
    """Max concurrent provider arms in the recall fan-out gather.

    1 at ncpu ≤ 2 (today's sequential provider calls — byte-identical floor) OR
    when `YADGAR_RECALL_PARALLELISM=1`. Otherwise `min(ncpu - 1, _PROVIDER_COUNT)`
    — capped at the provider count (2, no third arm) and leaving ≥ 1 core for the
    torch intra-op threads the gathered arms trigger (embedding/KNN), so
    gather × torch composes within ncpu (see module docstring).
    """
    if _parallelism_disabled():
        return 1
    ncpu = available_cpus()
    if ncpu <= 2:
        return 1
    # ncpu ≥ 3: fan out to all providers (2), leaving ≥ 1 core for torch intra-op.
    # min(ncpu - 1, _PROVIDER_COUNT) keeps ≥ 1 core in reserve and never exceeds
    # the number of real arms.
    return max(1, min(ncpu - 1, _PROVIDER_COUNT))


@observe(tier="stage", span=False)
def recall_heavy_concurrency_default() -> int:
    """CPU-derived default for RECALL_HEAVY_CONCURRENCY (the backend /rerank gate).

    1 at ncpu ≤ 2 (today's default — the --cpus-2 backend serves one rerank wave).
    Scales with cores above that, capped conservatively at ncpu // 2 so the gate
    stays strictly below a widened pool. Consumed by offload._heavy_concurrency()
    when the configured value is the 0=auto sentinel.
    """
    ncpu = available_cpus()
    if ncpu <= 2:
        return 1
    return max(1, ncpu // 2)


@observe(tier="stage", span=False)
def torch_intraop_threads() -> int:
    """torch intra-op thread count for the batched CE / embedding stages.

    1 at ncpu ≤ 2 → today's implicit single-thread behavior (byte-identical: torch
    defaults are unchanged, results are thread-count invariant for inference).
    `ncpu // 2` above that, reserving the other half for the provider gather arms
    so the two budgets compose within ncpu (module docstring). Never < 1.
    Zero RAM cost, model-agnostic — the cheapest CE CPU-awareness lever.
    """
    ncpu = available_cpus()
    if ncpu <= 2:
        return 1
    return max(1, ncpu // 2)
