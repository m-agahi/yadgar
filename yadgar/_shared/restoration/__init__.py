"""yadgar._shared.restoration — checkpoint/restore contract package.

T2 Car C split the flat restoration.py; T2 Car B moved the impl to backend:

  contract.py            — CheckpointContext dataclass (pure contract, stays
                           in _shared)
  checkpoint_restore.py  — PEP-562 shim; the CheckpointRestore impl now lives
                           in yadgar.backend.restoration.checkpoint_restore
                           behind the backend POST /restore forward (census
                           verdict #7)

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.restoration import
CheckpointContext, CheckpointRestore`` keeps working for tests. Contract-only
consumers should import from ``yadgar._shared.restoration.contract`` directly
so the backend impl module never loads. Lazy string-target imports — no static
``_shared → backend`` edge.
"""

from typing import Final

_EXPORTS: Final = {
    "CheckpointContext": "yadgar._shared.restoration.contract",
    "CheckpointRestore": "yadgar.backend.restoration.checkpoint_restore",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
