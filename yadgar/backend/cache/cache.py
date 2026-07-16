"""Unified byte-bounded backend cache (``Cache``) + invalidation policies.

  * ``Cache`` (backend 5.17.0, Car 0 of the backend caching train) — the unified
    backend cache, one class / N named instances / policy bound at construction.
    Mirrors the core ``yadgar/cache.py`` ``Cache`` but with **byte-bounded LRU
    eviction** (a % of the backend container RAM) instead of a fixed entry cap,
    because backend entries vary wildly (a ``ce`` score is ~8 bytes; a
    ``memory_doc`` holds full content + embedding). ``_ce_cache`` / ``_embed_cache``
    fold into it behaviour-neutrally (same keys, same values, same
    ModelCkpt-in-key invalidation, same external metric series, same snapshot).

The original count-capped ``LRUCache`` + the shared msgpack snapshot format
(``_write_snapshot`` / ``_read_snapshot``) live in the sibling ``lru.py`` (task
#18 C2 internal split); ``Cache.save_snapshot`` / ``load_snapshot`` delegate to
those free functions for ce/embed parity. ``ScopeVersions`` (version-in-key
invalidation) lives in ``scope_versions.py``. Both are re-exported through the
package ``__init__`` so external importers are byte-unaffected.

Namespace factory functions, per-namespace enabled/TTL resolvers, and the
RAM-% byte-budget machinery (``_read_container_memory_bytes``,
``_backend_cache_total_budget_bytes``, ``_namespace_budget_bytes``,
``_CGROUP_*``, ``_FALLBACK_CONTAINER_BYTES``, ``_NAMESPACE_WEIGHTS``) live in
the sibling ``cache_budgets.py`` (backend 5.51.0, I13 ≤500 split). Registry
getters (``get_ce_cache`` etc.) also live there; re-exported below.

Consumers depend on ``CacheProtocol`` (``get`` / ``put`` / ``invalidate`` /
``stats``), never the concrete class — constructor-DI ready. ``NullCache`` is the
disable/test double.

I13: nesting ≤ 4.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yadgar._shared.observability.metrics import (
    record_cache_evict,
    record_cache_hit,
    record_cache_miss,
)
from yadgar._shared.observability.observe import observe

# LRUCache + the shared msgpack snapshot format moved to ``lru.py`` (task #18 C2
# internal split). ``Cache.save_snapshot``/``load_snapshot`` delegate to the
# ``_write_snapshot`` / ``_read_snapshot`` free functions; both are re-exported
# from the package ``__init__`` for back-compat importers.
from yadgar.backend.cache.lru import (
    LRUCache as LRUCache,  # noqa: PLC0414 — intentional re-export
)
from yadgar.backend.cache.lru import (
    _read_snapshot,
    _write_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Unified backend Cache (Car 0, backend 5.17.0)
# ═══════════════════════════════════════════════════════════════════════════
#
# One class, N named instances; policy bound at construction (never dispatched
# per call). Byte-bounded LRU eviction sized from a % of the backend container
# RAM. ce/embed fold in behaviour-neutrally.


# ── Invalidation policy objects (thin; bound at construction) ─────────────────


@dataclass(frozen=True)
class ModelCkpt:
    """Freshness lives in the key via a model-checkpoint hash suffix.

    Model swap changes ``ckpt_sha`` → old keys become unreachable and age out via
    LRU. Nothing is ever explicitly invalidated (the ce/embed contract today).
    """


@dataclass(frozen=True)
class DataEpoch:
    """Freshness lives in the key via a structural ``data_epoch`` suffix.

    Reserved for the data namespaces (memory_doc / engram_slot / graph) landing in
    later cars. Same key-embedding mechanism as ModelCkpt; different bump source.
    """


@dataclass(frozen=True)
class TTL:
    """Value expires ``seconds`` after write, checked on read."""

    seconds: float


@dataclass(frozen=True)
class Manual:
    """Explicit ``invalidate(key)`` / ``clear()`` on a mutation event."""


Invalidation = ModelCkpt | DataEpoch | TTL | Manual


def _identity(key: Hashable) -> Hashable:
    return key


@observe(tier="hot", metric="backend.cache.estimate_bytes")
def _estimate_bytes(value: Any) -> int:
    """Approximate stored byte size of ``value`` (LRU byte-budget signal).

    Uses the msgpack encoding length — the same serialization the snapshot path
    uses, so the estimate is consistent across value types (a bare float ≈ 9 B, a
    100-float vector ≈ hundreds of bytes, a full doc dict scales with content).
    Falls back to a coarse ``sys.getsizeof`` on any encode failure.
    """
    try:
        import msgpack  # noqa: PLC0415

        return len(msgpack.packb(value, use_bin_type=True))
    except Exception:  # noqa: BLE001 — estimate only; never fail a put
        import sys  # noqa: PLC0415

        return sys.getsizeof(value)


# ── Protocol + registry ───────────────────────────────────────────────────────


# Car 2 (folder-split #17): CacheProtocol moved to yadgar/_shared/protocols.py
# (the single home for cross-boundary seams). Re-exported here for back-compat —
# existing consumers still ``from yadgar.backend.cache import CacheProtocol``.
# The redundant ``as CacheProtocol`` alias marks this an intentional re-export
# (ruff F401 pass).
from yadgar._shared.contracts.protocols import CacheProtocol as CacheProtocol  # noqa: E402,PLC0414

# Backend registry — enumeration + one config surface. Tolerates re-registration
# (importlib.reload of embed_service re-creates the ce/embed namespaces in tests).
# This differs from the core registry, which raises on a duplicate name.
_REGISTRY: dict[str, Cache] = {}


class Cache:
    """One unified backend cache; N named instances; policy bound at construction.

    Args:
        name: bounded ``{cache="<name>"}`` metric label + registry key. REQUIRED.
        max_bytes: byte budget for this namespace. LRU-evict until the total
            estimated stored bytes fit. ``0`` = disabled (all puts no-op) — the
            fold-in disable path (mirrors the old ``max_entries=0``).
        invalidation: ``ModelCkpt`` (default) | ``DataEpoch`` | ``TTL(secs)`` |
            ``Manual``. Freshness for ModelCkpt/DataEpoch lives in the key.
        key_fn: pluggable key derivation embedding freshness (default identity).
        deep_copy: ``copy.deepcopy`` on ``get``/``put`` — ON for row-dict values,
            OFF for floats/vectors (never mutated on the ce/embed hot path).
        obs_tier: ``"cold"`` = inline ``record_cache_*`` per get (generic
            ``yadgar_cache_*`` family) + internal ints; ``"hot"`` = internal ints
            only (a scrape collector reads them — no per-get ``.labels().inc()``).
        checkpoint_hash: model-checkpoint hash stamped into the snapshot; a load
            with a mismatching hash discards the snapshot (ce/embed model-swap).
        clock: injectable time source (tests). Defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        name: str,
        max_bytes: int,
        invalidation: Invalidation | None = None,
        key_fn: Callable[..., Hashable] = _identity,
        deep_copy: bool = False,
        obs_tier: str = "cold",
        checkpoint_hash: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.max_bytes = max_bytes
        self._invalidation: Invalidation = invalidation if invalidation is not None else ModelCkpt()
        self._key_fn = key_fn
        self._deep_copy = deep_copy
        self._obs_tier = obs_tier
        self._ckpt = checkpoint_hash
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
        _REGISTRY[name] = self  # overwrite-on-dup (reload-safe)

    # ── Core ops ─────────────────────────────────────────────────────────────

    @observe(tier="hot", metric="backend.cache.get")
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

    @observe(tier="hot", metric="backend.cache.put")
    def put(self, key: Hashable, value: Any) -> None:
        """Insert/update key → value; byte-bounded LRU eviction when over budget."""
        if self.max_bytes == 0:
            return
        if self._deep_copy:
            value = copy.deepcopy(value)
        stored = (self._clock(), value) if self._ttl is not None else value
        nbytes = _estimate_bytes(value)
        eff = self._key_fn(key)
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

    @observe(tier="stage", metric="backend.cache.invalidate")
    def invalidate(self, scope: Hashable = None) -> None:
        """Drop a single effective key (Manual bust). Rare/structural → tier=stage."""
        eff = self._key_fn(scope)
        with self._lock:
            self._drop_locked(eff)

    @observe(tier="stage", metric="backend.cache.clear")
    def clear(self) -> None:
        """Whole-flush (Manual). Rare/structural → tier=stage."""
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

    @property
    def size_entries(self) -> int:
        """Entry count — consumer + CacheStatsCollector surface (LRUCache parity)."""
        return len(self._store)

    @property
    def size_bytes(self) -> int:
        """Tracked approximate stored bytes (LRUCache parity, but real not shallow)."""
        return self.current_bytes

    # ── Snapshot I/O (delegates to LRUCache format for ce/embed parity) ───────

    @observe(tier="stage", metric="backend.cache.save_snapshot")
    def save_snapshot(self, snap_dir: str, name: str) -> None:
        """Serialize to <snap_dir>/<name>.snap (shared LRUCache msgpack format).

        Only meaningful for non-TTL namespaces (ce/embed); the stored value is the
        raw value, mirroring LRUCache.
        """
        with self._lock:
            items = [(k, v) for k, v in self._store.items()]
        _write_snapshot(items, self._ckpt, snap_dir, name)

    @observe(tier="stage", metric="backend.cache.load_snapshot")
    def load_snapshot(self, snap_dir: str, name: str) -> None:
        """Restore entries from <snap_dir>/<name>.snap (ckpt-gated, byte-bounded)."""
        items = _read_snapshot(self._ckpt, snap_dir, name)
        if items is None:
            return
        with self._lock:
            self._store.clear()
            self._sizes.clear()
            self.current_bytes = 0
            for k, v in items:
                self._store[k] = v
                nb = _estimate_bytes(v)
                self._sizes[k] = nb
                self.current_bytes += nb
                self._evict_to_budget_locked(keep=k)

    def snapshot_age_seconds(self, snap_dir: str, name: str) -> float:
        """Return seconds since snapshot was last written, or -1 if no file."""
        path = Path(snap_dir) / f"{name}.snap"
        if not path.exists():
            return -1.0
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return -1.0

    # ── internals (lock held by callers where noted) ─────────────────────────

    def _is_expired(self, entry: Any) -> bool:
        if self._ttl is None:
            return False
        return (self._clock() - entry[0]) > self._ttl

    def _drop_locked(self, eff: Hashable) -> None:
        if self._store.pop(eff, None) is not None:
            self.current_bytes -= self._sizes.pop(eff, 0)

    @observe(tier="hot", metric="backend.cache.evict")
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


