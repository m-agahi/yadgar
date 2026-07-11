"""yadgar._shared.embeddings — embedding engines package (local + remote).

T2 Car D1 (layer-boundary train): the flat embeddings pair packaged per the
no-lone-files law (ADR-0084). Genuinely dual — both layers embed.

  embeddings.py        — EmbeddingEngine (local sentence-transformers) +
                         model dimension/prefix registries
  remote_embeddings.py — RemoteEmbeddingEngine (HTTP client for the backend
                         embed service; selected by the composition root)

PEP-562 re-export (Car 0 #167 precedent): ``from yadgar._shared.embeddings
import EmbeddingEngine`` keeps working — the package IS the old
``embeddings.py`` dotted path. A back-compat shim remains at the old
``yadgar._shared.remote_embeddings`` path.
"""

from typing import Final

_EXPORTS: Final = {
    "EmbeddingEngine": "yadgar._shared.embeddings.embeddings",
    "MODEL_DIMENSIONS": "yadgar._shared.embeddings.embeddings",
    "MODEL_DOC_PREFIX": "yadgar._shared.embeddings.embeddings",
    "MODEL_QUERY_PREFIX": "yadgar._shared.embeddings.embeddings",
    "OrderedDict": "yadgar._shared.embeddings.embeddings",
    "Path": "yadgar._shared.embeddings.embeddings",
    "RemoteEmbeddingEngine": "yadgar._shared.embeddings.remote_embeddings",
    "_CACHE_MAX": "yadgar._shared.embeddings.embeddings",
    "_MODEL_DIMENSIONS": "yadgar._shared.embeddings.embeddings",
    "annotations": "yadgar._shared.embeddings.remote_embeddings",
    "httpx": "yadgar._shared.embeddings.remote_embeddings",
    "logger": "yadgar._shared.embeddings.embeddings",
    "logging": "yadgar._shared.embeddings.embeddings",
    "np": "yadgar._shared.embeddings.embeddings",
    "observe": "yadgar._shared.embeddings.embeddings",
    "os": "yadgar._shared.embeddings.embeddings",
    "threading": "yadgar._shared.embeddings.remote_embeddings",
    "time": "yadgar._shared.embeddings.embeddings",
    "trace_span": "yadgar._shared.embeddings.embeddings",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
