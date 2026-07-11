"""Back-compat shim — viz_daemon_health moved into ``yadgar.core.viz`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.viz.viz_daemon_health`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.viz.viz_daemon_health"
_EXPORTS: Final = (
    "Any",
    "JSONResponse",
    "Request",
    "UTC",
    "_BACKEND_DEFAULT_URL",
    "_BACKEND_TIMEOUT_S",
    "_PLACEHOLDER",
    "_SCRAPE_INTERVAL_S_DEFAULT",
    "_backend_prev_cpu_s",
    "_backend_prev_cpu_t",
    "_core_prev_cpu_s",
    "_core_prev_cpu_t",
    "_cpu_pct",
    "_ensure_scraper_running",
    "_get_backend_metrics_url",
    "_health_cache",
    "_histogram_p95",
    "_labeled_values",
    "_parse_core_process",
    "_parse_log",
    "_parse_process",
    "_register_routes",
    "_sample_value",
    "_scrape_core_text",
    "_scrape_once",
    "_scraper_heartbeat",
    "_scraper_record_exc",
    "_scraper_task",
    "annotations",
    "api_daemon_health",
    "asyncio",
    "datetime",
    "get_settings",
    "httpx",
    "logger",
    "logging",
    "os",
    "parse_backend_metrics",
    "parse_core_metrics",
    "run_health_scraper",
    "scrape_backend_metrics_text",
    "text_string_to_metric_families",
    "time",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
