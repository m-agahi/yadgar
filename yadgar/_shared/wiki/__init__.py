"""yadgar._shared.wiki — wiki contract + store package.

T2 Car C (layer-boundary train): split from the flat wiki.py module
(placement part only — internal I13 splitting of the store is task #18).

  contract.py — WikiAddOptions dataclass + canonical CATEGORIES /
                CONFIDENCE_LEVELS registries (pure contract, stays in _shared)
  store.py    — WikiStore impl + markdown/positional-edit helpers. Verified
                genuinely DUAL (core tools + backend admin_exec/write_exec via
                _st._wiki; constructed by the composition root in
                _shared/runtime/lifecycle.py) → stays in _shared per the
                dual-import law. Core-viz read forwarding is Car E3.

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.wiki import
WikiAddOptions, WikiStore`` keeps working. Contract-only consumers should
import from ``yadgar._shared.wiki.contract`` directly so the store module
never loads.
"""

from typing import Final

_EXPORTS: Final = {
    "WikiAddOptions": "yadgar._shared.wiki.contract",
    "CATEGORIES": "yadgar._shared.wiki.contract",
    "CONFIDENCE_LEVELS": "yadgar._shared.wiki.contract",
    "WikiStore": "yadgar._shared.wiki.store",
    "WIKI_STALE_DAYS": "yadgar._shared.wiki.store",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
