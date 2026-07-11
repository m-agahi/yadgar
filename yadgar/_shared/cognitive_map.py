"""PEP-562 shim — CognitiveMap moved to ``yadgar.backend.restoration`` (T2 Car B).

The numpy SR-matrix compute is backend territory (census verdict #7, behind
``POST /restore``); the session-side transition recording lives in
``yadgar._shared.runtime.sr_session.SRTransitionRecorder``. This shim keeps
``from yadgar._shared.cognitive_map import CognitiveMap`` working for tests
(Car 0 #167 precedent). Lazy string-target import — creates NO static
``_shared → backend`` edge.
"""

from typing import Final

_EXPORTS: Final = {
    "CognitiveMap": "yadgar.backend.restoration.cognitive_map",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
