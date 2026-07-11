"""The generalized `Cache` class — one class, N named instances (Car 1, v5.111).

Sizing (core 5.112.0, #49): the core cache is **byte-bounded** — its LRU eviction
budget is a % of the CORE container RAM (`YADGAR_CORE_CACHE_RAM_PCT` ×
`--memory 1g`), mirroring what backend Car 0 did to `yadgar/backend/cache.py`. The
four core read-tool namespaces (project_brief / wiki_read / wiki_query /
agent_prompt_prelude) share ONE process budget via a weighted split. This replaces
the earlier fixed `max_entries` count-cap: same keys/values/deep_copy/TTL/epoch
invalidation, only the eviction BOUND changed count→bytes (behaviour-neutral — at
10 % × 1 GiB ≈ 100 MiB / 4 namespaces the ceiling never triggers in practice; TTL +
epoch-in-key still do all real eviction).

The core machinery is deliberately SEPARATE from the backend's (own knob, own
1 GiB fallback vs the backend's 4 GiB, own cgroup reader) so the two caches size
from their own containers and never cross import graphs.

Generalizes the backend `LRUCache` (`yadgar/backend/cache.py`) into a core-side
cache whose policy is bound at CONSTRUCTION (never dispatched per call). Three
bits are genuinely new versus `LRUCache`:

  1. **deep-copy-on-return** — `LRUCache.get` returns a reference; callers that
     mutate a returned row-dict (`m["heat"] = …`) corrupt the cached value.
     `deep_copy=True` returns `copy.deepcopy(value)`. OFF (default) for
     float/vector values that are never mutated.
  2. **pluggable invalidation** — `KeyFn` (freshness lives in the key; nothing is
     ever invalidated, stale keys age out via LRU), `TTL(secs)` (value stored with
     a write-timestamp, expired on read), or `Manual` (explicit `clear()` /
     `invalidate(key)` on a mutation event). Bound at construction; NOT branched
     per get on the hot path beyond a cheap `isinstance`-free attribute check.
  3. **observability-by-construction** — `get`/`put` carry `@observe(tier="hot")`
     (an I33 span source with zero per-call metric/log). Hit/miss/evict counts are
     always captured in cheap internal ints; a `"cold"` instance ALSO emits the
     generic Car 0 `yadgar_cache_{hit,miss,evictions}_total{cache=<name>}` family
     inline per get (rare caches), while a `"hot"` instance stays metric-only via
     the internal ints (a scrape collector reads them — no per-get `.labels().inc()`).

The epoch invalidation primitive is NOT re-implemented here: an epoch cache is a
`KeyFn` whose `key_fn` appends `_current_epoch(dir)` from the existing bus in
`yadgar/_shared/runtime/cache_epoch.py`.

I13: getters kept flat (nesting ≤ 4); policy objects are thin dataclasses.
"""

from __future__ import annotations

import copy
import threading
import time as _time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from yadgar._shared.observability.metrics import (
    record_cache_evict,
    record_cache_hit,
    record_cache_miss,
)
from yadgar._shared.observability.observe import observe

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Invalidation policy objects (thin; bound at construction) ─────────────────


@dataclass(frozen=True)
class KeyFn:
    """Freshness lives in the key (ckpt-hash / time-bucket / epoch). Default.

    Nothing is ever explicitly invalidated — when the effective key moves, the
    prior entry simply becomes unreachable and ages out via LRU.
    """


@dataclass(frozen=True)
class TTL:
    """Value expires `seconds` after write, checked on read."""

    seconds: float


@dataclass(frozen=True)
class Manual:
    """Explicit `clear()` / `invalidate(key)` on a mutation event (whole-flush)."""


Invalidation = KeyFn | TTL | Manual


def _identity(key: Hashable) -> Hashable:
    return key


# ── RAM-% byte-budget machinery (core; mirrors backend Car 0, own container) ───
#
# Core cache byte budget = YADGAR_CORE_CACHE_RAM_PCT × core container memory
# (cgroup). Split across the core read-tool namespaces by fixed weights. The core
# process runs `--memory 1g` (flake.nix), so the fallback (when no cgroup limit is
# readable, e.g. the test suite on a dev host) is 1 GiB — NOT the backend's 4 GiB.

# Cgroup v2 (memory.max) then v1 (memory.limit_in_bytes).
_CORE_CGROUP_V2 = "/sys/fs/cgroup/memory.max"
_CORE_CGROUP_V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

# Fallback assumed CORE container size when no cgroup limit is readable: 1 GiB
# matches the documented `--memory 1g` core container envelope (flake.nix).
_CORE_FALLBACK_CONTAINER_BYTES = 1 * 1024**3

