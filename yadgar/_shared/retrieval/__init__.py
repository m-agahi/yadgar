"""Retrieval CONTRACT package (T2 Car E2 sink).

After the landscape-recall forward, BOTH retrieval executors live in the
backend (the /recall fan-out pipeline + predictive_coding) — the retrieval
implementation sank from ``_shared`` to ``yadgar.backend.retrieval`` per the
dual-import law. What remains here is the genuinely DUAL contract surface:

- ``profiles`` — retrieval profile registry. Core validates the ``profile=``
  MCP param against ``_VALID_PROFILES``; the backend pipeline consumes the
  full profile dicts. Pure config, no compute.

PEP-562 shim (Car 0 #167 precedent): the old package-level names (``Retriever``,
``RetrievalPipeline``, …) lazily forward to ``yadgar.backend.retrieval`` so
existing imports keep working; the string-based importlib forward avoids a
static _shared→backend edge. New code must import from the backend package.
"""

from typing import Final

from yadgar._shared.retrieval.profiles import _VALID_PROFILES, get_profile  # noqa: F401

_TARGET: Final = "yadgar.backend.retrieval"
_FORWARDS: Final = (
    "Retriever",
    "RetrievalPipeline",
    "RetrievalState",
    "recall_compare",
    "_derive_implied_fact_passages",
    "_extract_query_entities",
    "_pseudo_hyde_expand",
    "_question_to_statement",
    "analyze_query",
)

__all__ = [
    "get_profile",
    "_VALID_PROFILES",
    *_FORWARDS,
]


def __getattr__(name: str):
    if name not in _FORWARDS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_FORWARDS)
