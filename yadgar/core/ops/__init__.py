"""yadgar.core.ops — service-controller ops package.

T2 Car D (D3, layer-boundary train): the flat ``ops.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.ops`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.ops.ops``
directly.

  ops.py — ServiceController + service-mode detection (host-ops)
"""

from typing import Final

_EXPORTS: Final = {
    "ManualModeError": "yadgar.core.ops.ops",
    "Path": "yadgar.core.ops.ops",
    "ServiceController": "yadgar.core.ops.ops",
    "VacuumTriggerNotConfiguredError": "yadgar.core.ops.ops",
    "_DEFAULT_VACUUM_TRIGGER_PATH": "yadgar.core.ops.ops",
    "_fire_vacuum_service": "yadgar.core.ops.ops",
    "annotations": "yadgar.core.ops.ops",
    "detect_service_mode": "yadgar.core.ops.ops",
    "observe": "yadgar.core.ops.ops",
    "os": "yadgar.core.ops.ops",
    "subprocess": "yadgar.core.ops.ops",
    "sys": "yadgar.core.ops.ops",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
