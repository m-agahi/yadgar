"""yadgar._shared.blocks_render — memory-block markdown renderer package.

T2 Car D (D1, layer-boundary train): the flat ``blocks_render.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.blocks_render`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.blocks_render.blocks_render``
directly.

  blocks_render.py — render_blocks_section — presentation-only block formatting
"""

from typing import Final

_EXPORTS: Final = {
    "annotations": "yadgar._shared.blocks_render.blocks_render",
    "render_blocks_section": "yadgar._shared.blocks_render.blocks_render",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
