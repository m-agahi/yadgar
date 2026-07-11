"""Back-compat shim — config_yaml moved into ``yadgar._shared.config`` (T2 Car D1).

Packaged per the no-lone-files law (ADR-0084): the config family now lives in
the ``yadgar/_shared/config/`` package. New code must import from
``yadgar._shared.config.config_yaml`` instead.

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.config_yaml import
FIELD_META`` keeps working. Lazy importlib forward — the target only loads on
first attribute access.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.config.config_yaml"
_EXPORTS: Final = (
    "Any",
    "CommentedMap",
    "FIELD_META",
    "Path",
    "SECTION_TITLES",
    "YAML",
    "_SECTION_ORDER",
    "_paths",
    "cmd_config_edit",
    "cmd_config_get",
    "cmd_config_init",
    "cmd_config_list",
    "cmd_config_set",
    "coerce_value",
    "get_config_path",
    "get_field_section",
    "load_yaml",
    "observe",
    "os",
    "save_yaml",
    "set_config_value",
    "subprocess",
    "sys",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
