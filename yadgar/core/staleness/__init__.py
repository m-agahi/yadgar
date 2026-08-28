"""yadgar.core.staleness — memory staleness detector package.

T2 Car D (D3, layer-boundary train): the flat ``staleness.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.staleness`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.staleness.staleness``
directly.

  staleness.py — StalenessDetector — host-side file hashing + validate_memory (the flag WRITE forwards to the backend; Car K deleted the watchdog arm)
"""

from typing import Final

_EXPORTS: Final = {
    "Settings": "yadgar.core.staleness.staleness",
    "StalenessDetector": "yadgar.core.staleness.staleness",
    "StorageEngine": "yadgar.core.staleness.staleness",
    "hashlib": "yadgar.core.staleness.staleness",
    "logger": "yadgar.core.staleness.staleness",
    "logging": "yadgar.core.staleness.staleness",
    "observe": "yadgar.core.staleness.staleness",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
