"""Back-compat shim — embed_service_metrics moved into ``yadgar.backend.embed_service`` (T2 Car D2, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.backend.embed_service.embed_service_metrics`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.backend.embed_service.embed_service_metrics"
_EXPORTS: Final = (
    "CONTENT_TYPE_LATEST",
    "CacheStatsCollector",
    "CollectorRegistry",
    "Counter",
    "Gauge",
    "Histogram",
    "PlatformCollector",
    "ProcessCollector",
    "Request",
    "Response",
    "_Path",
    "_SWAP_STATES",
    "_bind_swap_state_collectors",
    "_default_backend_cache_instances",
    "_os",
    "_registry",
    "_swap_state_flags",
    "annotations",
    "cache_snapshot_age_seconds",
    "ce_cache_evictions_total",
    "ce_cache_hits_total",
    "ce_cache_misses_total",
    "ce_cache_size_bytes",
    "ce_cache_size_entries",
    "embed_cache_evictions_total",
    "embed_cache_hits_total",
    "embed_cache_misses_total",
    "embed_cache_size_bytes",
    "embed_cache_size_entries",
    "embed_dbsize_cache_hits_total",
    "embed_dbsize_cache_misses_total",
    "embed_drainer_running",
    "embed_restart_reason_total",
    "generate_latest",
    "metrics_handler",
    "model_load_duration_seconds",
    "model_loaded",
    "model_unload_total",
    "rerank_503_total",
    "rerank_duration_seconds",
    "rerank_requests_total",
    "rerank_semaphore_held",
    "store_swap_state",
    "yadgar_log_dropped_total",
    "yadgar_log_file_rotations_total",
    "yadgar_log_file_size_bytes",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
