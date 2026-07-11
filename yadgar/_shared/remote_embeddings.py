"""Back-compat shim — remote_embeddings moved into ``yadgar._shared.embeddings`` (T2 Car D1).

Packaged per the no-lone-files law (ADR-0084): the embedding engines now live
in the ``yadgar/_shared/embeddings/`` package. New code must import from
``yadgar._shared.embeddings.remote_embeddings`` instead.

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.remote_embeddings
import RemoteEmbeddingEngine`` keeps working. Lazy importlib forward — the
target only loads on first attribute access.
"""

from typing import Final

_TARGET: Final = "yadgar._shared.embeddings.remote_embeddings"
_EXPORTS: Final = (
    "MODEL_DIMENSIONS",
    "MODEL_DOC_PREFIX",
    "MODEL_QUERY_PREFIX",
    "OrderedDict",
    "RemoteEmbeddingEngine",
    "_CACHE_MAX",
    "annotations",
    "httpx",
    "logger",
    "logging",
    "np",
    "observe",
    "os",
    "threading",
    "trace_span",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
