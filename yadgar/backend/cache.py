"""LRU cache with msgpack snapshot for backend hot-path caching.

Two shapes live here:

  * ``LRUCache`` (backend v5.4.0) — the original count-capped LRU. Kept intact
    for its own tests + snapshot format; NOT the unified surface.
  * ``Cache`` (backend 5.17.0, Car 0 of the backend caching train) — the unified
    backend cache, one class / N named instances / policy bound at construction.
    Mirrors the core ``yadgar/cache.py`` ``Cache`` but with **byte-bounded LRU
    eviction** (a % of the backend container RAM) instead of a fixed entry cap,
    because backend entries vary wildly (a ``ce`` score is ~8 bytes; a
    ``memory_doc`` holds full content + embedding). ``_ce_cache`` / ``_embed_cache``
    fold into it behaviour-neutrally (same keys, same values, same
    ModelCkpt-in-key invalidation, same external metric series, same snapshot).

Consumers depend on ``CacheProtocol`` (``get`` / ``put`` / ``invalidate`` /
``stats``), never the concrete class — constructor-DI ready. ``NullCache`` is the
disable/test double.

Snapshot format (shared by LRUCache + Cache):
  YADCACHE\\0 (9 bytes magic)
  version byte (1 byte, currently 0x01)
  checkpoint_hash as UTF-8 length-prefixed string (4-byte LE len + bytes)
  msgpack-encoded list of [key, value] pairs (all remaining bytes)
On load: magic + version must match, checkpoint_hash must match current model
hash — mismatch silently returns empty cache.

I13: nesting ≤ 4.
"""

from __future__ import annotations

import copy
import logging
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yadgar._shared.metrics import record_cache_evict, record_cache_hit, record_cache_miss
from yadgar._shared.observability.observe import observe

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Snapshot file magic header + version
_MAGIC = b"YADCACHE\x00"
_VERSION = b"\x01"


