"""yadgar.core.cache — core response-cache package.

T2 Car D3 (layer-boundary train): the flat ``cache.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.cache`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.cache.cache``
directly.

  cache.py — core-side response caches (TTL/Manual/Invalidation) +
             container-aware RAM budgets (census verdict #4: response
             caches stay core)
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar.core.cache.cache",
    "Cache": "yadgar.core.cache.cache",
    "Hashable": "yadgar.core.cache.cache",
    "Invalidation": "yadgar.core.cache.cache",
    "KeyFn": "yadgar.core.cache.cache",
    "Manual": "yadgar.core.cache.cache",
    "OrderedDict": "yadgar.core.cache.cache",
    "TTL": "yadgar.core.cache.cache",
    "TYPE_CHECKING": "yadgar.core.cache.cache",
    "_CORE_CGROUP_V1": "yadgar.core.cache.cache",
    "_CORE_CGROUP_V2": "yadgar.core.cache.cache",
    "_CORE_FALLBACK_CONTAINER_BYTES": "yadgar.core.cache.cache",
    "_NAMESPACE_WEIGHTS": "yadgar.core.cache.cache",
    "_REGISTRY": "yadgar.core.cache.cache",
    "_core_cache_ram_pct": "yadgar.core.cache.cache",
    "_core_cache_total_budget_bytes": "yadgar.core.cache.cache",
    "_estimate_bytes": "yadgar.core.cache.cache",
    "_identity": "yadgar.core.cache.cache",
    "_namespace_budget_bytes": "yadgar.core.cache.cache",
    "_read_container_memory_bytes": "yadgar.core.cache.cache",
    "_time": "yadgar.core.cache.cache",
    "annotations": "yadgar.core.cache.cache",
    "copy": "yadgar.core.cache.cache",
    "dataclass": "yadgar.core.cache.cache",
    "observe": "yadgar.core.cache.cache",
    "record_cache_evict": "yadgar.core.cache.cache",
    "record_cache_hit": "yadgar.core.cache.cache",
    "record_cache_miss": "yadgar.core.cache.cache",
    "threading": "yadgar.core.cache.cache",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
