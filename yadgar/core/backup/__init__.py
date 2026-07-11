"""yadgar.core.backup — host-ops snapshot/backup package.

T2 Car D (D3, layer-boundary train): the flat ``backup.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.backup`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.backup.backup``
directly.

  backup.py — quiesced snapshot create/restore/prune (host-ops, census verdict #12)
"""

from typing import Final

_EXPORTS: Final = {
    "Path": "yadgar.core.backup.backup",
    "UTC": "yadgar.core.backup.backup",
    "_create_export_snapshot": "yadgar.core.backup.backup",
    "_log": "yadgar.core.backup.backup",
    "_paths": "yadgar.core.backup.backup",
    "annotations": "yadgar.core.backup.backup",
    "create_snapshot": "yadgar.core.backup.backup",
    "datetime": "yadgar.core.backup.backup",
    "default_retention": "yadgar.core.backup.backup",
    "glob": "yadgar.core.backup.backup",
    "logging": "yadgar.core.backup.backup",
    "observe": "yadgar.core.backup.backup",
    "os": "yadgar.core.backup.backup",
    "prune_snapshots": "yadgar.core.backup.backup",
    "restore_snapshot": "yadgar.core.backup.backup",
    "shutil": "yadgar.core.backup.backup",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
