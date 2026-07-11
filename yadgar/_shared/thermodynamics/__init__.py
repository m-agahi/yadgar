"""yadgar._shared.thermodynamics — memory heat/thermodynamics package.

T2 Car D (D1, layer-boundary train): the flat ``thermodynamics.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.thermodynamics`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.thermodynamics.thermodynamics``
directly.

  thermodynamics.py — MemoryThermodynamics — heat scoring + decay math over memory rows
"""

from typing import Final

_EXPORTS: Final = {
    "EmbeddingEngine": "yadgar._shared.thermodynamics.thermodynamics",
    "MemoryThermodynamics": "yadgar._shared.thermodynamics.thermodynamics",
    "Settings": "yadgar._shared.thermodynamics.thermodynamics",
    "StorageEngine": "yadgar._shared.thermodynamics.thermodynamics",
    "UTC": "yadgar._shared.thermodynamics.thermodynamics",
    "_ARCHITECTURE_KEYWORDS": "yadgar._shared.thermodynamics.thermodynamics",
    "_CODE_BLOCK_RE": "yadgar._shared.thermodynamics.thermodynamics",
    "_DECISION_KEYWORDS": "yadgar._shared.thermodynamics.thermodynamics",
    "_ERROR_KEYWORDS": "yadgar._shared.thermodynamics.thermodynamics",
    "_FILE_PATH_RE": "yadgar._shared.thermodynamics.thermodynamics",
    "_SUCCESS_KEYWORDS": "yadgar._shared.thermodynamics.thermodynamics",
    "datetime": "yadgar._shared.thermodynamics.thermodynamics",
    "logger": "yadgar._shared.thermodynamics.thermodynamics",
    "logging": "yadgar._shared.thermodynamics.thermodynamics",
    "observe": "yadgar._shared.thermodynamics.thermodynamics",
    "re": "yadgar._shared.thermodynamics.thermodynamics",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