# ── Version-in-key invalidation ───────────────────────────────────────────────
#
# ``ScopeVersions`` + the process-global ``_SCOPE_VERSIONS`` singleton +
# ``get_scope_versions()`` moved to ``scope_versions.py`` (task #18 C2 internal
# split). Re-exported from the package ``__init__`` for back-compat importers
# (the StorageEngine slot/graph read paths + the invalidation e2e tests).


class NullCache:
    """Always-miss / no-op cache (disable + test double). Implements CacheProtocol."""

    def get(self, key: Hashable) -> Any | None:  # noqa: ARG002
        return None

    def put(self, key: Hashable, value: Any) -> None:  # noqa: ARG002
        return None

    def invalidate(self, scope: Hashable = None) -> None:  # noqa: ARG002
        return None

    def stats(self) -> dict:
        return {"hits": 0, "misses": 0, "evictions": 0, "size": 0, "bytes": 0}


# Namespace factories + RAM-% budget machinery live in the sibling
# ``cache_budgets.py`` (backend 5.51.0, I13 ≤500 split). Package-path importers
# (``from yadgar.backend.cache import get_ce_cache``) reach them via the
# ``__init__`` PEP-562 re-export shim. Direct ``...cache.cache`` submodule
# importers are internal only — none exist in the current codebase.
