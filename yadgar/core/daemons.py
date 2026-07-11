"""Back-compat shim — daemons moved into ``yadgar.core.daemon`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.daemon.daemons`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.daemon.daemons"
_EXPORTS: Final = (
    "_lc_heartbeat",
    "_lc_record_exc",
    "_lifecycle_span",
    "_maybe_auto_check_for_update",
    "_metrics_loop",
    "_reranker_idle_loop",
    "_run_update_check",
    "_st",
    "_start_daemon_threads",
    "_viz_loop",
    "annotations",
    "contextlib",
    "get_settings",
    "logger",
    "logging",
    "observe",
    "os",
    "resolve_knob",
    "settings",
    "threading",
    "time",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