class LRUCache:
    """OrderedDict-backed LRU cache with msgpack snapshot.

    Args:
        max_entries: Maximum number of entries. 0 = disabled (all puts no-op).
        checkpoint_hash: Hash of the model checkpoint. Snapshots written with a
            different hash are silently discarded on load.
    """

    def __init__(self, max_entries: int, checkpoint_hash: str) -> None:
        self._max = max_entries
        self._ckpt = checkpoint_hash
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        # Counters (informational, not thread-safe at int level but acceptable
        # for metric reporting — off-by-one on counter in rare race is fine)
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0

    # ── Core ops ─────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        """Return value for key, or None on miss. Promotes to MRU on hit."""
        if self._max == 0:
            self.misses += 1
            return None
        with self._lock:
            if key not in self._store:
                self.misses += 1
                return None
            # Move to end (most-recently-used)
            self._store.move_to_end(key)
            self.hits += 1
            return self._store[key]

    def put(self, key: str, value: Any) -> None:
        """Insert or update key → value. Evicts LRU entry if at cap."""
        if self._max == 0:
            return
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = value
            else:
                self._store[key] = value
                if len(self._store) > self._max:
                    self._store.popitem(last=False)  # evict LRU (oldest)
                    self.evictions += 1

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def size_entries(self) -> int:
        return len(self._store)

    @property
    def size_bytes(self) -> int:
        """Rough byte estimate via sys.getsizeof on the internal dict."""
        import sys

        with self._lock:
            return sys.getsizeof(self._store)

    # ── Snapshot I/O ─────────────────────────────────────────────────────────

    @observe(tier="stage", metric="backend.cache.save_snapshot")
    def save_snapshot(self, snap_dir: str, name: str) -> None:
        """Serialize cache to <snap_dir>/<name>.snap using msgpack.

        Takes a shallow copy under lock, then writes without holding the lock.
        Writes to a temp file and renames atomically.
        """
        try:
            import msgpack  # noqa: PLC0415
        except ImportError:
            logger.warning("cache.save_snapshot: msgpack not installed — skipping")
            return

        with self._lock:
            items = list(self._store.items())

        path = Path(snap_dir) / f"{name}.snap"
        tmp_path = path.with_suffix(".snap.tmp")

        ckpt_bytes = self._ckpt.encode("utf-8")
        ckpt_len = struct.pack("<I", len(ckpt_bytes))

        payload = msgpack.packb(items, use_bin_type=True)
        header = _MAGIC + _VERSION + ckpt_len + ckpt_bytes

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(header + payload)
            tmp_path.replace(path)
        except OSError as exc:
            logger.warning("cache.save_snapshot: write failed for %s: %s", path, exc)

    @observe(tier="stage", metric="backend.cache.load_snapshot")
    def load_snapshot(self, snap_dir: str, name: str) -> None:
        """Restore entries from <snap_dir>/<name>.snap.

        Silently discards on: missing file, magic mismatch, version mismatch,
        checkpoint hash mismatch, or any parse error.
        """
        try:
            import msgpack  # noqa: PLC0415
        except ImportError:
            logger.warning("cache.load_snapshot: msgpack not installed — skipping")
            return

        path = Path(snap_dir) / f"{name}.snap"
        if not path.exists():
            return

        try:
            data = path.read_bytes()
            # Check magic (9 bytes) + version (1 byte)
            if len(data) < 10 or data[:9] != _MAGIC or data[9:10] != _VERSION:
                logger.warning("cache.load_snapshot: bad header in %s — discarding", path)
                return

            offset = 10
            if len(data) < offset + 4:
                logger.warning("cache.load_snapshot: truncated ckpt len in %s", path)
                return
            ckpt_len = struct.unpack("<I", data[offset : offset + 4])[0]
            offset += 4

            if len(data) < offset + ckpt_len:
                logger.warning("cache.load_snapshot: truncated ckpt hash in %s", path)
                return
            stored_ckpt = data[offset : offset + ckpt_len].decode("utf-8", errors="replace")
            offset += ckpt_len

            if stored_ckpt != self._ckpt:
                logger.info(
                    "cache.load_snapshot: checkpoint mismatch (%s != %s) — discarding %s",
                    stored_ckpt[:16],
                    self._ckpt[:16],
                    path,
                )
                return

            items: list = msgpack.unpackb(data[offset:], raw=False)
            with self._lock:
                self._store.clear()
                for k, v in items:
                    self._store[k] = v
                    if self._max > 0 and len(self._store) > self._max:
                        self._store.popitem(last=False)
                        self.evictions += 1
        except Exception as exc:
            logger.warning("cache.load_snapshot: error loading %s: %s — discarding", path, exc)
            with self._lock:
                self._store.clear()

    def snapshot_age_seconds(self, snap_dir: str, name: str) -> float:
        """Return seconds since snapshot was last written, or -1 if no file."""
        path = Path(snap_dir) / f"{name}.snap"
        if not path.exists():
            return -1.0
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return -1.0


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
from yadgar._shared.protocols import CacheProtocol as CacheProtocol  # noqa: E402,PLC0414

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


