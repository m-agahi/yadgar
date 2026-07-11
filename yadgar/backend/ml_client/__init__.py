"""yadgar.backend.ml_client — ML client package (local + remote).

T2 Car D (D2, layer-boundary train): the flat ``ml_client.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.ml_client`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.ml_client.ml_client``
directly.

  ml_client.py — LocalMLClient / RemoteMLClient + circuit breaker
"""

from typing import Final

_EXPORTS: Final = {
    "Callable": "yadgar.backend.ml_client.ml_client",
    "LocalMLClient": "yadgar.backend.ml_client.ml_client",
    "MLClient": "yadgar.backend.ml_client.ml_client",
    "OnnxRerankerUnavailableError": "yadgar.backend.ml_client.ml_client",
    "RemoteMLClient": "yadgar.backend.ml_client.ml_client",
    "_CircuitBreaker": "yadgar.backend.ml_client.ml_client",
    "_STATE_CLOSED": "yadgar.backend.ml_client.ml_client",
    "_STATE_HALF_OPEN": "yadgar.backend.ml_client.ml_client",
    "_STATE_OPEN": "yadgar.backend.ml_client.ml_client",
    "_emit_unload_telemetry": "yadgar.backend.ml_client.ml_client",
    "_idle_eviction_seconds": "yadgar.backend.ml_client.ml_client",
    "_record_model_load": "yadgar.backend.ml_client.ml_client",
    "_rpc_span": "yadgar.backend.ml_client.ml_client",
    "annotations": "yadgar.backend.ml_client.ml_client",
    "logger": "yadgar.backend.ml_client.ml_client",
    "logging": "yadgar.backend.ml_client.ml_client",
    "observe": "yadgar.backend.ml_client.ml_client",
    "os": "yadgar.backend.ml_client.ml_client",
    "resolve_knob": "yadgar.backend.ml_client.ml_client",
    "threading": "yadgar.backend.ml_client.ml_client",
    "time": "yadgar.backend.ml_client.ml_client",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
