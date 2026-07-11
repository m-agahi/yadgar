"""yadgar._shared.sensory_buffer — action-log sensory buffer package.

T2 Car D (D1, layer-boundary train): the flat ``sensory_buffer.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.sensory_buffer`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.sensory_buffer.sensory_buffer``
directly.

  sensory_buffer.py — ActionLogger — buffered tool-action capture feeding consolidation
"""

from typing import Final

_EXPORTS: Final = {
    "ActionLogger": "yadgar._shared.sensory_buffer.sensory_buffer",
    "Settings": "yadgar._shared.sensory_buffer.sensory_buffer",
    "StorageEngine": "yadgar._shared.sensory_buffer.sensory_buffer",
    "UTC": "yadgar._shared.sensory_buffer.sensory_buffer",
    "datetime": "yadgar._shared.sensory_buffer.sensory_buffer",
    "deque": "yadgar._shared.sensory_buffer.sensory_buffer",
    "observe": "yadgar._shared.sensory_buffer.sensory_buffer",
    "uuid": "yadgar._shared.sensory_buffer.sensory_buffer",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
