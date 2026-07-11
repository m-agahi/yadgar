"""Back-compat shim — graph_layout moved to the backend (T2 Car E3, census verdict #11).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working; the lazy string-based importlib forward avoids a static core→backend
edge. New code must import from ``yadgar.backend.graph.graph_layout`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.backend.graph.graph_layout"
_EXPORTS: Final = (
    "_LAYOUT_SCALE",
    "_LAYOUT_SEED",
    "_edge_pairs",
    "_node_ids",
    "annotations",
    "attach_cached_positions",
    "compute_graph_layout",
    "graph_signature",
    "hashlib",
    "logger",
    "logging",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