# Namespace budget weights (relative share of the total core budget). The four
# core read-tool caches hold small dicts (a brief dict, a wiki page dict, a fuzzy
# result list, a prompt-prelude dict) — comparable footprints, so equal weights.
# Weights are normalised to the namespaces actually requested.
_NAMESPACE_WEIGHTS = {
    "project_brief": 1.0,
    "wiki_read": 1.0,
    "wiki_query": 1.0,
    "agent_prompt_prelude": 1.0,
}


@observe(tier="hot", metric="cache.estimate_bytes")
def _estimate_bytes(value: Any) -> int:
    """Approximate stored byte size of ``value`` (LRU byte-budget signal).

    Uses the msgpack encoding length — consistent across value types. Falls back to
    a coarse ``sys.getsizeof`` on any encode failure (a value msgpack can't encode,
    e.g. a nested non-primitive; never fail a put over an estimate).
    """
    try:
        import msgpack  # noqa: PLC0415

        return len(msgpack.packb(value, use_bin_type=True))
    except Exception:  # noqa: BLE001 — estimate only; never fail a put
        import sys  # noqa: PLC0415

        return sys.getsizeof(value)


@observe(tier="stage", metric="cache.read_container_memory")
def _read_container_memory_bytes() -> int | None:
    """Return the core container memory limit in bytes, or None if unbounded."""
    for path_str in (_CORE_CGROUP_V2, _CORE_CGROUP_V1):
        try:
            from pathlib import Path  # noqa: PLC0415

            raw = Path(path_str).read_text().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        # cgroup "no limit" sentinels are absurdly large — treat as unbounded.
        if val <= 0 or val >= 1 << 62:
            return None
        return val
    return None


def _core_cache_ram_pct() -> float:
    """% of the core container RAM budgeted for the unified core Cache."""
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob("YADGAR_CORE_CACHE_RAM_PCT", "CORE_CACHE_RAM_PCT", float, 10.0)


@observe(tier="stage", metric="cache.total_budget")
def _core_cache_total_budget_bytes(pct: float) -> int:
    """Total core cache byte budget = pct%% × core container memory (or fallback)."""
    limit = _read_container_memory_bytes()
    if limit is None:
        limit = _CORE_FALLBACK_CONTAINER_BYTES
    return int((pct / 100.0) * limit)


@observe(tier="stage", metric="cache.namespace_budget")
def _namespace_budget_bytes(
    namespace: str,
    total_budget: int,
    *,
    active: tuple[str, ...] = (
        "project_brief",
        "wiki_read",
        "wiki_query",
        "agent_prompt_prelude",
    ),
) -> int:
    """This namespace's weighted share of ``total_budget``.

    Unlike the backend (one namespace per process → each gets the full budget), the
    four core read-tool caches live in ONE process and SHARE the budget: ``active``
    defaults to all four so each gets its weighted slice.
    """
    weight_sum = sum(_NAMESPACE_WEIGHTS.get(n, 1.0) for n in active)
    if weight_sum <= 0:
        return 0
    return int(total_budget * (_NAMESPACE_WEIGHTS.get(namespace, 1.0) / weight_sum))


# ── Registry (thin: enumeration + one config surface; NOT a per-get dispatcher)


_REGISTRY: dict[str, Cache] = {}


