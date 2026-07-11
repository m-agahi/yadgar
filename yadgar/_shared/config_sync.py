"""Back-compat shim — config_sync moved to ``yadgar.core.config_sync`` (T2 Car A).

Dual-import law (layer-boundary train): every prod importer was core-only, so
the module left `_shared` for ``yadgar/core/config_sync/sync.py``.

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.config_sync import
cmd_config_sync`` keeps working. New code must import from
``yadgar.core.config_sync`` instead. The forward is a lazy string-based
importlib call ON PURPOSE — a static ``from yadgar.core ... import`` here
would create a forbidden _shared→core edge (import-linter contract 1).
"""

from typing import Final

_TARGET: Final = "yadgar.core.config_sync.sync"
_EXPORTS: Final = (
    "cmd_config_sync",
    "_compute_missing",
    "_compute_unknown",
    "_handle_check",
    "_handle_dry_run",
    "_apply_missing",
    "_atomic_yaml_write",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
