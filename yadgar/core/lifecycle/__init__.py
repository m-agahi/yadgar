"""yadgar.core.lifecycle — core process-lifecycle package.

T2 Car D (D3, layer-boundary train): the flat ``lifecycle.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.lifecycle`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.lifecycle.lifecycle``
directly.

  lifecycle.py — shutdown/signal handling + file-queue init (core side)
"""

from typing import Final

_EXPORTS: Final = {
    "_SENSITIVE_DRAIN_POLL_SEC": "yadgar.core.lifecycle.lifecycle",
    "_drain_sensitive_lock": "yadgar.core.lifecycle.lifecycle",
    "_emit_sd_ready": "yadgar.core.lifecycle.lifecycle",
    "_emit_sd_stopping": "yadgar.core.lifecycle.lifecycle",
    "_get_file_queue": "yadgar.core.lifecycle.lifecycle",
    "_init_file_queue": "yadgar.core.lifecycle.lifecycle",
    "_shared_shutdown": "yadgar.core.lifecycle.lifecycle",
    "_signal_handler": "yadgar.core.lifecycle.lifecycle",
    "_snapshot_embed_caches": "yadgar.core.lifecycle.lifecycle",
    "_st": "yadgar.core.lifecycle.lifecycle",
    "annotations": "yadgar.core.lifecycle.lifecycle",
    "get_settings": "yadgar.core.lifecycle.lifecycle",
    "logger": "yadgar.core.lifecycle.lifecycle",
    "logging": "yadgar.core.lifecycle.lifecycle",
    "observe": "yadgar.core.lifecycle.lifecycle",
    "os": "yadgar.core.lifecycle.lifecycle",
    "shutdown": "yadgar.core.lifecycle.lifecycle",
    "sys": "yadgar.core.lifecycle.lifecycle",
    "time": "yadgar.core.lifecycle.lifecycle",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
