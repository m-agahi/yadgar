"""Back-compat shim — log_config moved into ``yadgar._shared.observability`` (T2 Car D1, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar._shared.observability.log_config`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.observability.log_config"
_EXPORTS: Final = (
    "ContentRedactor",
    "JSONLogFormatter",
    "JsonFormatter",
    "Literal",
    "RateLimitFilter",
    "RequestLoggingMiddleware",
    "RotatingJSONLFileHandler",
    "TRACEBACK_MAX_CHARS",
    "UTC",
    "_DEFAULT_BACKEND_LOG_FILENAME",
    "_DEFAULT_CORE_LOG_FILENAME",
    "_EXACT_DENYLIST",
    "_FALLBACK_LOG_DIR",
    "_I14_EXPLICIT_FIELDS",
    "_I14_SKIP_FIELDS",
    "_LOG_SIZE_GAUGE_INTERVAL",
    "_RATE_LIMIT_SUMMARY_INTERVAL",
    "_SUBSTRING_DENYLIST",
    "_configure_request_logger",
    "_configure_yadgar_logger",
    "_increment_request_metric",
    "_install_file_handler",
    "_install_rate_limiter",
    "_is_sensitive",
    "_outcome_from_status",
    "_paths",
    "_redact_dict",
    "_request_logger",
    "_resolve_log_dir",
    "_resolve_log_env_int",
    "_resolve_log_file_path",
    "_resolve_metrics_module",
    "_resolve_route_label",
    "_suppress_noisy_framework_loggers",
    "annotations",
    "configure_logging",
    "datetime",
    "json",
    "logging",
    "os",
    "resolve_knob",
    "sys",
    "time",
    "uuid",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
