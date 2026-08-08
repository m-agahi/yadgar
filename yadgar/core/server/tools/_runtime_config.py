"""Runtime config resolver — core read-through cache + PTC getter (Car G2).

The storage half (Car G1, ``_RuntimeConfigMixin``) stores typed config rows keyed
by ``(key, directory)`` (``directory=None`` = global). This module wraps that
durable store in a read-through Path-To-Cache (PTC) resolver on the shared core
``Cache`` engine, and does the per-dir → global → default RESOLUTION that the raw
storage rows deliberately leave to the reader.

Design (ADR-0140 precedent):

  * namespace ``runtime_config`` (registered in ``cache._NAMESPACE_WEIGHTS``).
  * ``Manual`` invalidation — writes are rare and small, so a whole-flush on every
    write (``invalidate_config_cache``) is cheaper than tracking per-key busts.
  * ``deep_copy=True`` — a cached ``list``/``dict`` value is caller-owned/mutable.
  * ``obs_tier="cold"`` — low call rate.

Cache key = ``f"{key}\x00{directory or ''}"`` — the REQUESTED ``(key, dir)`` pair,
NOT the resolved row's directory. Resolution (per-dir → global) happens on the
miss path; the resolved value is cached under the requested key so a repeated
``config_get(key, dir)`` is a single-lookup hit.

Reads stay CORE via ``_get_storage()`` (matches ``blocks`` — only WRITES are admin
ops; ADR-0078 forbids core reading the DB *directly*, but ``_get_storage`` is the
in-process shared StorageEngine, not a raw DB handle).

Fail-safe: ANY storage exception → return ``default`` (never raise out of a config
read — a config lookup must not crash its caller). Logged at warning.

G2→G3 seam: G3's ``config_get`` MCP tool calls :func:`config_get` for reads; its
``config_set`` / ``config_delete`` tools write via the G1 admin op and then call
:func:`invalidate_config_cache` to bust this cache.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

# Imported at module level (not lazily) so tests can monkeypatch this symbol on
# the resolver module to inject a fake StorageEngine.
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)

# Null byte separates key from directory in the cache key — a config key never
# contains a NUL, so the pair is unambiguous.
_KEY_SEP = "\x00"

_cache: Any = None


@observe(tier="stage", metric="tools._runtime_config._make_cache")
def _make_runtime_config_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
        Cache,
        Manual,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("runtime_config", total)
    return Cache(
        name="runtime_config",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget)
        invalidation=Manual(),  # whole-flush on write (writes rare + small)
        deep_copy=True,  # returned list/dict is caller-owned / mutable
        obs_tier="cold",  # low call rate
    )


@observe(tier="hot", metric="tools._runtime_config._get_cache")
def _get_cache():
    """Return the singleton cache (built once at import).

    The core ``Cache`` self-registers into a module-global registry and raises on a
    duplicate name, so the instance must be built exactly once. Reuse a prior
    instance if already registered (e.g. after a module reload).
    """
    global _cache
    if _cache is None:
        from yadgar.core.cache.cache import _REGISTRY  # noqa: PLC0415

        existing = _REGISTRY.get("runtime_config")
        _cache = existing if existing is not None else _make_runtime_config_cache()
    return _cache


def _cache_key(key: str, directory: str | None) -> str:
    """Cache key for the REQUESTED (key, directory) pair."""
    return f"{key}{_KEY_SEP}{directory or ''}"


@observe(tier="hot", metric="tools._runtime_config._resolve_from_storage")
def _resolve_from_storage(key: str, directory: str | None, default: Any) -> Any:
    """Per-dir → global → default resolution via the durable store.

    Fail-safe: any storage exception → ``default`` (never raises).
    """
    try:
        storage = _get_storage()
        if storage is None:
            return default
        if directory is not None:
            row = storage.get_config_row(key, directory=directory)
            if row is not None:
                return row["value"]
        row = storage.get_config_row(key, directory=None)
        if row is not None:
            return row["value"]
        return default
    except Exception as exc:  # noqa: BLE001 — a config read must never crash its caller
        logger.warning("runtime_config resolve failed for key=%s dir=%s: %s", key, directory, exc)
        return default


@observe(tier="hot", metric="tools._runtime_config.config_get")
def config_get(key: str, directory: str | None = None, default: Any = None) -> Any:
    """Resolve a config value: per-dir override → global fallback → ``default``.

    PTC read-through: a cache hit returns the cached resolved value without
    touching storage; a miss resolves via the durable store, caches the result
    under the REQUESTED ``(key, directory)`` pair, and returns it.

    Fail-safe: any storage error yields ``default`` and is NOT cached (so a
    transient backend blip does not pin a wrong value).
    """
    ck = _cache_key(key, directory)
    cache = _get_cache()
    cached = cache.get(ck)
    if cached is not None:
        return cached

    resolved = _resolve_from_storage(key, directory, default)
    # Cache the resolved value under the requested (key, dir). Caching a genuine
    # miss's ``default`` is correct — absence is a stable answer, busted on the next
    # write's whole-flush. The rare storage-error path also returns ``default``
    # here; a stale-default from that path is bounded by the next write's flush.
    cache.put(ck, resolved)
    return resolved


@observe(tier="stage", metric="tools._runtime_config.invalidate_config_cache")
def invalidate_config_cache() -> None:
    """Whole-flush the runtime_config cache (Manual bust on any config write).

    Called by G3's ``config_set`` / ``config_delete`` tools after a write, and
    defensively at the ``clear_config_caches()`` core call site so a Settings
    hot-reload also flushes. Must never raise out of a write path.
    """
    try:
        _get_cache().clear()
    except Exception:  # noqa: BLE001 — invalidation must never break the write path
        logger.debug("runtime_config cache clear failed", exc_info=True)


@observe(tier="stage", metric="tools._runtime_config.warmup")
def warmup_runtime_config_cache(storage: Any) -> None:
    """Bulk-populate the cache from ALL stored rows at daemon start (best-effort).

    Pre-fills the cache with every STORED ``(key, directory)`` row's value so the
    common reads are hits from the first request. Per-dir fallbacks (a requested
    ``(key, dir)`` with no stored row that resolves to global) populate lazily on
    first miss — warmup seeds only the rows that actually exist.

    Best-effort: any failure is logged and swallowed — a warmup error must NOT
    block daemon start.
    """
    try:
        if storage is None:
            return
        rows = storage.list_config_rows()  # sentinel default = ALL rows
        cache = _get_cache()
        for row in rows:
            ck = _cache_key(row["key"], row.get("directory"))
            cache.put(ck, row["value"])
    except Exception:  # noqa: BLE001 — warmup must never block daemon start
        logger.warning("runtime_config cache warmup failed", exc_info=True)
