"""PEP-562 shim — CheckpointRestore moved to ``yadgar.backend.restoration`` (T2 Car B).

Restore is compute over DB data (census verdict #7): the impl now lives in
``yadgar.backend.restoration.checkpoint_restore`` behind the backend
``POST /restore`` forward. The ``CheckpointContext`` contract stays in
``yadgar._shared.restoration.contract``. This shim keeps
``from yadgar._shared.restoration.checkpoint_restore import CheckpointRestore``
working for tests (Car 0 #167 precedent). Lazy string-target import — creates
NO static ``_shared → backend`` edge.
"""

from typing import Final

_EXPORTS: Final = {
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
