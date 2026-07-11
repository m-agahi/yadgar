"""yadgar.core.sensitive_lock — sensitive-operation lock package.

T2 Car D (D3, layer-boundary train): the flat ``sensitive_lock.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.sensitive_lock`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.sensitive_lock.sensitive_lock``
directly.

  sensitive_lock.py — cross-process lock file guarding sensitive jobs
"""

from typing import Final

_EXPORTS: Final = {
    "Path": "yadgar.core.sensitive_lock.sensitive_lock",
    "_LOCK_FILENAME": "yadgar.core.sensitive_lock.sensitive_lock",
    "_is_stale": "yadgar.core.sensitive_lock.sensitive_lock",
    "_paths": "yadgar.core.sensitive_lock.sensitive_lock",
    "_pid_alive": "yadgar.core.sensitive_lock.sensitive_lock",
    "_ttl_seconds": "yadgar.core.sensitive_lock.sensitive_lock",
    "_write_lock": "yadgar.core.sensitive_lock.sensitive_lock",
    "acquire": "yadgar.core.sensitive_lock.sensitive_lock",
    "annotations": "yadgar.core.sensitive_lock.sensitive_lock",
    "held": "yadgar.core.sensitive_lock.sensitive_lock",
    "is_held_by_live_job": "yadgar.core.sensitive_lock.sensitive_lock",
    "json": "yadgar.core.sensitive_lock.sensitive_lock",
    "lock_path": "yadgar.core.sensitive_lock.sensitive_lock",
    "logger": "yadgar.core.sensitive_lock.sensitive_lock",
    "logging": "yadgar.core.sensitive_lock.sensitive_lock",
    "observe": "yadgar.core.sensitive_lock.sensitive_lock",
    "os": "yadgar.core.sensitive_lock.sensitive_lock",
    "read": "yadgar.core.sensitive_lock.sensitive_lock",
    "release": "yadgar.core.sensitive_lock.sensitive_lock",
    "resolve_knob": "yadgar.core.sensitive_lock.sensitive_lock",
    "time": "yadgar.core.sensitive_lock.sensitive_lock",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
