"""yadgar.backend.predictive_coding — predictive-coding write gate package.

T2 Car D (D2, layer-boundary train): the flat ``predictive_coding.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.predictive_coding`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import
``yadgar.backend.predictive_coding.predictive_coding`` directly.

C6 split (module-standardization train, ADR-0066):
  predictive_coding.py — WriteGate class + entity-cache + task-continuity +
                          surprisal orchestration + boundary + directory-model
  _signals.py          — _SignalsMixin: four novelty-signal computation methods
                          (_compute_embedding_novelty, _compute_entity_novelty,
                          _compute_temporal_novelty, _compute_structural_novelty
                          + their helpers)
  _bypass.py           — bypass-keyword constants + _bypass_reason() helper

CATCH-ALL forward: ``WriteGate`` is the sole public symbol. The catch-all also
forwards bypass constants for any caller that reached them through the package
path (test patching, etc.).
"""


def __getattr__(name: str):
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    # Route bypass constants to their new home; everything else to the main module.
    _bypass_names = frozenset(
        {"_ERROR_BYPASS_RE", "_DECISION_BYPASS_RE", "_BYPASS_TAGS", "_bypass_reason"}
    )
    if name in _bypass_names:
        return getattr(importlib.import_module("yadgar.backend.predictive_coding._bypass"), name)
    return getattr(
        importlib.import_module("yadgar.backend.predictive_coding.predictive_coding"), name
    )


def __dir__() -> list[str]:
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    bypass = dir(importlib.import_module("yadgar.backend.predictive_coding._bypass"))
    main = dir(importlib.import_module("yadgar.backend.predictive_coding.predictive_coding"))
    return list(globals()) + bypass + main
