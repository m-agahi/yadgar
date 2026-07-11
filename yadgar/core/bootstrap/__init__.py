"""yadgar.core.bootstrap — core engine bootstrap package.

T2 Car D (D3, layer-boundary train): the flat ``bootstrap.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.bootstrap`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.bootstrap.bootstrap``
directly.

  bootstrap.py — core_init_engines — core-side composition-root entry
"""

from typing import Final

_EXPORTS: Final = {
    "StalenessDetector": "yadgar.core.bootstrap.bootstrap",
    "_build_core_only_engines": "yadgar.core.bootstrap.bootstrap",
    "_shared_init_engines": "yadgar.core.bootstrap.bootstrap",
    "_st": "yadgar.core.bootstrap.bootstrap",
    "annotations": "yadgar.core.bootstrap.bootstrap",
    "core_init_engines": "yadgar.core.bootstrap.bootstrap",
    "get_settings": "yadgar.core.bootstrap.bootstrap",
    "observe": "yadgar.core.bootstrap.bootstrap",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
