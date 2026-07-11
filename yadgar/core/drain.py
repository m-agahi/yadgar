"""Back-compat shim — drain moved into ``yadgar.core.daemon`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.daemon.drain`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.daemon.drain"
_EXPORTS: Final = (
    "_POLL_INTERVAL",
    "_RequestCounter",
    "_request_counter",
    "annotations",
    "asyncio",
    "drain_in_flight_requests",
    "logger",
    "logging",
    "observe",
    "snapshot_embed_caches",
    "threading",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
