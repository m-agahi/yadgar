"""yadgar.backend.predictive_coding — predictive-coding write gate package.

T2 Car D (D2, layer-boundary train): the flat ``predictive_coding.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.predictive_coding`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.predictive_coding.predictive_coding``
directly.

  predictive_coding.py — WriteGate — surprise scoring + duplicate gating on the write path
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar.backend.predictive_coding.predictive_coding",
    "Counter": "yadgar.backend.predictive_coding.predictive_coding",
    "EmbeddingEngine": "yadgar.backend.predictive_coding.predictive_coding",
    "Retriever": "yadgar.backend.predictive_coding.predictive_coding",
    "Settings": "yadgar.backend.predictive_coding.predictive_coding",
    "StorageEngine": "yadgar.backend.predictive_coding.predictive_coding",
    "UTC": "yadgar.backend.predictive_coding.predictive_coding",
    "WriteGate": "yadgar.backend.predictive_coding.predictive_coding",
    "_BYPASS_TAGS": "yadgar.backend.predictive_coding.predictive_coding",
    "_DECISION_BYPASS_RE": "yadgar.backend.predictive_coding.predictive_coding",
    "_ERROR_BYPASS_RE": "yadgar.backend.predictive_coding.predictive_coding",
    "datetime": "yadgar.backend.predictive_coding.predictive_coding",
    "deque": "yadgar.backend.predictive_coding.predictive_coding",
    "logger": "yadgar.backend.predictive_coding.predictive_coding",
    "logging": "yadgar.backend.predictive_coding.predictive_coding",
    "np": "yadgar.backend.predictive_coding.predictive_coding",
    "observe": "yadgar.backend.predictive_coding.predictive_coding",
    "re": "yadgar.backend.predictive_coding.predictive_coding",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
