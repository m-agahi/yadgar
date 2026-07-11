"""yadgar.core.config_sync — incremental YAML config sync package.

T2 Car A (layer-boundary train): moved from the flat
``yadgar/_shared/config_sync.py``. Dual-import law: every prod importer is
core-only (``core/cli/config.py`` dispatch table), no compute — belongs in
core, not `_shared`. Packaged per the no-lone-files law.

  sync.py — ``cmd_config_sync`` CLI command impl + helpers.

PEP-562 re-export (Car 0 #167 precedent): ``from yadgar.core.config_sync
import cmd_config_sync`` resolves lazily so importing the package alone stays
cheap. A back-compat shim also remains at the old
``yadgar._shared.config_sync`` path.
"""

from typing import Final

_EXPORTS: Final = {
    "cmd_config_sync": "yadgar.core.config_sync.sync",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
