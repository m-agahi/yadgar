"""Unified byte-bounded backend cache (``Cache``) + namespace factories.

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


@observe(tier="stage", metric="backend.cache.get_ce_cache")
def get_ce_cache() -> CacheProtocol:
    """Return the process-global ``ce`` namespace (Car 1 recall-CE-dedup seam).

    The ``ce`` cache is a SINGLE process-wide instance (ModelCkpt-keyed), created
    and registered by ``embed_service`` at import (``_ce_cache = _make_ce_cache()``).
    Recall's reranker/fusion CE path reuses THIS instance (constructor-DI default)
    so that:
      * crossfuse's already-computed ``(query, text)`` scores are reused by
        cross_encoder / multi_passage within one request (#41 within-request dedup);
      * the process-global lifetime also gives cross-request repeat hits for free.

    Reusing the registered instance (rather than constructing a fresh
    ``Cache(name="ce")``, which would collide under the overwrite-on-dup registry
    and split hit/miss stats) keeps ONE ``ce`` object, one obs surface, one
    snapshot — and honors the ``YADGAR_CE_CACHE_ENABLED`` kill switch (disabled →
    ``max_bytes=0`` → all-miss ≡ NullCache) automatically.

    If ``embed_service`` has not been imported (e.g. a pure LocalMLClient/stdio
    process that never touched the backend HTTP module), lazily invoke its factory
    to materialise + register the namespace. The import is deferred to keep the
    retrieval hot-path import free of FastAPI.

    Import-order caveat: "one shared ``ce`` object" holds when the reranker resolves
    its cache AFTER ``embed_service`` registered ``_ce_cache`` (the normal order —
    stdio never imports embed_service, and the backend process does not build a
    retrieval ``Reranker``). If a ``Reranker`` resolved this accessor first and
    ``embed_service`` later re-ran ``_make_ce_cache()``, the overwrite-on-dup
    registry would leave the reranker holding a now-superseded instance (split
    stats). Harmless in current deployments; noted for future car wiring.
    """
    ce = _REGISTRY.get("ce")
    if ce is not None:
        return ce
    from yadgar.backend.embed_service import _make_ce_cache  # noqa: PLC0415

    return _make_ce_cache()


@observe(tier="stage", metric="backend.cache.get_memory_doc_cache")
def get_memory_doc_cache() -> CacheProtocol:
    """Return the process-global ``memory_doc`` namespace (Car 2 build_results seam).

    ``memory_doc`` caches ONLY the two immutable, KB-scale columns of a memory row
    (``content`` + ``embedding``) keyed by ``memory_id``. Everything else — heat,
    access_count, and every consolidation/decay-mutated field — is fetched fresh on
    every recall via a light ``SELECT * OMIT content, embedding`` query, so recall
    output stays byte-identical INCLUDING live heat (heat-freshness by construction).

    Invalidation is ``TTL`` (a 30-60 min backstop that bounds worst-case staleness
    from a content edit / reembed) plus an optional per-id ``invalidate(memory_id)``
    on ``memory_update(content)``. Deliberately NOT the cross-service structural
    ``data_epoch`` (deferred — the spec flags it as the highest-risk correctness
    item; TTL is the simplest-correct backstop for Car 2). DELETE needs no
    invalidation: a deleted memory is never a fusion candidate (retrieval queries
    the live DB), so ``build_results`` never re-fetches it, and memory ids are
    monotonic (``_next_id`` UPSERT counter) so a stale entry can never be served
    for a reused id.

    Single process-wide instance (constructor-DI default on ``StorageEngine``); a
    ``NullCache`` injected ⇒ always-miss ⇒ today's single-query behaviour. The
    ``YADGAR_MEMORY_DOC_CACHE_ENABLED`` kill switch (disabled ⇒ ``max_bytes=0`` ⇒
    all-miss ≡ NullCache) is honoured automatically.
    """
    existing = _REGISTRY.get("memory_doc")
    if existing is not None:
        return existing
    return _make_memory_doc_cache()


def _memory_doc_cache_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_MEMORY_DOC_CACHE_ENABLED",
        "MEMORY_DOC_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


def _memory_doc_cache_ttl_sec() -> float:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    # 45 min default — a TTL backstop that bounds worst-case staleness from a
    # content edit / reembed that bypasses the per-id evict path. Cache lifetime
    # ≈ this interval; content is immutable except on edit/reembed.
    return resolve_knob(
        "YADGAR_MEMORY_DOC_CACHE_TTL_SEC", "MEMORY_DOC_CACHE_TTL_SEC", float, 2700.0
    )


@observe(tier="stage", metric="backend.cache.make_memory_doc_cache")
def _make_memory_doc_cache() -> Cache:
    """Build (and register) the unified ``memory_doc`` namespace (Car 2).

    Byte-budget from RAM-% (``memory_doc`` weight 4.0 — entries hold full content +
    embedding, large). TTL invalidation; ``deep_copy=True`` because the cached
    value carries a mutable ``embedding`` list/bytes and full row dicts.
    """
    if not _memory_doc_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct_local())
        budget = _namespace_budget_bytes("memory_doc", total, active=("memory_doc",))
    return Cache(
        name="memory_doc",
        max_bytes=budget,
        invalidation=TTL(_memory_doc_cache_ttl_sec()),
        deep_copy=True,
        obs_tier="cold",
    )


def _backend_cache_ram_pct_local() -> float:
    """% of backend container RAM budgeted for the unified backend cache.

    Duplicated here (not imported from ``embed_service``) so the ``memory_doc``
    cache — resolved from the storage layer — never pulls the FastAPI embed_service
    module into the retrieval hot-path import graph.
    """
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob("YADGAR_BACKEND_CACHE_RAM_PCT", "BACKEND_CACHE_RAM_PCT", float, 10.0)


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


@observe(tier="stage", metric="backend.cache.get_engram_slot_cache")
def get_engram_slot_cache() -> CacheProtocol:
    """Return the process-global ``engram_slot`` namespace (Car 3 slot-links seam).

    ``engram_slot`` caches ONLY the STRUCTURAL slot membership: the ordered
    (``created_at``) candidate memory ids for a slot, keyed by
    ``(slot_index, slot_version)``. The volatile ``heat>0`` predicate (and the
    live ``slot_index`` match) is re-verified FRESH on every read against the
    cached candidate ids — so heat→0 decay, delete, and reslot-away are all inert
    (the fresh recheck drops them) with NO version bump. The version-in-key covers
    the ONLY vector the recheck cannot: a NEW member appearing in the slot
    (create-alloc / reslot-into), bumped at ``assign_memory_slot`` (ops.py).

    Single process-wide instance (constructor-DI default on ``StorageEngine``); a
    ``NullCache`` injected ⇒ always-miss ⇒ today's single full-slot-scan behaviour.
    The ``YADGAR_ENGRAM_SLOT_CACHE_ENABLED`` kill switch (disabled ⇒ ``max_bytes=0``
    ⇒ all-miss ≡ NullCache) is honoured automatically.
    """
    existing = _REGISTRY.get("engram_slot")
    if existing is not None:
        return existing
    return _make_engram_slot_cache()


def _engram_slot_cache_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_ENGRAM_SLOT_CACHE_ENABLED",
        "ENGRAM_SLOT_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


@observe(tier="stage", metric="backend.cache.make_engram_slot_cache")
def _make_engram_slot_cache() -> Cache:
    """Build (and register) the unified ``engram_slot`` namespace (Car 3).

    Byte-budget from RAM-% (``engram_slot`` weight 1.0 — entries are small id
    lists). Invalidation is version-in-key (``DataEpoch`` policy marker — freshness
    lives in the key via the per-slot version, no TTL / explicit bust on the read
    path). ``deep_copy=True`` because the cached value is a mutable id list.
    """
    if not _engram_slot_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct_local())
        budget = _namespace_budget_bytes("engram_slot", total, active=("engram_slot",))
    return Cache(
        name="engram_slot",
        max_bytes=budget,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )


@observe(tier="stage", metric="backend.cache.get_graph_cache")
def get_graph_cache() -> CacheProtocol:
    """Return the process-global ``graph`` namespace (Car 4 adjacency seam).

    ``graph`` caches the PURE-STRUCTURAL per-entity adjacency — the neighbour
    dicts (``{entity_id, relationship_type, weight}``) for an entity — keyed by
    ``(entity_id, rel_types_key, entity_version)``. Both graph read paths (PPR
    ``_build_networkx_graph`` + spreading ``_spreading_bfs_step``) fan out through
    ``KnowledgeGraph._get_adjacent_batch``, which reads this cache per entity and
    only queries the DB (``get_relationships_for_frontier``) for the miss subset.

    Unlike Car 3's ``engram_slot`` there is NO fresh recheck: the adjacency read
    (``get_relationships_for_frontier``) filters ONLY on endpoint id — no
    ``heat`` / ``archived`` / ``valid_until`` / ``weight`` predicate — so the
    cached value is served whole. The tradeoff: DELETE is NOT inert here (Car 3's
    heat/reslot vectors were caught by its recheck). EVERY edge mutation — insert,
    weight-change (reinforce / field update), delete — bumps BOTH endpoint
    entities' versions (``scope_kind="entity"`` on the shared ``ScopeVersions``),
    or a stale adjacency would survive. ``rel_types_key`` (normalized ``rel_types``,
    ``None`` today) is in the key so a typed query never serves the ``None`` superset.

    Single process-wide instance (constructor-DI default on ``StorageEngine``); a
    ``NullCache`` injected ⇒ always-miss ⇒ today's per-depth frontier query. The
    ``YADGAR_GRAPH_CACHE_ENABLED`` kill switch (disabled ⇒ ``max_bytes=0`` ⇒
    all-miss ≡ NullCache) is honoured automatically.
    """
    existing = _REGISTRY.get("graph")
    if existing is not None:
        return existing
    return _make_graph_cache()


def _graph_cache_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_GRAPH_CACHE_ENABLED",
        "GRAPH_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


@observe(tier="stage", metric="backend.cache.make_graph_cache")
def _make_graph_cache() -> Cache:
    """Build (and register) the unified ``graph`` namespace (Car 4).

    Byte-budget from RAM-% (``graph`` weight 2.0 — entries are small neighbour-dict
    lists). Invalidation is version-in-key (``DataEpoch`` policy marker — freshness
    lives in the per-entity version, no TTL / explicit bust on the read path).
    ``deep_copy=True`` because the cached value is a list of mutable neighbour dicts.
    """
    if not _graph_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct_local())
        budget = _namespace_budget_bytes("graph", total, active=("graph",))
    return Cache(
        name="graph",
        max_bytes=budget,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )


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


# ── RAM-% byte-budget machinery ───────────────────────────────────────────────
#
# Backend cache byte budget = YADGAR_BACKEND_CACHE_RAM_PCT × backend container
# memory limit (cgroup). Split across namespaces by fixed weights (embed vectors
# and memory_doc entries are large; a ce score is tiny) — documented, not equal.

# Cgroup v2 (memory.max) then v1 (memory.limit_in_bytes).
_CGROUP_V2 = "/sys/fs/cgroup/memory.max"
_CGROUP_V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

# Fallback assumed backend container size when no cgroup limit is readable
# (e.g. running the test suite on a dev host): 4 GiB matches the documented
# `--memory 4g` backend container envelope.
_FALLBACK_CONTAINER_BYTES = 4 * 1024**3

# Namespace budget weights (relative share of the total backend budget). ce
# scores are 8-byte floats (small); embed vectors are ~1.5 KB each (large). The
# data namespaces (later cars) get placeholder weights so the split is stable as
# they land. Weights are normalised to the namespaces actually requested.
_NAMESPACE_WEIGHTS = {
    "ce": 1.0,
    "embed": 4.0,
    "memory_doc": 4.0,
    "engram_slot": 1.0,
    "graph": 2.0,
}


@observe(tier="stage", metric="backend.cache.read_container_memory")
def _read_container_memory_bytes() -> int | None:
    """Return the container memory limit in bytes, or None if unbounded/unknown."""
    for path_str in (_CGROUP_V2, _CGROUP_V1):
        try:
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


@observe(tier="stage", metric="backend.cache.total_budget")
def _backend_cache_total_budget_bytes(pct: float) -> int:
    """Total backend cache byte budget = pct%% × container memory (or fallback)."""
    limit = _read_container_memory_bytes()
    if limit is None:
        limit = _FALLBACK_CONTAINER_BYTES
    return int((pct / 100.0) * limit)


@observe(tier="stage", metric="backend.cache.namespace_budget")
def _namespace_budget_bytes(
    namespace: str, total_budget: int, *, active: tuple[str, ...] = ("ce", "embed")
) -> int:
    """This namespace's weighted share of ``total_budget``.

    Only the ``active`` namespaces (those a service actually instantiates) share
    the budget — Car 0 = ("ce", "embed"). Weighted (not equal): embed's larger
    entries earn a bigger slice.
    """
    weight_sum = sum(_NAMESPACE_WEIGHTS.get(n, 1.0) for n in active)
    if weight_sum <= 0:
        return 0
    return int(total_budget * (_NAMESPACE_WEIGHTS.get(namespace, 1.0) / weight_sum))
