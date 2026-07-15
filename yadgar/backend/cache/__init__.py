"""yadgar.backend.cache — backend cache package.

T2 Car D (D2, layer-boundary train): the flat ``cache.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.cache`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.cache.cache``
directly.

  cache.py — Cache/LRUCache + namespace budgets + scope-version invalidation
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar.backend.cache.cache",
    "Cache": "yadgar.backend.cache.cache",
    "CacheProtocol": "yadgar.backend.cache.cache",
    "DataEpoch": "yadgar.backend.cache.cache",
    "Hashable": "yadgar.backend.cache.cache",
    "Invalidation": "yadgar.backend.cache.cache",
    "LRUCache": "yadgar.backend.cache.cache",
    "Manual": "yadgar.backend.cache.cache",
    "ModelCkpt": "yadgar.backend.cache.cache",
    "NullCache": "yadgar.backend.cache.cache",
    "OrderedDict": "yadgar.backend.cache.cache",
    "Path": "yadgar.backend.cache.cache",
    "ScopeVersions": "yadgar.backend.cache.scope_versions",
    "TTL": "yadgar.backend.cache.cache",
    "TYPE_CHECKING": "yadgar.backend.cache.cache",
    "_CGROUP_V1": "yadgar.backend.cache.cache",
    "_CGROUP_V2": "yadgar.backend.cache.cache",
    "_FALLBACK_CONTAINER_BYTES": "yadgar.backend.cache.cache",
    "_MAGIC": "yadgar.backend.cache.cache",
    "_NAMESPACE_WEIGHTS": "yadgar.backend.cache.cache",
    "_REGISTRY": "yadgar.backend.cache.cache",
    "_SCOPE_VERSIONS": "yadgar.backend.cache.scope_versions",
    "_VERSION": "yadgar.backend.cache.cache",
    "_backend_cache_ram_pct_local": "yadgar.backend.cache.cache",
    "_backend_cache_total_budget_bytes": "yadgar.backend.cache.cache",
    "_engram_slot_cache_enabled": "yadgar.backend.cache.cache",
    "_estimate_bytes": "yadgar.backend.cache.cache",
    "_graph_cache_enabled": "yadgar.backend.cache.cache",
    "_identity": "yadgar.backend.cache.cache",
    "_make_engram_slot_cache": "yadgar.backend.cache.cache",
    "_make_graph_cache": "yadgar.backend.cache.cache",
    "_make_memory_doc_cache": "yadgar.backend.cache.cache",
    "_memory_doc_cache_enabled": "yadgar.backend.cache.cache",
    "_memory_doc_cache_ttl_sec": "yadgar.backend.cache.cache",
    "_namespace_budget_bytes": "yadgar.backend.cache.cache",
    "_read_container_memory_bytes": "yadgar.backend.cache.cache",
    "_read_snapshot": "yadgar.backend.cache.cache",
    "_write_snapshot": "yadgar.backend.cache.cache",
    "annotations": "yadgar.backend.cache.cache",
    "copy": "yadgar.backend.cache.cache",
    "dataclass": "yadgar.backend.cache.cache",
    "get_ce_cache": "yadgar.backend.cache.cache",
    "get_engram_slot_cache": "yadgar.backend.cache.cache",
    "get_graph_cache": "yadgar.backend.cache.cache",
    "get_memory_doc_cache": "yadgar.backend.cache.cache",
    "get_scope_versions": "yadgar.backend.cache.scope_versions",
    "logger": "yadgar.backend.cache.cache",
    "logging": "yadgar.backend.cache.cache",
    "observe": "yadgar.backend.cache.cache",
    "record_cache_evict": "yadgar.backend.cache.cache",
    "record_cache_hit": "yadgar.backend.cache.cache",
    "record_cache_miss": "yadgar.backend.cache.cache",
    "struct": "yadgar.backend.cache.cache",
    "threading": "yadgar.backend.cache.cache",
    "time": "yadgar.backend.cache.cache",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
