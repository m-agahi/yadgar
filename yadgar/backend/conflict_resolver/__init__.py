"""yadgar.backend.conflict_resolver — memory conflict resolver package.

T2 Car D (D2, layer-boundary train): the flat ``conflict_resolver.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.conflict_resolver`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.conflict_resolver.conflict_resolver``
directly.

  conflict_resolver.py — resolve_conflict — contradiction resolution between memory rows
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_ENABLED": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_PROMPT_TEMPLATE": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_VALID_OPS": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_build_prompt": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_client": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_fetch_similar": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_get_client": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_log": "yadgar.backend.conflict_resolver.conflict_resolver",
    "_parse_ollama_response": "yadgar.backend.conflict_resolver.conflict_resolver",
    "annotations": "yadgar.backend.conflict_resolver.conflict_resolver",
    "json": "yadgar.backend.conflict_resolver.conflict_resolver",
    "logging": "yadgar.backend.conflict_resolver.conflict_resolver",
    "observe": "yadgar.backend.conflict_resolver.conflict_resolver",
    "os": "yadgar.backend.conflict_resolver.conflict_resolver",
    "resolve_conflict": "yadgar.backend.conflict_resolver.conflict_resolver",
    "time": "yadgar.backend.conflict_resolver.conflict_resolver",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
