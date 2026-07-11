"""yadgar._shared.astrocyte_pool — astrocyte domain-pool package.

T2 Car D (D1, layer-boundary train): the flat ``astrocyte_pool.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.astrocyte_pool`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.astrocyte_pool.astrocyte_pool``
directly.

  astrocyte_pool.py — AstrocytePool — domain registries + astrocyte process bookkeeping
"""

from typing import Final

_EXPORTS: Final = {
    "AstrocytePool": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "DOMAIN_DEFINITIONS": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "EmbeddingEngine": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "KnowledgeGraph": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "MemoryThermodynamics": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "Settings": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "StorageEngine": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "json": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "logger": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "logging": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "observe": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "re": "yadgar._shared.astrocyte_pool.astrocyte_pool",
    "time": "yadgar._shared.astrocyte_pool.astrocyte_pool",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
