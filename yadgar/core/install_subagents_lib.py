"""Back-compat shim — install_subagents_lib moved into ``yadgar.core.install`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.install.install_subagents_lib`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.install.install_subagents_lib"
_EXPORTS: Final = (
    "Path",
    "_get_bundled_agents_dir",
    "annotations",
    "install_subagents_impl",
    "logger",
    "logging",
    "observe",
    "shutil",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
