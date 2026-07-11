"""Back-compat shim — config_registry moved into ``yadgar._shared.config`` (T2 Car D1).

Packaged per the no-lone-files law (ADR-0084): the config family now lives in
the ``yadgar/_shared/config/`` package. New code must import from
``yadgar._shared.config.config_registry`` instead.

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.config_registry
import list_config`` keeps working. Lazy importlib forward — the target only
loads on first attribute access.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.config.config_registry"
_EXPORTS: Final = (
    "ConfigEntry",
    "_REDACTED",
    "_REDACT_RE",
    "_REGISTRY",
    "_paths",
    "_set_config_gauges",
    "_stringify_yaml_value",
    "_yaml_layer",
    "annotations",
    "build_config_table",
    "clear_config_caches",
    "dataclass",
    "emit_startup_config_log",
    "list_config",
    "logger",
    "logging",
    "lru_cache",
    "observe",
    "os",
    "re",
    "warn_comet_dormant",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
