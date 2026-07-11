"""yadgar.backend.prospective — prospective-memory package.

T2 Car D (D2, layer-boundary train): the flat ``prospective.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.prospective`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.prospective.prospective``
directly.

  prospective.py — ProspectiveMemoryEngine — trigger-based future-intent memories
"""

from typing import Final

_EXPORTS: Final = {
    "MAX_TRIGGER_COUNT": "yadgar.backend.prospective.prospective",
    "ProspectiveMemoryEngine": "yadgar.backend.prospective.prospective",
    "Settings": "yadgar.backend.prospective.prospective",
    "StorageEngine": "yadgar.backend.prospective.prospective",
    "UTC": "yadgar.backend.prospective.prospective",
    "VALID_TRIGGER_TYPES": "yadgar.backend.prospective.prospective",
    "_PROSPECTIVE_PATTERNS": "yadgar.backend.prospective.prospective",
    "_TIME_HOUR_RE": "yadgar.backend.prospective.prospective",
    "_TIME_WEEKDAY_RE": "yadgar.backend.prospective.prospective",
    "datetime": "yadgar.backend.prospective.prospective",
    "observe": "yadgar.backend.prospective.prospective",
    "re": "yadgar.backend.prospective.prospective",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
