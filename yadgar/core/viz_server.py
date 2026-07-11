"""Back-compat shim — viz_server moved into ``yadgar.core.viz`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.viz.viz_server`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.viz.viz_server"
_EXPORTS: Final = (
    "Any",
    "BaseHTTPRequestHandler",
    "Callable",
    "HTTPServer",
    "INDEX_HTML",
    "Path",
    "STATIC_DIR",
    "Timer",
    "_Handler",
    "_MIME_MAP",
    "_ThreadingHTTPServer",
    "_mime_type",
    "_proxy_enabled",
    "_proxy_request",
    "annotations",
    "httpx",
    "observe",
    "os",
    "run_viz_server",
    "socketserver",
    "webbrowser",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
