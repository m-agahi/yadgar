"""yadgar._shared.knowledge_graph — entity/relationship knowledge-graph package.

T2 Car D (D1, layer-boundary train): the flat ``knowledge_graph.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.knowledge_graph`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.knowledge_graph.knowledge_graph``
directly.

  knowledge_graph.py — KnowledgeGraph — entity + relationship extraction/traversal
"""

from typing import Final

_EXPORTS: Final = {
    "KnowledgeGraph": "yadgar._shared.knowledge_graph.knowledge_graph",
    "RelationshipMeta": "yadgar._shared.knowledge_graph.knowledge_graph",
    "Settings": "yadgar._shared.knowledge_graph.knowledge_graph",
    "StorageEngine": "yadgar._shared.knowledge_graph.knowledge_graph",
    "UTC": "yadgar._shared.knowledge_graph.knowledge_graph",
    "VALID_REL_TYPES": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_CALL_RE": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_DECIDED_RE": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_DEF_RE": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_ERROR_FIX_RE": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_FROM_IMPORT_RE": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_IMPORT_FULL_RE": "yadgar._shared.knowledge_graph.knowledge_graph",
    "_error_fix_entities": "yadgar._shared.knowledge_graph.knowledge_graph",
    "datetime": "yadgar._shared.knowledge_graph.knowledge_graph",
    "deque": "yadgar._shared.knowledge_graph.knowledge_graph",
    "logger": "yadgar._shared.knowledge_graph.knowledge_graph",
    "logging": "yadgar._shared.knowledge_graph.knowledge_graph",
    "observe": "yadgar._shared.knowledge_graph.knowledge_graph",
    "re": "yadgar._shared.knowledge_graph.knowledge_graph",
    "time": "yadgar._shared.knowledge_graph.knowledge_graph",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
