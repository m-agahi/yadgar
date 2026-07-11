"""yadgar._shared.paths — XDG path-constants package.

T2 Car D1 (layer-boundary train): the flat paths.py packaged per the
no-lone-files law (ADR-0084). Genuinely dual — every layer resolves data/log
paths through it.

  paths.py — the XDG-compliant path constants (DATA_DIR, LOG_DIR, …), all
             resolved lazily via that module's own PEP-562 ``__getattr__`` so
             env overrides are read at access time.

This ``__init__`` forwards EVERY attribute to the submodule (catch-all, not an
_EXPORTS map) because the constant set is dynamic by design — the submodule
itself resolves names lazily. ``from yadgar._shared.paths import DATA_DIR``
and ``paths.DATA_DIR`` module-attribute access both keep working.
"""


def __getattr__(name: str):
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module("yadgar._shared.paths.paths"), name)


def __dir__() -> list[str]:
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return list(globals()) + dir(importlib.import_module("yadgar._shared.paths.paths"))
