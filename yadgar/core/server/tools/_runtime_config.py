"""Runtime config resolver — core read-through cache + HTTP forward (Car B).

The storage half (Car G1, ``_RuntimeConfigMixin`` on SurrealDB ``StorageEngine``)
stores typed config rows keyed by ``(key, directory)`` (``directory=None`` =
global). This module wraps that durable store in a read-through Path-To-Cache
(PTC) resolver on the shared core ``Cache`` engine, and does the per-dir →
global → default RESOLUTION that the raw storage rows deliberately leave to the
reader.

Car B: config READS no longer touch ``_get_storage()`` in core (the
ADR-0078 / ADR-0200 violation this car closes). Each read forwards over HTTP
via ``_forward_admin("get_config_row" / "list_config_rows", ...)``; the
backend admin-op body resolves against the durable store and returns the row.
The core PTC caches the RESOLVED value so a repeat ``config_get(key, dir)``
is a single-lookup hit with no HTTP.

Design:

  * namespace ``runtime_config`` (registered in ``cache._NAMESPACE_WEIGHTS``).
  * ``Manual`` invalidation — writes are rare and small, so a whole-flush on
    every write (``invalidate_config_cache``) is cheaper than tracking per-key
    busts. Defense-in-depth: a backend scope_version bump on ``config``
    invalidates the entire namespace via the piggyback envelope field; the
    cached scope_version key acts as a TTL-less backstop.
  * ``deep_copy=True`` — a cached ``list``/``dict`` value is caller-owned /
    mutable.
  * ``obs_tier="cold"`` — low call rate.

Cache key = ``f"{key}\x00{directory or ''}"`` — the REQUESTED ``(key, dir)``
pair, NOT the resolved row's directory. Resolution (per-dir → global) happens
on the miss path; the resolved value is cached under the requested key so a
repeated ``config_get(key, dir)`` is a single-lookup hit.

Fail-safe: ANY forward exception → return ``default`` (never raise out of a
config read — a config lookup must not crash its caller). Logged at warning.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar.core.forward import _forward_admin

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

    The core ``Cache`` self-registers into a module-global registry and raises
    on a duplicate name, so the instance must be built exactly once. Reuse a
    prior instance if already registered (e.g. after a module reload).
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


@observe(tier="hot", metric="tools._runtime_config._fetch_from_backend")
def _fetch_from_backend(key: str, directory: str | None) -> dict | None:
    """Single-row forward to backend ``get_config_row``.

    Returns the row dict, or None when absent (per-dir miss / global miss).
    Any forward exception is logged and yields None — the resolver treats it
    as 'no row → default'.
    """
    try:
        result = _forward_admin(
            "get_config_row",
            {"key": key, "directory": directory},
        )
    except Exception as exc:  # noqa: BLE001 — a config read must never crash its caller
        logger.warning("runtime_config forward failed for key=%s dir=%s: %s", key, directory, exc)
        return None
    return result.get("row") if isinstance(result, dict) else None


@observe(tier="hot", metric="tools._runtime_config._resolve_from_storage")
def _resolve_from_storage(key: str, directory: str | None, default: Any) -> Any:
    """Per-dir → global → default resolution via the backend forward.

    Fail-safe: any forward exception → ``default`` (never raises).
    """
    if directory is not None:
        row = _fetch_from_backend(key, directory)
        if row is not None:
            return row["value"]
    row = _fetch_from_backend(key, None)
    if row is not None:
        return row["value"]
    return default


@observe(tier="hot", metric="tools._runtime_config.config_get")
def config_get(key: str, directory: str | None = None, default: Any = None) -> Any:
    """Resolve a config value: per-dir override → global fallback → ``default``.

    PTC read-through: a cache hit returns the cached resolved value without
    an HTTP call; a miss resolves via the backend forward, caches the result
    under the REQUESTED ``(key, directory)`` pair, and returns it.

    Fail-safe: any forward error yields ``default`` and is NOT cached (so a
    transient backend blip does not pin a wrong value).
    """
    ck = _cache_key(key, directory)
    cache = _get_cache()
    cached = cache.get(ck)
    if cached is not None:
        return cached

    resolved = _resolve_from_storage(key, directory, default)
    # Cache the resolved value under the requested (key, dir). Caching a genuine
    # miss's ``default`` is correct — absence is a stable answer, busted on the
    # next write's whole-flush. The rare forward-error path also returns
    # ``default`` here; a stale-default from that path is bounded by the next
    # write's flush.
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

    Car B: pulls rows via ``_forward_admin("list_config_rows", ...)`` —
    ADR-0078 forbids core touching the DB directly. The ``storage`` argument
    is RETAINED for back-compat with ``bootstrap.py`` callers but unused
    (the forward is the only read path now).

    Pre-fills the cache with every STORED ``(key, directory)`` row's value so
    the common reads are hits from the first request. Per-dir fallbacks
    populate lazily on first miss — warmup seeds only the rows that exist.

    Best-effort: any failure is logged and swallowed — a warmup error must
    NOT block daemon start.
    """
    del storage  # unused under Car B (forward-only); kept for back-compat
    try:
        result = _forward_admin("list_config_rows", {})
    except Exception as exc:  # noqa: BLE001 — warmup must never block daemon start
        logger.warning("runtime_config warmup forward failed: %s", exc)
        return
    rows = result.get("rows", []) if isinstance(result, dict) else []
    cache = _get_cache()
    for row in rows:
        try:
            ck = _cache_key(row["key"], row.get("directory"))
            cache.put(ck, row["value"])
        except Exception:  # noqa: BLE001 — one bad row must not abort the warmup
            logger.debug("warmup row skipped: %s", row, exc_info=True)
