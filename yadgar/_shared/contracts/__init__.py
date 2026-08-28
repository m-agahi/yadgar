"""yadgar._shared.contracts — cross-layer data contracts package.

T2 Car D1 (layer-boundary train): the flat contract modules packaged per the
no-lone-files law (ADR-0084). Contracts belong in `_shared` even when only one
layer imports them today (placement law 4, plan 2026-07-09).

  models.py    — pydantic models for stored records (Entity, Relationship,
                 MemoryCluster, ADR, AgentPrompt, …)
  protocols.py — structural Protocols + Null objects for DI seams
                 (StorageProtocol, MLClientProtocol, CacheProtocol)
  engram.py    — EngramAllocator slot-allocation contract/state machine

PEP-562 re-export (Car 0 #167 precedent): ``from yadgar._shared.contracts
import Entity`` works; back-compat shims remain at the old
``yadgar._shared.contracts.models`` / ``.protocols`` / ``.engram`` paths. New code
imports the submodules directly.
"""

from typing import Final

_EXPORTS: Final = {
    "ADR": "yadgar._shared.contracts.models",
    "AgentPrompt": "yadgar._shared.contracts.models",
    "Any": "yadgar._shared.contracts.protocols",
    "AstrocyteProcess": "yadgar._shared.contracts.models",
    "BaseModel": "yadgar._shared.contracts.models",
    "CacheProtocol": "yadgar._shared.contracts.protocols",
    "CausalDAGEdge": "yadgar._shared.contracts.models",
    "ConsolidationLog": "yadgar._shared.contracts.models",
    "EngramAllocator": "yadgar._shared.contracts.engram",
    "Entity": "yadgar._shared.contracts.models",
    "Field": "yadgar._shared.contracts.models",
    "FileHash": "yadgar._shared.contracts.models",
    "Hashable": "yadgar._shared.contracts.protocols",
    "Literal": "yadgar._shared.contracts.models",
    "MLClientProtocol": "yadgar._shared.contracts.protocols",
    "MemoryArchive": "yadgar._shared.contracts.models",
    "MemoryCluster": "yadgar._shared.contracts.models",
    "MemoryRule": "yadgar._shared.contracts.models",
    "MemoryStats": "yadgar._shared.contracts.models",
    "MemoryTransition": "yadgar._shared.contracts.models",
    "NullCache": "yadgar._shared.contracts.protocols",
    "NullMLClient": "yadgar._shared.contracts.protocols",
    "NullScopeVersions": "yadgar._shared.contracts.protocols",
    "Protocol": "yadgar._shared.contracts.protocols",
    "Relationship": "yadgar._shared.contracts.models",
    "Settings": "yadgar._shared.contracts.engram",
    "StorageEngine": "yadgar._shared.contracts.engram",
    "StorageProtocol": "yadgar._shared.contracts.protocols",
    "UTC": "yadgar._shared.contracts.models",
    "_ADR_VALID_STATUSES": "yadgar._shared.contracts.models",
    "_indent_continuation": "yadgar._shared.contracts.models",
    "annotations": "yadgar._shared.contracts.protocols",
    "datetime": "yadgar._shared.contracts.models",
    "logger": "yadgar._shared.contracts.engram",
    "logging": "yadgar._shared.contracts.engram",
    "observe": "yadgar._shared.contracts.models",
    "runtime_checkable": "yadgar._shared.contracts.protocols",
    "time": "yadgar._shared.contracts.engram",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