class Cache:
    """One cache; N instances; policy bound at construction.

    Byte-bounded LRU (core 5.112.0, #49): eviction is driven by a byte budget
    (`max_bytes`), not a fixed entry count — the budget is a % of the core container
    RAM split across the core namespaces (see the module-level RAM-% machinery).

    Args:
        name: bounded `{cache="<name>"}` metric label + registry key. REQUIRED;
            self-registers into `_REGISTRY` (raises `ValueError` on a duplicate).
        max_bytes: byte budget for this namespace — LRU-evict until the total
            estimated stored bytes fit. `0` = disabled (all puts no-op, all gets
            miss — the kill-switch / whole-flush case).
        invalidation: `KeyFn()` (default) | `TTL(secs)` | `Manual()`.
        key_fn: pluggable key derivation embedding freshness (default identity).
        deep_copy: `copy.deepcopy` on `get` return — ON for row-dict values, OFF
            for floats/vectors.
        obs_tier: `"cold"` = inline `record_cache_*` per get + full tri-signal;
            `"hot"` = metric-only via internal ints (no per-get label-inc).
        clock: injectable time source (tests). Defaults to `time.monotonic`.
    """

    def __init__(
        self,
        name: str,
        max_bytes: int,
        invalidation: Invalidation | None = None,
        key_fn: Callable[..., Hashable] = _identity,
        deep_copy: bool = False,
        obs_tier: str = "cold",
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        if name in _REGISTRY:
            raise ValueError(f"Cache name already registered: {name!r}")
        self.name = name
        self.max_bytes = max_bytes
        self._invalidation: Invalidation = invalidation if invalidation is not None else KeyFn()
        self._key_fn = key_fn
        self._deep_copy = deep_copy
        self._obs_tier = obs_tier
        self._clock = clock
        self._ttl = self._invalidation.seconds if isinstance(self._invalidation, TTL) else None
        # value store; for TTL instances the stored value is (write_ts, value).
        self._store: OrderedDict[Hashable, Any] = OrderedDict()
        # parallel byte-size map (effective_key → estimated bytes).
        self._sizes: dict[Hashable, int] = {}
        self.current_bytes = 0
        self._lock = threading.Lock()
        # cheap internal counters — always current; the scrape source for hot tier.
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        _REGISTRY[name] = self

    # ── Core ops ─────────────────────────────────────────────────────────────

    @observe(tier="hot", metric="cache.get")
    def get(self, key: Hashable) -> Any | None:
        """Return value for key (deep-copied if configured), or None on miss."""
        if self.max_bytes == 0:
            self._record_miss()
            return None
        eff = self._key_fn(key)
        with self._lock:
            entry = self._store.get(eff)
            if entry is None or self._is_expired(entry):
                if entry is not None:  # expired TTL entry — drop it
                    self._drop_locked(eff)
                self._record_miss()
                return None
            self._store.move_to_end(eff)
            value = entry[1] if self._ttl is not None else entry
            self._record_hit()
        return copy.deepcopy(value) if self._deep_copy else value

    @observe(tier="hot", metric="cache.put")
    def put(self, key: Hashable, value: Any) -> None:
        """Insert or update key → value; byte-bounded LRU eviction when over budget.

        With deep_copy=True the STORED value is a deep copy of the caller's object,
        so a caller that keeps mutating the object it passed in (e.g. the miss-path
        return value it also received) cannot corrupt the cached copy. Symmetric
        with the deep-copy on get.
        """
        if self.max_bytes == 0:
            return
        eff = self._key_fn(key)
        if self._deep_copy:
            value = copy.deepcopy(value)
        stored = (self._clock(), value) if self._ttl is not None else value
        nbytes = _estimate_bytes(value)
        with self._lock:
            if eff in self._store:
                self.current_bytes -= self._sizes.get(eff, 0)
                self._store[eff] = stored
                self._store.move_to_end(eff)
            else:
                self._store[eff] = stored
            self._sizes[eff] = nbytes
            self.current_bytes += nbytes
            self._evict_to_budget_locked(keep=eff)

    # ── Invalidation ─────────────────────────────────────────────────────────

    @observe(tier="stage", metric="cache.invalidate")
    def invalidate(self, scope: Hashable = None) -> None:
        """Drop a single effective key (Manual bust). Rare/structural → tier=stage
        (span + ERROR-on-raise log; spec §7 'log on bust ALWAYS')."""
        eff = self._key_fn(scope)
        with self._lock:
            self._drop_locked(eff)

    @observe(tier="stage", metric="cache.clear")
    def clear(self) -> None:
        """Whole-flush (rules case / Manual). Rare/structural → tier=stage."""
        with self._lock:
            self._store.clear()
            self._sizes.clear()
            self.current_bytes = 0

    # ── Introspection ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """{hits, misses, evictions, size, bytes} — the scrape collector reads this."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "size": len(self._store),
            "bytes": self.current_bytes,
        }

    # ── internals (lock held by callers where noted) ─────────────────────────

    def _is_expired(self, entry: Any) -> bool:
        if self._ttl is None:
            return False
        return (self._clock() - entry[0]) > self._ttl

    def _drop_locked(self, eff: Hashable) -> None:
        """Remove one effective key + its byte accounting (lock held by caller)."""
        if self._store.pop(eff, None) is not None:
            self.current_bytes -= self._sizes.pop(eff, 0)

    @observe(tier="hot", metric="cache.evict")
    def _evict_to_budget_locked(self, keep: Hashable) -> None:
        """Evict LRU entries until current_bytes ≤ max_bytes (never evict `keep`)."""
        while self.current_bytes > self.max_bytes and len(self._store) > 1:
            old_key, _ = next(iter(self._store.items()))
            if old_key == keep:
                break
            self._store.popitem(last=False)
            self.current_bytes -= self._sizes.pop(old_key, 0)
            self.evictions += 1
            self._record_evict()

    def _record_hit(self) -> None:
        self.hits += 1
        if self._obs_tier == "cold":
            record_cache_hit(self.name)

    def _record_miss(self) -> None:
        self.misses += 1
        if self._obs_tier == "cold":
            record_cache_miss(self.name)

    def _record_evict(self) -> None:
        if self._obs_tier == "cold":
            record_cache_evict(self.name)
