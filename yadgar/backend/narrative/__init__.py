"""yadgar.backend.narrative — narrative engine package.

T2 Car D (D2, layer-boundary train): the flat ``narrative.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.narrative`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.narrative.narrative``
directly.

  narrative.py — NarrativeEngine — auto-narration over consolidated memories
"""

from typing import Final

_EXPORTS: Final = {
    "Counter": "yadgar.backend.narrative.narrative",
    "KnowledgeGraph": "yadgar.backend.narrative.narrative",
    "NarrativeEngine": "yadgar.backend.narrative.narrative",
    "Settings": "yadgar.backend.narrative.narrative",
    "StorageEngine": "yadgar.backend.narrative.narrative",
    "UTC": "yadgar.backend.narrative.narrative",
    "_DECISION_KEYWORDS": "yadgar.backend.narrative.narrative",
    "_EVENT_KEYWORDS": "yadgar.backend.narrative.narrative",
    "datetime": "yadgar.backend.narrative.narrative",
    "json": "yadgar.backend.narrative.narrative",
    "logger": "yadgar.backend.narrative.narrative",
    "logging": "yadgar.backend.narrative.narrative",
    "observe": "yadgar.backend.narrative.narrative",
    "timedelta": "yadgar.backend.narrative.narrative",
    "trace_span": "yadgar.backend.narrative.narrative",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
