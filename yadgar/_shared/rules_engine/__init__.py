"""yadgar._shared.rules_engine — neuro-symbolic rules-engine package.

T2 Car D (D1, layer-boundary train): the flat ``rules_engine.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.rules_engine`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.rules_engine.rules_engine``
directly.

  rules_engine.py — RulesEngine — hard/soft rule parsing, filtering + write policy
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar._shared.rules_engine.rules_engine",
    "NUMERIC_FIELDS": "yadgar._shared.rules_engine.rules_engine",
    "RulesEngine": "yadgar._shared.rules_engine.rules_engine",
    "Settings": "yadgar._shared.rules_engine.rules_engine",
    "StorageEngine": "yadgar._shared.rules_engine.rules_engine",
    "VALID_OPERATORS": "yadgar._shared.rules_engine.rules_engine",
    "_apply_score_delta": "yadgar._shared.rules_engine.rules_engine",
    "_coerce_none_field": "yadgar._shared.rules_engine.rules_engine",
    "_compare_contains": "yadgar._shared.rules_engine.rules_engine",
    "_compare_equality": "yadgar._shared.rules_engine.rules_engine",
    "_compare_numeric": "yadgar._shared.rules_engine.rules_engine",
    "_get_field_value": "yadgar._shared.rules_engine.rules_engine",
    "_parse_action": "yadgar._shared.rules_engine.rules_engine",
    "_parse_condition": "yadgar._shared.rules_engine.rules_engine",
    "_parse_write_action": "yadgar._shared.rules_engine.rules_engine",
    "fnmatch": "yadgar._shared.rules_engine.rules_engine",
    "logger": "yadgar._shared.rules_engine.rules_engine",
    "logging": "yadgar._shared.rules_engine.rules_engine",
    "observe": "yadgar._shared.rules_engine.rules_engine",
    "re": "yadgar._shared.rules_engine.rules_engine",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
