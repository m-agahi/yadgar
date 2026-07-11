"""yadgar.core.sanitize — log-field sanitizer package.

T2 Car D (D3, layer-boundary train): the flat ``sanitize.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.sanitize`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.sanitize.sanitize``
directly.

  sanitize.py — sanitize_log_field — untrusted-input log sanitation
"""

from typing import Final

_EXPORTS: Final = {
    "_ANSI_RE": "yadgar.core.sanitize.sanitize",
    "_BIDI_RE": "yadgar.core.sanitize.sanitize",
    "_CTRL_RE": "yadgar.core.sanitize.sanitize",
    "_DEFAULT_MAX_LEN": "yadgar.core.sanitize.sanitize",
    "annotations": "yadgar.core.sanitize.sanitize",
    "observe": "yadgar.core.sanitize.sanitize",
    "re": "yadgar.core.sanitize.sanitize",
    "sanitize_log_field": "yadgar.core.sanitize.sanitize",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
