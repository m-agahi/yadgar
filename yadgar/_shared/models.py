"""Back-compat shim — models moved into ``yadgar._shared.contracts`` (T2 Car D1, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar._shared.contracts.models`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.contracts.models"
_EXPORTS: Final = (
    "ADR",
    "AgentPrompt",
    "AstrocyteProcess",
    "BaseModel",
    "CausalDAGEdge",
    "Checkpoint",
    "ConsolidationLog",
    "Entity",
    "Episode",
    "Field",
    "FileHash",
    "Literal",
    "Memory",
    "MemoryArchive",
    "MemoryCluster",
    "MemoryRule",
    "MemoryStats",
    "MemoryTransition",
    "NarrativeEntry",
    "ProspectiveMemory",
    "Relationship",
    "UTC",
    "_ADR_VALID_STATUSES",
    "_indent_continuation",
    "datetime",
    "observe",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
