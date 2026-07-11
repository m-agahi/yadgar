"""yadgar.core.auth_middleware — bearer-auth middleware package.

T2 Car D (D3, layer-boundary train): the flat ``auth_middleware.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core.auth_middleware`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core.auth_middleware.auth_middleware``
directly.

  auth_middleware.py — BearerAuthMiddleware — token auth + debug-API gating
"""

from typing import Final

_EXPORTS: Final = {
    "ASGIApp": "yadgar.core.auth_middleware.auth_middleware",
    "BearerAuthMiddleware": "yadgar.core.auth_middleware.auth_middleware",
    "JSONResponse": "yadgar.core.auth_middleware.auth_middleware",
    "Receive": "yadgar.core.auth_middleware.auth_middleware",
    "Scope": "yadgar.core.auth_middleware.auth_middleware",
    "Send": "yadgar.core.auth_middleware.auth_middleware",
    "_DEBUG_API_PREFIXES": "yadgar.core.auth_middleware.auth_middleware",
    "_EXEMPT_PATHS": "yadgar.core.auth_middleware.auth_middleware",
    "_PROTECTED_PREFIXES": "yadgar.core.auth_middleware.auth_middleware",
    "_UNGATED_OPS_PATHS": "yadgar.core.auth_middleware.auth_middleware",
    "_UNGATED_OPS_PREFIXES": "yadgar.core.auth_middleware.auth_middleware",
    "_extract_bearer": "yadgar.core.auth_middleware.auth_middleware",
    "_is_auth_required": "yadgar.core.auth_middleware.auth_middleware",
    "_is_debug_api_path": "yadgar.core.auth_middleware.auth_middleware",
    "_is_debug_apis_enabled": "yadgar.core.auth_middleware.auth_middleware",
    "_is_protected": "yadgar.core.auth_middleware.auth_middleware",
    "_observe_auth_duration": "yadgar.core.auth_middleware.auth_middleware",
    "_startup_warned": "yadgar.core.auth_middleware.auth_middleware",
    "annotations": "yadgar.core.auth_middleware.auth_middleware",
    "hmac": "yadgar.core.auth_middleware.auth_middleware",
    "logger": "yadgar.core.auth_middleware.auth_middleware",
    "logging": "yadgar.core.auth_middleware.auth_middleware",
    "observe": "yadgar.core.auth_middleware.auth_middleware",
    "os": "yadgar.core.auth_middleware.auth_middleware",
    "resolve_knob": "yadgar.core.auth_middleware.auth_middleware",
    "time": "yadgar.core.auth_middleware.auth_middleware",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
