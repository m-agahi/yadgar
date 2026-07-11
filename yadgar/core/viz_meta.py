"""Back-compat shim — viz_meta moved into ``yadgar.core.viz`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.viz.viz_meta`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.viz.viz_meta"
_EXPORTS: Final = (
    "EDGE_TYPES",
    "HEAT_META",
    "LAZY_EDGE_TYPES",
    "NODE_TYPES",
    "TYPE_CHECKING",
    "WIKI_CATEGORIES",
    "_CAT_COLOR_FALLBACK",
    "annotations",
    "build_category_colors",
    "build_edge_colors",
    "build_legend",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
