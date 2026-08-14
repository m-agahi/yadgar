"""yadgar._shared.config — configuration package (settings, registry, YAML).

T2 Car D1 (layer-boundary train): the flat config family packaged per the
no-lone-files law (ADR-0084). Genuinely dual — both layers resolve knobs.

  config.py          — Settings model + resolve_knob / get_settings (env +
                       YAML-aware resolution)
  config_registry.py — ConfigEntry registry, config table rendering, startup
                       config log + gauges
  config_yaml.py     — YAML config file I/O, FIELD_META schema, config CLI
                       command impls (I13 oversized; internal split = task #18)

PEP-562 re-export (Car 0 #167 precedent): ``from yadgar._shared.config import
get_settings`` keeps working — the package IS the old ``config.py`` dotted
path. Back-compat shims remain at the old ``yadgar._shared.config_registry``
and ``yadgar._shared.config_yaml`` paths; new code imports the submodules
directly.
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar._shared.config.config",
    "BaseSettings": "yadgar._shared.config.config",
    "Callable": "yadgar._shared.config.config",
    "FieldInfo": "yadgar._shared.config.config",
    "Path": "yadgar._shared.config.config",
    "PydanticBaseSettingsSource": "yadgar._shared.config.config",
    "Settings": "yadgar._shared.config.config",
    "YamlConfigSource": "yadgar._shared.config.config",
    "_KNOB_PARSE_ERRORS": "yadgar._shared.config.config",
    "_paths": "yadgar._shared.config.config",
    "_is_db_url_local": "yadgar._shared.config.db_url",
    "field_validator": "yadgar._shared.config.config",
    "get_settings": "yadgar._shared.config.config",
    "lru_cache": "yadgar._shared.config.config",
    "observe": "yadgar._shared.config.config",
    "os": "yadgar._shared.config.config",
    "re": "yadgar._shared.config.config",
    "resolve_knob": "yadgar._shared.config.config",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
