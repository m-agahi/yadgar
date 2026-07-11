"""Back-compat shim — tracing moved into ``yadgar._shared.observability`` (T2 Car D1, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar._shared.observability.tracing`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.observability.tracing"
_EXPORTS: Final = (
    "Any",
    "_OTLP_CB_FAILURE_THRESHOLD",
    "_OTLP_CB_RESET_SEC",
    "_SETUP_DONE",
    "_SPAN_LOG_HANDLER",
    "_SPAN_LOG_LISTENER",
    "_SPAN_LOG_LOCK",
    "_SPAN_LOG_PREV_PROPAGATE",
    "_SPAN_LOG_QUEUE",
    "_build_otlp_exporter",
    "_install_span_log_queue",
    "_instrument_httpx",
    "_parse_otlp_headers",
    "_span_log_reentry",
    "_stop_span_log_queue",
    "annotations",
    "contextlib",
    "functools",
    "get_current_span_id",
    "get_current_trace_id",
    "inspect",
    "logger",
    "logging",
    "queue",
    "setup_tracing",
    "shutdown_tracing",
    "span",
    "threading",
    "trace_span",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