class ScopeVersions:
    """Per-scope monotonic version map — the reusable version-in-key mechanism.

    A small in-process, thread-safe ``(scope_kind, scope_id) -> int`` counter.
    Structural writes bump the version of the scope they mutate; a reader embeds
    the current version in its cache key (e.g. ``(slot_index, slot_version)``), so
    a bump makes every prior key for that scope unreachable — a stale entry is
    simply never hit, with NO explicit ``invalidate`` call and NO cross-service
    round-trip (the version is read cheaply, in-process, on the read path).

    Car 3 uses ``scope_kind="slot"`` (slot occupancy). Car 4 will reuse the SAME
    map with ``scope_kind="entity"`` (graph neighbourhoods) — different kind,
    identical mechanism. Bumps are O(1); versions start at 0 and only increase.

    Staleness guarantee: a cached ``(scope, v)`` entry is served ONLY while the
    scope's version equals ``v``. The instant a structural mutator bumps the
    scope (create/reslot-into for slots), the reader computes ``(scope, v+1)`` →
    miss → recompute. Vectors that the fresh read-side recheck already covers
    (delete, reslot-away, heat→0 for slots) need NO bump — see the engram_slot
    cache docstring.
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[str, Hashable], int] = {}
        self._lock = threading.Lock()

    @observe(tier="hot", metric="backend.cache.scope_version_read")
    def version(self, scope_kind: str, scope_id: Hashable) -> int:
        """Current version for a scope (0 if never bumped)."""
        with self._lock:
            return self._versions.get((scope_kind, scope_id), 0)

    @observe(tier="hot", metric="backend.cache.scope_version_bump")
    def bump(self, scope_kind: str, scope_id: Hashable) -> int:
        """Increment and return the scope's version. O(1), cheap enough for the
        write hot-path (a single dict update under a short lock)."""
        key = (scope_kind, scope_id)
        with self._lock:
            v = self._versions.get(key, 0) + 1
            self._versions[key] = v
            return v


# Process-global ScopeVersions — the version store the backend StorageEngine reads
# on the slot-read path and bumps at the slot-write site. Single instance because
# slot writes (assign_memory_slot) and the slot read (get_memories_in_slot) share
# ONE backend process, so no header-passing / cross-service signal is needed.
_SCOPE_VERSIONS = ScopeVersions()


def get_scope_versions() -> ScopeVersions:
    """Return the process-global :class:`ScopeVersions` (version-in-key store)."""
    return _SCOPE_VERSIONS


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


# ── Snapshot helpers (shared free functions; LRUCache-compatible format) ──────


@observe(tier="stage", metric="backend.cache.write_snapshot")
def _write_snapshot(items: list, ckpt: str, snap_dir: str, name: str) -> None:
    try:
        import msgpack  # noqa: PLC0415
    except ImportError:
        logger.warning("cache.save_snapshot: msgpack not installed — skipping")
        return
    path = Path(snap_dir) / f"{name}.snap"
    tmp_path = path.with_suffix(".snap.tmp")
    ckpt_bytes = ckpt.encode("utf-8")
    ckpt_len = struct.pack("<I", len(ckpt_bytes))
    payload = msgpack.packb(items, use_bin_type=True)
    header = _MAGIC + _VERSION + ckpt_len + ckpt_bytes
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(header + payload)
        tmp_path.replace(path)
    except OSError as exc:
        logger.warning("cache.save_snapshot: write failed for %s: %s", path, exc)


@observe(tier="stage", metric="backend.cache.read_snapshot")
def _read_snapshot(ckpt: str, snap_dir: str, name: str) -> list | None:
    """Return the [key, value] list, or None on any discard condition."""
    try:
        import msgpack  # noqa: PLC0415
    except ImportError:
        logger.warning("cache.load_snapshot: msgpack not installed — skipping")
        return None
    path = Path(snap_dir) / f"{name}.snap"
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        if len(data) < 10 or data[:9] != _MAGIC or data[9:10] != _VERSION:
            logger.warning("cache.load_snapshot: bad header in %s — discarding", path)
            return None
        offset = 10
        if len(data) < offset + 4:
            return None
        ckpt_len = struct.unpack("<I", data[offset : offset + 4])[0]
        offset += 4
        if len(data) < offset + ckpt_len:
            return None
        stored_ckpt = data[offset : offset + ckpt_len].decode("utf-8", errors="replace")
        offset += ckpt_len
        if stored_ckpt != ckpt:
            logger.info(
                "cache.load_snapshot: checkpoint mismatch (%s != %s) — discarding %s",
                stored_ckpt[:16],
                ckpt[:16],
                path,
            )
            return None
        return msgpack.unpackb(data[offset:], raw=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.load_snapshot: error loading %s: %s — discarding", path, exc)
        return None


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
