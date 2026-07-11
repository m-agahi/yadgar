"""Back-compat shim — wiki_meta moved into ``yadgar._shared.wiki`` (T2 Car D1).

Packaged per the no-lone-files law (ADR-0084): wiki page-type metadata now
lives in the ``yadgar/_shared/wiki/`` package next to the contract + store.
New code must import from ``yadgar._shared.wiki.wiki_meta`` instead.

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.wiki_meta import
PAGE_TYPES`` keeps working. Lazy importlib forward — the target only loads on
first attribute access.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.wiki.wiki_meta"
_EXPORTS: Final = (
    "PAGE_TYPES",
    "PAGE_TYPE_SCHEMAS",
    "WIKI_SCHEMA_VERSION",
    "_SCHEMA_DATA",
    "_load_page_type_schemas",
    "check_page_type_format",
    "observe",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
