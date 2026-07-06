"""The generalized `Cache` class — one class, N named instances (Car 1, v5.111).

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
`yadgar/server/tools/_recall_shadow.py`.

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

from yadgar.metrics import record_cache_evict, record_cache_hit, record_cache_miss
from yadgar.observability.observe import observe

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


# ── Registry (thin: enumeration + one config surface; NOT a per-get dispatcher)


_REGISTRY: dict[str, Cache] = {}


class Cache:
    """One cache; N instances; policy bound at construction.

    Args:
        name: bounded `{cache="<name>"}` metric label + registry key. REQUIRED;
            self-registers into `_REGISTRY` (raises `ValueError` on a duplicate).
        max_entries: bounded LRU cap; `0` = unbounded (rules whole-flush case).
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
        max_entries: int,
        invalidation: Invalidation | None = None,
        key_fn: Callable[..., Hashable] = _identity,
        deep_copy: bool = False,
        obs_tier: str = "cold",
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        if name in _REGISTRY:
            raise ValueError(f"Cache name already registered: {name!r}")
        self.name = name
        self._max = max_entries
        self._invalidation: Invalidation = invalidation if invalidation is not None else KeyFn()
        self._key_fn = key_fn
        self._deep_copy = deep_copy
        self._obs_tier = obs_tier
        self._clock = clock
        self._ttl = self._invalidation.seconds if isinstance(self._invalidation, TTL) else None
        # value store; for TTL instances the stored value is (write_ts, value).
        self._store: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()
        # cheap internal counters — always current; the scrape source for hot tier.
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        _REGISTRY[name] = self

    # ── Core ops ─────────────────────────────────────────────────────────────

    @observe(tier="hot", name="cache.get")
    def get(self, key: Hashable) -> Any | None:
        """Return value for key (deep-copied if configured), or None on miss."""
        eff = self._key_fn(key)
        with self._lock:
            entry = self._store.get(eff)
            if entry is None or self._is_expired(entry):
                if entry is not None:  # expired TTL entry — drop it
                    del self._store[eff]
                self._record_miss()
                return None
            self._store.move_to_end(eff)
            value = entry[1] if self._ttl is not None else entry
            self._record_hit()
        return copy.deepcopy(value) if self._deep_copy else value

    @observe(tier="hot", name="cache.put")
    def put(self, key: Hashable, value: Any) -> None:
        """Insert or update key → value. Evicts the LRU entry when over cap.

        With deep_copy=True the STORED value is a deep copy of the caller's object,
        so a caller that keeps mutating the object it passed in (e.g. the miss-path
        return value it also received) cannot corrupt the cached copy. Symmetric
        with the deep-copy on get.
        """
        eff = self._key_fn(key)
        if self._deep_copy:
            value = copy.deepcopy(value)
        stored = (self._clock(), value) if self._ttl is not None else value
        with self._lock:
            if eff in self._store:
                self._store[eff] = stored
                self._store.move_to_end(eff)
                return
            self._store[eff] = stored
            if self._max and len(self._store) > self._max:
                self._store.popitem(last=False)
                self.evictions += 1
                self._record_evict()

    # ── Invalidation ─────────────────────────────────────────────────────────

    @observe(tier="stage", name="cache.invalidate")
    def invalidate(self, scope: Hashable = None) -> None:
        """Drop a single effective key (Manual bust). Rare/structural → tier=stage
        (span + ERROR-on-raise log; spec §7 'log on bust ALWAYS')."""
        eff = self._key_fn(scope)
        with self._lock:
            self._store.pop(eff, None)

    @observe(tier="stage", name="cache.clear")
    def clear(self) -> None:
        """Whole-flush (rules case / Manual). Rare/structural → tier=stage."""
        with self._lock:
            self._store.clear()

    # ── Introspection ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """{hits, misses, evictions, size} — the scrape collector reads this."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "size": len(self._store),
        }

    # ── internals (lock held by callers where noted) ─────────────────────────

    def _is_expired(self, entry: Any) -> bool:
        if self._ttl is None:
            return False
        return (self._clock() - entry[0]) > self._ttl

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
